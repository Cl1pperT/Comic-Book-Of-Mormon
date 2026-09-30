"""Write the book's scenes chapter by chapter, ahead of rendering: one run folder per chapter.

Each chapter is written in one call, audited in one call, and its rejected scenes are rewritten (told why) and
re-audited up to REPAIR_ROUNDS times. Scenes are never approved here; that stays a review step. A finished chapter
leaves status.json in its folder and is skipped on the next run, so a run stopped by a usage limit simply resumes.
"""
import json
import os
from pathlib import Path
from .analysis import deterministic_issues, unquote
from .errors import ProviderError
from .pipeline import Pipeline
from .scripture import load
from .storage import Store

REPAIR_ROUNDS = 2
LIBRARY = Path("portraits/book-of-mormon")
LOCATIONS = LIBRARY / "locations.json"
# Measured on 1 Nephi 4: the small model writes well at low effort in one call per chapter; auditing needs medium,
# where low effort flip-flopped between false rejects and passes.
# Rewrites of rejected scenes go to the mid-size model: they're the hard cases, and few enough to afford it.
# Scenes that survive those rounds get one escalation round: the frontier model rewrites them and the mid-size model
# re-audits (the small auditor's inconsistency was part of what kept them rejected).
CODEX_DEFAULTS = {"CODEX_TEXT_MODEL": "gpt-6-luna", "CODEX_TEXT_EFFORT": "low",
                  "CODEX_VALIDATOR_MODEL": "gpt-6-luna", "CODEX_VALIDATOR_EFFORT": "medium",
                  "CODEX_REPAIR_MODEL": "gpt-6-sol", "CODEX_REPAIR_EFFORT": "medium",
                  "CODEX_ESCALATE_MODEL": "gpt-6-astra", "CODEX_ESCALATE_EFFORT": "medium",
                  "CODEX_ESCALATE_VALIDATOR_MODEL": "gpt-6-sol", "CODEX_ESCALATE_VALIDATOR_EFFORT": "medium"}


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
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True).stdout
        # Windows reuses process IDs: a lock left by a killed run can name an unrelated process, so only a live
        # Python process counts as holding it.
        return any(line.lower().startswith('"python') and f'"{pid}"' in line for line in out.splitlines())
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
    return _finish(store, pipeline, book, chapter, last, _escalate(pipeline, _repair(pipeline, report)))


def _rejected(report):
    return sorted(sid for sid, verdict in report.items() if verdict["status"] == "REJECT")


def _repair(pipeline, report):
    """Rewrite rejected scenes (each told why) and re-audit just those, until clean, out of rounds, or a round
    changes nothing. Scenes that already passed keep their verdicts, so a re-check can't flip them."""
    for _ in range(REPAIR_ROUNDS):
        rejected = _rejected(report)
        if not rejected:
            break
        for sid in rejected:
            try:
                pipeline.analyze(sid)
            except ValueError:
                pass  # a rewrite that changed its verse coverage is discarded; the scene stays rejected
        report = pipeline.revalidate(rejected)
        if _rejected(report) == rejected:
            break  # the same scenes failed again: more rounds would only spend usage
    return report


def _escalate(pipeline, report):
    """One last round for scenes the regular repairs couldn't fix: stronger models for the rewrite and the re-audit.
    Providers without model settings (tests, other services) just get one more ordinary round."""
    if not _rejected(report):
        return report
    provider = pipeline.provider
    models, effort = getattr(provider, "models", None), getattr(provider, "effort", None)
    saved = (dict(models), dict(effort)) if models is not None and effort is not None else None
    if saved:
        models.update(repair=os.getenv("CODEX_ESCALATE_MODEL", models["repair"]),
                      validator=os.getenv("CODEX_ESCALATE_VALIDATOR_MODEL", models["validator"]))
        effort.update(repair=os.getenv("CODEX_ESCALATE_EFFORT", effort["repair"]),
                      validator=os.getenv("CODEX_ESCALATE_VALIDATOR_EFFORT", effort["validator"]))
    try:
        rejected = _rejected(report)
        for sid in rejected:
            try:
                pipeline.analyze(sid)
            except ValueError:
                pass
        # Only the rewritten scenes face the stricter auditor; re-auditing the whole chapter rejected scenes the
        # regular auditor had passed (Mosiah 7 went from 2 rejected to 6).
        return pipeline.revalidate(rejected)
    finally:
        if saved:
            models.clear(), models.update(saved[0])
            effort.clear(), effort.update(saved[1])


def _finish(store, pipeline, book, chapter, last, report):
    calls, tokens = _usage(store)
    status = {"book": book, "chapter": chapter, "verses": last, "scenes": len(report),
              "counts": {s: sum(v["status"] == s for v in report.values()) for s in ("PASS", "PASS WITH WARNINGS", "REJECT")},
              "rejected": {sid: v["issues"] for sid, v in report.items() if v["status"] == "REJECT"},
              "calls": calls, "tokens": tokens}
    store.write("status.json", status)
    return status


