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
        events = '{"type":"turn.started"}\n{"type":"turn.completed","usage":{"input_tokens":5000,"output_tokens":40}}\n'
        return subprocess.CompletedProcess(args, 0, events, "")
    monkeypatch.setattr(codex.subprocess, "run", run)
    monkeypatch.setenv("CODEX_VALIDATOR_EFFORT", "high")
    store = Store(tmp_path / "run")
    provider = codex.Codex(store)
    assert provider.structured("Check this scene.", Verdict, "validate_scene_001", "validator").status == "PASS"
    args, stdin = calls[0]
    assert args[:2] == ["C:/fake/codex.cmd", "exec"] and args[-1] == "-" and stdin == "Check this scene."
    assert args[args.index("--sandbox") + 1] == "read-only" and "--skip-git-repo-check" in args and "--ephemeral" in args
    assert "model_reasoning_effort=high" in args and "--model" not in args
    # Lean calls: Codex's agent extras are off and our short instructions replace its built-in ones.
    assert "--json" in args and "agents.enabled=false" in args and args.count("--disable") == len(codex.LEAN_FEATURES)
    assert any(a.startswith("model_instructions_file=") for a in args)
    assert store.read("api/validate_scene_001.response.json")["usage"] == {"input_tokens": 5000, "output_tokens": 40}
    # --resume reuses an identical request instead of spending plan usage again.
    provider.reuse_responses = True
    provider.structured("Check this scene.", Verdict, "validate_scene_001", "validator")
    assert len(calls) == 1
    # Rewrites of rejected scenes can use a stronger model than first drafts.
    monkeypatch.setenv("CODEX_TEXT_MODEL", "small")
    monkeypatch.setenv("CODEX_REPAIR_MODEL", "bigger")
    codex.Codex(store).structured("Rewrite.", Verdict, "regenerate_scene_001_x", "repair")
    assert calls[-1][0][calls[-1][0].index("--model") + 1] == "bigger"
    # Failures surface as provider errors, with usage limits called out so the run can resume later.
    monkeypatch.setattr(codex.subprocess, "run", lambda args, input, **k: subprocess.CompletedProcess(
        args, 1, "", "ERROR: You've hit your usage limit. Try again later."))
    with pytest.raises(ProviderError, match="usage limit"):
        provider.structured("Other scene.", Verdict, "validate_scene_002", "validator")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-install"))
    with pytest.raises(ProviderError, match="codex login"):
        codex.Codex(store)


def test_whole_chapter_analysis_and_known_locations():
    from bom_comic.analysis import analyze
    from bom_comic.models import Claim, Scene, SceneBatch
    prompts = []

    class Writer:
        def structured(self, prompt, schema, tag, kind="text"):
            prompts.append((tag, prompt))
            return SceneBatch(scenes=[Scene(scene_id="scene_0", title="t", refs=["1 Nephi 1:1"], summary="s",
                                            explicit_facts=[Claim(text="x", refs=["1 Nephi 1:1"])])])
    analyze(Writer(), VERSES, 0, known_characters=["Nephi"], known_locations=["Jerusalem"])
    assert [tag for tag, _ in prompts] == ["analyze_1_nephi_1_0"]  # 12 verses, one call
    assert 'use exactly this label: ["Jerusalem"]' in prompts[0][1] and "never a description of the moment" in prompts[0][1]
    assert "Break this whole chapter" in prompts[0][1] and "never merge separate events" in prompts[0][1]
    analyze(Writer(), VERSES, 6)
    assert len(prompts) == 3 and "Break this portion into a few" in prompts[1][1]


def test_batch_validation_audits_a_chapter_in_one_call():
    from bom_comic.analysis import validate_batch
    from bom_comic.models import ChapterVerdicts, Claim, Scene, SceneVerdict
    calls = []

    class Auditor:
        def structured(self, prompt, schema, tag, kind="text"):
            calls.append((schema, tag, kind))
            # Answers for scene_1 only; scene_2 is skipped by the auditor.
            return ChapterVerdicts(verdicts=[SceneVerdict(scene_id="scene_1", status="PASS WITH WARNINGS", issues=["x"])])
    make = lambda sid, ref, text: Scene(scene_id=sid, title="t", refs=[ref], summary="s",
                                       explicit_facts=[Claim(text="x", refs=[ref])], narration=[Claim(text=text, refs=[ref])])
    scenes = [make("scene_1", "1 Nephi 1:1", "event number 1"), make("scene_2", "1 Nephi 1:2", "event number 2"),
              make("scene_3", "1 Nephi 1:3", "not in the verse")]
    results = validate_batch(Auditor(), scenes, VERSES)
    assert len(calls) == 1 and calls[0][0] is ChapterVerdicts and calls[0][2] == "validator"
    assert results["scene_1"].status == "PASS WITH WARNINGS"
    assert results["scene_2"].status == "REJECT" and "no verdict" in results["scene_2"].issues[0]
    # Deterministic failures are rejected without being sent to the model.
    assert results["scene_3"].status == "REJECT" and "exact source quotation" in results["scene_3"].issues[0]
    assert calls[0][1] == "validate_scene_1_scene_2"


