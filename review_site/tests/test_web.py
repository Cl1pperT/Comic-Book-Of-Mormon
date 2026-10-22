import json

from fastapi.testclient import TestClient

from review_site.app.models import Chapter, Issue, Review
from review_site.app.services import accounts
from .conftest import WEBP, csrf_from, make_payload, open_all, publish


def seed(app, count=3):
    for n in range(1, count + 1):
        publish(app, make_payload(slug=f"alma/{n:03d}", number=n, sort_order=n))
    open_all(app)


def start_review(client):
    response = client.post("/claim", data={"csrf": client.csrf})
    assert response.status_code == 303, response.text
    review_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/review/{review_id}")
    assert page.status_code == 200
    data = json.loads(page.text.split('id="review-data" type="application/json">')[1].split("</script>")[0])
    return review_id, data


def test_join_with_invite_then_login(app, make_user):
    admin_id = make_user("admin@example.com", "Admin", role="admin")
    with app.state.session_factory() as db:
        from review_site.app.models import User
        invite = accounts.create_invite(db, db.get(User, admin_id), max_uses=1)
        db.commit()
        code = invite.code
    client = TestClient(app, follow_redirects=False)
    token = csrf_from(client.get("/join"))
    bad = client.post("/join", data={"csrf": token, "code": "WRONG", "email": "a@example.com",
                                     "display_name": "Ann", "password": "long-enough-pass"})
    assert bad.status_code == 400 and "invite code isn" in bad.text
    ok = client.post("/join", data={"csrf": token, "code": code, "email": "a@example.com", "display_name": "Ann",
                                    "password": "long-enough-pass", "credit_ok": "1"})
    assert ok.status_code == 303
    assert "Hello, Ann" in client.get("/").text
    reuse = TestClient(app, follow_redirects=False)
    token = csrf_from(reuse.get("/join"))
    again = reuse.post("/join", data={"csrf": token, "code": code, "email": "b@example.com", "display_name": "Bob",
                                      "password": "long-enough-pass"})
    assert again.status_code == 400


