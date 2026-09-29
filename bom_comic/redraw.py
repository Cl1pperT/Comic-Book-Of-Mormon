"""Redraw panels a reviewer flagged in the reader, before any new panels are drawn.

A flag is pending while its chapter's current image is still the one that was flagged. Redrawing turns the note
into image-model instructions (one small text-model call, cached on the flag), draws the panel again with a new
seed and those instructions, records an AUTOMATED image approval, and reassembles the chapter. The flag then moves
to review/flag-history.json, and the reader marks the panel "Redrawn" so the reviewer looks again; flagging it again
starts another round.
"""
import hashlib
import json
from datetime import datetime, timezone
from .comic import _sort_clauses
from .models import Corrections
from .pipeline import Pipeline
from .reader import FLAGS, chapters, flags
from .storage import Store

HISTORY = "review/flag-history.json"
PROMPT = """A reviewer flagged this drawn comic panel. Turn the reviewer's note into short instructions for an
image-generation model that will redraw the panel.
"add": things the new image must show, each a short positive visual phrase (never "no ..." or "not ...").
"avoid": things to leave out of the image, each just the thing itself (e.g. "baseball caps", "a second Lehi").
Stay within the panel's approved action and people below; never add events, people, or text to the image.
"""


def pending(root):
    """[(chapter store, panel_id, flag)] for flags whose panel still shows the flagged image, in book order."""
    out = []
    for chapter in chapters(Store(root)).values():
        for panel_id, flag in sorted(flags(chapter).items()):
            record = chapter.path(f"images/{panel_id}.json")
            if record.exists() and json.loads(record.read_text(encoding="utf-8"))["image_hash"] == flag["image_hash"]:
                out.append((chapter, panel_id, flag))
    return out


def history(store):
    path = store.path(HISTORY)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def corrections(text_provider, panel, note):
    """The reviewer's note as image-model instructions; without a text model, a best-effort split of the note."""
    if text_provider is not None:
        request = {"note": note, "approved_action": panel.action, "visible_people": panel.characters_visible,
                   "location": panel.location}
        tag = f"correct_{panel.panel_id}_{hashlib.sha256(note.encode()).hexdigest()[:10]}"
        return text_provider.structured(PROMPT + json.dumps(request), Corrections, tag).model_dump()
    add, avoid = [], []
    _sort_clauses([note], add, avoid)
    return {"add": add, "avoid": avoid}


def redraw(store, panel_id, flag, image_provider, text_provider=None):
    """Redraw one flagged panel and reassemble its chapter; returns the history entry."""
    pipeline = Pipeline(store, image_provider)
    panel = next(p for p in pipeline.panels() if p.panel_id == panel_id)
    fixes = flag.get("corrections") or corrections(text_provider, panel, flag["note"])
    pipeline.generate(panel_id, references=False, corrections={panel_id: fixes})
    record = pipeline.image_record(panel)
    pipeline.review("image", panel_id, "approve",
                    "AUTOMATED redraw after reviewer flag: " + flag["note"] + ". Review the new drawing in the reader.")
    pipeline.assemble()
    entry = {"panel_id": panel_id, "note": flag["note"], "flagged": flag.get("time"), "corrections": fixes,
             "old_image": flag["image_path"], "new_image": record["path"], "new_image_hash": record["image_hash"],
             "redrawn": datetime.now(timezone.utc).isoformat()}
    store.write(HISTORY, history(store) + [entry])
    current = flags(store)
    current.pop(panel_id, None)
    path = store.path(FLAGS)
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    store.event("redraw", panel_id=panel_id, note=flag["note"], image=record["path"])
    return entry


def redrawn(store):
    """Panels showing a redraw the reviewer hasn't flagged again: the reader's "Redrawn" badges."""
    current, flagged = {}, flags(store)
    for entry in history(store):
        if entry["panel_id"] in flagged:
            continue
        record = store.path(f"images/{entry['panel_id']}.json")
        if record.exists() and json.loads(record.read_text(encoding="utf-8"))["image_hash"] == entry["new_image_hash"]:
            current[entry["panel_id"]] = entry["note"]
    return current
