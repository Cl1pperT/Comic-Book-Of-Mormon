import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from review_site.app.models import Chapter, utcnow
from review_site.app.services import accounts, assignment, reviews
from .conftest import make_payload, open_all, publish


@pytest.fixture
def book(app):
    for n in range(1, 7):
        publish(app, make_payload(slug=f"alma/{n:03d}", number=n, sort_order=n))
    open_all(app)


def users(db, count):
    out = []
    for i in range(count):
        out.append(accounts.create_user(db, f"u{i}@example.com", f"User {i}", "correct-horse-battery"))
    db.flush()
    return out


def finish(db, review):
    review.state, review.submitted_at = "submitted", utcnow()
    db.flush()


def test_first_reviews_cover_every_chapter_before_any_gets_a_second(app, db, book, settings):
    people = users(db, 12)
    seen = []
    for person in people:
        review = assignment.claim(db, person, settings, rng=random.Random(person.id))
        seen.append(review.version.chapter.slug)
        finish(db, review)
    # After the first six reviewers every chapter has exactly one; the next six produce exactly one more each.
    assert sorted(seen[:6]) == [f"alma/{n:03d}" for n in range(1, 7)]
    assert sorted(seen[6:]) == [f"alma/{n:03d}" for n in range(1, 7)]


def test_never_the_same_version_twice_and_clear_message_when_done(app, db, book, settings):
    person, = users(db, 1)
    for _ in range(6):
        finish(db, assignment.claim(db, person, settings))
    with pytest.raises(assignment.NothingAvailable) as info:
        assignment.claim(db, person, settings)
    assert "reviewed every chapter" in str(info.value)


def test_claim_is_idempotent_while_holding(app, db, book, settings):
    person, = users(db, 1)
    first = assignment.claim(db, person, settings)
    assert assignment.claim(db, person, settings).id == first.id


def test_prefers_unheld_chapters_among_equals(app, db, book, settings):
    a, b = users(db, 2)
    first = assignment.claim(db, a, settings)
    for seed in range(20):
        second = assignment.choose(assignment.candidates(db, b, settings), random.Random(seed))
        assert second.chapter.slug != first.version.chapter.slug


def test_lowest_count_wins_over_unheld(app, db, book, settings):
    a, b, c = users(db, 3)
    done = assignment.claim(db, a, settings)
    finish(db, done)    # one chapter now has a review
    options = assignment.candidates(db, b, settings)
    pick = assignment.choose(options, random.Random(1))
    assert pick.reviews == 0 and pick.chapter.slug != done.version.chapter.slug


def test_expired_hold_returns_chapter_to_the_pool(app, db, book, settings):
    a, = users(db, 1)
    review = assignment.claim(db, a, settings)
    review.expires_at = utcnow() - timedelta(minutes=1)
    db.flush()
    assert assignment.expire_stale(db) == 1
    assert review.state == "expired"
    assert assignment.active_review(db, a) is None


def test_skip_limit_and_skipped_chapter_comes_last(app, db, book, settings):
    a, = users(db, 1)
    skipped = []
    for _ in range(settings.skip_limit):
        review = assignment.claim(db, a, settings)
        skipped.append(review.version.chapter.slug)
        assignment.skip(db, review, settings)
    review = assignment.claim(db, a, settings)
    assert review.version.chapter.slug not in skipped    # three others remain, so skipped ones wait
    with pytest.raises(assignment.SkipLimit):
        assignment.skip(db, review, settings)


def test_closed_chapters_are_not_assigned(app, db, book, settings):
    db.execute(Chapter.__table__.update().values(status="closed"))
    a, = users(db, 1)
    with pytest.raises(assignment.NothingAvailable):
        assignment.claim(db, a, settings)


def test_excluded_reviews_do_not_count(app, db, book, settings):
    a, b = users(db, 2)
    first = assignment.claim(db, a, settings)
    finish(db, first)
    first.excluded = True
    db.flush()
    assert all(o.reviews == 0 for o in assignment.candidates(db, b, settings))


