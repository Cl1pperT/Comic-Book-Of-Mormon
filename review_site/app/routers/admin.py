"""Editor and admin screens: coverage, issues, chapters and versions, review history, contributors, users."""
import math

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants
from ..deps import csrf_protect, flash, get_db, render, require_admin, require_editor
from ..models import AuditLog, Chapter, ChapterVersion, Invite, Issue, Page, Review, User
from ..services import accounts, assignment, issues as issue_service, stats, versions
from ..services.audit import log

router = APIRouter(prefix="/admin")


def _back(url="/admin"):
    return RedirectResponse(url, status_code=303)


@router.get("")
def overview(request: Request, db=Depends(get_db), user=Depends(require_editor)):
    assignment.expire_stale(db)
    db.commit()
    rows = stats.coverage(db)
    groups = {}
    for row in rows:
        groups.setdefault(row["chapter"].book, []).append(row)
    return render(request, "admin/overview.html", user=user, groups=groups, book=stats.book_progress(rows),
                  totals=stats.issue_totals(db), pending=[r for r in rows if r["pending"]])


# ---- issues ----------------------------------------------------------------------------------------------------------

FILTERS = ("status", "category", "severity", "chapter", "reviewer", "q")


def _filters(request):
    got = {name: request.query_params.get(name, "") for name in FILTERS}
    got["current_only"] = request.query_params.get("current_only") == "1"
    return got


@router.get("/issues")
def issues_list(request: Request, page: int = 1, db=Depends(get_db), user=Depends(require_editor)):
    filters = _filters(request)
    if filters["reviewer"] and not filters["reviewer"].isdigit():
        filters["reviewer"] = ""
    rows, total = issue_service.search(db, filters, max(page, 1))
    query = "&".join(f"{k}={v}" for k, v in filters.items() if v and k != "current_only") + (
        "&current_only=1" if filters["current_only"] else "")
    return render(request, "admin/issues.html", user=user, issues=rows, total=total, filters=filters, page=page,
                  pages=max(1, math.ceil(total / issue_service.PAGE_SIZE)), query=query,
                  chapters=db.scalars(select(Chapter).order_by(Chapter.sort_order)).all(),
                  reviewers=db.scalars(select(User).order_by(User.display_name)).all())


