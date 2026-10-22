"""The pull tool, run against a throwaway copy of one real assembled chapter (skipped if runs/book isn't here)."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from bom_comic.reader import chapters as find_chapters, flags
from bom_comic.storage import Store
from review_site.tools import pull

SOURCE = Path("runs/book/1-nephi/001")
pytestmark = pytest.mark.skipif(not (SOURCE / "final/manifest.json").exists(), reason="no assembled chapter here")


@pytest.fixture
def sandbox(tmp_path):
    target = tmp_path / "1-nephi" / "001"
    (target / "final").mkdir(parents=True)
    (target / "pages").mkdir()
    shutil.copy(SOURCE / "final/manifest.json", target / "final/manifest.json")
    shutil.copy(SOURCE / "status.json", target / "status.json")
    for page in (SOURCE / "pages").glob("page_*.png"):
        shutil.copy(page, target / "pages" / page.name)
    return tmp_path


def issue(sandbox, **over):
    page = sandbox / "1-nephi/001/pages/page_001.png"
    base = {"id": 1, "chapter": "1-nephi/001", "book": "1 Nephi", "chapter_number": 1, "version": 1,
            "version_is_current": True, "page": 1, "page_image_hash": hashlib.sha256(page.read_bytes()).hexdigest(),
            "panel": "panel_001", "scene_id": "scene_001", "x": 0.5, "y": 0.2, "category": "art_error",
            "severity": "major", "description": "Modern lamp on the wall", "suggestion": "Remove it", "verse_ref": "",
            "refs": "1 Nephi 1:1-3", "note": "", "reported_at": "2026-10-21T00:00:00+00:00"}
    return {**base, **over}


def run(sandbox, issues, queue=True):
    stores = find_chapters(Store(sandbox))
    decisions = pull.plan(issues, stores)
    handled = pull.apply(issues, decisions, stores, sandbox, queue=queue)
    return stores, decisions, handled


def test_art_issue_becomes_a_reader_flag_and_is_idempotent(sandbox):
    stores, decisions, handled = run(sandbox, [issue(sandbox)])
    assert decisions[1][0] == "flag" and handled == [1]
    flag = flags(stores["1-nephi/001"])["panel_001"]
    assert "Modern lamp" in flag["note"] and flag["image_hash"] and "[Site #1 " in flag["note"]
    run(sandbox, [issue(sandbox)])    # pulling it again must not duplicate the line
    assert flags(find_chapters(Store(sandbox))["1-nephi/001"])["panel_001"]["note"].count("[Site #1 ") == 1


def test_two_issues_on_one_panel_share_a_flag(sandbox):
    run(sandbox, [issue(sandbox), issue(sandbox, id=2, description="Extra finger")])
    note = flags(find_chapters(Store(sandbox))["1-nephi/001"])["panel_001"]["note"]
    assert "Modern lamp" in note and "Extra finger" in note


def test_story_issue_is_queued(sandbox):
    _, decisions, handled = run(sandbox, [issue(sandbox, category="scripture_accuracy", description="Wrong speaker")])
    assert decisions[1][0] == "story" and handled == [1]
    queue = json.loads((sandbox / "review-queue.json").read_text(encoding="utf-8"))
    assert queue == [{"book": "1 Nephi", "chapter": 1, "scene_id": "scene_001",
                      "problem": "Wrong speaker (reported by a site reviewer, issue #1)", "fix": "Remove it"}]


def test_story_issues_can_be_left_out(sandbox):
    _, _, handled = run(sandbox, [issue(sandbox, category="missing_content")], queue=False)
    assert handled == [] and not (sandbox / "review-queue.json").exists()


def test_page_level_stale_and_unknown_are_reported_not_applied(sandbox):
    page_level = issue(sandbox, id=3, panel=None, scene_id="")
    stale = issue(sandbox, id=4, page_image_hash="0" * 64)
    unknown = issue(sandbox, id=5, chapter="nope/001")
    _, decisions, handled = run(sandbox, [page_level, stale, unknown])
    assert [decisions[i][0] for i in (3, 4, 5)] == ["report", "stale", "missing"] and handled == []
    text = pull.report([page_level, stale, unknown], decisions, sandbox).read_text(encoding="utf-8")
    assert "Needs your decision" in text and "Stale" in text
