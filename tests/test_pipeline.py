import json
import tempfile
from pathlib import Path
import pytest
from bom_comic.models import Scene, Claim, Speech, Verse, SceneBatch, Verdict, Panel
from bom_comic.scripture import load, select, coordinate, COUNTS
from bom_comic.storage import Store, digest
from bom_comic.config import Config
from bom_comic.analysis import deterministic_issues
from bom_comic.comic import (build_prompt, DEFAULT_CONTINUITY, PAGE_SIZE, PAGE_UNITS, MARGIN, BOTTOM, GUTTER,
    MIN_ASPECT, MAX_ASPECT, classify_shot, classify_weight, plan, page_frames, frames, frame_aspect, assemble)
from bom_comic.pipeline import Pipeline
from bom_comic.providers import PlaceholderImages

# Synthetic words exercise software only. They are not scripture or an adaptation.
@pytest.fixture
def source(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("3 Nephi 8:1 TEST FIXTURE: People gathered.\n3 Nephi 8:2 TEST FIXTURE: A voice was heard.\n")
    return path

class FakeAI(PlaceholderImages):
    def structured(self, prompt, schema, tag, kind="text"):
        if schema is Verdict:
            return Verdict(status="PASS")
        return SceneBatch(scenes=[Scene(scene_id="scene_001", title="Software test",
            refs=["3 Nephi 8:1", "3 Nephi 8:2"], summary="People gathered.",
            characters=["Unnamed people"], locations=["Unspecified gathering place"],
            explicit_facts=[Claim(text="People gathered.", refs=["3 Nephi 8:1"])],
            narration=[Claim(text="People gathered.", refs=["3 Nephi 8:1"])])])

@pytest.fixture
def pipeline(tmp_path, source):
    p = Pipeline(Store(tmp_path / "run"), FakeAI())
    p.init(source, "3 Nephi 8:1", "3 Nephi 8:2")
    p.analyze()
    p.validate()
    return p

def ready(p):
    p.review("scene", "scene_001", "approve")
    p.plan()
    p.review("panel", "panel_001", "approve")


def test_parser_formats_and_ranges(source, tmp_path):
    verses = load(source)
    assert len(select(verses, "3 Nephi 8:1", "3 Nephi 8:2")) == 2
    path = tmp_path / "v.json"
    path.write_text(json.dumps([v.model_dump() for v in verses]))
    assert load(path) == verses
    for start, end in [("3 Nephi 8:1", "3 Nephi 8:3"), ("3 Nephi 8:2", "3 Nephi 8:1")]:
        with pytest.raises(ValueError):
            select(verses, start, end)
    with pytest.raises(ValueError):
        coordinate("Nonexistent Book 1:1")
    with pytest.raises(ValueError):
        coordinate("3 Nephi 0:1")
    source.write_text(source.read_text() * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        load(source)


def test_select_detects_missing_verse_in_any_single_chapter():
    """The old missing-verse check only worked for 3 Nephi 8-11; select() must catch a gap
    in any book/chapter without needing a canonical verse-count table for that book."""
    verses = [Verse(book="Alma", chapter=17, verse=v, text="TEST") for v in (20, 21, 23, 24)]
    assert len(select(verses, "Alma 17:20", "Alma 17:21")) == 2
    with pytest.raises(ValueError, match="missing verses"):
        select(verses, "Alma 17:20", "Alma 17:24")


def test_full_scope_selection():
    verses = [Verse(book="3 Nephi", chapter=c, verse=v, text="TEST ONLY") for c, n in COUNTS.items() for v in range(1, n + 1)]
    assert len(select(verses, "3 Nephi 8:1", "3 Nephi 11:7")) == 73


def test_serialization_and_prompt(pipeline):
    ready(pipeline)
    panel = pipeline.panels()[0]
    assert Panel.model_validate_json(panel.model_dump_json()) == panel
    prompt = build_prompt(panel, DEFAULT_CONTINUITY, "3:2")
    assert "1.50 times as wide as it is tall" in prompt
    assert "No unsupported doctrinal symbolism" in prompt
    assert "UNSPECIFIED CREATIVE DETAILS" in prompt
    assert "EXPLICIT SCRIPTURAL FACTS" in prompt
    assert panel.action in prompt


def test_configuration(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_IMAGE_MODEL", "chosen-model")
    assert Config.load().require("image") == "chosen-model"
    with pytest.raises(ValueError):
        Config().require("image")


def test_paths(tmp_path):
    store = Store(tmp_path / "run")
    with pytest.raises(ValueError):
        store.path("../escape")
    store.write("nested/a.json", {"x": 1})
    assert store.read("nested/a.json") == {"x": 1}


def test_unapproved_scene_blocks(pipeline):
    with pytest.raises(ValueError, match="human approval"):
        pipeline.plan()


def test_source_and_scene_edits_invalidate(pipeline):
    ready(pipeline)
    scenes = pipeline.store.read("scenes.json")
    scenes[0]["summary"] = "Changed"
    pipeline.store.write("scenes.json", scenes)
    with pytest.raises(ValueError, match="validate again"):
        pipeline.generate()
    data = pipeline.store.read("source.json")
    data[0]["text"] = "Changed"
    pipeline.store.write("source.json", data)
    with pytest.raises(ValueError, match="snapshot changed"):
        pipeline.verses()


def test_reject_invented_dialogue(pipeline):
    scene = pipeline.scenes()[0]
    scene.spoken_dialogue = [Speech(speaker="Someone", text="Invented dialogue", refs=scene.refs)]
    assert "Lettering must be an exact source quotation" in deterministic_issues(scene, pipeline.verses())


def test_rejected_scene_cannot_be_approved(pipeline):
    report = pipeline.store.read("validation.json")
    report["results"]["scene_001"]["status"] = "REJECT"
    pipeline.store.write("validation.json", report)
    with pytest.raises(ValueError, match="Rejected"):
        pipeline.review("scene", "scene_001", "approve")


def test_warning_requires_reason(pipeline):
    report = pipeline.store.read("validation.json")
    report["results"]["scene_001"] = {"status": "PASS WITH WARNINGS", "issues": ["Unspecified location"]}
    pipeline.store.write("validation.json", report)
    with pytest.raises(ValueError, match="Explain"):
        pipeline.review("scene", "scene_001", "approve")
    pipeline.review("scene", "scene_001", "approve", "Generic background accepted as design only")


def test_end_to_end_and_regeneration(pipeline):
    ready(pipeline)
    pipeline.generate()
    with pytest.raises(ValueError, match="visual/scripture review"):
        pipeline.assemble()
    pipeline.review("image", "panel_001", "approve", "Placeholder software test")
    pdf = pipeline.assemble()
    assert pdf.read_bytes().startswith(b"%PDF")
    original = pipeline.image_record(pipeline.panels()[0])
    pipeline.generate("panel_001")
    assert pipeline.store.path(original["path"]).exists()
    with pytest.raises(ValueError, match="visual/scripture review"):
        pipeline.assemble()


def test_reader_flags_panels_of_the_assembled_draft(pipeline):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from bom_comic import reader
    with pytest.raises(ValueError, match="assemble first"):
        reader.draft(pipeline.store)
    ready(pipeline)
    pipeline.generate()
    pipeline.review("image", "panel_001", "approve", "Placeholder software test")
    pipeline.assemble()
    [page] = reader.draft(pipeline.store)["pages"]
    assert page["panels"][0]["id"] == "panel_001" and len(page["panels"][0]["frame"]) == 4
    saved = reader.flag(pipeline.store, "panel_001", "  Anachronism: a pen  ")
    record = pipeline.image_record(pipeline.panels()[0])
    assert saved["panel_001"]["note"] == "Anachronism: a pen"
    assert saved["panel_001"]["image_hash"] == record["image_hash"]
    assert reader.flags(pipeline.store) == saved
    with pytest.raises(ValueError, match="not in the assembled draft"):
        reader.flag(pipeline.store, "panel_999", "x")
    assert reader.flag(pipeline.store, "panel_001", " ") == {}
    # Flagging never touches approvals, so the draft still assembles.
    assert pipeline.assemble().exists()
    # The served page, data, page image, and flag endpoint round-trip.
    started = threading.Event()
    real = ThreadingHTTPServer
    servers = []

    class Capture(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            servers.append(self)
            started.set()
    reader.ThreadingHTTPServer = Capture
    try:
        threading.Thread(target=reader.serve, args=(pipeline.store, 0, False), daemon=True).start()
        started.wait(5)
        base = f"http://127.0.0.1:{servers[0].server_address[1]}"
        assert b"Draft review" in urllib.request.urlopen(base + "/").read()
        assert json.loads(urllib.request.urlopen(base + "/data").read())["flags"] == {}
        assert urllib.request.urlopen(base + "/page/1").read().startswith(b"\x89PNG")
        request = urllib.request.Request(base + "/flag", json.dumps({"panel_id": "panel_001", "note": "Wings"}).encode(),
                                         {"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(request).read())["flags"]["panel_001"]["note"] == "Wings"
    finally:
        reader.ThreadingHTTPServer = real
        for server in servers:
            server.shutdown()


def test_panel_story_edits_blocked(pipeline):
    ready(pipeline)
    data = pipeline.store.read("panels.json")
    data["panels"][0]["action"] = "Invented event"
    pipeline.store.write("panels.json", data)
    with pytest.raises(ValueError, match="Edit story content"):
        pipeline.panels()


def test_continuity_edits_invalidate(pipeline):
    ready(pipeline)
    pipeline.store.write("continuity/locations.json", {"Unspecified gathering place": {"visual_design_choices": ["Stone paving"]}})
    with pytest.raises(ValueError, match="composition/continuity"):
        pipeline.generate()


def test_modified_image_blocks_assembly(pipeline):
    ready(pipeline)
    pipeline.generate()
    pipeline.review("image", "panel_001", "approve")
    record = pipeline.image_record(pipeline.panels()[0])
    pipeline.store.path(record["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="stale or modified"):
        pipeline.assemble()


def test_individual_scene_regeneration_archives(pipeline):
    pipeline.analyze("scene_001")
    assert pipeline.scenes()[0].scene_id == "scene_001"
    assert list(pipeline.store.path("archive").glob("*/scenes.json"))


def test_cliffhanger_requires_complete_introduction(tmp_path):
    source = tmp_path / "last.txt"
    source.write_text("3 Nephi 11:7 SYNTHETIC final introduction.\n")
    p = Pipeline(Store(tmp_path / "last-run"), FakeAI())
    p.init(source, "3 Nephi 11:7", "3 Nephi 11:7")
    scene = Scene(scene_id="scene_001", title="End", refs=["3 Nephi 11:7"], summary="Test ending",
        explicit_facts=[Claim(text="Test", refs=["3 Nephi 11:7"])])
    p.store.write("scenes.json", [scene.model_dump()])
    assert p.validate()["scene_001"]["status"] == "REJECT"
    scene.spoken_dialogue = [Speech(speaker="Off-screen voice", text="SYNTHETIC final introduction.", refs=scene.refs)]
    p.store.write("scenes.json", [scene.model_dump()])
    assert p.validate()["scene_001"]["status"] == "PASS"


def test_gemini_image_adapter_with_mocked_sdk(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from google.genai import types
    from bom_comic.providers import Gemini
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(data, format="PNG")
    response = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        parts=[types.Part.from_bytes(data=data.getvalue(), mime_type="image/png")]))])
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        return response
    provider = Gemini.__new__(Gemini)
    provider.config = Config(api_key="fake", image_model="configurable-model")
    provider.store = Store(tmp_path / "adapter")
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    output = provider.store.path("test.png")
    provider.generate_image("Approved prompt", output_path=output)
    assert Image.open(output).size == (32, 32)
    assert calls[0]["model"] == "configurable-model"
    assert provider.store.read("api/test.response.json")["candidates"]


def test_rejecting_panel_blocks_previously_approved_image(pipeline):
    ready(pipeline)
    pipeline.generate()
    pipeline.review("image", "panel_001", "approve")
    pipeline.review("panel", "panel_001", "reject")
    with pytest.raises(ValueError, match="composition/continuity approval"):
        pipeline.assemble()


def test_api_diagnostics_redact_secrets():
    from types import SimpleNamespace
    from bom_comic.errors import api_error
    result = api_error(SimpleNamespace(code=403, message="Denied custom-secret https://example.com?key=secret API_KEY=another-secret"), "custom-secret")
    assert "403" in result
    assert "project access" in result
    for secret in ("custom-secret", "another-secret", "https://example.com"):
        assert secret not in result


def test_structured_adapter_uses_json_schema(tmp_path):
    from types import SimpleNamespace
    from bom_comic.providers import Gemini
    provider = Gemini.__new__(Gemini)
    provider.config = Config(api_key="fake", text_model="test-model")
    provider.store = Store(tmp_path / "structured")
    calls = []
    class Response:
        text = '{"status":"PASS","issues":[]}'
        def model_dump(self, **kwargs):
            return {"text": self.text}
    def generate_content(**kwargs):
        calls.append(kwargs)
        return Response()
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    assert provider.structured("Check facts", Verdict, "test").status == "PASS"
    config = calls[0]["config"]
    assert "response_schema" not in config
    assert config["response_json_schema"]["additionalProperties"] is False
    assert config["automatic_function_calling"]["disable"] is True


def test_ollama_adapter_uses_json_schema_and_caches(tmp_path):
    from bom_comic.providers import Ollama
    calls = []
    def call(payload, timeout=600):
        calls.append(payload)
        return {"message": {"content": '{"status":"PASS","issues":[]}'}}
    provider = Ollama(Store(tmp_path / "ollama"), url="http://fake")
    provider._call = call
    provider.reuse_responses = True
    assert provider.structured("Check facts", Verdict, "test", "validator").status == "PASS"
    assert calls[0]["model"] == provider.validator_model
    assert calls[0]["format"] == Verdict.model_json_schema()
    assert calls[0]["stream"] is False
    assert provider.store.read("api/test.cache.json")["text"] == '{"status":"PASS","issues":[]}'
    provider.structured("Check facts", Verdict, "test", "validator")
    assert len(calls) == 1  # second call served from cache; unchanged request


def test_ollama_adapter_rejects_invalid_structured_output(tmp_path):
    from bom_comic.providers import Ollama
    from bom_comic.errors import ProviderError
    provider = Ollama(Store(tmp_path / "ollama-bad"), url="http://fake")
    provider._call = lambda payload, timeout=600: {"message": {"content": "not json"}}
    with pytest.raises(ProviderError, match="invalid structured output"):
        provider.structured("Check facts", Verdict, "test")


def test_epub_extraction_preserves_embedded_verse_and_is_book_general(tmp_path):
    """Extraction itself is general-purpose (grabs whatever the epub has, any book); scope
    trimming to a particular run's range is select()'s job, not the extractor's."""
    from zipfile import ZipFile
    from bom_comic.epub import extract
    paragraphs = ['<html><body><h2>THIRD BOOK OF NEPHI</h2>']
    for chapter, count in COUNTS.items():
        paragraphs.append(f'<h3>3 Nephi Chapter {chapter}</h3>')
        for verse in range(1, count + 1):
            if (chapter, verse) == (11, 2):
                continue
            text = f'{chapter}:{verse} Synthetic fixture {chapter}-{verse}.'
            if (chapter, verse) == (11, 1):
                text += ' 3\nNephi 11:2 Synthetic fixture 11-2.'
            paragraphs.append(f'<p>{text}</p>')
    paragraphs.append('<p>11:8 Beyond this prototype run\'s scope but still a real verse.</p></body></html>')
    path = tmp_path / 'fixture.epub'
    with ZipFile(path, 'w') as archive:
        archive.writestr('book.xhtml', ''.join(paragraphs))
    verses, evidence = extract(path)
    assert len(verses) == len(evidence) == 74
    assert verses[-8].text == 'Synthetic fixture 11-1.'
    assert verses[-7].text == 'Synthetic fixture 11-2.'
    assert verses[-1].ref == '3 Nephi 11:8'
    assert len(select(verses, '3 Nephi 8:1', '3 Nephi 11:7')) == 73


def test_preview_does_not_bypass_approvals(pipeline):
    path = pipeline.preview()
    assert path.exists()
    assert 'NOT APPROVED' in pipeline.store.read('review/draft-panels.json')['status']
    assert not pipeline.store.path('panels.json').exists()
    assert pipeline.approvals() == {}
    with pytest.raises(ValueError, match='human approval'):
        pipeline.plan()


def test_parallel_validation_preserves_results(pipeline):
    expected = pipeline.validate()
    assert pipeline.validate(workers=4) == expected
    with pytest.raises(ValueError, match='workers'):
        pipeline.validate(workers=5)


def test_structured_cache_requires_identical_request(tmp_path):
    from types import SimpleNamespace
    from bom_comic.providers import Gemini
    provider = Gemini.__new__(Gemini)
    provider.config = Config(api_key='fake', validator_model='validator')
    provider.store = Store(tmp_path / 'cached')
    provider.reuse_responses = True
    calls = []
    class Response:
        text = '{"status":"PASS","issues":[]}'
        def model_dump(self, **kwargs):
            return {'text': self.text}
    def generate_content(**kwargs):
        calls.append(kwargs)
        return Response()
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    provider.structured('first', Verdict, 'scene', 'validator')
    provider.structured('first', Verdict, 'scene', 'validator')
    assert len(calls) == 1
    provider.structured('changed', Verdict, 'scene', 'validator')
    assert len(calls) == 2
    provider.config.validator_model = 'different-model'
    provider.structured('changed', Verdict, 'scene', 'validator')
    assert len(calls) == 3


def make_scene(**overrides):
    base = dict(scene_id="scene_001", title="Test", refs=["3 Nephi 8:1"], summary="Test.",
        explicit_facts=[Claim(text="Test.", refs=["3 Nephi 8:1"])])
    base.update(overrides)
    return Scene(**base)


def test_classify_shot_major_and_final_verse_are_splash():
    assert classify_shot(make_scene(importance="major")) == "splash"
    assert classify_shot(make_scene(refs=["3 Nephi 11:7"])) == "splash"


def test_classify_shot_short_exchange_between_few_people_is_close():
    scene = make_scene(characters=["A", "B"],
        spoken_dialogue=[Speech(speaker="A", text="A short line.", refs=["3 Nephi 8:1"])])
    assert classify_shot(scene) == "close"


def test_classify_shot_disembodied_voice_is_not_close():
    """A scene with dialogue but zero visible characters (an off-screen voice, common in
    3 Nephi 9-10) must never get 'close' — that shot's camera/composition text calls for
    intimate framing on faces, which directly fights a no-figures-visible scene."""
    scene = make_scene(characters=[],
        spoken_dialogue=[Speech(speaker="Voice", text="A short line.", refs=["3 Nephi 8:1"])])
    assert classify_shot(scene) == "wide"


def test_classify_shot_long_dialogue_is_not_close():
    long = " ".join(["word"] * 40)
    scene = make_scene(characters=["A"], spoken_dialogue=[Speech(speaker="A", text=long, refs=["3 Nephi 8:1"])])
    assert classify_shot(scene) != "close"


def test_classify_shot_no_or_many_characters_is_wide():
    assert classify_shot(make_scene(characters=[])) == "wide"
    assert classify_shot(make_scene(characters=["A", "B", "C", "D"])) == "wide"


def test_classify_shot_narration_length_splits_tall_and_medium():
    short = make_scene(characters=["A"], narration=[Claim(text="Short beat here.", refs=["3 Nephi 8:1"])])
    long = make_scene(characters=["A"], narration=[Claim(text=" ".join(["word"] * 20), refs=["3 Nephi 8:1"])])
    assert classify_shot(short) == "tall"
    assert classify_shot(long) == "medium"


def test_classify_shot_is_deterministic_and_content_based():
    """Two adjacent scenes with identical content signals must land on the same shot, so
    they can pair into a two-column row — a purely positional tie-break would prevent that."""
    scene = make_scene(characters=["A"], narration=[Claim(text="Short beat here.", refs=["3 Nephi 8:1"])])
    other = make_scene(scene_id="scene_002", characters=["B"], narration=[Claim(text="Also short.", refs=["3 Nephi 8:1"])])
    assert classify_shot(scene) == classify_shot(other) == "tall"


def make_panel(i, weight=1, shot="medium", page=1, number=None, words=8, dialogue=False):
    text = " ".join(["TEST"] * words)
    claim = dict(text=text, refs=[f"3 Nephi 8:{i}"])
    return Panel(panel_id=f"panel_{i:03d}", scene_id=f"scene_{i:03d}", page=page, panel_number=number or i,
        weight=weight, refs=[f"3 Nephi 8:{i}"], characters_visible=[], location=[], action="Test", visual_facts=[],
        shot=shot, dialogue=[Speech(speaker="Test voice", **claim)] if dialogue else [],
        narration=[] if dialogue else [Claim(**claim)], visual_inferences=[], creative_details=[], prohibited=[])


def art_for(store, panels):
    from PIL import Image
    images = {}
    for panel_id, frame in frames(panels).items():
        path = store.path(f"images/{panel_id}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (frame[2], frame[3]), "gray").save(path)
        images[panel_id] = {"path": f"images/{panel_id}.png"}
    return images


def test_classify_weight_major_is_full_page_and_text_needs_room():
    assert classify_weight(make_scene(importance="major")) == PAGE_UNITS
    assert classify_weight(make_scene(refs=["3 Nephi 11:7"])) == PAGE_UNITS
    words = lambda n: [Claim(text=" ".join(["w"] * n), refs=["3 Nephi 8:1"])]
    assert classify_weight(make_scene(narration=words(20))) == 1
    assert classify_weight(make_scene(narration=words(60))) == 2
    assert classify_weight(make_scene(narration=words(120))) == 3


def test_plan_fills_pages_by_weight_and_gives_major_moments_a_page():
    scenes = [make_scene(scene_id=f"scene_{i:03d}", narration=[Claim(text="Short.", refs=["3 Nephi 8:1"])])
              for i in range(1, 8)]
    scenes[6] = make_scene(scene_id="scene_007", importance="major")
    panels = plan(scenes)
    pages = {}
    for p in panels:
        pages.setdefault(p.page, []).append(p)
    assert [len(g) for g in pages.values()] == [6, 1]
    assert all(sum(p.weight for p in g) <= PAGE_UNITS for g in pages.values())
    assert pages[2][0].weight == PAGE_UNITS


def test_plan_moves_a_stranded_small_panel_off_its_own_page():
    scenes = [make_scene(scene_id=f"scene_{i:03d}", narration=[Claim(text="Short.", refs=["3 Nephi 8:1"])])
              for i in range(1, 8)] + [make_scene(scene_id="scene_008", importance="major")]
    pages = [p.page for p in plan(scenes)]
    assert pages == [1, 1, 1, 1, 1, 2, 2, 3]


def test_plan_raises_weight_until_lettering_fits():
    # Thirty words scores weight 1, but six separate captions stack too tall for a sixth of a page.
    stacked = [Claim(text="word word word word word", refs=["3 Nephi 8:1"])] * 6
    scenes = [make_scene(scene_id=f"scene_{i:03d}", narration=stacked) for i in range(1, 7)]
    panels = plan(scenes)
    layout = frames(panels)
    assert any(p.weight > classify_weight(s) for p, s in zip(panels, scenes))
    store = Store(Path(tempfile.mkdtemp()) / "fits")
    assemble(store, panels, art_for(store, panels))
    assert all(MIN_ASPECT - 0.02 <= w / h <= MAX_ASPECT + 0.02 for _, _, w, h in layout.values())


def test_page_frames_flow_left_to_right_then_wrap_and_fill_the_page():
    group = [make_panel(i) for i in range(1, 7)]
    boxes = page_frames(group)
    width, height = PAGE_SIZE
    assert min(x for x, *_ in boxes) == MARGIN and min(y for _, y, *_ in boxes) == MARGIN
    assert max(x + w for x, _, w, _ in boxes) == width - MARGIN
    assert max(y + h for _, y, _, h in boxes) == height - BOTTOM
    order = [(y, x) for x, y, *_ in boxes]
    assert order == sorted(order)
    rows = {}
    for x, y, w, h in boxes:
        rows.setdefault(y, []).append((x, w))
    for cells in rows.values():
        for (x1, w1), (x2, _) in zip(cells, cells[1:]):
            assert x2 - (x1 + w1) == GUTTER
    assert max(len(c) for c in rows.values()) > 1


def test_page_frames_give_heavier_panels_more_area():
    group = [make_panel(1, weight=3), make_panel(2), make_panel(3), make_panel(4)]
    areas = [w * h for *_, w, h in page_frames(group)]
    # Weight orders sizes; renderable frame shapes can compress the exact proportion.
    assert areas[0] > 1.4 * max(areas[1:])


def test_ref_range_uses_the_actual_book_not_a_hardcoded_one():
    from bom_comic.comic import _ref_range
    assert _ref_range(["Alma 17:20"]) == "Alma 17:20"
    assert _ref_range(["Alma 17:20", "Alma 17:21", "Alma 17:23"]) == "Alma 17:20–23"
    assert _ref_range(["Alma 17:39", "Alma 18:1"]) == "Alma 17:39–18:1"
    assert _ref_range(["3 Nephi 8:1", "3 Nephi 8:2"]) == "3 Nephi 8:1–2"


def test_layout_letters_on_the_art_and_refuses_overcrowding(tmp_path):
    from PIL import Image
    store = Store(tmp_path / "layout")
    panels = [make_panel(1, words=30), make_panel(2, words=30, dialogue=True)]
    assemble(store, panels, art_for(store, panels))
    page = Image.open(store.path("pages/page_001.png"))
    assert page.size == PAGE_SIZE
    crowded, other = [make_panel(1, words=400), make_panel(2)], Store(tmp_path / "crowded")
    with pytest.raises(ValueError, match="Too much lettering"):
        assemble(other, crowded, art_for(other, crowded))


def test_captions_move_to_the_corner_that_keeps_faces_clear():
    from bom_comic.comic import place_captions
    frame = (100, 100, 600, 400)
    tl, tr, bl, br = (103, 103, 303, 183), (497, 103, 697, 183), (103, 417, 303, 497), (497, 417, 697, 497)
    narration = [(tl, ["n"], "caption", 0), (tr, ["n"], "caption", 1), (bl, ["n"], "caption", 2)]
    speech = [(br, ["s"], "balloon", 0), (bl, ["s"], "balloon", 1)]
    corners = lambda placed: [b for b, _, _ in placed]
    assert place_captions([narration, speech], frame, []) == ([o[0][:3] for o in (narration, speech)], [])
    assert corners(place_captions([narration, speech], frame, None)[0]) == [tl, br]
    # A face in the top-left sends narration top-right; speech stays in its default corner.
    placed, covered = place_captions([narration, speech], frame, [(20, 10, 80, 80)])
    assert corners(placed) == [tr, br] and covered == []
    # Faces in both top corners send narration to the bottom, still read before the speech beside it.
    placed, covered = place_captions([narration, speech], frame, [(20, 10, 80, 80), (560, 60, 20, 20)])
    assert corners(placed) == [bl, br] and covered == []
    # Narration never lands after speech on the bottom edge, so an uncleared face is kept and reported.
    placed, covered = place_captions([[narration[0], (br, ["n"], "caption", 3)], [speech[1]]], frame,
                                     [(20, 10, 80, 80)])
    assert corners(placed) == [tl, bl] and covered == [(20, 10, 80, 80)]
    # Boxes never overlap: speech can't flip under a tall narration box.
    tall = [((103, 103, 303, 450), ["n"], "caption", 0)]
    placed, _ = place_captions([tall, speech], frame, [(500, 330, 60, 60)])
    assert corners(placed) == [(103, 103, 303, 450), br]


def test_caption_options_offer_every_corner_and_width_default_first():
    from bom_comic.comic import _caption_options, _measure, BORDER
    draw, fonts = _measure()
    frame = (48, 48, 600, 600)
    for panel in (make_panel(1, words=30), make_panel(2, words=30, dialogue=True)):
        [options] = _caption_options(draw, panel, frame, fonts)
        assert options[0][3] == 0 and all(o[3] > 0 for o in options[1:])
        assert all(48 + BORDER <= b[0] < b[2] <= 648 - BORDER and 48 + BORDER <= b[1] < b[3] <= 648 - BORDER
                   for b, *_ in options)
        assert len({b[2] - b[0] for b, *_ in options}) > 1
        tops = {b[1] == 48 + BORDER for b, *_ in options}
        assert tops == ({True, False} if panel.narration else {False})


def test_assemble_records_caption_corners(tmp_path, monkeypatch):
    import bom_comic.comic as comic
    store = Store(tmp_path / "corners")
    panels = [make_panel(1, words=10), make_panel(2, words=10, dialogue=True)]
    monkeypatch.setattr(comic, "detect_faces", lambda art: [(5, 5, 60, 60)])
    assemble(store, panels, art_for(store, panels))
    record = store.read("final/captions.json")
    assert record["panel_001"]["corners"] == ["top-right"]
    assert record["panel_002"]["corners"] == ["bottom-right"] and record["panel_002"]["covered_faces"] == []


def test_moving_a_panel_without_reshaping_its_frame_keeps_its_image(pipeline):
    ready(pipeline)
    pipeline.generate()
    data = pipeline.store.read("panels.json")
    data["panels"][0]["page"], data["panels"][0]["weight"] = 3, 4
    pipeline.store.write("panels.json", data)
    pipeline.image_record(pipeline.panels()[0])


def test_page_weight_overflow_is_rejected(pipeline):
    ready(pipeline)
    data = pipeline.store.read("panels.json")
    data["panels"].append(dict(data["panels"][0], panel_id="panel_002", panel_number=2, weight=6))
    data["panels"][0]["weight"] = 6
    pipeline.store.write("panels.json", data)
    with pytest.raises(ValueError):
        pipeline.panels()


def test_render_size_and_gemini_snapping_handle_any_frame():
    from bom_comic.providers import render_size, nearest_gemini_aspect
    for aspect in ("652:261", "326:467", "1:1"):
        w, h = render_size(aspect)
        a, b = (int(n) for n in aspect.split(":"))
        assert w % 16 == 0 and h % 16 == 0 and abs(w / h - a / b) < 0.03
        assert 0.8e6 < w * h < 1.25e6
    assert nearest_gemini_aspect("652:261") == "21:9"
    assert nearest_gemini_aspect("326:467") == "2:3"
    assert frame_aspect((0, 0, 1304, 614)) == "652:307"


def test_placeholder_provider_honors_aspect_ratio(tmp_path):
    from PIL import Image
    store = Store(tmp_path / "placeholder")
    path = store.path("wide.png")
    PlaceholderImages().generate_image("prompt", output_path=path, aspect_ratio="16:9")
    w, h = Image.open(path).size
    assert abs(w / h - 16 / 9) < 0.01


def test_generate_requests_the_panel_shot_aspect_ratio(pipeline):
    ready(pipeline)
    calls = []
    original = pipeline.provider.generate_image
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    pipeline.provider.generate_image = capture
    pipeline.generate()
    assert calls[0]["aspect_ratio"] == pipeline.aspects()["panel_001"]


def test_gemini_image_adapter_sends_aspect_ratio(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from google.genai import types
    from bom_comic.providers import Gemini
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(data, format="PNG")
    response = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        parts=[types.Part.from_bytes(data=data.getvalue(), mime_type="image/png")]))])
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        return response
    provider = Gemini.__new__(Gemini)
    provider.config = Config(api_key="fake", image_model="configurable-model")
    provider.store = Store(tmp_path / "adapter")
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    output = provider.store.path("test.png")
    provider.generate_image("Approved prompt", output_path=output, aspect_ratio="16:9")
    assert calls[0]["config"]["image_config"] == {"aspect_ratio": "16:9"}


def test_diffusion_prompts_move_negations_to_negative(pipeline):
    from bom_comic.comic import diffusion_prompts
    ready(pipeline)
    panel = pipeline.panels()[0]
    positive, negative = diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY, "4:3"))
    assert panel.action in positive
    assert "lettering, captions, logos, or speech bubbles in the image" in negative
    assert "photoreal, a photograph or film still" in negative
    assert "European or Asian" in negative
    assert not any(word in positive for word in ("No ", "Never ", "never ", "Visualize only"))
    assert diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY, "4:3")) == (positive, negative)