def test_new_version_resets_counts_and_lets_earlier_reviewers_back_in_last(app, db, book, settings):
    from review_site.app.services import versions
    a, b = users(db, 2)
    for _ in range(6):
        finish(db, assignment.claim(db, a, settings))
    chapter = db.scalar(select(Chapter).where(Chapter.slug == "alma/001"))
    publish_changed = make_payload(slug="alma/001", number=1, sort_order=1, seed="changed")
    for page in publish_changed.pages:
        app.state.media.save(page.image_hash, b"RIFF\x00\x00\x00\x00WEBPxxxx")
    versions.import_chapter(db, app.state.media, publish_changed)
    db.refresh(chapter)
    db.expire(chapter, ["versions"])
    versions.make_live(db, chapter.pending, "full", None)
    options = assignment.candidates(db, a, settings)
    assert [o.chapter.slug for o in options] == ["alma/001"]
    assert options[0].tier == 1 and options[0].reviews == 0
    assert [o.tier for o in assignment.candidates(db, b, settings)] == [0] * 6


def test_review_progress_requires_every_page(app, db, book, settings):
    a, = users(db, 1)
    review = assignment.claim(db, a, settings)
    done, total, todo = reviews.progress(review)
    assert (done, total) == (0, 4)
    with pytest.raises(reviews.ReviewError):
        reviews.submit(db, review)
    for page in review.version.pages[:3]:
        reviews.set_page_checked(db, review, page.id, True)
    last = review.version.pages[3]
    reviews.add_issue(db, review, settings, {"page_id": last.id, "category": "art_error", "severity": "minor",
                                             "description": "Extra finger", "x": 0.5, "y": 0.75})
    reviews.submit(db, review)
    assert review.state == "submitted"


def chooser(db, email="pick@example.com"):
    return accounts.create_user(db, email, "Picky", "correct-horse-battery", can_choose=True)


def test_only_pre_approved_users_can_choose(app, db, book, settings):
    plain, = users(db, 1)
    chapter = db.scalar(select(Chapter).where(Chapter.slug == "alma/003"))
    with pytest.raises(assignment.NotAllowed):
        assignment.claim_chapter(db, plain, settings, chapter.id)
    picky = chooser(db)
    review = assignment.claim_chapter(db, picky, settings, chapter.id)
    assert review.version.chapter.slug == "alma/003" and review.state == "active"


def test_choosing_keeps_the_independence_rules(app, db, book, settings):
    picky = chooser(db)
    chapter = db.scalar(select(Chapter).where(Chapter.slug == "alma/002"))
    other = db.scalar(select(Chapter).where(Chapter.slug == "alma/004"))
    first = assignment.claim_chapter(db, picky, settings, chapter.id)
    assert assignment.claim_chapter(db, picky, settings, chapter.id).id == first.id      # same chapter again: same review
    with pytest.raises(assignment.NothingAvailable, match="in progress"):
        assignment.claim_chapter(db, picky, settings, other.id)                           # one at a time
    finish(db, first)
    with pytest.raises(assignment.NothingAvailable, match="already reviewed"):
        assignment.claim_chapter(db, picky, settings, chapter.id)                         # never the same version twice
    assert assignment.claim_chapter(db, picky, settings, other.id).state == "active"


def test_cannot_choose_a_closed_chapter(app, db, book, settings):
    picky = chooser(db)
    chapter = db.scalar(select(Chapter).where(Chapter.slug == "alma/005"))
    chapter.status = "closed"
    db.flush()
    with pytest.raises(assignment.NothingAvailable, match="isn't open"):
        assignment.claim_chapter(db, picky, settings, chapter.id)


def test_people_who_can_choose_are_not_limited_in_skips(app, db, book, settings):
    picky = chooser(db)
    for _ in range(settings.skip_limit + 2):
        review = assignment.claim(db, picky, settings)
        assignment.skip(db, review, settings)


def test_pickable_lists_every_open_chapter_with_standing(app, db, book, settings):
    picky = chooser(db)
    chapter = db.scalar(select(Chapter).where(Chapter.slug == "alma/001"))
    finish(db, assignment.claim_chapter(db, picky, settings, chapter.id))
    rows = {r["chapter"].slug: r for r in assignment.pickable(db, picky, settings)}
    assert len(rows) == 6 and rows["alma/001"]["standing"] == "done" and rows["alma/002"]["standing"] == ""
    assert rows["alma/001"]["reviews"] == 1
