"""Draft reader: a local page for reading assembled chapters and flagging panels to re-render later.

It reads each chapter's last assembly (final/manifest.json and pages/), so the clickable panels match the pages
exactly, and writes only review/flags.json in that chapter. Point it at one run, or at a folder of runs (such as
runs/book) to page through every assembled chapter. Nothing here calls a model or changes an approval."""
import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .comic import PAGE_SIZE, _ref_range, frames
from .models import Panel
from .scripture import BOOKS
from .storage import Store

FLAGS = "review/flags.json"
PAGE = Path(__file__).parent / "reader.html"


def chapters(store):
    """{chapter id: Store} for every assembled run: the run itself (id ""), or each run under a folder of runs,
    in book order."""
    if store.path("final/manifest.json").exists():
        return {"": store}
    found = []
    for manifest in store.root.rglob("final/manifest.json"):
        run = manifest.parent.parent
        if "archive" in run.relative_to(store.root).parts:
            continue  # earlier assemblies kept by the store, not chapters
        status = run / "status.json"
        name = json.loads(status.read_text(encoding="utf-8")) if status.exists() else {}
        book = name.get("book")
        key = (BOOKS.index(book) if book in BOOKS else len(BOOKS), name.get("chapter", 0), run.as_posix())
        found.append((key, run.relative_to(store.root).as_posix(), run))
    return {chapter_id: Store(run) for _, chapter_id, run in sorted(found)}


def title(chapter_store, chapter_id):
    status = chapter_store.path("status.json")
    if status.exists():
        value = json.loads(status.read_text(encoding="utf-8"))
        return f"{value['book']} {value['chapter']}"
    return chapter_id or chapter_store.root.name


def flags(store):
    """Flagged panels: {panel_id: {note, image_path, image_hash, time}}."""
    path = store.path(FLAGS)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def draft(store):
    """The assembled draft's pages with each panel's frame, from the last assembly."""
    if not store.path("final/manifest.json").exists():
        raise ValueError("No assembled draft yet. Run assemble first.")
    manifest = store.read("final/manifest.json")
    panels = [Panel.model_validate(p) for p in manifest["panels"]]
    layout = frames(panels)
    pages = {}
    for panel in sorted(panels, key=lambda p: (p.page, p.panel_number)):
        pages.setdefault(panel.page, []).append({"id": panel.panel_id, "scene_id": panel.scene_id,
                                                 "refs": _ref_range(panel.refs), "frame": layout[panel.panel_id]})
    return {"run": store.root.name, "size": PAGE_SIZE, "images": manifest["images"],
            "pages": [{"number": n, "panels": group} for n, group in sorted(pages.items())]}


def flag(store, panel_id, note):
    """Flag a panel with a note, or clear its flag when the note is blank."""
    images = draft(store)["images"]
    if panel_id not in images:
        raise ValueError(f"{panel_id} is not in the assembled draft")
    current, note = flags(store), note.strip()
    if note:
        image = images[panel_id]
        current[panel_id] = {"note": note, "image_path": image["path"], "image_hash": image["image_hash"],
                             "time": datetime.now(timezone.utc).isoformat()}
    else:
        current.pop(panel_id, None)
    path = store.path(FLAGS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    store.event("flag" if note else "unflag", panel_id=panel_id, note=note)
    return current


def overview(store):
    """Every assembled chapter with its size and flags, for the chapter picker and the book-wide flag list."""
    from .redraw import redrawn  # redraw builds on this module
    out = []
    for chapter_id, chapter in chapters(store).items():
        manifest = chapter.read("final/manifest.json")
        panels = [Panel.model_validate(p) for p in manifest["panels"]]
        out.append({"id": chapter_id, "title": title(chapter, chapter_id), "pages": len({p.page for p in panels}),
                    "panels": {p.panel_id: _ref_range(p.refs) for p in panels}, "flags": flags(chapter),
                    "redrawn": redrawn(chapter)})
    return out


def serve(store, port=8765, open_browser=True):
    """Serve the reader on localhost until interrupted."""
    if not chapters(store):
        raise ValueError(f"No assembled draft under {store.root}. Run assemble (or the nightly job) first.")

    def chapter(query):
        # Rediscovered per request, so chapters rendered while the reader is open appear on the next load.
        chapter_id = query.get("chapter", [""])[0]
        found = chapters(store)
        if chapter_id not in found:
            raise ValueError(f"Unknown chapter {chapter_id!r}")
        return found[chapter_id]

    class Handler(BaseHTTPRequestHandler):
        def send(self, body, kind, status=200):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value, status=200):
            self.send(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/chapters":
                    self.send_json(overview(store))
                elif url.path == "/data":
                    from .redraw import redrawn
                    run = chapter(query)
                    self.send_json({**draft(run), "flags": flags(run), "redrawn": redrawn(run)})
                elif url.path.startswith("/page/") and url.path[6:].isdigit():
                    page = chapter(query).path(f"pages/page_{int(url.path[6:]):03d}.png")
                    if page.exists():
                        self.send(page.read_bytes(), "image/png")
                    else:
                        self.send_json({"error": "no such page"}, 404)
                else:
                    self.send_json({"error": "not found"}, 404)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 404)

        def do_POST(self):
            if self.path != "/flag":
                return self.send_json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                run = chapter({"chapter": [str(body.get("chapter", ""))]})
                self.send_json({"flags": flag(run, str(body["panel_id"]), str(body.get("note", "")))})
            except (ValueError, KeyError) as exc:
                self.send_json({"error": str(exc)}, 400)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Reading {store.root} at {url} (Ctrl+C to stop). Flags save to each chapter's {FLAGS}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
