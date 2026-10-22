"""Publish the assembled chapters in runs/book to the review site. Safe to run again: unchanged chapters are skipped,
changed ones arrive as a *pending* version that you approve on the site (Admin > Chapters).

    python -m review_site.tools.publish --server https://your-app.fly.dev --token TOKEN
    python -m review_site.tools.publish --local                      # into the local dev database
    python -m review_site.tools.publish --only 1-nephi/001 --limit 3 --dry-run

The server address and token can also come from REVIEW_SITE_URL and REVIEW_TOOL_TOKEN.
This is the only part of the review site that reads the comic pipeline's files."""
import argparse
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from bom_comic.reader import chapters as find_chapters, draft
from bom_comic.scripture import BOOKS
from bom_comic.storage import Store

from ..app.config import HERE
from ..app.services.versions import ChapterPayload

BUILD = HERE / "build" / "pages"
WEBP_QUALITY = 90


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def to_webp(png, image_hash):
    """The page as a WebP at full size (cached by the PNG's hash), returning its path."""
    out = BUILD / f"{image_hash}.webp"
    if not out.exists():
        BUILD.mkdir(parents=True, exist_ok=True)
        with Image.open(png) as image:
            image.convert("RGB").save(out.with_suffix(".tmp"), "WEBP", quality=WEBP_QUALITY, method=4)
        out.with_suffix(".tmp").replace(out)
    return out


def verses_of(store):
    """{"1 Nephi 1:1": text} for the chapter, from its source snapshot."""
    return {f"{v['book']} {v['chapter']}:{v['verse']}": v["text"] for v in store.read("source.json")}


def build_chapter(slug, store):
    """(ChapterPayload, {image_hash: png path}) for one assembled chapter."""
    status = store.read("status.json")
    manifest = store.read("final/manifest.json")
    refs = {p["panel_id"]: p["refs"] for p in manifest["panels"]}
    if manifest.get("cover"):
        refs["cover"] = manifest["cover"]["intro"]["cover"]["refs"]
    shown = draft(store)
    width, height = shown["size"]
    pages, sources = [], {}
    for page in shown["pages"]:
        png = store.path(f"pages/page_{page['number']:03d}.png")
        digest = sha256_file(png)
        sources[digest] = png
        with Image.open(png) as image:
            w, h = image.size
        pages.append({"number": page["number"], "label": page.get("label", ""), "image_hash": digest,
                      "width": w, "height": h,
                      "panels": [{"key": p["id"], "scene_id": p["scene_id"] if p["id"] != "cover" else "",
                                  "refs_label": p["refs"], "refs": refs.get(p["id"], []),
                                  "frame": [p["frame"][0] / width, p["frame"][1] / height,
                                            p["frame"][2] / width, p["frame"][3] / height]}
                                 for p in page["panels"]]})
    payload = ChapterPayload(
        slug=slug, book=status["book"], number=status["chapter"],
        sort_order=BOOKS.index(status["book"]) * 1000 + status["chapter"], verses=verses_of(store), pages=pages)
    return payload, sources


class HttpSink:
    def __init__(self, server, token):
        import httpx
        self.client = httpx.Client(base_url=server.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=120)

    def missing(self, hashes):
        response = self.client.post("/api/tool/plan", json={"hashes": sorted(hashes)})
        response.raise_for_status()
        return response.json()["missing"]

    def upload(self, image_hash, path):
        response = self.client.put(f"/api/tool/image/{image_hash}", content=path.read_bytes())
        response.raise_for_status()

    def publish(self, payload):
        response = self.client.post("/api/tool/chapter", content=payload.model_dump_json(),
                                    headers={"Content-Type": "application/json"})
        if response.status_code >= 400:
            raise RuntimeError(f"{payload.slug}: {response.status_code} {response.text}")
        return response.json()


class LocalSink:
    """Writes straight into the local dev database and media folder (no server needed)."""

    def __init__(self):
        from ..app.config import load_settings
        from ..app.db import make_engine, make_session_factory
        from ..app.media import MediaStore
        from ..manage import upgrade
        self.settings = load_settings()
        upgrade(self.settings)
        self.factory = make_session_factory(make_engine(self.settings.db_url))
        self.media = MediaStore(self.settings.media_dir)

    def missing(self, hashes):
        return self.media.missing(hashes)

    def upload(self, image_hash, path):
        self.media.save(image_hash, path.read_bytes())

    def publish(self, payload):
        from ..app.services.versions import import_chapter
        with self.factory() as db:
            result = import_chapter(db, self.media, payload)
            db.commit()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", default="runs/book", help="folder of chapter runs (default runs/book)")
    parser.add_argument("--server", default=os.environ.get("REVIEW_SITE_URL"))
    parser.add_argument("--token", default=os.environ.get("REVIEW_TOOL_TOKEN"))
    parser.add_argument("--local", action="store_true", help="publish into the local dev database")
    parser.add_argument("--only", nargs="*", default=[], help="chapter folders to publish, e.g. alma/018")
    parser.add_argument("--limit", type=int, help="only the first N chapters (for trying things out)")
    parser.add_argument("--dry-run", action="store_true", help="build everything but upload nothing")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if not args.dry_run and not args.local and not (args.server and args.token):
        sys.exit("Give --server and --token (or set REVIEW_SITE_URL and REVIEW_TOOL_TOKEN), or use --local.")

    found = find_chapters(Store(Path(args.runs)))
    slugs = [s for s in found if not args.only or s in args.only]
    if args.limit:
        slugs = slugs[:args.limit]
    missing_names = set(args.only) - set(found)
    if missing_names:
        sys.exit(f"No assembled chapter named: {', '.join(sorted(missing_names))}")
    print(f"Reading {len(slugs)} chapters from {args.runs}...")
    built, sources = [], {}
    for slug in slugs:
        payload, found_sources = build_chapter(slug, found[slug])
        built.append(payload)
        sources.update(found_sources)
    print(f"{sum(len(p.pages) for p in built)} pages, converting to WebP (quality {WEBP_QUALITY})...")
    with ThreadPoolExecutor(args.workers) as pool:
        webp = dict(zip(sources, pool.map(lambda item: to_webp(item[1], item[0]), sources.items())))
    if args.dry_run:
        size = sum(w.stat().st_size for w in webp.values()) / 1e6
        print(f"Dry run: {len(built)} chapters ready, {size:.0f} MB of WebP in {BUILD}")
        return
    sink = LocalSink() if args.local else HttpSink(args.server, args.token)
    todo = sink.missing(list(webp))
    print(f"Uploading {len(todo)} new page images ({len(webp) - len(todo)} already there)...")
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(lambda h: sink.upload(h, webp[h]), todo))
    counts, pending = {}, []
    for payload in built:
        result = sink.publish(payload)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"].startswith("pending"):
            pending.append(f"{payload.book} {payload.number}")
    print("Done:", ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing to do")
    if counts.get("created"):
        print("New chapters start CLOSED. Open them for reviewers on the site: Admin > Chapters > Open all for review.")
    if pending:
        print(f"{len(pending)} chapters changed since they went live and are waiting for your approval "
              f"(Admin > Chapters): {', '.join(pending)}")


if __name__ == "__main__":
    main()
