"""The API used by the publish/pull tools on the owner's PC. Authenticated with a bearer token, not a login."""
import sqlite3
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from ..deps import get_db
from ..models import Chapter, Issue
from ..security import same
from ..services import issues as issue_service, versions

router = APIRouter(prefix="/api/tool")


def tool_auth(request: Request):
    token = request.app.state.settings.tool_token
    header = request.headers.get("authorization", "")
    if not token or not header.startswith("Bearer ") or not same(header[7:], token):
        raise HTTPException(401, "Bad or missing tool token.")


class PlanIn(BaseModel):
    hashes: list[str]


class ExportedIn(BaseModel):
    ids: list[int]


@router.post("/plan", dependencies=[Depends(tool_auth)])
def plan(request: Request, body: PlanIn):
    """Which page images does the server still need?"""
    try:
        return {"missing": request.app.state.media.missing(body.hashes)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.put("/image/{image_hash}", dependencies=[Depends(tool_auth)])
async def upload_image(request: Request, image_hash: str):
    try:
        request.app.state.media.save(image_hash, await request.body())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.post("/chapter", dependencies=[Depends(tool_auth)])
def publish_chapter(request: Request, payload: versions.ChapterPayload, db=Depends(get_db)):
    try:
        result = versions.import_chapter(db, request.app.state.media, payload)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    return result


@router.get("/issues", dependencies=[Depends(tool_auth)])
def export_issues(db=Depends(get_db), status: str = "accepted", unexported: bool = True):
    """Accepted issues (by default only those not yet pulled), with what the PC needs to find the panel again."""
    query = issue_service.base_query().where(Issue.status == status)
    if unexported:
        query = query.where(Issue.exported_at.is_(None))
    out = []
    for issue in db.scalars(query.order_by(Issue.id)).unique():
        version = issue.review.version
        out.append({
            "id": issue.id, "chapter": version.chapter.slug, "book": version.chapter.book,
            "chapter_number": version.chapter.number, "version": version.number,
            "version_is_current": version.state == "current",
            "page": issue.page.number, "page_image_hash": issue.page.image_hash,
            "panel": issue.panel.key if issue.panel else None, "scene_id": issue.panel.scene_id if issue.panel else "",
            "x": issue.x, "y": issue.y, "category": issue.category, "severity": issue.severity,
            "description": issue.description, "suggestion": issue.suggestion, "verse_ref": issue.verse_ref,
            "refs": issue.panel.refs_label if issue.panel else "", "note": issue.resolution_note,
            "reported_at": issue.created_at.isoformat()})
    return {"issues": out}


@router.post("/issues/exported", dependencies=[Depends(tool_auth)])
def mark_exported(body: ExportedIn, db=Depends(get_db)):
    now = datetime.now(timezone.utc)
    count = 0
    for issue in db.scalars(select(Issue).where(Issue.id.in_(body.ids))):
        issue.exported_at, count = now, count + 1
    db.commit()
    return {"marked": count}


@router.get("/backup", dependencies=[Depends(tool_auth)])
def backup(request: Request):
    """A consistent copy of the SQLite database (page images are re-creatable by publishing again)."""
    settings = request.app.state.settings
    if not settings.db_url.startswith("sqlite"):
        raise HTTPException(501, "Backups are for the SQLite setup; use your database's own tools.")
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    handle.close()
    target = sqlite3.connect(handle.name)
    raw = request.app.state.engine.raw_connection()
    try:
        raw.driver_connection.backup(target)
    finally:
        raw.close()
        target.close()
    return FileResponse(handle.name, media_type="application/octet-stream", filename="review-site-backup.db")


@router.get("/status", dependencies=[Depends(tool_auth)])
def status(db=Depends(get_db)):
    chapters = db.scalars(select(Chapter).options(joinedload(Chapter.versions))).unique().all()
    return JSONResponse({"chapters": len(chapters), "with_pending": sum(1 for c in chapters if c.pending)})
