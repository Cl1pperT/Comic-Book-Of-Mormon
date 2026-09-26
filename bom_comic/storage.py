import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
    def path(self, name):
        path = (self.root / name).resolve()
        if self.root not in path.parents:
            raise ValueError("Artifact path must stay inside run directory")
        return path
    def read(self, name):
        return json.loads(self.path(name).read_text(encoding="utf-8"))
    def write(self, name, value):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            archive = self.path(f"archive/{uuid4().hex}/{name}")
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(path.read_bytes())
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    def event(self, action, **details):
        with self.path("history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(), action=action, **details)) + "\n")