def test_book_writer_writes_audits_repairs_and_resumes(tmp_path, monkeypatch):
    import pytest
    from bom_comic import book
    from bom_comic.errors import ProviderError
    from bom_comic.models import ChapterVerdicts, Claim, Scene, SceneBatch, SceneVerdict
    source = tmp_path / "src.txt"
    source.write_text("1 Nephi 1:1 TEST FIXTURE: One.\n1 Nephi 1:2 TEST FIXTURE: Two.\n"
                      "1 Nephi 2:1 TEST FIXTURE: Three.\n2 Nephi 1:1 TEST FIXTURE: Four.\n", encoding="utf-8")
    monkeypatch.setattr(book, "_continuity", lambda: {"characters": {}, "locations": {}, "visual_style": {}})
    tags = []

    class Fake:
        def __init__(self, store):
            self.store = store

        def structured(self, prompt, schema, tag, kind="text"):
            tags.append(tag)
            if schema is ChapterVerdicts:
                # The first audit of 1 Nephi 1 rejects scene_002; later audits pass everything.
                reject = "1-nephi/001" in self.store.root.as_posix() and not any(t.startswith("regenerate") for t in tags)
                ids = [s["scene_id"] for s in json.loads(prompt[prompt.index("{"):])["scenes"]]
                return ChapterVerdicts(verdicts=[SceneVerdict(scene_id=i, status="REJECT" if reject and i == "scene_002"
                                                              else "PASS", issues=["x"] if reject else []) for i in ids])
            if "2 Nephi" in prompt and "Regenerate" not in prompt:
                raise ProviderError("Codex usage limit reached during analyze; resume later with --resume.")
            verses = json.loads(prompt[prompt.rindex("Source:\n") + 8:]) if "Source:\n" in prompt else \
                json.loads(prompt[prompt.index("{"):])["source"]
            return SceneBatch(scenes=[Scene(scene_id="scene_0", title="t", refs=[f"{v['book']} {v['chapter']}:{v['verse']}"],
                summary="s", explicit_facts=[Claim(text="x", refs=[f"{v['book']} {v['chapter']}:{v['verse']}"])])
                for v in verses])
    import json
    assert book.chapters(source) == [("1 Nephi", 1, 2), ("1 Nephi", 2, 1), ("2 Nephi", 1, 1)]
    written, reason = book.write_book(tmp_path / "book", source, make_provider=Fake)
    assert reason == "usage limit" and [(s["book"], s["chapter"]) for s in written] == [("1 Nephi", 1), ("1 Nephi", 2)]
    first = written[0]
    assert first["scenes"] == 2 and first["counts"]["REJECT"] == 0  # repaired in one round
    assert any(t.startswith("regenerate_scene_002") for t in tags)
    assert (tmp_path / "book" / "1-nephi" / "001" / "status.json").exists()
    # Rerunning skips finished chapters and resumes at the one that hit the limit.
    tags.clear()
    written, reason = book.write_book(tmp_path / "book", source, make_provider=Fake)
    assert written == [] and reason == "usage limit" and tags == ["analyze_2_nephi_1_0"]
    with pytest.raises(ValueError, match="Unknown chapter"):
        book.write_book(tmp_path / "book", source, start="3 Nephi 99", make_provider=Fake)
    # A live writer's lock makes a second writer stand down; a dead one's lock is taken over.
    import subprocess, sys
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "book" / "writing.lock").write_text(str(other.pid))
        tags.clear()
        written, reason = book.write_book(tmp_path / "book", source, make_provider=Fake)
        assert written == [] and reason.startswith("another writer is running") and tags == []
        assert book.repair_blocked(tmp_path / "book", make_provider=Fake)[1].startswith("another writer")
    finally:
        other.kill()
        other.wait()
    written, reason = book.write_book(tmp_path / "book", source, make_provider=Fake)
    assert reason == "usage limit" and not (tmp_path / "book" / "writing.lock").exists()