def test_diffusion_prompts_only_asserts_architecture_when_a_structure_is_in_scene(pipeline):
    """Architecture style rules describe a building IF one appears; asserted unconditionally
    they put a temple in every panel, including open-country scenes with no structure at all."""
    from bom_comic.comic import diffusion_prompts
    ready(pipeline)
    panel = pipeline.panels()[0]
    positive, negative = diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY, "4:3"))
    assert "Mesoamerican/Andean-inspired" not in positive
    assert "buildings, temples, pyramids, palaces, or other man-made structures" in negative

    built = panel.model_copy(update={"location": ["the temple court"]})
    positive, negative = diffusion_prompts(build_prompt(built, DEFAULT_CONTINUITY, "4:3"))
    assert "Mesoamerican/Andean-inspired" in positive
    assert "buildings, temples, pyramids, palaces, or other man-made structures" not in negative


def test_comfyui_adapter_submits_workflow_and_saves_image(tmp_path, pipeline):
    import io
    from PIL import Image
    from bom_comic.providers import ComfyUI
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(data, format="PNG")
    history = {"abc": {"status": {"status_str": "success"},
        "outputs": {"save": {"images": [{"filename": "p.png", "subfolder": "bom_comic", "type": "output"}]}}}}
    calls = []
    def call(path, payload=None, timeout=60):
        calls.append((path, payload))
        if path == "/prompt":
            return json.dumps({"prompt_id": "abc"}).encode()
        if path.startswith("/history/"):
            return json.dumps(history).encode()
        return data.getvalue()
    provider = ComfyUI(Store(tmp_path / "comfy"), url="http://fake")
    provider._call = call
    ready(pipeline)
    panel_prompt = build_prompt(pipeline.panels()[0], DEFAULT_CONTINUITY, "16:9")
    output = provider.store.path("images/panel_001_aaa.png")
    output.parent.mkdir(parents=True)
    provider.generate_image(panel_prompt, output_path=output, aspect_ratio="16:9")
    workflow = calls[0][1]["prompt"]
    assert (workflow["latent"]["inputs"]["width"], workflow["latent"]["inputs"]["height"]) == (1360, 768)
    assert "People gathered." in workflow["pos"]["inputs"]["text"]
    assert "?filename=p.png&subfolder=bom_comic&type=output" in calls[-1][0]
    assert Image.open(output).size == (32, 32)
    assert provider.store.read("api/panel_001_aaa.request.json")["workflow"] == workflow