def test_pages_need_login_and_forms_need_csrf(app, make_user, login):
    make_user()
    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.get("/").status_code == 303
    assert anonymous.get("/admin").status_code == 303
    client = login()
    assert client.post("/claim", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert TestClient(app, follow_redirects=False).post("/login", data={"email": "r@example.com"}).status_code == 403
    assert client.get("/admin").status_code == 403    # reviewers can't see admin screens


def test_full_review_flow(app, make_user, login):
    seed(app, 2)
    make_user()
    client = login()
    review_id, data = start_review(client)
    assert len(data["pages"]) == 4 and data["progress"] == {"done": 0, "total": 4}
    # Submitting early is refused with a helpful message.
    early = client.post(f"/api/review/{review_id}/submit")
    assert early.status_code == 400 and "every page" in early.json()["error"]
    # Report an issue with a pin; the server finds the panel.
    page = data["pages"][1]
    created = client.post(f"/api/review/{review_id}/issues", json={
        "page_id": page["id"], "category": "art_error", "severity": "major", "description": "Six fingers",
        "suggestion": "Redraw hand", "x": 0.4, "y": 0.8})
    assert created.status_code == 200, created.text
    issue = created.json()["issue"]
    assert issue["panel_id"] == page["panels"][1]["id"]    # lower half of the page
    # Pages with issues count as checked; the rest are ticked off.
    for p in data["pages"]:
        if p["id"] != page["id"]:
            assert client.put(f"/api/review/{review_id}/pages/{p['id']}/check", json={"checked": True}).status_code == 200
    # Can't mark a page with issues as 'looks good'.
    assert client.put(f"/api/review/{review_id}/pages/{page['id']}/check", json={"checked": True}).status_code == 400
    # Edit the issue.
    edited = client.put(f"/api/review/{review_id}/issues/{issue['id']}", json={
        "category": "art_error", "severity": "blocking", "description": "Six fingers on left hand", "x": 0.4, "y": 0.8})
    assert edited.json()["issue"]["severity"] == "blocking"
    done = client.post(f"/api/review/{review_id}/submit")
    assert done.status_code == 200 and done.json()["redirect"].endswith("/done")
    assert "submitted" in client.get(done.json()["redirect"]).text
    # Locked afterwards.
    locked = client.post(f"/api/review/{review_id}/issues", json={
        "page_id": page["id"], "category": "other", "severity": "minor", "description": "late"})
    assert locked.status_code == 400
    # And a second claim gives a different chapter.
    second_id, _ = start_review(client)
    assert second_id != review_id
    with app.state.session_factory() as db:
        assert {r.version.chapter.slug for r in db.query(Review).all()} == {"alma/001", "alma/002"}


def test_issue_validation(app, make_user, login):
    seed(app, 1)
    make_user()
    client = login()
    review_id, data = start_review(client)
    page_id = data["pages"][0]["id"]
    base = {"page_id": page_id, "category": "other", "severity": "minor", "description": "x"}
    for patch, fragment in [({"category": "nope"}, "kind of problem"), ({"severity": ""}, "how serious"),
                            ({"description": "  "}, "describe"), ({"x": 2.0, "y": 0.5}, "off the page"),
                            ({"page_id": 99999}, "isn't part")]:
        response = client.post(f"/api/review/{review_id}/issues", json={**base, **patch})
        assert response.status_code == 400 and fragment in response.json()["error"], (patch, response.text)


def test_reviewers_cannot_see_each_others_work(app, make_user, login):
    seed(app, 1)
    make_user("a@example.com", "Ann")
    make_user("b@example.com", "Bob")
    ann = login("a@example.com")
    review_id, data = start_review(ann)
    created = ann.post(f"/api/review/{review_id}/issues", json={
        "page_id": data["pages"][0]["id"], "category": "other", "severity": "minor", "description": "secret note"})
    issue_id = created.json()["issue"]["id"]
    bob = login("b@example.com")
    assert bob.get(f"/review/{review_id}").status_code == 404
    assert bob.put(f"/api/review/{review_id}/issues/{issue_id}", json={
        "category": "other", "severity": "minor", "description": "hacked"}).status_code == 404
    assert bob.delete(f"/api/review/{review_id}/issues/{issue_id}").status_code == 404
    assert bob.get("/admin/issues").status_code == 403
    bob_review, bob_data = start_review(bob)
    assert "secret note" not in json.dumps(bob_data)


def test_media_requires_login(app, make_user, login):
    seed(app, 1)
    make_user()
    client = login()
    _, data = start_review(client)
    url = data["pages"][0]["image"]
    assert client.get(url).status_code == 200
    assert client.get(url).headers["content-type"] == "image/webp"
    assert TestClient(app, follow_redirects=False).get(url).status_code in (303, 401)
    assert client.get("/media/pages/" + "0" * 64 + ".webp").status_code == 404
    assert client.get("/media/pages/..%2F..%2Fapp.db").status_code == 404


def test_admin_triage_flow(app, make_user, login):
    seed(app, 1)
    make_user("admin@example.com", "Admin", role="admin")
    make_user("r@example.com", "Rita", credit=True)
    reviewer = login("r@example.com")
    review_id, data = start_review(reviewer)
    for n, text in enumerate(["Wrong robe colour", "Wrong robe colour on Nephi"]):
        reviewer.post(f"/api/review/{review_id}/issues", json={
            "page_id": data["pages"][1]["id"], "category": "character_consistency", "severity": "moderate",
            "description": text, "x": 0.2, "y": 0.2})
    for p in data["pages"]:
        reviewer.put(f"/api/review/{review_id}/pages/{p['id']}/check", json={"checked": True})
    # Not visible to editors until submitted.
    admin = login("admin@example.com")
    assert "Wrong robe colour" not in admin.get("/admin/issues").text
    assert reviewer.post(f"/api/review/{review_id}/submit").status_code == 200
    page = admin.get("/admin/issues")
    assert "Wrong robe colour" in page.text and "Rita" in page.text
    with app.state.session_factory() as db:
        ids = [i.id for i in db.query(Issue).order_by(Issue.id)]
    detail = admin.get(f"/admin/issues/{ids[0]}")
    assert detail.status_code == 200 and "Other reports on this page" in detail.text
    admin.post(f"/admin/issues/{ids[0]}", data={"csrf": admin.csrf, "status": "accepted", "note": "Yes, fix"})
    admin.post(f"/admin/issues/{ids[1]}", data={"csrf": admin.csrf, "status": "duplicate", "duplicate_of": str(ids[0])})
    with app.state.session_factory() as db:
        first, second = db.get(Issue, ids[0]), db.get(Issue, ids[1])
        assert (first.status, second.status, second.duplicate_of_id) == ("accepted", "duplicate", ids[0])
        assert [e.to_status for e in first.events] == ["accepted"]
    assert admin.post(f"/admin/issues/{ids[1]}", data={"csrf": admin.csrf, "status": "duplicate"}).status_code == 303
    csv = admin.get("/admin/issues.csv?status=accepted").text
    assert "Wrong robe colour" in csv and "Wrong robe colour on Nephi" not in csv
    assert "Rita" in admin.get("/admin/contributors").text and "Rita" in admin.get("/thanks").text
    assert "Alma 1" in admin.get("/admin").text
    for path in ("/admin/chapters", "/admin/reviews", "/admin/users", "/admin/audit", "/admin/chapter?slug=alma/001"):
        assert admin.get(path).status_code == 200, path


def test_chapters_start_closed_and_open_on_command(app, make_user, login):
    publish(app, make_payload(slug="alma/001", number=1, sort_order=1))
    make_user("admin@example.com", "Admin", role="admin")
    make_user("r@example.com", "Rita")
    reviewer = login("r@example.com")
    response = reviewer.post("/claim", data={"csrf": reviewer.csrf})
    assert response.headers["location"] == "/"          # nothing open yet
    admin = login("admin@example.com")
    admin.post("/admin/chapters/status", data={"csrf": admin.csrf, "status": "open", "scope": "all"})
    assert reviewer.post("/claim", data={"csrf": reviewer.csrf}).headers["location"].startswith("/review/")


def test_new_version_flow_through_the_admin(app, make_user, login):
    seed(app, 1)
    make_user("admin@example.com", "Admin", role="admin")
    changed = make_payload(slug="alma/001", number=1, sort_order=1)
    changed.pages[2].image_hash = "f" * 64
    publish(app, changed)
    admin = login("admin@example.com")
    detail = admin.get("/admin/chapter?slug=alma/001").text
    assert "A new version is waiting (v2)" in detail and "<strong>2</strong>" in detail
    admin.post("/admin/chapter/version", data={"csrf": admin.csrf, "slug": "alma/001", "action": "live",
                                               "scope": "changed_pages"})
    with app.state.session_factory() as db:
        chapter = db.query(Chapter).one()
        assert chapter.current.number == 2 and chapter.current.review_scope == "changed_pages"
        assert [p.number for p in chapter.current.pages if p.changed] == [2]
        assert chapter.pending is None


def test_tool_api_publish_and_pull(app, make_user, login):
    client = TestClient(app)
    auth = {"Authorization": "Bearer tool-token"}
    payload = make_payload(slug="alma/009", number=9, sort_order=9)
    assert client.post("/api/tool/plan", json={"hashes": []}).status_code == 401
    hashes = [p.image_hash for p in payload.pages]
    missing = client.post("/api/tool/plan", headers=auth, json={"hashes": hashes}).json()["missing"]
    assert sorted(missing) == sorted(hashes)
    assert client.put(f"/api/tool/image/{hashes[0]}", headers=auth, content=b"not a webp").status_code == 400
    assert client.post("/api/tool/chapter", headers=auth, content=payload.model_dump_json(),
                       ).status_code in (400, 415, 422)
    for h in hashes:
        assert client.put(f"/api/tool/image/{h}", headers=auth, content=WEBP).status_code == 200
    result = client.post("/api/tool/chapter", headers={**auth, "Content-Type": "application/json"},
                         content=payload.model_dump_json())
    assert result.json() == {"status": "created", "version": 1}
    again = client.post("/api/tool/chapter", headers={**auth, "Content-Type": "application/json"},
                        content=payload.model_dump_json())
    assert again.json()["status"] == "unchanged"
    assert client.get("/api/tool/issues", headers=auth).json() == {"issues": []}
    backup = client.get("/api/tool/backup", headers=auth)
    assert backup.status_code == 200 and backup.content.startswith(b"SQLite format 3")
