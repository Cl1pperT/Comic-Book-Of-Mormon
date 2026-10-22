"""Shared fixtures: an in-memory app, tiny synthetic chapters, and helpers to log in."""
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from review_site.app.config import Settings
from review_site.app.main import create_app
from review_site.app.services import accounts
from review_site.app.services.versions import ChapterPayload

WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-image-bytes"


def make_payload(slug="alma/001", book="Alma", number=1, sort_order=100, pages=3, seed="a", verses=None):
    """A chapter with a cover (page 0) and `pages` numbered pages, each with two stacked panels."""
    def page(n):
        digest = hashlib.sha256(f"{seed}-{slug}-{n}".encode()).hexdigest()
        return {"number": n, "label": "", "image_hash": digest, "width": 1400, "height": 2000,
                "panels": [{"key": "cover" if n == 0 else "panel_%03d" % (n * 2 - 1), "scene_id": f"scene_{n:03d}",
                            "refs_label": f"{book} {number}:{n + 1}", "refs": [f"{book} {number}:{n + 1}"],
                            "frame": [0.0, 0.0, 1.0, 0.5]},
                           {"key": "panel_%03d" % (n * 2), "scene_id": f"scene_{n:03d}b",
                            "refs_label": f"{book} {number}:{n + 2}", "refs": [f"{book} {number}:{n + 2}"],
                            "frame": [0.0, 0.5, 1.0, 0.5]}]}
    return ChapterPayload(slug=slug, book=book, number=number, sort_order=sort_order,
                          verses=verses or {f"{book} {number}:{v}": f"Verse {v} text." for v in range(1, pages + 4)},
                          pages=[page(n) for n in range(pages + 1)])


def publish(app, payload):
    from review_site.app.services.versions import import_chapter
    with app.state.session_factory() as db:
        for page in payload.pages:
            if not app.state.media.has(page.image_hash):
                app.state.media.save(page.image_hash, WEBP)
        result = import_chapter(db, app.state.media, payload)
        db.commit()
    return result


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, database_url="sqlite://", secret_key="test-secret", tool_token="tool-token")


@pytest.fixture
def app(settings):
    application = create_app(settings, create_tables=True)
    from review_site.app.services.assignment import ensure_meta
    with application.state.session_factory() as db:
        ensure_meta(db)
        db.commit()
    return application


@pytest.fixture
def db(app):
    with app.state.session_factory() as session:
        yield session


@pytest.fixture
def make_user(app):
    def make(email="r@example.com", name="Reviewer", role="reviewer", credit=False):
        with app.state.session_factory() as db:
            user = accounts.create_user(db, email, name, "correct-horse-battery", role=role, credit_ok=credit)
            db.commit()
            return user.id
    return make


def csrf_from(response):
    match = re.search(r'name="csrf" value="([^"]+)"', response.text) or re.search(r'name="csrf" content="([^"]+)"', response.text)
    return match.group(1) if match else None


@pytest.fixture
def login(app):
    def do(email="r@example.com", password="correct-horse-battery"):
        client = TestClient(app, follow_redirects=False)
        token = csrf_from(client.get("/login"))
        response = client.post("/login", data={"email": email, "password": password, "csrf": token})
        assert response.status_code == 303, response.text
        page = client.get("/")
        client.csrf = csrf_from(page)   # any page with a form carries the session's token
        client.headers["X-CSRF-Token"] = client.csrf or ""
        return client
    return do


def open_all(app):
    from sqlalchemy import update
    from review_site.app.models import Chapter
    with app.state.session_factory() as db:
        db.execute(update(Chapter).values(status="open"))
        db.commit()
