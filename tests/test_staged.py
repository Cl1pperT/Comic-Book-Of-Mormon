from bom_comic.analysis import deterministic_issues
from bom_comic.models import Verse
from bom_comic.staged import analyze_staged, segment_schema, lettering_schema

VERSES = [Verse(book="1 Nephi", chapter=1, verse=n, text=f"And it came to pass that event number {n} happened here.")
          for n in range(1, 13)]


class ScriptedModel:
    """Answers each staged pass like a sloppy small model would."""
    def __init__(self):
        self.tags = []

    def structured(self, prompt, schema, tag, kind="text"):
        self.tags.append(tag)
        name = schema.__name__
        if name == "Segments":
            return schema.model_validate({"scenes": [{"first_verse": 4, "title": "Later"},
                                                     {"first_verse": 7, "title": "Last"}]})  # forgets verse 1
        refs = [line.split('"ref": "')[1].split('"')[0] for line in prompt.split("{")[1:] if '"ref": "' in line]
        if name == "Details":
            return schema.model_validate({
                "summary": "Something happens.", "characters": ["Nephi"], "locations": ["Jerusalem"],
                "explicit_facts": [{"text": "An event happened.", "refs": [refs[0]]}],
                "reasonable_visual_inferences": [], "unspecified_visual_details": [],
                "doctrinal_or_story_notes": [], "prohibited_inventions": []})
        if name == "Lettering":
            n = int(refs[0].rsplit(":", 1)[1])
            text = f"event number {n} happened" if "repair" in tag else "a paraphrase of the event"
            return schema.model_validate({"narration": [{"verse": refs[0], "text": text}], "dialogue": []})
        if name == "Importance":
            return schema.model_validate({"major": [2]})
        raise AssertionError(name)


def test_staged_analysis_enforces_coverage_quotes_and_one_major():
    model = ScriptedModel()
    scenes = analyze_staged(model, VERSES, known_characters=["Nephi"])
    covered = [r for s in scenes for r in s.refs]
    assert covered == [v.ref for v in VERSES]                      # no verse lost, none duplicated
    assert [s.refs[0] for s in scenes] == ["1 Nephi 1:1", "1 Nephi 1:4", "1 Nephi 1:7"]
    assert max(len(s.refs) for s in scenes) <= 6
    assert all(s.narration and s.narration[0].text.startswith("event number") for s in scenes)
    assert any("repair" in t for t in model.tags)                  # bad quote was sent back once
    assert [s.importance for s in scenes].count("major") == 1
    assert not any(deterministic_issues(s, VERSES) for s in scenes)


def test_staged_schemas_restrict_references_to_the_scene():
    seg = segment_schema([1, 2, 3]).model_json_schema()
    assert "enum" in json_dump(seg) and "[1, 2, 3]" in json_dump(seg)
    lett = lettering_schema(["1 Nephi 1:1"]).model_json_schema()
    assert '"1 Nephi 1:1"' in json_dump(lett) and "1 Nephi 2:3-4" not in json_dump(lett)


def json_dump(value):
    import json
    return json.dumps(value)


def test_failed_lettering_answer_goes_to_repair_not_crash():
    from bom_comic.errors import ProviderError
    class Flaky(ScriptedModel):
        def structured(self, prompt, schema, tag, kind="text"):
            if schema.__name__ == "Lettering" and "repair" not in tag:
                self.tags.append(tag)
                raise ProviderError("garbled JSON")
            return super().structured(prompt, schema, tag, kind)
    scenes = analyze_staged(Flaky(), VERSES)
    assert all(s.narration for s in scenes)


def test_claude_provider_returns_parsed_output_and_caches(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bom_comic.claude import Claude
    from bom_comic.errors import ProviderError
    from bom_comic.models import Verdict
    from bom_comic.storage import Store
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CLAUDE_TEXT_MODEL", "claude-haiku-4-5")
    provider = Claude(Store(tmp_path / "claude"))
    calls = []
    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=Verdict(status="PASS"), to_dict=lambda: {"ok": 1})
    provider.client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    provider.reuse_responses = True
    assert provider.structured("check", Verdict, "t").status == "PASS"
    assert calls[0]["output_format"] is Verdict and calls[0]["model"] == "claude-haiku-4-5"
    provider.structured("check", Verdict, "t")
    assert len(calls) == 1
    def refuse(**kwargs):
        return SimpleNamespace(stop_reason="refusal", parsed_output=None, to_dict=lambda: {})
    provider.client = SimpleNamespace(messages=SimpleNamespace(parse=refuse))
    import pytest
    with pytest.raises(ProviderError, match="declined"):
        provider.structured("other", Verdict, "t2")


def test_offline_validation_reuses_identical_checks_and_flags_changed_ones(tmp_path):
    from bom_comic.providers import CachedChecks
    from bom_comic.models import Verdict
    from bom_comic.storage import Store
    store = Store(tmp_path / "offline")
    store.write("api/validate_scene_001.cache.json", {"request": {"model": "m", "prompt": "same", "schema": Verdict.model_json_schema()},
                                                      "text": '{"status":"PASS","issues":[]}'})
    checks = CachedChecks(store, "m")
    assert checks.structured("same", Verdict, "validate_scene_001").status == "PASS"
    changed = checks.structured("edited", Verdict, "validate_scene_001")
    assert changed.status == "PASS WITH WARNINGS" and "human review" in changed.issues[0]


