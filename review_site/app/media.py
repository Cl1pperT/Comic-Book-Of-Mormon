"""Page images, stored once per source-image hash. Unchanged pages are shared between chapter versions."""
import re
from pathlib import Path

HASH = re.compile(r"^[0-9a-f]{64}$")
MAX_BYTES = 8 * 1024 * 1024


class MediaStore:
    def __init__(self, root):
        self.root = Path(root) / "pages"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, image_hash):
        if not HASH.match(image_hash):
            raise ValueError("Bad image hash")
        return self.root / f"{image_hash}.webp"

    def has(self, image_hash):
        return self.path(image_hash).exists()

    def missing(self, hashes):
        return sorted({h for h in hashes if not self.has(h)})

    def save(self, image_hash, data):
        if len(data) > MAX_BYTES:
            raise ValueError("Image too large")
        if not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
            raise ValueError("Pages must be WebP images")
        path = self.path(image_hash)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(path)