def test_plan_sets_shot_specific_camera_and_mood(pipeline):
    ready(pipeline)
    panel = pipeline.panels()[0]
    from bom_comic.comic import SHOT_CAMERA, SHOT_MOOD, SHOT_COMPOSITION
    assert panel.camera == SHOT_CAMERA[panel.shot]
    assert panel.mood == SHOT_MOOD[panel.shot]
    assert panel.composition == SHOT_COMPOSITION[panel.shot]


def _give_record(p, **extra):
    characters = p.store.read("continuity/characters.json")
    characters["Unnamed people"] = {"scriptural_facts": [], "visual_design_choices": ["Muted earth-tone garments"],
                                    "locked_traits": [], **extra}
    p.store.write("continuity/characters.json", characters)


def capture_images(p):
    calls = []
    original = p.provider.generate_image
    def capture(prompt, **kwargs):
        calls.append(dict(kwargs, prompt=prompt))
        return original(prompt, **kwargs)
    p.provider.generate_image = capture
    return calls


def test_panels_attach_at_most_three_reference_portraits(pipeline):
    from bom_comic.pipeline import MAX_REFERENCES
    names = ["Ann", "Ben", "Cal", "Dan", "Eve"]
    scenes = pipeline.store.read("scenes.json")
    scenes[0]["characters"] = names
    pipeline.store.write("scenes.json", scenes)
    characters = pipeline.store.read("continuity/characters.json")
    for name in names:
        characters[name] = {"scriptural_facts": [], "visual_design_choices": [f"{name}'s robe"], "locked_traits": []}
    pipeline.store.write("continuity/characters.json", characters)
    assert sorted(pipeline.portraits()) == names
    pipeline.validate()
    ready(pipeline)
    panel = pipeline.panels()[0]
    assert MAX_REFERENCES == 3 and list(pipeline.panel_portraits(panel)) == ["Ann", "Ben", "Cal"]
    # Speakers come first, then people named in the action; the rest are described by text alone.
    focused = panel.model_copy(update={"action": "Eve kneels.", "dialogue": [Speech(speaker="Dan", text="Hello.", refs=["3 Nephi 8:1"])]})
    assert list(pipeline.panel_portraits(focused)) == ["Ann", "Dan", "Eve"]


