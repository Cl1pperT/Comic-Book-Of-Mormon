"""Draft reader: a local page for reading an assembled chapter and flagging panels to re-render later.

It reads the last assembly (final/manifest.json and pages/), so the clickable panels match the pages exactly,
and writes only review/flags.json. Nothing here calls a model or changes an approval."""
import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .comic import PAGE_SIZE, _ref_range, frames
from .models import Panel

FLAGS = "review/flags.json"
PAGE = Path(__file__).parent / "reader.html"


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


def serve(store, port=8765, open_browser=True):
    """Serve the reader on localhost until interrupted."""
    draft(store)

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
            if self.path == "/":
                self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/data":
                self.send_json({**draft(store), "flags": flags(store)})
            elif self.path.startswith("/page/") and self.path[6:].isdigit():
                page = store.path(f"pages/page_{int(self.path[6:]):03d}.png")
                if page.exists():
                    self.send(page.read_bytes(), "image/png")
                else:
                    self.send_json({"error": "no such page"}, 404)
            else:
                self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/flag":
                return self.send_json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_json({"flags": flag(store, str(body["panel_id"]), str(body.get("note", "")))})
            except (ValueError, KeyError) as exc:
                self.send_json({"error": str(exc)}, 400)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Reading {store.root} at {url} (Ctrl+C to stop). Flags save to {store.path(FLAGS)}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
