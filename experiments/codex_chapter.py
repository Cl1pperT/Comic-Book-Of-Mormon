"""Write and check one chapter's scenes with the Codex CLI (ChatGPT plan), for comparison with the Gemini run.

Usage: python experiments/codex_chapter.py "1 Nephi" 4 38
Writes experiments/codex/<book>-<chapter>/ and prints timing and scene counts; nothing else is touched.
"""
import sys
import time
from pathlib import Path
from bom_comic.codex import Codex
from bom_comic.pipeline import Pipeline
from bom_comic.storage import Store

book, chapter, last = sys.argv[1], sys.argv[2], sys.argv[3]
root = Path("experiments/codex") / f"{book.lower().replace(' ', '-')}-{chapter}"
store = Store(root)
pipeline = Pipeline(store, Codex(store))
if not store.path("source.json").exists():
    pipeline.init("data/full-scripture.txt", f"{book} {chapter}:1", f"{book} {chapter}:{last}")
    # Same continuity the Gemini run had, so both writers know the same character and place labels.
    source = Store("runs/1-nephi-1-4")
    for part in ("characters", "locations", "visual_style"):
        store.write(f"continuity/{part}.json", source.read(f"continuity/{part}.json"))
pipeline.provider.reuse_responses = True  # rerunning after a usage limit picks up where it stopped

started = time.monotonic()
pipeline.analyze()
analyzed = time.monotonic()
scenes = pipeline.scenes()
print(f"analyze: {len(scenes)} scenes in {analyzed - started:.0f}s", flush=True)
report = pipeline.validate()
print(f"validate: {time.monotonic() - analyzed:.0f}s", flush=True)
for scene in scenes:
    result = report[scene.scene_id]
    print(scene.scene_id, scene.refs[0], "-", scene.refs[-1], scene.importance, result["status"], "|", scene.title,
          "|", result.get("issues", []))
calls = len(list(store.path("api").glob("*.request.json")))
print(f"Codex calls: {calls}")
