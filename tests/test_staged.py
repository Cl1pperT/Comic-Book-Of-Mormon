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
