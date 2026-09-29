"""Write and check one chapter's scenes with the Codex CLI (ChatGPT plan), for comparison with the Gemini run.

Usage: python experiments/codex_chapter.py "1 Nephi" 4 38 [label] [chunk_size] [batch]
  e.g. python experiments/codex_chapter.py "1 Nephi" 4 38 lean 0 batch
Model and effort come from CODEX_TEXT_MODEL / CODEX_TEXT_EFFORT / CODEX_VALIDATOR_MODEL / CODEX_VALIDATOR_EFFORT.
Writes experiments/codex/<book>-<chapter>[-label]/ and prints timing, token totals, and verdicts.
"""
import sys
import time
from pathlib import Path
from bom_comic.codex import Codex
from bom_comic.pipeline import Pipeline
from bom_comic.storage import Store

book, chapter, last = sys.argv[1], sys.argv[2], sys.argv[3]
label = sys.argv[4] if len(sys.argv) > 4 else ""
chunk_size = int(sys.argv[5]) if len(sys.argv) > 5 else 6
batch = len(sys.argv) > 6 and sys.argv[6] == "batch"
root = Path("experiments/codex") / (f"{book.lower().replace(' ', '-')}-{chapter}" + (f"-{label}" if label else ""))
store = Store(root)
pipeline = Pipeline(store, Codex(store))
if not store.path("source.json").exists():
    pipeline.init("data/full-scripture.txt", f"{book} {chapter}:1", f"{book} {chapter}:{last}")
    # Same continuity the Gemini run had, so both writers know the same character and place labels.
    source = Store("runs/1-nephi-1-4")
    for part in ("characters", "locations", "visual_style"):
        store.write(f"continuity/{part}.json", source.read(f"continuity/{part}.json"))
pipeline.provider.reuse_responses = True  # rerunning after a usage limit picks up where it stopped
print("models:", pipeline.provider.models, "effort:", pipeline.provider.effort, "chunk:", chunk_size, "batch:", batch,
      flush=True)

started = time.monotonic()
pipeline.analyze(chunk_size=chunk_size)
analyzed = time.monotonic()
scenes = pipeline.scenes()
print(f"analyze: {len(scenes)} scenes in {analyzed - started:.0f}s", flush=True)
report = pipeline.validate(batch=batch)
print(f"validate: {time.monotonic() - analyzed:.0f}s", flush=True)
for scene in scenes:
    result = report[scene.scene_id]
    print(scene.scene_id, scene.refs[0], "-", scene.refs[-1], scene.importance, result["status"], "|", scene.title,
          "|", scene.locations, "|", result.get("issues", []))
responses = [store.read(f"api/{p.name}") for p in store.path("api").glob("*.response.json")]
totals = {}
for response in responses:
    for key, value in response.get("usage", {}).items():
        totals[key] = totals.get(key, 0) + value
print(f"Codex calls: {len(responses)} | tokens: {totals}")