@router.get("/issues.csv")
def issues_csv(request: Request, db=Depends(get_db), user=Depends(require_editor)):
    rows = db.scalars(issue_service.filtered(_filters(request)).order_by(Issue.id)).unique().all()
    return PlainTextResponse(issue_service.to_csv(rows), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="issues.csv"'})


@router.post("/issues/bulk", dependencies=[Depends(csrf_protect)])
async def issues_bulk(request: Request, db=Depends(get_db), user=Depends(require_editor)):
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    status = form.get("status", "")
    if not ids or status not in constants.ISSUE_STATUS_KEYS or status == "duplicate":
        flash(request, "Tick some issues and choose a status first.", "error")
    else:
        count = issue_service.bulk_status(db, ids, user, status)
        db.commit()
        flash(request, f"{count} issues marked {constants.ISSUE_STATUS_LABELS[status].lower()}.", "ok")
    return _back(form.get("return", "/admin/issues"))


@router.get("/issues/{issue_id}")
def issue_detail(request: Request, issue_id: int, db=Depends(get_db), user=Depends(require_editor)):
    issue = issue_service.get(db, issue_id)
    if issue is None:
        raise HTTPException(404, "That issue wasn't found.")
    return render(request, "admin/issue.html", user=user, issue=issue, similar=issue_service.similar(db, issue),
                  verses=issue.review.version.verses)


@router.post("/issues/{issue_id}", dependencies=[Depends(csrf_protect)])
def issue_update(request: Request, issue_id: int, status: str = Form(...), note: str = Form(""),
                 duplicate_of: str = Form(""), db=Depends(get_db), user=Depends(require_editor)):
    issue = issue_service.get(db, issue_id)
    if issue is None:
        raise HTTPException(404, "That issue wasn't found.")
    try:
        issue_service.change_status(db, issue, user, status, note.strip(),
                                    int(duplicate_of) if duplicate_of.strip().isdigit() else None)
        db.commit()
        flash(request, "Saved.", "ok")
    except issue_service.IssueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(f"/admin/issues/{issue_id}")


# ---- chapters and versions -------------------------------------------------------------------------------------------

@router.get("/chapters")
def chapters_page(request: Request, db=Depends(get_db), user=Depends(require_editor)):
    return render(request, "admin/chapters.html", user=user, rows=stats.coverage(db))


@router.post("/chapters/status", dependencies=[Depends(csrf_protect)])
async def chapters_status(request: Request, db=Depends(get_db), user=Depends(require_editor)):
    form = await request.form()
    status, slugs = form.get("status", ""), form.getlist("slugs")
    if status not in constants.CHAPTER_STATUSES:
        raise HTTPException(400, "Unknown status")
    query = select(Chapter)
    if form.get("scope") != "all":
        query = query.where(Chapter.slug.in_(slugs))
    changed = 0
    for chapter in db.scalars(query):
        if status == "open" and chapter.current is None:
            continue
        if chapter.status != status:
            chapter.status, changed = status, changed + 1
    log(db, user, "chapters-" + status, "chapter", "all" if form.get("scope") == "all" else ",".join(slugs[:10]),
        f"{changed} chapters")
    db.commit()
    flash(request, f"{changed} chapters are now {status}.", "ok")
    return _back(form.get("return", "/admin/chapters"))


@router.get("/chapter")
def chapter_detail(request: Request, slug: str, db=Depends(get_db), user=Depends(require_editor)):
    chapter = db.scalar(select(Chapter).where(Chapter.slug == slug).options(
        selectinload(Chapter.versions).selectinload(ChapterVersion.pages)))
    if chapter is None:
        raise HTTPException(404, "That chapter wasn't found.")
    counts = dict(db.execute(select(Review.version_id, func.count()).where(
        Review.state == "submitted", Review.excluded.is_(False)).group_by(Review.version_id)).all())
    reviews_ = db.scalars(select(Review).join(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id)
                          .options(joinedload(Review.user), joinedload(Review.version), selectinload(Review.issues))
                          .order_by(Review.claimed_at.desc())).unique().all()
    return render(request, "admin/chapter.html", user=user, chapter=chapter, counts=counts, reviews=reviews_,
                  changed=versions.changed_page_numbers(chapter.pending) if chapter.pending else [])


@router.post("/chapter/version", dependencies=[Depends(csrf_protect)])
def chapter_version(request: Request, slug: str = Form(...), action: str = Form(...), scope: str = Form("full"),
                    note: str = Form(""), db=Depends(get_db), user=Depends(require_editor)):
    chapter = db.scalar(select(Chapter).where(Chapter.slug == slug))
    if chapter is None or chapter.pending is None:
        raise HTTPException(404, "There's no pending version for that chapter.")
    try:
        if action == "live":
            versions.make_live(db, chapter.pending, scope, user, note)
            flash(request, "The new version is live. Reviews of earlier versions are kept as history.", "ok")
        else:
            versions.discard_pending(db, chapter.pending, user)
            flash(request, "The pending version was discarded.", "ok")
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(f"/admin/chapter?slug={slug}")


# ---- reviews ---------------------------------------------------------------------------------------------------------

@router.get("/reviews")
def reviews_page(request: Request, state: str = "", db=Depends(get_db), user=Depends(require_editor)):
    assignment.expire_stale(db)
    db.commit()
    query = (select(Review).options(joinedload(Review.user), joinedload(Review.version).joinedload(ChapterVersion.chapter),
                                    selectinload(Review.issues))
             .order_by(Review.claimed_at.desc()).limit(300))
    if state in constants.REVIEW_STATES:
        query = query.where(Review.state == state)
    rows = db.scalars(query).unique().all()
    page_counts = dict(db.execute(select(Page.version_id, func.count()).group_by(Page.version_id)).all())
    return render(request, "admin/reviews.html", user=user, rows=rows, state=state, page_counts=page_counts,
                  fast=request.app.state.settings.fast_seconds_per_page)


@router.post("/reviews/{review_id}", dependencies=[Depends(csrf_protect)])
def review_action(request: Request, review_id: int, action: str = Form(...), reason: str = Form(""),
                  db=Depends(get_db), user=Depends(require_editor)):
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(404, "That review wasn't found.")
    if action == "exclude":
        review.excluded, review.excluded_reason = True, reason.strip()[:300]
    elif action == "include":
        review.excluded, review.excluded_reason = False, ""
    elif action == "release" and review.state == "active":
        review.state = "released"
    else:
        raise HTTPException(400, "Unknown action")
    log(db, user, f"review-{action}", "review", review.id, reason.strip()[:200])
    db.commit()
    flash(request, "Saved.", "ok")
    return _back(request.query_params.get("return", "/admin/reviews"))


@router.get("/contributors")
def contributors_page(request: Request, db=Depends(get_db), user=Depends(require_editor)):
    return render(request, "admin/contributors.html", user=user, rows=stats.contributors(db))


# ---- users (admin only) ----------------------------------------------------------------------------------------------

@router.get("/users")
def users_page(request: Request, db=Depends(get_db), user=Depends(require_admin)):
    return render(request, "admin/users.html", user=user, users=db.scalars(select(User).order_by(User.created_at)).all(),
                  invites=db.scalars(select(Invite).order_by(Invite.created_at.desc()).limit(50)).all(),
                  reset_link=request.session.pop("reset_link", None), base=str(request.base_url).rstrip("/"))


@router.post("/users/invite", dependencies=[Depends(csrf_protect)])
def make_invite(request: Request, role: str = Form("reviewer"), max_uses: int = Form(1), note: str = Form(""),
                can_choose: str = Form(""), db=Depends(get_db), user=Depends(require_admin)):
    try:
        accounts.create_invite(db, user, role, max_uses, note=note, can_choose=bool(can_choose))
        db.commit()
    except accounts.AccountError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back("/admin/users")


@router.post("/users/invite/{invite_id}/revoke", dependencies=[Depends(csrf_protect)])
def revoke_invite(invite_id: int, db=Depends(get_db), user=Depends(require_admin)):
    invite = db.get(Invite, invite_id)
    if invite:
        invite.revoked = True
        log(db, user, "invite-revoke", "invite", invite.code)
        db.commit()
    return _back("/admin/users")


@router.post("/users/{user_id}", dependencies=[Depends(csrf_protect)])
def user_action(request: Request, user_id: int, action: str = Form(...), role: str = Form(""),
                db=Depends(get_db), user=Depends(require_admin)):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "That user wasn't found.")
    if target.id == user.id and action in ("deactivate", "role"):
        flash(request, "You can't change your own access.", "error")
        return _back("/admin/users")
    if action == "deactivate":
        target.is_active = False
        for review in db.scalars(select(Review).where(Review.user_id == target.id, Review.state == "active")):
            review.state = "released"
    elif action == "activate":
        target.is_active = True
    elif action == "role" and role in constants.ROLES:
        target.role = role
    elif action in ("choose_on", "choose_off"):
        target.can_choose = action == "choose_on"
    elif action == "reset":
        token = accounts.start_reset(db, user, target)
        request.session["reset_link"] = [target.display_name, f"/reset/{token}"]
    else:
        raise HTTPException(400, "Unknown action")
    log(db, user, f"user-{action}", "user", target.id, role)
    db.commit()
    return _back("/admin/users")


@router.get("/audit")
def audit_page(request: Request, db=Depends(get_db), user=Depends(require_admin)):
    rows = db.scalars(select(AuditLog).options(joinedload(AuditLog.actor)).order_by(AuditLog.id.desc()).limit(200)).all()
    return render(request, "admin/audit.html", user=user, rows=rows)