def _upgrade(pipeline):
    """Art-stamp a drawn chapter's existing drawings (a no-op for undrawn chapters or already-upgraded records)."""
    try:
        pipeline.upgrade_image_records()
    except (ValueError, FileNotFoundError):
        pass  # no current plan or approvals to compare against: nothing to keep


def repair_chapter(root, book, chapter, make_provider):
    """Re-clean, re-audit, and repair a finished chapter that still has rejected scenes. Callers hold the writing
    lock (repair_blocked and the nightly job do)."""
    store = Store(folder(root, book, chapter))
    status = store.read("status.json")
    pipeline = Pipeline(store, make_provider(store))
    pipeline.provider.reuse_responses = True
    _upgrade(pipeline)  # before the scenes change, while old drawings still pass the old rule
    scenes = pipeline.scenes()
    cleaned = [unquote(scene.model_copy(deep=True)) for scene in scenes]
    if cleaned != scenes:
        store.write("scenes.json", [s.model_dump() for s in cleaned])
    report = _escalate(pipeline, _repair(pipeline, pipeline.validate(batch=True)))
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
        recheck(root)  # rules added since a chapter was written count too
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


def recheck(root="runs/book"):
    """Re-apply the automatic rules (e.g. a new minimum-lettering rule) to written chapters, marking scenes that now
    fail as rejected so the next repair pass rewrites them. No model calls. Drawn chapters are included: their
    drawings get art stamps first, so after the repair only panels whose drawing really changed are redrawn."""
    changed = []
    with writing_lock(root):
        for status_path in sorted(Path(root).glob("*/*/status.json")):
            run = status_path.parent
            store = Store(run)
            status = store.read("status.json")
            pipeline = Pipeline(store, None)
            _upgrade(pipeline)
            try:
                validation = pipeline.validation()
            except ValueError:
                # Scenes changed after their last audit (e.g. a repair cut off by a usage limit): block the chapter
                # so the repair pass re-audits it, rather than letting the renderer trip over it.
                if "(audit)" not in status["rejected"]:
                    status["rejected"]["(audit)"] = ["Scenes changed after their last audit; re-audit needed"]
                    status["counts"]["REJECT"] += 1
                    store.write("status.json", status)
                    changed.append((status["book"], status["chapter"], ["(audit)"]))
                continue
            results, verses, newly = dict(validation["results"]), pipeline.verses(), []
            for scene in pipeline.scenes():
                issues = deterministic_issues(scene, verses)
                if issues and results[scene.scene_id]["status"] != "REJECT":
                    results[scene.scene_id] = {"status": "REJECT", "issues": issues}
                    newly.append(scene.scene_id)
            if newly:
                store.write("validation.json", {**validation, "results": results})
                store.event("recheck", rejected=newly)
                _finish(store, pipeline, status["book"], status["chapter"], status["verses"], results)
                changed.append((status["book"], status["chapter"], newly))
    return changed


def relabel(root="runs/book", aliases=None, make_provider=None):
    """Rename people's labels in written scenes (visible people and speakers) to known identities, e.g.
    "Nephi’s wife" -> "Nephi's wife". aliases(book, chapter) returns {old label: new label} for a chapter. New
    library records are copied into every chapter's continuity first, then changed scenes are re-audited (a rejected
    one goes through the normal repair). Returns [(book, chapter, changed scene ids)]."""
    make_provider = make_provider or _codex()
    library = json.loads((LIBRARY / "characters.json").read_text(encoding="utf-8"))
    out = []
    with writing_lock(root):
        for status_path in sorted(Path(root).glob("*/*/status.json")):
            store = Store(status_path.parent)
            status = store.read("status.json")
            pipeline = Pipeline(store, None)
            _upgrade(pipeline)  # before continuity or scenes change
            characters = store.read("continuity/characters.json")
            missing = {name: record for name, record in library.items() if name not in characters}
            if missing:
                store.write("continuity/characters.json", {**characters, **missing})
            mapping = aliases(status["book"], status["chapter"])
            scenes, changed = pipeline.scenes(), set()
            for scene in scenes:
                people = list(dict.fromkeys(mapping.get(c, c) for c in scene.characters))
                speakers = [mapping.get(q.speaker, q.speaker) for q in scene.spoken_dialogue]
                if people != scene.characters or speakers != [q.speaker for q in scene.spoken_dialogue]:
                    scene.characters = people
                    for q, name in zip(scene.spoken_dialogue, speakers):
                        q.speaker = name
                    changed.add(scene.scene_id)
            if not changed:
                continue
            store.write("scenes.json", [s.model_dump() for s in scenes])
            pipeline.provider = make_provider(store)
            pipeline.provider.reuse_responses = True
            try:
                report = _escalate(pipeline, _repair(pipeline, pipeline.revalidate(sorted(changed))))
            except ProviderError as exc:
                print(f"{status['book']} {status['chapter']}: relabelled, but the re-audit stopped: {exc}", flush=True)
                # Left blocked so the next repair pass re-audits it.
                status["rejected"]["(audit)"] = ["Scenes changed after their last audit; re-audit needed"]
                status["counts"]["REJECT"] += 1
                store.write("status.json", status)
                out.append((status["book"], status["chapter"], sorted(changed)))
                continue
            _finish(store, pipeline, status["book"], status["chapter"], status["verses"], report)
            out.append((status["book"], status["chapter"], sorted(changed)))
            print(f"Relabelled {status['book']} {status['chapter']}: {len(changed)} scenes", flush=True)
    return out


