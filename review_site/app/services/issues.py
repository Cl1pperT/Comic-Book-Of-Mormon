"""Editor-side issue handling: filtering, status changes, duplicates, and export rows.

Only issues from *submitted* reviews are visible here; a half-finished review stays private to its reviewer."""
import csv
import io

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from ..constants import CATEGORY_LABELS, ISSUE_STATUS_KEYS, SEVERITY_LABELS
from ..models import Chapter, ChapterVersion, Issue, IssueEvent, Page, Review, User, utcnow
from .audit import log

PAGE_SIZE = 50


class IssueError(ValueError):
    pass


def base_query():
    return (select(Issue).join(Review, Review.id == Issue.review_id).join(ChapterVersion, ChapterVersion.id == Review.version_id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id).join(User, User.id == Review.user_id)
            .where(Review.state == "submitted")
            .options(joinedload(Issue.review).joinedload(Review.user), joinedload(Issue.review).joinedload(Review.version)
                     .joinedload(ChapterVersion.chapter), joinedload(Issue.page), joinedload(Issue.panel)))


def filtered(filters):
    query = base_query()
    if filters.get("status"):
        query = query.where(Issue.status == filters["status"])
    if filters.get("category"):
        query = query.where(Issue.category == filters["category"])
    if filters.get("severity"):
        query = query.where(Issue.severity == filters["severity"])
    if filters.get("chapter"):
        query = query.where(Chapter.slug == filters["chapter"])
    if filters.get("reviewer"):
        query = query.where(User.id == int(filters["reviewer"]))
    if filters.get("current_only"):
        query = query.where(ChapterVersion.state == "current")
    if filters.get("q"):
        like = f"%{filters['q'].strip()}%"
        query = query.where(or_(Issue.description.ilike(like), Issue.suggestion.ilike(like)))
    return query


def search(db, filters, page=1):
    query = filtered(filters)
    total = db.scalar(select(func.count()).select_from(query.with_only_columns(Issue.id).order_by(None).subquery()))
    rows = db.scalars(query.order_by(Chapter.sort_order, Issue.id)
                      .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).unique().all()
    return rows, total


def get(db, issue_id):
    return db.scalars(base_query().where(Issue.id == issue_id)).unique().one_or_none()


def similar(db, issue):
    """Other submitted reports on the same panel (or page) of this chapter, to help spot duplicates."""
    chapter_id = issue.review.version.chapter_id
    query = base_query().where(ChapterVersion.chapter_id == chapter_id, Issue.id != issue.id,
                               Issue.page_id.in_(select(Page.id).where(Page.number == issue.page.number)))
    return db.scalars(query.order_by(Issue.id)).unique().all()


def change_status(db, issue, actor, status, note="", duplicate_of=None):
    if status not in ISSUE_STATUS_KEYS:
        raise IssueError("Unknown status")
    if status == "duplicate":
        original = db.get(Issue, duplicate_of) if duplicate_of else None
        if original is None or original.id == issue.id:
            raise IssueError("Choose the issue this duplicates")
        if original.duplicate_of_id == issue.id:
            raise IssueError("That issue is already marked as a duplicate of this one")
        issue.duplicate_of_id = original.id
    else:
        issue.duplicate_of_id = None
    if status != issue.status or note:
        db.add(IssueEvent(issue_id=issue.id, actor_id=actor.id, from_status=issue.status, to_status=status, note=note))
    if status != issue.status and issue.exported_at and status in ("open", "rejected", "duplicate"):
        issue.exported_at = None   # it will be re-evaluated if accepted again
    issue.status, issue.resolution_note = status, note or issue.resolution_note
    issue.updated_at = utcnow()
    db.flush()


def bulk_status(db, ids, actor, status, note=""):
    count = 0
    for issue_id in ids:
        issue = get(db, issue_id)
        if issue is not None and status != "duplicate":
            change_status(db, issue, actor, status, note)
            count += 1
    if count:
        log(db, actor, "issues-bulk-status", "issue", ",".join(map(str, ids[:20])), f"{status} x{count}")
    return count


CSV_COLUMNS = ["id", "chapter", "version", "page", "panel", "category", "severity", "status", "description",
               "suggestion", "verse_ref", "reporter", "reported_at"]


def to_csv(issues):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for i in issues:
        version = i.review.version
        writer.writerow([i.id, version.chapter.title, version.number, "cover" if i.page.number == 0 else i.page.number,
                         i.panel.key if i.panel else "", CATEGORY_LABELS[i.category], SEVERITY_LABELS[i.severity],
                         i.status, i.description, i.suggestion, i.verse_ref, i.review.user.display_name,
                         i.created_at.isoformat(timespec="seconds")])
    return out.getvalue()
