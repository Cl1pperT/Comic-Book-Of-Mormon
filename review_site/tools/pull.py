"""Bring the issues you ACCEPTED on the review site back to this PC, in the formats the comic pipeline already reads.

    python -m review_site.tools.pull --server https://your-app.fly.dev --token TOKEN
    python -m review_site.tools.pull --dry-run          # show what would happen, change nothing

For each accepted issue not yet pulled:
  * art problems on a panel (character looks wrong, art error, continuity, layout) become a flag in that chapter's
    review/flags.json, exactly like a flag made in the draft reader, so the redraw step picks them up;
  * story problems on a panel (accuracy, missing/invented content, dialogue, spelling) are queued in
    runs/book/review-queue.json for the story job (use --no-queue to skip this);
  * anything else (whole-page reports, "other") is listed in the report only.
Issues whose page picture has changed on this PC since the reviewer saw it are left alone and listed as stale.
A summary is written to runs/book/site-issues-report.md. Pulled issues are marked as sent on the site."""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from bom_comic.book import Busy, writing_lock
from bom_comic.reader import COVER, chapters as find_chapters, flag, flags
from bom_comic.storage import Store

from ..app.constants import CATEGORY_LABELS, SEVERITY_LABELS

ART = {"character_consistency", "location_continuity", "art_error", "layout_readability"}
STORY = {"scripture_accuracy", "missing_content", "invented_content", "dialogue_attribution", "spelling_grammar"}
QUEUE = "review-queue.json"


def note_line(issue):
    text = f"[Site #{issue['id']} · {SEVERITY_LABELS[issue['severity']]} · {CATEGORY_LABELS[issue['category']]}] {issue['description']}"
    if issue["suggestion"]:
        text += f" Suggested: {issue['suggestion']}"
    if issue["verse_ref"]:
        text += f" (Verse: {issue['verse_ref']})"
    return text


def classify(issue):
    """'flag' (redraw), 'story' (rewrite the scene or guide), or 'report' (human decides)."""
    if issue["panel"] is None:
        return "report"
    if issue["category"] in ART:
        return "flag"
    if issue["category"] in STORY:
        return "story"
    return "report"


def page_matches(store, issue):
    path = store.path(f"pages/page_{issue['page']:03d}.png")
    if not path.exists():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == issue["page_image_hash"]


def queue_entry(issue):
    if issue["panel"] == COVER:   # the cover has no scene; story fixes go to the chapter's reader's guide
        return {"book": issue["book"], "chapter": issue["chapter_number"], "guide": note_line(issue)}
    return {"book": issue["book"], "chapter": issue["chapter_number"], "scene_id": issue["scene_id"],
            "problem": f"{issue['description']} (reported by a site reviewer, issue #{issue['id']})",
            "fix": issue["suggestion"] or f"Make the scene match the verses it cites ({issue['refs'] or 'see scene'})."}


def plan(issues, stores):
    """Decide each issue's fate without touching anything. Returns {id: (action, reason)}."""
    out = {}
    for issue in issues:
        store = stores.get(issue["chapter"])
        action = classify(issue)
        if store is None:
            out[issue["id"]] = ("missing", "chapter not found in runs/book")
        elif not page_matches(store, issue):
            out[issue["id"]] = ("stale", "the page picture changed on this PC after it was reviewed")
        else:
            out[issue["id"]] = (action, "")
    return out


def apply(issues, decisions, stores, root, queue=True):
    """Write flags and the story queue. Returns the ids that were handled (and may be marked as sent)."""
    handled, by_chapter = [], {}
    for issue in issues:
        if decisions[issue["id"]][0] == "flag":
            by_chapter.setdefault(issue["chapter"], []).append(issue)
    for slug, items in by_chapter.items():
        store = stores[slug]
        for panel in sorted({i["panel"] for i in items}):
            existing = flags(store).get(panel, {}).get("note", "")
            lines = [note_line(i) for i in items if i["panel"] == panel and f"[Site #{i['id']} " not in existing]
            if lines:
                flag(store, panel, "\n".join(filter(None, [existing, *lines])))
        handled.extend(i["id"] for i in items)
    stories = [i for i in issues if decisions[i["id"]][0] == "story"]
    if stories and queue:
        path = Path(root) / QUEUE
        try:
            with writing_lock(root):
                current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
                for issue in stories:
                    entry = queue_entry(issue)
                    if entry not in current:
                        current.append(entry)
                path.write_text(json.dumps(current, indent=1, ensure_ascii=False), encoding="utf-8")
            handled.extend(i["id"] for i in stories)
        except Busy as exc:
            print(f"Could not queue story fixes, another job is writing ({exc}). Run pull again later.")
    return handled


def report(issues, decisions, root):
    heads = {"flag": "Flagged for redraw", "story": "Queued for the story job", "report": "Needs your decision",
             "stale": "Stale (page changed since it was reviewed)", "missing": "Chapter missing here"}
    lines = [f"# Issues pulled from the review site, {datetime.now():%Y-%m-%d %H:%M}", ""]
    for action, title in heads.items():
        items = [i for i in issues if decisions[i["id"]][0] == action]
        if not items:
            continue
        lines += [f"## {title} ({len(items)})", ""]
        for i in sorted(items, key=lambda i: (i["chapter"], i["page"], i["id"])):
            where = "cover" if i["page"] == 0 else f"page {i['page']}"
            lines.append(f"- **{i['book']} {i['chapter_number']}**, {where}"
                         f"{', ' + i['panel'] if i['panel'] else ''} · site #{i['id']}: {note_line(i)}")
            if decisions[i["id"]][1]:
                lines.append(f"  - {decisions[i['id']][1]}")
        lines.append("")
    path = Path(root) / "site-issues-report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", default="runs/book")
    parser.add_argument("--server", default=os.environ.get("REVIEW_SITE_URL"))
    parser.add_argument("--token", default=os.environ.get("REVIEW_TOOL_TOKEN"))
    parser.add_argument("--no-queue", action="store_true", help="don't queue story fixes for the story job")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not (args.server and args.token):
        sys.exit("Give --server and --token (or set REVIEW_SITE_URL and REVIEW_TOOL_TOKEN).")
    import httpx
    client = httpx.Client(base_url=args.server.rstrip("/"), headers={"Authorization": f"Bearer {args.token}"}, timeout=60)
    response = client.get("/api/tool/issues")
    response.raise_for_status()
    issues = response.json()["issues"]
    if not issues:
        print("Nothing new: no accepted issues waiting.")
        return
    stores = find_chapters(Store(Path(args.runs)))
    decisions = plan(issues, stores)
    counts = {}
    for action, _ in decisions.values():
        counts[action] = counts.get(action, 0) + 1
    print(f"{len(issues)} accepted issues:", ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    if args.dry_run:
        for issue in issues:
            print(f"  #{issue['id']} {issue['chapter']} {issue['panel'] or 'page'} -> {decisions[issue['id']][0]}")
        return
    handled = apply(issues, decisions, stores, args.runs, queue=not args.no_queue)
    # Whole-page reports and stale ones stay "unsent" so they keep appearing until you resolve them on the site;
    # they are in the report either way.
    path = report(issues, decisions, args.runs)
    if handled:
        client.post("/api/tool/issues/exported", json={"ids": handled}).raise_for_status()
    print(f"Marked {len(handled)} as sent. Report: {path}")


if __name__ == "__main__":
    main()
