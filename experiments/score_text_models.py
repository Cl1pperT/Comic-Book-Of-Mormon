"""Score the text-model test runs made by text_models.py. Run from the repo root."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bom_comic.analysis import deterministic_issues  # noqa: E402
from bom_comic.models import Scene, Verse  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BOOK_VERSES = 6604
# Paid tier, standard (non-batch) USD per 1M tokens, from ai.google.dev/gemini-api/docs/pricing; thinking bills as output.
PRICES = {"gemini-3.1-pro-preview": (2.00, 12.00), "gemini-3.8-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50)}


def usage(run, prefix):
    tokens, cost = Counter(), 0.0
    for response in (run / "api").glob(f"{prefix}*.response.json"):
        request = run / "api" / response.name.replace(".response.", ".request.")
        model = json.loads(request.read_text(encoding="utf-8"))["model"] if request.exists() else "gemini-3.1-pro-preview"
        data = json.loads(response.read_text(encoding="utf-8"))
        if "usage_metadata" in data:
            u = data["usage_metadata"] or {}
            inp, out = u.get("prompt_token_count") or 0, (u.get("candidates_token_count") or 0) + (u.get("thoughts_token_count") or 0)
        else:  # Ollama: local, free
            inp, out, model = data.get("prompt_eval_count", 0), data.get("eval_count", 0), "local"
        tokens["calls"] += 1
        tokens["in"] += inp
        tokens["out"] += out
        price = PRICES.get(model, (0, 0))
        cost += inp / 1e6 * price[0] + out / 1e6 * price[1]
    return tokens, cost


def score(name):
    run = ROOT / "runs" / f"test-{name}"
    timing = json.loads((run / "timing.json").read_text(encoding="utf-8")) if (run / "timing.json").exists() else {}
    result = {"setup": name, "timing": timing}
    if not (run / "scenes.json").exists():
        result["failed"] = timing.get("analyze", {}).get("error", "no scenes")
        return result
    verses = [Verse.model_validate(v) for v in json.loads((run / "source.json").read_text(encoding="utf-8"))]
    scenes = [Scene.model_validate(s) for s in json.loads((run / "scenes.json").read_text(encoding="utf-8"))]
    covered = {r for s in scenes for r in s.refs}
    cast = set(json.loads((run / "continuity" / "characters.json").read_text(encoding="utf-8")))
    issues = Counter(i for s in scenes for i in deterministic_issues(s, verses))
    validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))["results"] if (run / "validation.json").exists() else {}
    labels = Counter(c for s in scenes for c in s.characters)
    a_tok, a_cost = usage(run, "analyze_")
    s_tok, s_cost = usage(run, "staged_")
    a_tok, a_cost = a_tok + s_tok, a_cost + s_cost
    v_tok, v_cost = usage(run, "validate_")
    per_verse = (a_cost + v_cost) / len(verses)
    result.update({
        "scenes": len(scenes),
        "missing_verses": len([v for v in verses if v.ref not in covered]),
        "deterministic_issues": dict(issues),
        "scenes_with_deterministic_issues": sum(1 for s in scenes if deterministic_issues(s, verses)),
        "validation": dict(Counter(v["status"] for v in validation.values())),
        "empty_characters": sum(1 for s in scenes if not s.characters),
        "major_scenes": sum(1 for s in scenes if s.importance == "major"),
        "lettering_words": sum(len(c.text.split()) for s in scenes for c in s.spoken_dialogue + s.narration),
        "known_cast_labels": sorted(l for l in labels if l in cast),
        "other_labels": sorted(l for l in labels if l not in cast),
        "analyze_tokens": dict(a_tok), "validate_tokens": dict(v_tok),
        "cost_usd": round(a_cost + v_cost, 3),
        "projected_book_cost_usd": round(per_verse * BOOK_VERSES, 0),
    })
    return result


def side_by_side(names):
    """Readable comparison: each setup's scene titles and lettering for the same verses."""
    lines = ["# 1 Nephi 1-4: scenes by setup", ""]
    for name in names:
        path = ROOT / "runs" / f"test-{name}" / "scenes.json"
        if not path.exists():
            continue
        lines += [f"## {name}", ""]
        for s in json.loads(path.read_text(encoding="utf-8")):
            refs = f"{s['refs'][0]}" + (f" to {s['refs'][-1]}" if len(s["refs"]) > 1 else "")
            lines.append(f"- **{s['title']}** ({refs}) chars: {', '.join(s.get('characters', [])) or 'none'}")
            for c in s.get("spoken_dialogue", []):
                lines.append(f"  - {c['speaker']}: â€œ{c['text']}â€")
            for c in s.get("narration", []):
                lines.append(f"  - Narration: {c['text']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    names = sys.argv[1:] or ["A-pro", "B-pro-low", "C-flash", "E-flash-lite", "D-local",
                             "F-staged-local", "F2-staged-local-think", "G-staged-flash"]
    results = [score(n) for n in names]
    out = ROOT / "experiments" / "results"
    out.mkdir(exist_ok=True)
    (out / "text_models.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out / "text_models_scenes.md").write_text(side_by_side(names), encoding="utf-8")
    for r in results:
        print(json.dumps({k: v for k, v in r.items() if k not in ("other_labels", "known_cast_labels", "timing")}))

