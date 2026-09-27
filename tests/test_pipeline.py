import json
import pytest
from bom_comic.models import Scene, Claim, Speech, Verse, SceneBatch, Verdict, Panel
from bom_comic.scripture import load, select, coordinate, COUNTS
from bom_comic.storage import Store, digest
from bom_comic.config import Config
from bom_comic.analysis import deterministic_issues
from bom_comic.comic import build_prompt, DEFAULT_CONTINUITY, SHOT_ASPECT, classify_shot, assemble, _rows
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
    p.plan(10)
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
        coordinate("3 Nephi 11:8")
    source.write_text(source.read_text() * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        load(source)


def test_full_scope_selection():
    verses = [Verse(chapter=c, verse=v, text="TEST ONLY") for c, n in COUNTS.items() for v in range(1, n + 1)]
    assert len(select(verses, "3 Nephi 8:1", "3 Nephi 11:7")) == 73


def test_serialization_and_prompt(pipeline):
    ready(pipeline)
    panel = pipeline.panels()[0]
    assert Panel.model_validate_json(panel.model_dump_json()) == panel
    prompt = build_prompt(panel, DEFAULT_CONTINUITY)
    assert "Never show Jesus Christ" in prompt
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
        pipeline.plan(10)


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


def test_epub_extraction_preserves_embedded_verse_and_cutoff(tmp_path):
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
    paragraphs.append('<p>11:8 Outside selected scope.</p></body></html>')
    path = tmp_path / 'fixture.epub'
    with ZipFile(path, 'w') as archive:
        archive.writestr('book.xhtml', ''.join(paragraphs))
    verses, evidence = extract(path)
    assert len(verses) == len(evidence) == 73
    assert verses[-6].text == 'Synthetic fixture 11-2.'
    assert verses[-7].text == 'Synthetic fixture 11-1.'
    assert verses[-1].ref == '3 Nephi 11:7'


def test_preview_does_not_bypass_approvals(pipeline):
    path = pipeline.preview(12)
    assert path.exists()
    assert 'NOT APPROVED' in pipeline.store.read('review/draft-panels.json')['status']
    assert not pipeline.store.path('panels.json').exists()
    assert pipeline.approvals() == {}
    with pytest.raises(ValueError, match='human approval'):
        pipeline.plan(12)


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


def test_rows_pairs_matching_tall_and_close_but_not_mixed_or_wide():
    panels = [Panel(panel_id=f"panel_{i:03d}", scene_id=f"scene_{i:03d}", page=1, panel_number=i,
        refs=["3 Nephi 8:1"], characters_visible=[], location=[], action="x", shot=shot,
        dialogue=[], narration=[], visual_facts=[], visual_inferences=[], creative_details=[], prohibited=[])
        for i, shot in enumerate(["tall", "tall", "wide", "close", "close", "close"], 1)]
    grouped = _rows(panels)
    sizes = [len(row) for row in grouped]
    assert sizes == [2, 1, 2, 1]  # tall+tall paired, wide alone, close+close paired, lone close alone


def layout_panels(store, shots, words=8, page=1, start=1):
    from PIL import Image
    panels, images = [], {}
    for offset, shot in enumerate(shots):
        i = start + offset
        text = " ".join(["TEST"] * words)
        panels.append(Panel(panel_id=f"panel_{i:03d}", scene_id=f"scene_{i:03d}", page=page, panel_number=offset + 1,
            refs=[f"3 Nephi 8:{i}"], characters_visible=[], location=[], action="Test", visual_facts=[],
            shot=shot, dialogue=[Speech(speaker="Test voice", text=text, refs=[f"3 Nephi 8:{i}"])], narration=[],
            visual_inferences=[], creative_details=[], prohibited=[]))
        rw, rh = (int(n) for n in SHOT_ASPECT[shot].split(":"))
        path = store.path(f"images/{i}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, round(1200 * rh / rw)), "gray").save(path)
        images[panels[-1].panel_id] = {"path": f"images/{i}.png"}
    return panels, images


def test_layout_renders_every_shot_and_fills_a_sparse_page(tmp_path):
    from bom_comic.comic import PAGE_SIZE, PAPER, TOP, BOTTOM
    from PIL import ImageColor, Image
    store = Store(tmp_path / "layout")
    panels, images = layout_panels(store, ["close", "close"])
    assemble(store, panels, images)
    page = Image.open(store.path("pages/page_001.png"))
    assert page.size == PAGE_SIZE
    # A two-panel page shouldn't be left floating in a mostly-blank column: the row-fill
    # step should expand it well past its two panels' own natural (unscaled) height.
    paper = ImageColor.getrgb(PAPER)
    column = [page.getpixel((page.width // 2, y)) for y in range(page.height)]
    content_rows = [y for y, pixel in enumerate(column) if pixel != paper]
    extent = max(content_rows) - min(content_rows)
    assert extent / (PAGE_SIZE[1] - TOP - BOTTOM) > 0.55


def test_layout_fits_full_lettering_budget_for_every_shot(tmp_path):
    """Every shot must hold the README's 65-word-per-scene lettering cap, in the page
    groupings plan() actually produces: a splash alone, three full-width rows, or a
    same-shot pair — never several full shots crammed onto one page."""
    from bom_comic.comic import PAGE_SIZE
    from PIL import Image
    store = Store(tmp_path / "budget")
    panels, images, start = [], {}, 1
    for page, shots in enumerate([["splash"], ["wide", "medium"], ["tall", "tall"], ["close", "close"]], 1):
        p, i = layout_panels(store, shots, words=65, page=page, start=start)
        panels += p
        images.update(i)
        start += len(shots)
    assemble(store, panels, images)
    for number in range(1, 5):
        assert Image.open(store.path(f"pages/page_{number:03d}.png")).size == PAGE_SIZE


def test_layout_refuses_overcrowded_page(tmp_path):
    store = Store(tmp_path / "crowded")
    panels, images = layout_panels(store, ["close", "close"], words=400)
    with pytest.raises(ValueError, match="Too much lettering"):
        assemble(store, panels, images)


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
    panel = pipeline.panels()[0]
    assert calls[0]["aspect_ratio"] == SHOT_ASPECT[panel.shot]


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
    positive, negative = diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY))
    assert panel.action in positive
    assert "lettering, captions, logos, or speech bubbles in the image" in negative
    assert "photoreal, a photograph or film still" in negative
    assert "European or Asian" in negative
    assert "Mesoamerican/Andean-inspired" in positive
    assert not any(word in positive for word in ("No ", "Never ", "never ", "Visualize only"))
    assert diffusion_prompts(build_prompt(panel, DEFAULT_CONTINUITY)) == (positive, negative)


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
    panel_prompt = build_prompt(pipeline.panels()[0], DEFAULT_CONTINUITY)
    output = provider.store.path("images/panel_001_aaa.png")
    output.parent.mkdir(parents=True)
    provider.generate_image(panel_prompt, output_path=output, aspect_ratio="16:9")
    workflow = calls[0][1]["prompt"]
    assert (workflow["latent"]["inputs"]["width"], workflow["latent"]["inputs"]["height"]) == ComfyUI.SIZES["16:9"]
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