def test_portraits_render_once_per_character_and_are_reused(pipeline):
    assert pipeline.portraits() == []  # no record yet: nothing to draw from
    _give_record(pipeline)
    calls = capture_images(pipeline)
    assert pipeline.portraits() == ["Unnamed people"]
    assert pipeline.portraits() == []
    assert len(calls) == 1 and calls[0]["aspect_ratio"] == "3:4"
    assert "Reference portrait of Unnamed people" in calls[0]["prompt"]
    assert "Muted earth-tone garments" in calls[0]["prompt"]
    first = pipeline.portrait("Unnamed people")["path"]
    assert pipeline.portraits("Unnamed people") == ["Unnamed people"]
    assert pipeline.portrait("Unnamed people")["path"] != first
    assert pipeline.store.path(first).exists()
    with pytest.raises(ValueError, match="No portrait-eligible"):
        pipeline.portraits("Someone else")


def test_character_record_can_opt_out_of_portraits(pipeline):
    _give_record(pipeline, reference_portrait=False)
    assert pipeline.portrait_names() == []
    assert pipeline.portraits() == []


def test_generate_attaches_portraits_and_approval_covers_them(pipeline):
    _give_record(pipeline)
    ready(pipeline)
    pipeline.portraits()
    # A new portrait changes what the panel will be drawn from, so it needs approval again.
    with pytest.raises(ValueError, match="Review panel"):
        pipeline.generate()
    pipeline.review("panel", "panel_001", "approve")
    calls = capture_images(pipeline)
    pipeline.generate()
    entry = pipeline.portrait("Unnamed people")
    assert calls[0]["reference_images"] == [("Unnamed people", pipeline.store.path(entry["path"]))]
    assert "CHARACTER REFERENCE PORTRAITS" in calls[0]["prompt"]
    record = pipeline.store.read("images/panel_001.json")
    assert pipeline.store.read(record["prompt"])["portraits"] == {"Unnamed people": entry["path"]}
    assert record["references"] == ["Unnamed people"]
    pipeline.review("image", "panel_001", "approve")
    assert pipeline.assemble().exists()