def test_comfyui_uses_kontext_with_reference_portraits(tmp_path):
    import io, json
    from PIL import Image
    from bom_comic.providers import ComfyUI
    from bom_comic.storage import Store
    png = io.BytesIO(); Image.new("RGB", (8, 8)).save(png, format="PNG")
    history = {"p": {"status": {"status_str": "success"},
                     "outputs": {"save": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]},
                                 "save_draft": {"images": [{"filename": "d.png", "subfolder": "", "type": "output"}]}}}}
    store = Store(tmp_path / "k")
    provider = ComfyUI(store, url="http://fake")
    sent = []
    def call(path, payload=None, timeout=60):
        if path == "/prompt":
            sent.append(payload["prompt"]); return b'{"prompt_id": "p"}'
        return json.dumps(history).encode() if path.startswith("/history") else png.getvalue()
    provider._call = call
    provider.upload = lambda path: f"uploaded_{Path(path).name}"
    portrait = tmp_path / "nephi.png"; Image.new("RGB", (8, 8)).save(portrait)
    from bom_comic.comic import DEFAULT_CONTINUITY, build_prompt
    from bom_comic.models import Panel
    panel = Panel(panel_id="panel_001", scene_id="scene_001", page=1, panel_number=1, weight=1, refs=["1 Nephi 1:1"],
                  characters_visible=["Nephi", "Laban"], location=[], action="Nephi stands.", dialogue=[], narration=[],
                  visual_facts=[], visual_inferences=[], creative_details=[], prohibited=[])
    prompt = build_prompt(panel, DEFAULT_CONTINUITY, "4:3")
    out = store.path("images/panel_001_x.png"); out.parent.mkdir(parents=True)
    provider.generate_image(prompt, reference_images=[("Nephi", portrait), ("Laban", portrait)], output_path=out, aspect_ratio="4:3")
    workflow = sent[-1]
    # Step one: plain Flux drafts the panel with the style prompt and negative prompt intact.
    assert workflow["unet"]["inputs"]["unet_name"] == provider.unet and workflow["sample"]["inputs"]["cfg"] == provider.cfg
    assert "Nephi stands" in workflow["pos"]["inputs"]["text"] and workflow["neg"]["class_type"] == "CLIPTextEncode"
    assert workflow["save_draft"]["inputs"]["images"] == ["decode", 0]
    # Step two: Kontext edits that draft (its latent first), with the portraits after it.
    assert workflow["k_unet"]["inputs"]["unet_name"] == provider.kontext_unet
    assert workflow["k_draft"]["inputs"]["latent"] == ["sample", 0]
    assert workflow["k_load0"]["inputs"]["image"] == "uploaded_nephi.png" and "k_ref1" in workflow
    assert workflow["save"]["inputs"]["images"] == ["k_decode", 0]
    edit = workflow["k_pos"]["inputs"]["text"]
    assert edit.startswith("Edit this comic panel so that Nephi, Laban match the reference images")
    assert "Keep everything else the same" in edit
    assert store.read("api/panel_001_x.request.json")["edit"] == edit
    assert out.with_name("panel_001_x_draft.png").exists()
    provider.generate_image(prompt, output_path=out, aspect_ratio="4:3")
    assert sent[-1]["unet"]["inputs"]["unet_name"] == provider.unet and "k_unet" not in sent[-1]


from pathlib import Path



def test_codex_runs_one_read_only_exec_per_call_and_holds_the_schema(tmp_path, monkeypatch):
    import json
    import subprocess
    import pytest
    from bom_comic import codex
    from bom_comic.errors import ProviderError
    from bom_comic.models import Verdict
    from bom_comic.storage import Store
    monkeypatch.setattr(codex.shutil, "which", lambda name: "C:/fake/codex.cmd")
    calls = []

    def run(args, input, **kwargs):
        calls.append((args, input))
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
        Path(args[args.index("--output-last-message") + 1]).write_text('{"status": "PASS", "issues": []}', encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(codex.subprocess, "run", run)
    monkeypatch.setenv("CODEX_VALIDATOR_EFFORT", "high")
    store = Store(tmp_path / "run")
    provider = codex.Codex(store)
    assert provider.structured("Check this scene.", Verdict, "validate_scene_001", "validator").status == "PASS"
    args, stdin = calls[0]
    assert args[:2] == ["C:/fake/codex.cmd", "exec"] and args[-1] == "-" and stdin == "Check this scene."
    assert args[args.index("--sandbox") + 1] == "read-only" and "--skip-git-repo-check" in args and "--ephemeral" in args
    assert "model_reasoning_effort=high" in args and "--model" not in args
    # --resume reuses an identical request instead of spending plan usage again.
    provider.reuse_responses = True
    provider.structured("Check this scene.", Verdict, "validate_scene_001", "validator")
    assert len(calls) == 1
    # Failures surface as provider errors, with usage limits called out so the run can resume later.
    monkeypatch.setattr(codex.subprocess, "run", lambda args, input, **k: subprocess.CompletedProcess(
        args, 1, "", "ERROR: You've hit your usage limit. Try again later."))
    with pytest.raises(ProviderError, match="usage limit"):
        provider.structured("Other scene.", Verdict, "validate_scene_002", "validator")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-install"))
    with pytest.raises(ProviderError, match="codex login"):
        codex.Codex(store)


def test_kontext_prompt_says_other_figures_are_different_people():
    from bom_comic.comic import visible_people
    prompt = "Visualize.\n\nVISIBLE PEOPLE\n[\"Lehi\", \"One\", \"twelve others\"]\n\nLOCATION\n[]"
    assert visible_people(prompt) == ["Lehi", "One", "twelve others"]