def test_escalation_rewrites_with_the_frontier_model_and_reaudits_with_the_mid_model(monkeypatch):
    from bom_comic import book
    monkeypatch.setenv("CODEX_ESCALATE_MODEL", "frontier")
    monkeypatch.setenv("CODEX_ESCALATE_VALIDATOR_MODEL", "mid")
    seen = []

    class Provider:
        models = {"text": "small", "validator": "small", "repair": "mid"}
        effort = {"text": "low", "validator": "medium", "repair": "medium"}

    class FakePipeline:
        provider = Provider()

        def analyze(self, sid):
            seen.append(("rewrite", sid, self.provider.models["repair"]))

        def validate(self, batch=False):
            seen.append(("audit", self.provider.models["validator"]))
            return {"scene_001": {"status": "PASS"}, "scene_002": {"status": "PASS"}}
    report = {"scene_001": {"status": "PASS"}, "scene_002": {"status": "REJECT"}}
    assert book._escalate(FakePipeline(), report) == {"scene_001": {"status": "PASS"}, "scene_002": {"status": "PASS"}}
    assert seen == [("rewrite", "scene_002", "frontier"), ("audit", "mid")]
    assert Provider.models == {"text": "small", "validator": "small", "repair": "mid"}  # restored afterwards
    seen.clear()
    assert book._escalate(FakePipeline(), {"scene_001": {"status": "PASS"}}) and seen == []  # nothing to escalate


def test_nightly_writes_ahead_renders_in_order_and_resumes(tmp_path, monkeypatch):
    import datetime
    import json
    import bom_comic.codex
    import bom_comic.providers
    from bom_comic import book, nightly
    from bom_comic.models import ChapterVerdicts, Claim, Scene, SceneBatch, SceneVerdict
    from bom_comic.providers import PlaceholderImages
    source = tmp_path / "src.txt"
    source.write_text("".join(f"1 Nephi {c}:{v} TEST FIXTURE: Event {c}.{v}.\n" for c in (1, 2, 3) for v in (1, 2)),
                      encoding="utf-8")
    library = tmp_path / "library"
    library.mkdir()
    (library / "characters.json").write_text("{}", encoding="utf-8")
    (library / "index.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(nightly, "ROOT", tmp_path / "book")
    monkeypatch.setattr(nightly, "SOURCE", str(source))
    monkeypatch.setattr(nightly, "LIBRARY", str(library))
    monkeypatch.setattr(nightly, "comfy_up", lambda: True)
    monkeypatch.setattr(book, "_continuity", lambda: {"characters": {}, "locations": {}, "visual_style": {}})
    calls = []

    class FakeCodex:
        def __init__(self, store):
            self.store = store

        def structured(self, prompt, schema, tag, kind="text"):
            calls.append(tag)
            if schema is ChapterVerdicts:
                ids = [s["scene_id"] for s in json.loads(prompt[prompt.index("{"):])["scenes"]]
                return ChapterVerdicts(verdicts=[SceneVerdict(scene_id=i, status="PASS") for i in ids])
            verses = json.loads(prompt[prompt.rindex("Source:\n") + 8:])
            ref = lambda v: f"{v['book']} {v['chapter']}:{v['verse']}"
            return SceneBatch(scenes=[Scene(scene_id="scene_0", title="t", refs=[ref(v) for v in verses], summary="s",
                                            explicit_facts=[Claim(text="x", refs=[ref(verses[0])])],
                                            narration=[Claim(text=f"“{verses[0]['text']}”", refs=[ref(verses[0])])])])
    monkeypatch.setattr(bom_comic.codex, "Codex", FakeCodex)
    monkeypatch.setattr(bom_comic.providers, "ComfyUI", lambda store: PlaceholderImages())
    until = (datetime.datetime.now() - datetime.timedelta(minutes=1)).time()  # ~24 hours away
    report = nightly.run(until)
    assert report["written"] == ["1 Nephi 1", "1 Nephi 2", "1 Nephi 3"]
    assert report["rendered"] == ["1 Nephi 1", "1 Nephi 2", "1 Nephi 3"] and report["stopped"] == "nothing ready to render"
    chapter = tmp_path / "book" / "1-nephi" / "002"
    assert (chapter / "final" / "comic.pdf").exists() and (chapter / "render.json").exists()
    # Quote marks the model wrapped around a caption were stripped, so the exact-quotation check passed.
    assert json.loads((chapter / "scenes.json").read_text(encoding="utf-8"))[0]["narration"][0]["text"] == "TEST FIXTURE: Event 2.1."
    reviews = json.loads((chapter / "reviews.json").read_text(encoding="utf-8"))
    assert all(r["note"].startswith("AUTOMATED") for r in reviews.values())
    assert list((tmp_path / "book" / "nightly").glob("*.json")) and not (tmp_path / "book" / "nightly" / "nightly.lock").exists()
    # The next night resumes: nothing to write or draw again.
    calls.clear()
    report = nightly.run(until)
    assert calls == [] and report["rendered"] == [] and report["written"] == []


def test_kontext_prompt_says_other_figures_are_different_people():
    from bom_comic.comic import visible_people
    prompt = "Visualize.\n\nVISIBLE PEOPLE\n[\"Lehi\", \"One\", \"twelve others\"]\n\nLOCATION\n[]"
    assert visible_people(prompt) == ["Lehi", "One", "twelve others"]