def test_first_drafts_can_render_without_reference_portraits(pipeline):
    _give_record(pipeline)
    pipeline.portraits()
    ready(pipeline)
    calls = capture_images(pipeline)
    pipeline.generate(references=False)
    assert calls[0]["reference_images"] is None and "CHARACTER REFERENCE PORTRAITS" not in calls[0]["prompt"]
    record = pipeline.image_record(pipeline.panels()[0])
    assert record["references"] == []
    # The draft is still a current image for the approved panel, so it can be reviewed and assembled.
    pipeline.review("image", "panel_001", "approve")
    assert pipeline.assemble().exists()


def test_edited_character_record_makes_its_portrait_stale(pipeline):
    _give_record(pipeline)
    pipeline.portraits()
    ready(pipeline)
    _give_record(pipeline, locked_traits=["Same palette every time"])
    with pytest.raises(ValueError, match="run portraits again"):
        pipeline.panel_portraits(pipeline.panels()[0])
    assert pipeline.portraits() == ["Unnamed people"]
    assert pipeline.panel_portraits(pipeline.panels()[0])


def test_panel_without_portraits_keeps_its_stamp(pipeline):
    ready(pipeline)
    panel = pipeline.panels()[0]
    before = pipeline.panel_stamp(panel)
    assert "CHARACTER REFERENCE PORTRAITS" not in build_prompt(panel, pipeline.continuity(), "4:3")
    assert before == digest({"panel": panel.model_dump(exclude={"page", "panel_number", "weight"}),
        "aspect": pipeline.aspects()[panel.panel_id], "continuity": pipeline.continuity(), "source": pipeline.stamp()})