SPEAKER_PROMPT = """Some speech lines in this chapter's comic scenes have a speaker label that names no one. For each
listed line, name the speaker when the chapter's verses or its first-person narrator make it clear (for example the
narrator's "I", or a person the nearby verses name as speaking); use the known character labels exactly when one
applies. When the verses genuinely don't say who speaks, return an empty speaker. Never guess.
"""


def fix_speakers(root="runs/book", make_provider=None):
    """Name the speakers of speech lines labelled "Unidentified speaker" and the like, one call per affected
    chapter, then re-audit just the changed scenes (a rejected one goes through the normal repair). Drawn chapters
    re-render afterwards, redrawing only panels whose visible people changed."""
    from .analysis import narrator, vague_speaker
    from .models import SpeakerFixes
    make_provider = make_provider or _codex()
    results = []
    try:
        with writing_lock(root):
            for status_path in sorted(Path(root).glob("*/*/status.json")):
                store = Store(status_path.parent)
                status = store.read("status.json")
                pipeline = Pipeline(store, make_provider(store))
                pipeline.provider.reuse_responses = True
                scenes = pipeline.scenes()
                lines = [{"scene_id": s.scene_id, "line": i, "label": q.speaker, "text": q.text, "refs": q.refs}
                         for s in scenes for i, q in enumerate(s.spoken_dialogue) if vague_speaker(q.speaker)]
                if not lines:
                    continue
                _upgrade(pipeline)  # drawn chapters keep drawings whose content didn't change
                request = {"narrator": narrator(status["book"], status["chapter"]),
                           "known_character_labels": list(pipeline.continuity()["characters"]),
                           "lines": lines, "chapter": [v.model_dump() for v in pipeline.verses()]}
                try:
                    answer = pipeline.provider.structured(SPEAKER_PROMPT + json.dumps(request), SpeakerFixes,
                                                          "speakers", "repair")
                except ProviderError as exc:
                    if "usage limit" in str(exc).lower():
                        return results, "usage limit"
                    print(f"{status['book']} {status['chapter']} speakers failed: {exc}", flush=True)
                    continue
                wanted = {(line["scene_id"], line["line"]) for line in lines}
                by_id, changed = {s.scene_id: s for s in scenes}, set()
                for fix in answer.fixes:
                    name = fix.speaker.strip()
                    scene = by_id.get(fix.scene_id)
                    if not name or vague_speaker(name) or scene is None or (fix.scene_id, fix.line) not in wanted:
                        continue
                    speech = scene.spoken_dialogue[fix.line]
                    old, speech.speaker = speech.speaker, name
                    # The same vague label among the visible people is the same person.
                    scene.characters = list(dict.fromkeys(name if c == old else c for c in scene.characters))
                    changed.add(fix.scene_id)
                if not changed:
                    continue
                store.write("scenes.json", [s.model_dump() for s in scenes])
                report = _escalate(pipeline, _repair(pipeline, pipeline.revalidate(sorted(changed))))
                fixed = _finish(store, pipeline, status["book"], status["chapter"], status["verses"], report)
                results.append(fixed)
                print(f"Speakers named in {status['book']} {status['chapter']}: {len(changed)} scenes "
                      f"({len(lines)} vague lines) | {fixed['counts']['REJECT']} rejected", flush=True)
    except Busy as exc:
        return results, f"another writer is running ({exc})"
    return results, "done"


def compile_pdf(root="runs/book", out=None):
    """Stitch every assembled chapter's pages into one PDF, in book order; returns (path, chapters, pages).
    Uses each chapter's last assembly, so a chapter waiting to be re-rendered shows its previous pages."""
    from PIL import Image
    from .reader import chapters as assembled, title
    out = Path(out or Path(root) / "comic-progress.pdf")
    pages, names = [], []
    for chapter_id, store in assembled(Store(root)).items():
        manifest = store.read("final/manifest.json")
        count = max(panel["page"] for panel in manifest["panels"])
        # Only the pages this assembly made (an earlier, longer assembly may have left extra page files).
        files = [store.path(f"pages/page_{n:03d}.png") for n in range(1, count + 1)]
        if all(f.exists() for f in files):
            pages += files
            names.append(title(store, chapter_id))
    if not pages:
        raise ValueError(f"No assembled chapters under {root}")
    images = (Image.open(p).convert("RGB") for p in pages)
    first = next(images)
    first.save(out, save_all=True, append_images=images, resolution=150, quality=88)
    return out, names, len(pages)


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
