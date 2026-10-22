"""Numbers for the dashboards: book coverage, issue counts, and each reviewer's contribution."""
from dataclasses import dataclass

from sqlalchemy import case, func, select

from ..models import Chapter, ChapterVersion, Issue, Page, Review, User


def coverage(db):
    """One row per chapter: the live version's number of submitted reviews and open holds."""
    done = dict(db.execute(select(Review.version_id, func.count()).where(
        Review.state == "submitted", Review.excluded.is_(False)).group_by(Review.version_id)).all())
    holds = dict(db.execute(select(Review.version_id, func.count()).where(Review.state == "active")
                            .group_by(Review.version_id)).all())
    open_issues = dict(db.execute(select(ChapterVersion.chapter_id, func.count()).select_from(Issue)
                                  .join(Review, Review.id == Issue.review_id)
                                  .join(ChapterVersion, ChapterVersion.id == Review.version_id)
                                  .where(Review.state == "submitted", Issue.status.in_(("open", "accepted")))
                                  .group_by(ChapterVersion.chapter_id)).all())
    rows = []
    for chapter in db.scalars(select(Chapter).order_by(Chapter.sort_order)):
        current = chapter.current
        rows.append({"chapter": chapter, "version": current, "pending": chapter.pending,
                     "reviews": done.get(current.id, 0) if current else 0,
                     "holds": holds.get(current.id, 0) if current else 0,
                     "open_issues": open_issues.get(chapter.id, 0)})
    return rows


@dataclass
class BookProgress:
    chapters: int
    open_chapters: int
    reviewed_once: int
    levels: dict
    percent_once: int


def book_progress(rows):
    counted = [r for r in rows if r["chapter"].status == "open" and r["version"]]
    levels = {0: 0, 1: 0, 2: 0, 3: 0}
    for r in counted:
        levels[min(r["reviews"], 3)] += 1
    once = sum(1 for r in counted if r["reviews"] >= 1)
    return BookProgress(chapters=len(rows), open_chapters=len(counted), reviewed_once=once, levels=levels,
                        percent_once=round(100 * once / len(counted)) if counted else 0)


def issue_totals(db):
    base = select(Issue.status, func.count()).join(Review, Review.id == Issue.review_id).where(
        Review.state == "submitted").group_by(Issue.status)
    return dict(db.execute(base).all())


def contributors(db, only=None):
    """Per reviewer: submitted reviews, pages checked, issues reported and how many editors accepted or rejected.
    only: limit to one user."""
    query = select(User).order_by(User.display_name)
    users = db.scalars(query.where(User.id == only.id) if only else query).all()
    reviews = {uid: (n, secs) for uid, n, secs in db.execute(
        select(Review.user_id, func.count(), func.coalesce(func.sum(Review.active_seconds), 0))
        .where(Review.state == "submitted").group_by(Review.user_id))}
    pages = {}
    for uid, version_id in db.execute(select(Review.user_id, Review.version_id).where(Review.state == "submitted")):
        pages[uid] = pages.get(uid, 0) + db.scalar(select(func.count()).select_from(Page).where(Page.version_id == version_id))
    issues = {uid: (total, accepted, rejected) for uid, total, accepted, rejected in db.execute(
        select(Review.user_id, func.count(),
               func.sum(case((Issue.status.in_(("accepted", "fixed", "verified")), 1), else_=0)),
               func.sum(case((Issue.status == "rejected", 1), else_=0)))
        .join(Review, Review.id == Issue.review_id).where(Review.state == "submitted").group_by(Review.user_id))}
    out = []
    for user in users:
        n, secs = reviews.get(user.id, (0, 0))
        total, accepted, rejected = issues.get(user.id, (0, 0, 0))
        out.append({"user": user, "reviews": n, "pages": pages.get(user.id, 0), "minutes": round(secs / 60),
                    "issues": total, "accepted": accepted or 0, "rejected": rejected or 0})
    return out


def personal(db, user):
    return contributors(db, only=user)[0]
