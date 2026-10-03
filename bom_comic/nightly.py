"""The nightly job: keep Codex-written scenes ahead of rendering, then render chapters in book order until morning.

Everything resumes from files: a chapter is written once it has status.json, rendered once it has render.json, and
a half-rendered chapter keeps its finished panels (only panels without a current image are drawn). Approvals made
here are recorded as AUTOMATED first-draft approvals; review happens afterwards in the reader, where flagged panels
wait for a later re-render. Chapters whose scenes are still rejected after repair are skipped and reported.

Run: python -m bom_comic.nightly --until 07:45
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from . import book
from .errors import ProviderError
from .pipeline import Pipeline
from .storage import Store, digest

ROOT = Path("runs/book")
SOURCE = "data/full-scripture.txt"
LIBRARY = "portraits/book-of-mormon"
LEAD = 2  # written chapters kept ready beyond the one being rendered
COMFY_HOME = Path(os.getenv("COMFYUI_HOME", r"C:\ComfyUI\ComfyUI_windows_portable"))
SCENE_NOTE = ("AUTOMATED nightly approval: the Codex audit passed this scene{}. Review the draft in the reader and "
              "flag anything wrong.")
IMAGE_NOTE = "AUTOMATED nightly first-draft approval; plain Flux, no reference portraits. Review in the reader."


def log(message):
    print(f"{datetime.datetime.now():%H:%M:%S} {message}", flush=True)


def keep_awake(on):
    """Stop Windows sleeping mid-render (the process's request lapses when it exits)."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def comfy_url():
    return (os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")


def comfy_up():
    try:
        with urllib.request.urlopen(comfy_url() + "/system_stats", timeout=5):
            return True
    except OSError:
        return False


def start_comfy(log_path):
    """Start the portable ComfyUI server if it isn't running; returns the process we started, or None."""
    if comfy_up():
        return None
    python = COMFY_HOME / "python_embeded" / "python.exe"
    if not python.exists():
        raise SystemExit(f"ComfyUI not found at {COMFY_HOME}; set COMFYUI_HOME")
    handle = open(log_path, "a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([str(python), "-s", "ComfyUI/main.py", "--windows-standalone-build", "--listen", "127.0.0.1",
                                "--port", "8188", "--disable-auto-launch"], cwd=COMFY_HOME, stdout=handle,
                               stderr=subprocess.STDOUT, creationflags=flags)
    for _ in range(120):
        if comfy_up():
            log("ComfyUI is up")
            return process
        if process.poll() is not None:
            raise SystemExit(f"ComfyUI exited while starting; see {log_path}")
        time.sleep(5)
    process.kill()
    raise SystemExit("ComfyUI did not start within 10 minutes")


def state(book_name, chapter, new_only=False):
    """new_only: a chapter that was rendered stays "rendered" even if its scenes changed since (no re-render)."""
    run = book.folder(ROOT, book_name, chapter)
    if new_only and (run / "render.json").exists():
        return "rendered"
    if (run / "render.json").exists():
        manifest = run / "final" / "manifest.json"
        # A chapter whose scenes changed after assembly (e.g. repaired) renders again; drawings that still fit
        # are reused, so only changed panels are redrawn.
        if not manifest.exists():
            return "rendered"
        assembled = json.loads(manifest.read_text(encoding="utf-8"))
        if assembled["scene_stamp"] == Pipeline(Store(run), None).stamp():
            # A guide written (or rewritten) since the last assembly gives the chapter its cover page: assemble
            # again (only the cover is drawn; every panel is still current).
            guide = run / "intro.json"
            if guide.exists() and Pipeline(Store(run), None).intro() is not None and \
                    (assembled.get("cover") or {}).get("intro_stamp") != digest(Store(run).read("intro.json")):
                return "ready"
            return "rendered"
    if (run / "render-error.json").exists():
        return "failed"  # waits for a person; delete render-error.json to retry
    if not (run / "status.json").exists():
        return "unwritten"
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    return "blocked" if status["counts"]["REJECT"] else "ready"


def render_chapter(book_name, chapter, deadline, provider_for):
    """Approve (automated), plan, and render one chapter; returns True when it is fully rendered and assembled."""
    store = Store(book.folder(ROOT, book_name, chapter))
    pipeline = Pipeline(store, provider_for(store), library=LIBRARY)
    report = pipeline.validation()
    reviews = pipeline.approvals()
    for scene in pipeline.scenes():
        verdict = report["results"][scene.scene_id]
        # A repair re-audit changes the report, which retires earlier approvals.
        if reviews.get("scene:" + scene.scene_id, {}).get("stamp") != digest({"report": report, "scene": scene.scene_id}):
            warned = " with warnings: " + "; ".join(verdict["issues"]) if verdict["status"] == "PASS WITH WARNINGS" else ""
            pipeline.review("scene", scene.scene_id, "approve", SCENE_NOTE.format(warned))
    if not store.path("panels.json").exists() or store.read("panels.json")["scene_stamp"] != pipeline.stamp():
        pipeline.plan()
    if not store.path("portraits/index.json").exists():
        # Adopted now so a later reference-portrait re-render of a flagged panel doesn't restale every draft.
        pipeline.adopt_portraits(LIBRARY)
    aspects = pipeline.aspects()
    panels = pipeline.panels()
    pipeline.reapprove_panels("AUTOMATED nightly composition approval.")
    for panel in panels:
        try:
            pipeline.image_record(panel, aspects)
            continue  # already drawn
        except (ValueError, FileNotFoundError):
            pass
        if datetime.datetime.now() >= deadline:
            return False
        started = time.monotonic()
        pipeline.generate(panel.panel_id, references=False)
        log(f"  {book_name} {chapter} {panel.panel_id} drawn in {time.monotonic() - started:.0f}s")
    for panel in panels:
        pipeline.review("image", panel.panel_id, "approve", IMAGE_NOTE)
    if pipeline.intro() is not None:
        # The cover page: the chapter's guide (opener card) and its cover art, both first drafts for the reader.
        reviews = pipeline.approvals()
        if reviews.get("intro:chapter", {}).get("stamp") != digest(store.read("intro.json")):
            pipeline.review("intro", "chapter", "approve", "AUTOMATED nightly approval: the chapter guide passed its "
                            "audit. Read it on the cover page.")
        try:
            pipeline.cover_record()
        except (ValueError, FileNotFoundError):
            if datetime.datetime.now() >= deadline:
                return False
            started = time.monotonic()
            pipeline.cover()
            log(f"  {book_name} {chapter} cover drawn in {time.monotonic() - started:.0f}s")
        pipeline.review("cover", "cover", "approve", IMAGE_NOTE)
    pdf = pipeline.assemble()
    store.write("render.json", {"pdf": str(pdf), "panels": len(panels), "time": datetime.datetime.now().isoformat()})
    return True


def run(until, write=True, render=True, new_only=False):
    """until: the local time to stop starting new panels, or None to run until stopped (or the book is drawn).
    new_only: draw only chapters never rendered, skipping flag redraws and re-renders of repaired chapters
    (useful while repairs are still landing, so nothing is drawn twice)."""
    from .config import Config
    Config.load()
    for key, value in book.CODEX_DEFAULTS.items():
        os.environ.setdefault(key, value)
    now = datetime.datetime.now()
    if until is None:
        deadline = datetime.datetime.max
    else:
        deadline = datetime.datetime.combine(now.date(), until)
        if deadline <= now:
            deadline += datetime.timedelta(days=1)
    night = ROOT / "nightly"
    report = {"started": now.isoformat(), "deadline": deadline.isoformat(), "written": [], "repaired": [],
              "rendered": [], "blocked": [], "stopped": None}
    comfy = None
    # One nightly run at a time: a run started by hand keeps the midnight run from doubling up.
    if book.held(night / "nightly.lock"):
        raise SystemExit(f"Another nightly run holds {night / 'nightly.lock'}; exiting.")
    with book.Lock(night / "nightly.lock"):
        keep_awake(True)
        try:
            chapters = book.chapters(SOURCE)
            try:
                for name, chapter, newly in book.recheck(ROOT):  # rules added since writing; no model calls
                    log(f"Rule re-check rejected {len(newly)} scenes in {name} {chapter}; queued for repair")
            except book.Busy:
                pass  # a writer is working through the book; its repair pass re-checks too
            codex_ok = write
            repaired_tonight = set()

            def make_codex(store):
                from .codex import Codex
                return Codex(store)

            def top_up():
                """Write ahead so LEAD chapters beyond the next render are ready; repair blocked ones once a night."""
                nonlocal codex_ok
                if not codex_ok:
                    return
                if book.held(ROOT / "writing.lock"):
                    return  # a writer started by hand is working through the book; just render tonight
                for name, chapter, _ in chapters:
                    if state(name, chapter) == "blocked" and (name, chapter) not in repaired_tonight:
                        repaired_tonight.add((name, chapter))
                        try:
                            with book.writing_lock(ROOT):
                                status = book.repair_chapter(ROOT, name, chapter, make_codex)
                        except book.Busy:
                            return
                        except ProviderError as exc:
                            codex_ok = "usage limit" not in str(exc).lower()
                            log(f"Repair of {name} {chapter} stopped: {exc}")
                            return
                        report["repaired"].append(f"{name} {chapter}: {status['counts']['REJECT']} still rejected")
                        log(f"Repaired {name} {chapter}: {status['counts']}")
                        break  # one repair per top-up keeps writing and rendering moving
                ready = sum(state(n, c, new_only) == "ready" for n, c, _ in chapters)
                if ready > LEAD:
                    return
                written, reason = book.write_book(ROOT, SOURCE, max_chapters=LEAD + 1 - ready, make_provider=make_codex)
                report["written"] += [f"{s['book']} {s['chapter']}" for s in written]
                if reason != "done":
                    codex_ok = False
                    log(f"Codex writing paused tonight: {reason}")

            def comfy_provider(store):
                from .providers import ComfyUI
                return ComfyUI(store)

            failed_redraws = set()

            def redraw_flags():
                """Panels the reviewer flagged come before any new panels. Returns False if the deadline hit."""
                from . import redraw
                nonlocal comfy
                for store, panel_id, flag in redraw.pending(ROOT):
                    if (store.root, panel_id) in failed_redraws:
                        continue  # one attempt per run; it waits in the report for a person
                    if datetime.datetime.now() >= deadline:
                        return False
                    if comfy is None and not comfy_up():
                        comfy = start_comfy(night / "comfyui.log")
                    text = None
                    try:
                        text = make_codex(store)
                        text.reuse_responses = True
                    except ProviderError:
                        pass  # no Codex: the note is split into instructions without a model
                    try:
                        try:
                            entry = redraw.redraw(store, panel_id, flag, comfy_provider(store), text, LIBRARY)
                        except ProviderError as exc:
                            if text is None or "ComfyUI" in str(exc):
                                raise
                            entry = redraw.redraw(store, panel_id, flag, comfy_provider(store), None, LIBRARY)
                    except (ValueError, ProviderError) as exc:
                        failed_redraws.add((store.root, panel_id))
                        log(f"Redraw of {store.root.name} {panel_id} failed: {exc}")
                        report["blocked"].append(f"redraw {store.root} {panel_id}: {exc}")
                        continue
                    report.setdefault("redrawn", []).append(f"{store.root.relative_to(ROOT.resolve()).as_posix()} {panel_id}")
                    log(f"Redrew {store.root.relative_to(ROOT.resolve()).as_posix()} {panel_id} for flag: {entry['note']}")
                return True

            while datetime.datetime.now() < deadline:
                top_up()
                if not render:
                    break
                if not new_only and not redraw_flags():
                    break
                todo = [(n, c) for n, c, _ in chapters if state(n, c, new_only) == "ready"]
                if not todo:
                    report["stopped"] = "nothing ready to render"
                    break
                if comfy is None and not comfy_up():
                    comfy = start_comfy(night / "comfyui.log")
                name, chapter = todo[0]
                if codex_ok:
                    # The chapter's guide (cover moment, opener card, dreams and visions) before its pages; skipped
                    # when current, and a rejected one gets a single escalated retry (book.write_intros).
                    try:
                        _, reason = book.write_intros(ROOT, make_codex, only={(name, chapter)})
                        if reason == "usage limit":
                            codex_ok = False
                    except ProviderError as exc:
                        log(f"Guide for {name} {chapter} not written: {exc}")
                log(f"Rendering {name} {chapter}")
                try:
                    finished = render_chapter(name, chapter, deadline, comfy_provider)
                except (ValueError, ProviderError) as exc:
                    # Leave this chapter for a human and keep the night moving.
                    log(f"{name} {chapter} could not render: {exc}")
                    report["blocked"].append(f"{name} {chapter}: {exc}")
                    Store(book.folder(ROOT, name, chapter)).write("render-error.json", {"error": str(exc)})
                    continue
                if finished:
                    report["rendered"].append(f"{name} {chapter}")
                    log(f"Finished {name} {chapter}")
            report["stopped"] = report["stopped"] or "morning deadline"
        finally:
            keep_awake(False)
            if comfy is not None:
                comfy.terminate()
            report["blocked"] += [f"{n} {c}: scenes still rejected" for n, c, _ in chapters if state(n, c) == "blocked"]
            report["finished"] = datetime.datetime.now().isoformat()
            night.mkdir(parents=True, exist_ok=True)
            (night / f"{now:%Y-%m-%d}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            log(f"Night summary: {len(report['written'])} written, {len(report['rendered'])} rendered, "
                f"{len(report['blocked'])} blocked; stopped: {report['stopped']}")
    return report


def main():
    parser = argparse.ArgumentParser(description="Nightly write-ahead and render job")
    parser.add_argument("--until", default="07:45",
                        help="Stop starting new panels at this local time (HH:MM), or 'never' to run until stopped")
    parser.add_argument("--no-write", action="store_true", help="Render only; don't call Codex")
    parser.add_argument("--no-render", action="store_true", help="Write/repair scenes only")
    parser.add_argument("--new-only", action="store_true",
                        help="Draw only never-rendered chapters: no flag redraws, no re-renders of repaired chapters")
    args = parser.parse_args()
    until = None if args.until.lower() == "never" else datetime.datetime.strptime(args.until, "%H:%M").time()
    run(until, write=not args.no_write, render=not args.no_render, new_only=args.new_only)


if __name__ == "__main__":
    main()
