"""Write the book's scenes chapter by chapter, ahead of rendering: one run folder per chapter.

Each chapter is written in one call, audited in one call, and its rejected scenes are rewritten (told why) and
re-audited up to REPAIR_ROUNDS times. Scenes are never approved here; that stays a review step. A finished chapter
leaves status.json in its folder and is skipped on the next run, so a run stopped by a usage limit simply resumes.
"""
import json
import os
from pathlib import Path
from .errors import ProviderError
from .pipeline import Pipeline
from .scripture import load
from .storage import Store

REPAIR_ROUNDS = 2
LIBRARY = Path("portraits/book-of-mormon")
LOCATIONS = Path("runs/1-nephi-1-4/continuity/locations.json")
# Measured on 1 Nephi 4: the small model writes well at low effort in one call per chapter; auditing needs medium,
# where low effort flip-flopped between false rejects and passes.
CODEX_DEFAULTS = {"CODEX_TEXT_MODEL": "gpt-6-luna", "CODEX_TEXT_EFFORT": "low",
                  "CODEX_VALIDATOR_MODEL": "gpt-6-luna", "CODEX_VALIDATOR_EFFORT": "medium"}


def chapters(source):
    """(book, chapter, last verse) in canonical order."""
    last = {}
    for verse in load(source):
        last[(verse.book, verse.chapter)] = max(verse.verse, last.get((verse.book, verse.chapter), 0))
    return [(book, chapter, n) for (book, chapter), n in last.items()]


def folder(root, book, chapter):
    return Path(root) / book.lower().replace(" ", "-") / f"{chapter:03d}"


def _continuity():
    return {"characters": json.loads((LIBRARY / "characters.json").read_text(encoding="utf-8")),
            "locations": json.loads(LOCATIONS.read_text(encoding="utf-8")) if LOCATIONS.exists() else {},
            "visual_style": json.loads((LIBRARY / "style.json").read_text(encoding="utf-8"))}


def _usage(store):
    totals, calls = {}, 0
    for path in store.path("api").glob("*.response.json"):
        calls += 1
        for key, value in json.loads(path.read_text(encoding="utf-8")).get("usage", {}).items():
            totals[key] = totals.get(key, 0) + value
    return calls, totals


def write_chapter(root, source, book, chapter, last, make_provider):
    store = Store(folder(root, book, chapter))
    pipeline = Pipeline(store, None)
    if not store.path("source.json").exists():
        pipeline.init(source, f"{book} {chapter}:1", f"{book} {chapter}:{last}")
        for part, value in _continuity().items():
            store.write(f"continuity/{part}.json", value)
    pipeline.provider = make_provider(store)
    pipeline.provider.reuse_responses = True  # a chapter interrupted mid-way reuses the answers it already has
    if not store.path("scenes.json").exists():
        pipeline.analyze(chunk_size=0)
    try:
        report = pipeline.validate(batch=True)
    except ValueError as exc:
        # A chapter draft that skipped verses: write it once more from scratch before giving up on it.
        if "omit source verses" not in str(exc):
            raise
        pipeline.provider.reuse_responses = False
        pipeline.analyze(chunk_size=0)
        pipeline.provider.reuse_responses = True
        report = pipeline.validate(batch=True)
    for _ in range(REPAIR_ROUNDS):
        rejected = [sid for sid, verdict in report.items() if verdict["status"] == "REJECT"]
        if not rejected:
            break
        for sid in rejected:
            try:
                pipeline.analyze(sid)
            except ValueError:
                pass  # a rewrite that changed its verse coverage is discarded; the scene stays rejected
        report = pipeline.validate(batch=True)
    calls, tokens = _usage(store)
    status = {"book": book, "chapter": chapter, "verses": last, "scenes": len(report),
              "counts": {s: sum(v["status"] == s for v in report.values()) for s in ("PASS", "PASS WITH WARNINGS", "REJECT")},
              "rejected": {sid: v["issues"] for sid, v in report.items() if v["status"] == "REJECT"},
              "calls": calls, "tokens": tokens}
    store.write("status.json", status)
    return status


def write_book(root="runs/book", source="data/full-scripture.txt", start=None, max_chapters=None, make_provider=None):
    """Write chapters in order from `start` ("Book chapter"), skipping finished ones. Stops at a usage limit."""
    if make_provider is None:
        for key, value in CODEX_DEFAULTS.items():
            os.environ.setdefault(key, value)
        from .codex import Codex
        make_provider = Codex
    todo = chapters(source)
    if start:
        names = [f"{book} {chapter}" for book, chapter, _ in todo]
        if start not in names:
            raise ValueError(f"Unknown chapter {start!r}; expected e.g. '1 Nephi 1'")
        todo = todo[names.index(start):]
    written, failures = [], 0
    for book, chapter, last in todo:
        if max_chapters is not None and len(written) >= max_chapters:
            break
        if (folder(root, book, chapter) / "status.json").exists():
            continue
        try:
            status = write_chapter(root, source, book, chapter, last, make_provider)
        except ProviderError as exc:
            if "usage limit" in str(exc).lower():
                print(f"Stopped at {book} {chapter}: {exc}", flush=True)
                return written, "usage limit"
            failures += 1
            print(f"{book} {chapter} failed: {exc}", flush=True)
            if failures >= 3:
                return written, "three provider failures in a row"
            continue
        failures = 0
        written.append(status)
        counts = status["counts"]
        print(f"{book} {chapter}: {status['scenes']} scenes | {counts['PASS']} pass, {counts['PASS WITH WARNINGS']} "
              f"warn, {counts['REJECT']} reject | {status['calls']} calls, "
              f"{status['tokens'].get('input_tokens', 0)} in / {status['tokens'].get('output_tokens', 0)} out tokens",
              flush=True)
    return written, "done"
