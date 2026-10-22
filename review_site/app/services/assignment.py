"""Choosing which chapter a reviewer gets next.

Rule (coverage first): among the chapters this reviewer is eligible for, take those with the fewest *submitted* reviews
of their current version, and pick one at random, preferring one nobody else is holding right now. Eligible chapters
are open, and not already reviewed or held by this reviewer on the same version. Preference tiers, best first:

  0. chapters this reviewer has never reviewed (and hasn't just skipped)
  1. chapters they reviewed in an *older* version (it has changed since)
  2. chapters they skipped in the last day (only if nothing else is left)

so a reviewer is only handed a chapter they've seen before when no unseen one exists."""
import random
from dataclasses import dataclass
from sqlalchemy import func, select, update

from ..models import Chapter, ChapterVersion, Meta, Review, utcnow


class NothingAvailable(Exception):
    """No chapter can be assigned right now; str(exc) is a friendly reason."""


class SkipLimit(Exception):
    pass


@dataclass
class Candidate:
    chapter: Chapter
    version: ChapterVersion
    reviews: int      # submitted, non-excluded reviews of the current version
    held: int         # other reviewers' live holds on the current version
    tier: int


def ensure_meta(db):
    if db.get(Meta, "claim_lock") is None:
        db.add(Meta(key="claim_lock", value=0))
        db.flush()


def _take_write_lock(db):
    """Serialise claims: bump a row first so the write lock is held before any candidate is read."""
    ensure_meta(db)
    db.execute(update(Meta).where(Meta.key == "claim_lock").values(value=Meta.value + 1))


def expire_stale(db, now=None):
    now = now or utcnow()
    result = db.execute(update(Review).where(Review.state == "active", Review.expires_at < now)
                        .values(state="expired"))
    return result.rowcount


def active_review(db, user):
    return db.scalar(select(Review).where(Review.user_id == user.id, Review.state == "active"))


def candidates(db, user, settings, now=None):
    """Every chapter this reviewer could be given, with its counts and preference tier."""
    now = now or utcnow()
    done = dict(db.execute(select(Review.version_id, func.count()).where(Review.state == "submitted",
                                                                          Review.excluded.is_(False))
                           .group_by(Review.version_id)).all())
    held = dict(db.execute(select(Review.version_id, func.count()).where(
        Review.state == "active", Review.expires_at >= now, Review.user_id != user.id)
        .group_by(Review.version_id)).all())
    mine = db.execute(select(Review.version_id, ChapterVersion.chapter_id, Review.state, Review.claimed_at)
                      .join(ChapterVersion, ChapterVersion.id == Review.version_id)
                      .where(Review.user_id == user.id)).all()
    same_version = {v for v, _, state, _ in mine if state in ("active", "submitted")}
    seen_chapters = {c for _, c, state, _ in mine if state == "submitted"}
    skipped = {c for _, c, state, at in mine if state == "skipped" and at >= now - settings.skip_window}
    out = []
    rows = db.execute(select(Chapter, ChapterVersion).join(
        ChapterVersion, (ChapterVersion.chapter_id == Chapter.id) & (ChapterVersion.state == "current"))
        .where(Chapter.status == "open").order_by(Chapter.sort_order)).all()
    for chapter, version in rows:
        if version.id in same_version:
            continue
        tier = 2 if chapter.id in skipped else 1 if chapter.id in seen_chapters else 0
        out.append(Candidate(chapter, version, done.get(version.id, 0), held.get(version.id, 0), tier))
    return out


def choose(options, rng=random):
    """The assignment rule on its own: best tier, then fewest reviews, then unheld, then random."""
    if not options:
        raise NothingAvailable("There are no chapters open for review right now. Please check back soon.")
    tier = min(o.tier for o in options)
    pool = [o for o in options if o.tier == tier]
    fewest = min(o.reviews for o in pool)
    pool = [o for o in pool if o.reviews == fewest]
    free = [o for o in pool if o.held == 0]
    return rng.choice(free or pool)


def claim(db, user, settings, now=None, rng=random):
    """Give the reviewer their chapter: the one they already hold, or a newly chosen one."""
    now = now or utcnow()
    _take_write_lock(db)
    expire_stale(db, now)
    existing = active_review(db, user)
    if existing:
        return existing
    options = candidates(db, user, settings, now)
    if not options:
        seen = db.scalar(select(func.count()).select_from(Review).where(Review.user_id == user.id,
                                                                         Review.state == "submitted"))
        raise NothingAvailable("You've reviewed every chapter that's currently open. Thank you!" if seen else
                               "There are no chapters open for review right now. Please check back soon.")
    pick = choose(options, rng)
    review = Review(user_id=user.id, version_id=pick.version.id, state="active", claimed_at=now,
                    last_activity_at=now, expires_at=now + settings.hold)
    db.add(review)
    db.flush()
    return review


def touch(review, settings, seconds=0, now=None):
    """Record activity: renew the hold and add up to a minute of working time."""
    now = now or utcnow()
    review.expires_at = now + settings.hold
    review.last_activity_at = now
    review.active_seconds += max(0, min(int(seconds), 60))


def skips_used(db, user, settings, now=None):
    now = now or utcnow()
    return db.scalar(select(func.count()).select_from(Review).where(
        Review.user_id == user.id, Review.state == "skipped", Review.claimed_at >= now - settings.skip_window))


def skip(db, review, settings, now=None):
    """Hand the chapter back. Limited to a few skips a day so reviewers can't cherry-pick."""
    now = now or utcnow()
    if review.state != "active":
        raise ValueError("This review isn't active")
    if skips_used(db, review.user, settings, now) >= settings.skip_limit:
        raise SkipLimit(f"You can skip {settings.skip_limit} chapters a day. Please review this one or come back tomorrow.")
    review.state = "skipped"
    review.claimed_at = now   # so the skip window counts from when it happened
    db.flush()
