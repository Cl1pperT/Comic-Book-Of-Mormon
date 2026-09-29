"""Write the book's scenes chapter by chapter, ahead of rendering: one run folder per chapter.

Each chapter is written in one call, audited in one call, and its rejected scenes are rewritten (told why) and
re-audited up to REPAIR_ROUNDS times. Scenes are never approved here; that stays a review step. A finished chapter
leaves status.json in its folder and is skipped on the next run, so a run stopped by a usage limit simply resumes.
"""
import json
import os
from pathlib import Path
from .analysis import unquote
from .errors import ProviderError
from .pipeline import Pipeline
from .scripture import load
from .storage import Store

REPAIR_ROUNDS = 2
LIBRARY = Path("portraits/book-of-mormon")
LOCATIONS = Path("runs/1-nephi-1-4/continuity/locations.json")
# Measured on 1 Nephi 4: the small model writes well at low effort in one call per chapter; auditing needs medium,
# where low effort flip-flopped between false rejects and passes.
# Rewrites of rejected scenes go to the mid-size model: they're the hard cases, and few enough to afford it.
CODEX_DEFAULTS = {"CODEX_TEXT_MODEL": "gpt-6-luna", "CODEX_TEXT_EFFORT": "low",
                  "CODEX_VALIDATOR_MODEL": "gpt-6-luna", "CODEX_VALIDATOR_EFFORT": "medium",
                  "CODEX_REPAIR_MODEL": "gpt-6-sol", "CODEX_REPAIR_EFFORT": "medium"}


class Busy(Exception):
    """Another process holds the lock."""


class Lock:
    """A pid lock file: one writer (or one nightly run) at a time; a lock left by a dead process is taken over."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if held(self.path):
            raise Busy(f"{self.path} is held by pid {self.path.read_text()}")
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def held(path):
    path = Path(path)
    if not path.exists():
        return False
    pid = int(path.read_text() or 0)
    if not pid or pid == os.getpid():
        return False
    if os.name == "nt":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def writing_lock(root):
    return Lock(Path(root) / "writing.lock")


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
    return _finish(store, pipeline, book, chapter, last, _repair(pipeline, report))


def _rejected(report):
    return sorted(sid for sid, verdict in report.items() if verdict["status"] == "REJECT")


def _repair(pipeline, report):
    """Rewrite rejected scenes (each told why) and re-audit, until clean, out of rounds, or a round changes nothing."""
    for _ in range(REPAIR_ROUNDS):
        rejected = _rejected(report)
        if not rejected:
            break
        for sid in rejected:
            try:
                pipeline.analyze(sid)
            except ValueError:
                pass  # a rewrite that changed its verse coverage is discarded; the scene stays rejected
        report = pipeline.validate(batch=True)
        if _rejected(report) == rejected:
            break  # the same scenes failed again: more rounds would only spend usage
    return report


def _finish(store, pipeline, book, chapter, last, report):
    calls, tokens = _usage(store)
    status = {"book": book, "chapter": chapter, "verses": last, "scenes": len(report),
              "counts": {s: sum(v["status"] == s for v in report.values()) for s in ("PASS", "PASS WITH WARNINGS", "REJECT")},
              "rejected": {sid: v["issues"] for sid, v in report.items() if v["status"] == "REJECT"},
              "calls": calls, "tokens": tokens}
    store.write("status.json", status)
    return status


def repair_chapter(root, book, chapter, make_provider):
    """Re-clean, re-audit, and repair a finished chapter that still has rejected scenes. Callers hold the writing
    lock (repair_blocked and the nightly job do)."""
    store = Store(folder(root, book, chapter))
    status = store.read("status.json")
    pipeline = Pipeline(store, make_provider(store))
    pipeline.provider.reuse_responses = True
    scenes = pipeline.scenes()
    cleaned = [unquote(scene.model_copy(deep=True)) for scene in scenes]
    if cleaned != scenes:
        store.write("scenes.json", [s.model_dump() for s in cleaned])
    report = _repair(pipeline, pipeline.validate(batch=True))
    return _finish(store, pipeline, book, chapter, status["verses"], report)


def _codex():
    for key, value in CODEX_DEFAULTS.items():
        os.environ.setdefault(key, value)
    from .codex import Codex
    return Codex


def repair_blocked(root="runs/book", make_provider=None):
    """Repair every written chapter that still has rejected scenes, in book order. Stops at a usage limit."""
    make_provider = make_provider or _codex()
    results = []
    try:
        with writing_lock(root):
            for status_path in sorted(Path(root).glob("*/*/status.json")):
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if not status["counts"]["REJECT"]:
                    continue
                try:
                    fixed = repair_chapter(root, status["book"], status["chapter"], make_provider)
                except ProviderError as exc:
                    if "usage limit" in str(exc).lower():
                        return results, "usage limit"
                    print(f"{status['book']} {status['chapter']} repair failed: {exc}", flush=True)
                    continue
                results.append(fixed)
                print(f"Repaired {fixed['book']} {fixed['chapter']}: {status['counts']['REJECT']} -> "
                      f"{fixed['counts']['REJECT']} rejected", flush=True)
    except Busy as exc:
        return results, f"another writer is running ({exc})"
    return results, "done"


def write_book(root="runs/book", source="data/full-scripture.txt", start=None, max_chapters=None, make_provider=None):
    """Write chapters in order from `start` ("Book chapter"), skipping finished ones. Stops at a usage limit.
    Holds the book's writing lock, so a second writer (e.g. the nightly job) never works on the same chapter."""
    make_provider = make_provider or _codex()
    todo = chapters(source)
    if start:
        names = [f"{book} {chapter}" for book, chapter, _ in todo]
        if start not in names:
            raise ValueError(f"Unknown chapter {start!r}; expected e.g. '1 Nephi 1'")
        todo = todo[names.index(start):]
    try:
        with writing_lock(root):
            return _write(root, source, todo, max_chapters, make_provider)
    except Busy as exc:
        return [], f"another writer is running ({exc})"


def _write(root, source, todo, max_chapters, make_provider):
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
