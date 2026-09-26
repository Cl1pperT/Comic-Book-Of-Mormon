import json
import pytest
from bom_comic.models import Scene, Claim, Speech, Verse, SceneBatch, Verdict, Panel
from bom_comic.scripture import load, select, coordinate, COUNTS
from bom_comic.storage import Store, digest
from bom_comic.config import Config
from bom_comic.analysis import deterministic_issues
from bom_comic.comic import build_prompt, DEFAULT_CONTINUITY
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
