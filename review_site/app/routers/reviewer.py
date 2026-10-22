"""Everything a reviewer sees: home, getting a chapter, the review screen and its JSON endpoints, their stats."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants
from ..deps import csrf_protect, flash, get_db, render, require_user
from ..models import ChapterVersion, Page, Review
from ..services import assignment, reviews, stats

router = APIRouter()


class IssueIn(BaseModel):
    page_id: int | None = None
    category: str = ""
    severity: str = ""
    description: str = ""
    suggestion: str = ""
    verse_ref: str = ""
    x: float | None = None
    y: float | None = None


class CheckIn(BaseModel):
    checked: bool


class BeatIn(BaseModel):
    seconds: int = 0


def load_review(db, user, review_id):
    """The reviewer's own review, or 404. Nobody can load another reviewer's review through this."""
    review = db.scalars(
        select(Review).where(Review.id == review_id, Review.user_id == user.id)
        .options(joinedload(Review.version).joinedload(ChapterVersion.chapter),
                 joinedload(Review.version).selectinload(ChapterVersion.pages).selectinload(Page.panels),
                 selectinload(Review.issues), selectinload(Review.checks))).unique().one_or_none()
    if review is None:
        raise HTTPException(404, "That review wasn't found.")
    return review


def _expire_check(db, review, request):
    if review.state == "active" and review.expires_at < assignment.utcnow():
        assignment.expire_stale(db)
        db.commit()
        db.refresh(review)


def review_payload(review):
    version = review.version
    verses = version.verses
    scope = {p.id for p in reviews.pages_in_scope(version)}
    done, total, _ = reviews.progress(review)
    return {
        "review": {"id": review.id, "state": review.state, "expires_at": review.expires_at.isoformat(),
                   "title": version.chapter.title, "scope": version.review_scope},
        "size": [version.pages[0].width, version.pages[0].height] if version.pages else [1400, 2000],
        "pages": [{
            "id": p.id, "number": p.number,
            "label": "Cover and reader's guide" if p.number == 0 else f"Page {p.number}",
            "image": f"/media/pages/{p.image_hash}.webp", "width": p.width, "height": p.height,
            "required": p.id in scope,
            "panels": [{"id": q.id, "key": q.key, "refs": q.refs_label, "frame": [q.x, q.y, q.w, q.h],
                        "verses": [[ref, verses.get(ref, "")] for ref in q.refs]} for q in p.panels]}
            for p in version.pages],
        "checked": sorted(c.page_id for c in review.checks),
        "issues": [reviews.serialize_issue(i) for i in review.issues],
        "progress": {"done": done, "total": total},
        "categories": [{"key": k, "label": label, "help": hint} for k, label, hint in constants.CATEGORIES],
        "severities": [{"key": k, "label": label, "help": hint} for k, label, hint in constants.SEVERITIES],
    }


@router.get("/")
def home(request: Request, db=Depends(get_db), user=Depends(require_user)):
    settings = request.app.state.settings
    assignment.expire_stale(db)
    db.commit()
    active = assignment.active_review(db, user)
    progress = None
    if active:
        active = load_review(db, user, active.id)
        done, total, _ = reviews.progress(active)
        progress = {"done": done, "total": total}
    rows = stats.coverage(db)
    return render(request, "home.html", user=user, active=active, progress=progress,
                  book=stats.book_progress(rows), mine=stats.personal(db, user),
                  skips_left=settings.skip_limit - assignment.skips_used(db, user, settings))


@router.post("/claim", dependencies=[Depends(csrf_protect)])
def claim(request: Request, db=Depends(get_db), user=Depends(require_user)):
    try:
        review = assignment.claim(db, user, request.app.state.settings)
        db.commit()
    except assignment.NothingAvailable as exc:
        db.rollback()
        flash(request, str(exc))
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/review/{review.id}", status_code=303)


@router.get("/choose")
def choose_page(request: Request, db=Depends(get_db), user=Depends(require_user)):
    if not user.can_choose:
        raise HTTPException(403, "You haven't been set up to choose your own chapter. Use the Get a chapter button instead.")
    settings = request.app.state.settings
    assignment.expire_stale(db)
    db.commit()
    groups = {}
    for row in assignment.pickable(db, user, settings):
        groups.setdefault(row["chapter"].book, []).append(row)
    return render(request, "choose.html", user=user, groups=groups, active=assignment.active_review(db, user))


@router.post("/choose/{chapter_id}", dependencies=[Depends(csrf_protect)])
def choose_chapter(request: Request, chapter_id: int, db=Depends(get_db), user=Depends(require_user)):
    try:
        review = assignment.claim_chapter(db, user, request.app.state.settings, chapter_id)
        db.commit()
    except assignment.NotAllowed as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from None
    except assignment.NothingAvailable as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/choose", status_code=303)
    return RedirectResponse(f"/review/{review.id}", status_code=303)


@router.post("/review/{review_id}/skip", dependencies=[Depends(csrf_protect)])
def skip(request: Request, review_id: int, db=Depends(get_db), user=Depends(require_user)):
    review = load_review(db, user, review_id)
    try:
        assignment.skip(db, review, request.app.state.settings)
        db.commit()
        flash(request, "No problem. Here's a chance to pick a different chapter.", "ok")
    except (assignment.SkipLimit, ValueError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/", status_code=303)


@router.get("/review/{review_id}")
def review_page(request: Request, review_id: int, db=Depends(get_db), user=Depends(require_user)):
    review = load_review(db, user, review_id)
    _expire_check(db, review, request)
    if review.state in ("expired", "skipped", "released"):
        flash(request, "That chapter was handed back, so it's no longer yours. You can get another one.", "error")
        return RedirectResponse("/", status_code=303)
    return render(request, "review.html", user=user, review=review, payload=review_payload(review),
                  readonly=review.state != "active", title=review.version.chapter.title)


def _api_review(db, user, review_id, settings, seconds=0):
    review = load_review(db, user, review_id)
    if review.state == "active" and review.expires_at < assignment.utcnow():
        raise HTTPException(409, "This chapter was handed back after a long break. Please get a new one.")
    return review


def _guard(fn):
    try:
        return fn()
    except reviews.ReviewError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/api/review/{review_id}/issues", dependencies=[Depends(csrf_protect)])
def add_issue(request: Request, review_id: int, body: IssueIn, db=Depends(get_db), user=Depends(require_user)):
    settings = request.app.state.settings
    review = _api_review(db, user, review_id, settings)

    def go():
        issue = reviews.add_issue(db, review, settings, body.model_dump())
        assignment.touch(review, settings)
        db.commit()
        db.refresh(issue)
        return {"issue": reviews.serialize_issue(issue), "checked": sorted(c.page_id for c in review.checks)}
    return _guard(go)


@router.put("/api/review/{review_id}/issues/{issue_id}", dependencies=[Depends(csrf_protect)])
def update_issue(request: Request, review_id: int, issue_id: int, body: IssueIn, db=Depends(get_db),
                 user=Depends(require_user)):
    settings = request.app.state.settings
    review = _api_review(db, user, review_id, settings)

    def go():
        fields = body.model_dump()
        fields.pop("page_id")
        issue = reviews.update_issue(db, review, issue_id, fields)
        assignment.touch(review, settings)
        db.commit()
        return {"issue": reviews.serialize_issue(issue)}
    return _guard(go)


@router.delete("/api/review/{review_id}/issues/{issue_id}", dependencies=[Depends(csrf_protect)])
def delete_issue(request: Request, review_id: int, issue_id: int, db=Depends(get_db), user=Depends(require_user)):
    settings = request.app.state.settings
    review = _api_review(db, user, review_id, settings)

    def go():
        reviews.delete_issue(db, review, issue_id)
        assignment.touch(review, settings)
        db.commit()
        return {"ok": True}
    return _guard(go)


@router.put("/api/review/{review_id}/pages/{page_id}/check", dependencies=[Depends(csrf_protect)])
def check_page(request: Request, review_id: int, page_id: int, body: CheckIn, db=Depends(get_db),
               user=Depends(require_user)):
    settings = request.app.state.settings
    review = _api_review(db, user, review_id, settings)

    def go():
        reviews.set_page_checked(db, review, page_id, body.checked)
        assignment.touch(review, settings)
        db.commit()
        return {"ok": True}
    return _guard(go)


@router.post("/api/review/{review_id}/heartbeat", dependencies=[Depends(csrf_protect)])
def heartbeat(request: Request, review_id: int, body: BeatIn, db=Depends(get_db), user=Depends(require_user)):
    settings = request.app.state.settings
    review = _api_review(db, user, review_id, settings)
    if review.state == "active":
        assignment.touch(review, settings, body.seconds)
        db.commit()
    return {"ok": True}


@router.post("/api/review/{review_id}/submit", dependencies=[Depends(csrf_protect)])
def submit(request: Request, review_id: int, db=Depends(get_db), user=Depends(require_user)):
    settings = request.app.state.settings
    review = _api_review(db, user, review_id, settings)

    def go():
        reviews.submit(db, review)
        db.commit()
        return {"ok": True, "redirect": f"/review/{review.id}/done"}
    return _guard(go)


@router.get("/review/{review_id}/done")
def done(request: Request, review_id: int, db=Depends(get_db), user=Depends(require_user)):
    review = load_review(db, user, review_id)
    if review.state != "submitted":
        return RedirectResponse(f"/review/{review.id}", status_code=303)
    return render(request, "done.html", user=user, review=review, issues=len(review.issues),
                  mine=stats.personal(db, user))


@router.get("/me")
def me(request: Request, db=Depends(get_db), user=Depends(require_user)):
    history = db.scalars(select(Review).where(Review.user_id == user.id, Review.state == "submitted")
                         .options(joinedload(Review.version).joinedload(ChapterVersion.chapter), selectinload(Review.issues))
                         .order_by(Review.submitted_at.desc())).unique().all()
    return render(request, "me.html", user=user, mine=stats.personal(db, user), history=history)


@router.post("/me/credit", dependencies=[Depends(csrf_protect)])
def set_credit(request: Request, db=Depends(get_db), user=Depends(require_user)):
    user.credit_ok = not user.credit_ok
    db.commit()
    flash(request, "You'll be thanked by name." if user.credit_ok else "You won't be named on the thank-you page.", "ok")
    return RedirectResponse("/me", status_code=303)


@router.get("/thanks")
def thanks(request: Request, db=Depends(get_db), user=Depends(require_user)):
    names = [row["user"].display_name for row in stats.contributors(db)
             if row["user"].credit_ok and row["user"].is_active and row["reviews"] > 0]
    return render(request, "thanks.html", user=user, names=names)


@router.get("/media/pages/{name}")
def media(request: Request, name: str, user=Depends(require_user)):
    image_hash = name.removesuffix(".webp")
    try:
        path = request.app.state.media.path(image_hash)
    except ValueError:
        raise HTTPException(404, "Not found") from None
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})
