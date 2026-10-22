"""Publishing chapters into the site, and deciding when a new version goes live.

The publish tool sends one ChapterPayload per chapter. A chapter's version is identified by a fingerprint of its page
images and verse text, so re-publishing unchanged work does nothing, and an edited chapter arrives as a *pending*
version that an editor reviews (which pages changed) before choosing how much of it needs checking again."""
import hashlib
import json

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ..constants import REVIEW_SCOPES
from ..media import HASH, MediaStore
from ..models import Chapter, ChapterVersion, Page, Panel, utcnow
from .audit import log


class PanelPayload(BaseModel):
    key: str = Field(max_length=40)
    scene_id: str = ""
    refs_label: str = ""
    refs: list[str] = []
    frame: list[float] = Field(min_length=4, max_length=4)   # x, y, w, h as fractions of the page


class PagePayload(BaseModel):
    number: int = Field(ge=0)
    label: str = ""
    image_hash: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    panels: list[PanelPayload] = []

    @field_validator("image_hash")
    @classmethod
    def valid_hash(cls, value):
        if not HASH.match(value):
            raise ValueError("image_hash must be a sha256 hex digest")
        return value


class ChapterPayload(BaseModel):
    slug: str = Field(max_length=80, pattern=r"^[a-z0-9][a-z0-9/_-]*$")
    book: str
    number: int
    sort_order: int
    verses: dict[str, str] = {}
    pages: list[PagePayload] = Field(min_length=1)

    @field_validator("pages")
    @classmethod
    def distinct_pages(cls, pages):
        if len({p.number for p in pages}) != len(pages):
            raise ValueError("duplicate page numbers")
        return pages


def fingerprint(payload):
    body = {"pages": [[p.number, p.image_hash] for p in sorted(payload.pages, key=lambda p: p.number)],
            "verses": hashlib.sha256(json.dumps(payload.verses, sort_keys=True).encode()).hexdigest()}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def missing_images(media, payloads):
    return media.missing(p.image_hash for payload in payloads for p in payload.pages)


def _build(db, chapter, number, payload, state, previous):
    old = {p.number: p.image_hash for p in previous.pages} if previous else {}
    version = ChapterVersion(chapter=chapter, number=number, fingerprint=fingerprint(payload), state=state,
                             verses_json=json.dumps(payload.verses, ensure_ascii=False))
    for p in sorted(payload.pages, key=lambda p: p.number):
        page = Page(version=version, number=p.number, label=p.label, image_hash=p.image_hash,
                    width=p.width, height=p.height, changed=old.get(p.number) != p.image_hash)
        for panel in p.panels:
            x, y, w, h = panel.frame
            page.panels.append(Panel(key=panel.key, scene_id=panel.scene_id, refs_label=panel.refs_label,
                                     refs_json=json.dumps(panel.refs), x=x, y=y, w=w, h=h))
    db.add(version)   # relationships no longer cascade into the session from the other side
    return version


def import_chapter(db, media: MediaStore, payload: ChapterPayload):
    """Add a chapter, or a new version of it. Returns {"status": ..., "version": n}.

    status: "created" (first publish; the chapter starts closed), "unchanged", "pending" (a changed version waits for an
    editor), "pending-updated" (replaced an earlier pending version), or "reverted" (back to the live version, so the
    pending one was dropped)."""
    if media.missing(p.image_hash for p in payload.pages):
        raise ValueError("Upload every page image before publishing the chapter")
    chapter = db.scalar(select(Chapter).where(Chapter.slug == payload.slug))
    if chapter is None:
        chapter = Chapter(slug=payload.slug, book=payload.book, number=payload.number,
                          sort_order=payload.sort_order, status="closed")
        db.add(chapter)
        version = _build(db, chapter, 1, payload, "current", None)
        version.made_live_at = utcnow()
        db.flush()
        return {"status": "created", "version": version.number}
    chapter.sort_order = payload.sort_order
    current, pending, new_print = chapter.current, chapter.pending, fingerprint(payload)
    if current and current.fingerprint == new_print:
        if pending:
            db.delete(pending)
            db.flush()
            db.expire(chapter, ["versions"])
            return {"status": "reverted", "version": current.number}
        return {"status": "unchanged", "version": current.number}
    if pending and pending.fingerprint == new_print:
        return {"status": "unchanged", "version": pending.number}
    replaced = pending is not None
    if pending:
        db.delete(pending)
        db.flush()
        db.expire(chapter, ["versions"])
    number = max((v.number for v in chapter.versions), default=0) + 1
    base = current
    if base is None:   # retired chapters without a live version: treat everything as changed
        _build(db, chapter, number, payload, "current", None).made_live_at = utcnow()
        db.flush()
        return {"status": "created", "version": number}
    _build(db, chapter, number, payload, "pending", base)
    db.flush()
    return {"status": "pending-updated" if replaced else "pending", "version": number}


def changed_page_numbers(version):
    return [p.number for p in version.pages if p.changed]


def make_live(db, version, scope, actor, note=""):
    """Promote a pending version. The old live version becomes history; only the new one counts toward coverage."""
    if version.state != "pending":
        raise ValueError("Only a pending version can be made live")
    if scope not in REVIEW_SCOPES:
        raise ValueError("Unknown review scope")
    if scope == "changed_pages" and not changed_page_numbers(version):
        raise ValueError("No page images changed, so the whole chapter must be re-reviewed")
    for other in version.chapter.versions:
        if other.state == "current":
            other.state = "superseded"
    db.flush()   # free the one-current-version slot before taking it
    version.state, version.review_scope, version.made_live_at = "current", scope, utcnow()
    version.note = note[:300]
    log(db, actor, "version-live", "chapter", version.chapter.slug, f"v{version.number} scope={scope}")
    db.flush()


def discard_pending(db, version, actor):
    if version.state != "pending":
        raise ValueError("Only a pending version can be discarded")
    chapter = version.chapter
    log(db, actor, "version-discard", "chapter", chapter.slug, f"v{version.number}")
    db.delete(version)
    db.flush()
    db.expire(chapter, ["versions"])
