"""What a reviewer can do inside their own review: mark pages, report issues, submit.

Every function takes the review, and callers only ever load reviews belonging to the logged-in reviewer, so a reviewer
can never read or change anyone else's work."""
from ..constants import CATEGORY_KEYS, SEVERITY_KEYS
from ..models import Issue, PageCheck, utcnow

MAX_TEXT = 2000


class ReviewError(ValueError):
    """A problem the reviewer can fix; the message is shown to them."""


def pages_in_scope(version):
    """Pages that must be checked: all of them, or only those that changed since the last version."""
    if version.review_scope == "changed_pages":
        return [p for p in version.pages if p.changed]
    return list(version.pages)


def _editable(review):
    if review.state != "active":
        raise ReviewError("This review is no longer open for changes.")


def _page(review, page_id):
    page = next((p for p in review.version.pages if p.id == page_id), None)
    if page is None:
        raise ReviewError("That page isn't part of this chapter.")
    return page


def progress(review):
    """(pages done, pages required, ids of pages still to check)."""
    scope = pages_in_scope(review.version)
    checked = {c.page_id for c in review.checks} | {i.page_id for i in review.issues}
    todo = [p.id for p in scope if p.id not in checked]
    return len(scope) - len(todo), len(scope), todo


def set_page_checked(db, review, page_id, checked):
    _editable(review)
    page = _page(review, page_id)
    has_issue = any(i.page_id == page.id for i in review.issues)
    existing = db.get(PageCheck, (review.id, page.id))
    if checked:
        if has_issue:
            raise ReviewError("This page already has issues reported on it.")
        if existing is None:
            db.add(PageCheck(review_id=review.id, page_id=page.id))
    elif existing is not None:
        db.delete(existing)
    db.flush()
    db.expire(review, ["checks"])


def locate(page, x, y):
    """The panel a pin belongs to: the one containing the point, else the nearest panel on that page."""
    best, best_distance = None, None
    for panel in page.panels:
        dx = max(panel.x - x, 0, x - (panel.x + panel.w))
        dy = max(panel.y - y, 0, y - (panel.y + panel.h))
        distance = (dx * dx + dy * dy) ** 0.5
        if best is None or distance < best_distance:
            best, best_distance = panel, distance
    return best


def _clean(fields):
    category, severity = fields.get("category"), fields.get("severity")
    if category not in CATEGORY_KEYS:
        raise ReviewError("Please choose what kind of problem this is.")
    if severity not in SEVERITY_KEYS:
        raise ReviewError("Please choose how serious it is.")
    description = (fields.get("description") or "").strip()
    if not description:
        raise ReviewError("Please describe the problem.")
    suggestion = (fields.get("suggestion") or "").strip()
    verse_ref = (fields.get("verse_ref") or "").strip()
    if len(description) > MAX_TEXT or len(suggestion) > MAX_TEXT or len(verse_ref) > 100:
        raise ReviewError("That text is too long.")
    return {"category": category, "severity": severity, "description": description,
            "suggestion": suggestion, "verse_ref": verse_ref}


def _pin(page, fields):
    x, y = fields.get("x"), fields.get("y")
    if x is None and y is None:
        return None, None, None
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        raise ReviewError("The pin position is invalid.") from None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ReviewError("The pin position is off the page.")
    panel = locate(page, x, y)
    return x, y, panel.id if panel else None


def add_issue(db, review, settings, fields):
    _editable(review)
    if len(review.issues) >= settings.max_issues_per_review:
        raise ReviewError("That's a lot of issues for one chapter. Please submit this review and start another.")
    page = _page(review, fields.get("page_id"))
    clean = _clean(fields)
    x, y, panel_id = _pin(page, fields)
    issue = Issue(review_id=review.id, page_id=page.id, panel_id=panel_id, x=x, y=y, **clean)
    db.add(issue)
    existing = db.get(PageCheck, (review.id, page.id))
    if existing is not None:   # reporting a problem replaces "looks good"
        db.delete(existing)
    db.flush()
    db.expire(review, ["issues", "checks"])
    return issue


def _mine(review, issue_id):
    issue = next((i for i in review.issues if i.id == issue_id), None)
    if issue is None:
        raise ReviewError("That issue wasn't found.")
    return issue


def update_issue(db, review, issue_id, fields):
    _editable(review)
    issue = _mine(review, issue_id)
    for name, value in _clean({**_fields_of(issue), **fields}).items():
        setattr(issue, name, value)
    if "x" in fields or "y" in fields:
        issue.x, issue.y, issue.panel_id = _pin(issue.page, fields)
    issue.updated_at = utcnow()
    db.flush()
    return issue


def _fields_of(issue):
    return {"category": issue.category, "severity": issue.severity, "description": issue.description,
            "suggestion": issue.suggestion, "verse_ref": issue.verse_ref}


def delete_issue(db, review, issue_id):
    _editable(review)
    issue = _mine(review, issue_id)
    db.delete(issue)
    db.flush()
    db.expire(review, ["issues"])


def submit(db, review, now=None):
    _editable(review)
    done, total, todo = progress(review)
    if todo:
        names = [p.number for p in review.version.pages if p.id in todo]
        raise ReviewError("Please check every page first. Still to do: "
                          + ", ".join("cover" if n == 0 else f"page {n}" for n in names) + ".")
    review.state, review.submitted_at = "submitted", now or utcnow()
    db.flush()


def serialize_issue(issue):
    return {"id": issue.id, "page_id": issue.page_id, "panel_id": issue.panel_id,
            "panel_key": issue.panel.key if issue.panel else None, "x": issue.x, "y": issue.y,
            "category": issue.category, "severity": issue.severity, "description": issue.description,
            "suggestion": issue.suggestion, "verse_ref": issue.verse_ref}