def test_diffusion_prompts_handle_portraits(pipeline):
    from bom_comic.comic import diffusion_prompts, build_portrait_prompt
    ready(pipeline)
    panel = pipeline.panels()[0]
    with_refs = diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY, "4:3", ["Unnamed people"]))
    assert with_refs == diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY, "4:3"))
    positive, negative = diffusion_prompts(build_portrait_prompt(
        "Ammon", {"visual_design_choices": ["Short dark beard"]}, DEFAULT_CONTINUITY["visual_style"]))
    assert "Reference portrait of Ammon" in positive and "Short dark beard" in positive
    assert "other people in the image" in negative


def test_gemini_adapter_labels_reference_portraits(tmp_path):
    import io
    from types import SimpleNamespace
    from PIL import Image
    from google.genai import types
    from bom_comic.providers import Gemini
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(data, format="PNG")
    response = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        parts=[types.Part.from_bytes(data=data.getvalue(), mime_type="image/png")]))])
    calls = []
    provider = Gemini.__new__(Gemini)
    provider.config = Config(api_key="fake", image_model="configurable-model")
    provider.store = Store(tmp_path / "adapter")
    provider.client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: calls.append(kwargs) or response))
    portrait = provider.store.path("ammon.png")
    Image.new("RGB", (30, 40), "red").save(portrait)
    provider.generate_image("Approved prompt", reference_images=[("Ammon", portrait)],
                            output_path=provider.store.path("out.png"))
    contents = calls[0]["contents"]
    assert contents[:2] == ["Approved prompt", "Reference portrait: Ammon"]
    assert contents[2].inline_data.mime_type == "image/png"


def test_character_bible_renders_reuses_and_rerenders_on_change(tmp_path):
    from bom_comic.pipeline import character_bible
    store = Store(tmp_path / "bible")
    record = {"scriptural_facts": [], "visual_design_choices": ["Tall, grey beard"]}
    store.write("characters.json", {"Lehi": record, "A crowd": {"visual_design_choices": ["x"], "reference_portrait": False},
                                    "Blank": {}})
    assert character_bible(store, PlaceholderImages()) == ["Lehi"]
    first = store.read("index.json")["Lehi"]
    assert store.path(first["path"]).exists()
    assert character_bible(store, PlaceholderImages()) == []
    store.write("characters.json", {"Lehi": dict(record, visual_design_choices=["Tall, white beard"])})
    assert character_bible(store, PlaceholderImages()) == ["Lehi"]
    assert store.read("index.json")["Lehi"]["path"] != first["path"]
    assert character_bible(store, PlaceholderImages(), "Lehi") == ["Lehi"]
    with pytest.raises(ValueError, match="No portrait-eligible"):
        character_bible(store, PlaceholderImages(), "Nobody")


def test_group_records_render_costume_sheets_and_panels_vary_faces(tmp_path, pipeline):
    from bom_comic.pipeline import character_bible
    from bom_comic.comic import build_portrait_prompt, is_group
    group = {"group": True, "scriptural_facts": [], "visual_design_choices": ["Shaved heads, skin girdles"]}
    person = {"scriptural_facts": [], "visual_design_choices": ["Grey beard"]}
    assert is_group(group) and not is_group(person)
    sheet = build_portrait_prompt("Lamanite warriors", group, DEFAULT_CONTINUITY["visual_style"])
    assert "four members" in sheet and "No identical faces" in sheet
    assert "No other people in the image" not in sheet
    calls = []
    class Recorder(PlaceholderImages):
        def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None):
            calls.append(aspect_ratio)
            return super().generate_image(prompt, reference_images, output_path, aspect_ratio)
    store = Store(tmp_path / "groups")
    store.write("characters.json", {"Lamanite warriors": group, "Lehi": person})
    character_bible(store, Recorder())
    assert calls == ["4:3", "3:4"]
    ready(pipeline)
    panel = pipeline.panels()[0]
    continuity = dict(DEFAULT_CONTINUITY, characters={"Lamanite warriors": group, "Lehi": person})
    prompt = build_prompt(panel, continuity, "4:3", ["Lehi", "Lamanite warriors"])
    assert "Reference portraits of individuals: Lehi" in prompt
    assert "Costume sheets for groups: Lamanite warriors" in prompt
    assert "never repeat one face" in prompt


def test_analyze_is_told_known_character_labels(tmp_path, source):
    prompts = []
    class Recorder(FakeAI):
        def structured(self, prompt, schema, tag, kind="text"):
            prompts.append(prompt)
            return super().structured(prompt, schema, tag, kind)
    p = Pipeline(Store(tmp_path / "known"), Recorder())
    p.init(source, "3 Nephi 8:1", "3 Nephi 8:2")
    p.store.write("continuity/characters.json", {"Nephi": {"visual_design_choices": ["tall"]}})
    p.analyze()
    assert 'use exactly this label: ["Nephi"]' in prompts[0]


def test_generate_skip_main_renders_only_panels_without_main_characters(pipeline):
    ready(pipeline)
    panel = pipeline.panels()[0]
    records = {name: {"visual_design_choices": ["x"]} for name in panel.characters_visible}
    pipeline.store.write("continuity/characters.json", records)
    pipeline.review("panel", panel.panel_id, "approve")
    assert pipeline.main_characters(panel) == panel.characters_visible
    pipeline.generate(skip_main=True)
    assert not pipeline.store.path(f"images/{panel.panel_id}.json").exists()
    records = {name: dict(r, group=True) for name, r in records.items()}
    pipeline.store.write("continuity/characters.json", records)
    pipeline.review("panel", panel.panel_id, "approve")
    assert pipeline.main_characters(panel) == []
    pipeline.generate(skip_main=True)
    assert pipeline.store.path(f"images/{panel.panel_id}.json").exists()


def test_validation_sends_only_nearby_verses():
    from bom_comic.analysis import scene_context
    verses = [Verse(book="Alma", chapter=17, verse=v, text=f"v{v}") for v in range(1, 40)]
    scene = make_scene(refs=["Alma 17:20", "Alma 17:21"])
    previous = make_scene(scene_id="scene_000", refs=["Alma 17:18"])
    got = [v["verse"] for v in scene_context(scene, verses, previous)]
    assert got == list(range(15, 25))


def test_gemini_waits_out_rate_limits_but_not_other_errors(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from google.genai.errors import APIError
    from bom_comic.providers import Gemini
    from bom_comic.errors import ProviderError
    monkeypatch.setattr("time.sleep", lambda s: None)
    provider = Gemini.__new__(Gemini)
    provider.config = Config(api_key="fake", text_model="m")
    provider.store = Store(tmp_path / "retry")
    calls = []
    def flaky(**kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise APIError(429, {"error": {"message": "Quota exceeded. Please retry in 1.5s."}})
        return "ok"
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=flaky))
    assert provider._request(model="m") == "ok" and len(calls) == 3
    def denied(**kwargs):
        raise APIError(403, {"error": {"message": "Denied"}})
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=denied))
    with pytest.raises(ProviderError):
        provider._request(model="m")


def test_malformed_scene_refs_are_rejected_not_crashed():
    verses = [Verse(book="1 Nephi", chapter=2, verse=v, text=f"v{v}") for v in (3, 4)]
    scene = make_scene(refs=["1 Nephi 2:3-4"], explicit_facts=[Claim(text="x", refs=["1 Nephi 2:3-4"])])
    assert any("malformed" in i for i in deterministic_issues(scene, verses))
