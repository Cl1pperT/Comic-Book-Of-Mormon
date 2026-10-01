"""Redraw panels a reviewer flagged in the reader, before any new panels are drawn.

A flag is pending while its chapter's current image is still the one that was flagged. Redrawing turns the note
into image-model instructions (one small text-model call, cached on the flag), draws the panel again with a new
seed and those instructions, records an AUTOMATED image approval, and reassembles the chapter. The flag then moves
to review/flag-history.json, and the reader marks the panel "Redrawn" so the reviewer looks again; flagging it again
starts another round.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from .comic import _sort_clauses
from .models import Corrections
from .pipeline import Pipeline
from .reader import FLAGS, chapters, flags
from .storage import Store

HISTORY = "review/flag-history.json"
# A note about the art style ("Wrong art style", "inconsistent art") is redrawn at full size, as drawings were before
# renders were sized to their frames: slower, but steadier in style.
STYLE_NOTE = re.compile(r"\bstyle\b|\binconsistent art\b", re.I)
PROMPT = """A reviewer flagged this drawn comic panel. Turn the reviewer's note into short instructions for an
image-generation model that will redraw the panel.
"add": things the new image must show, each a short positive visual phrase (never "no ..." or "not ...").
"avoid": things to leave out of the image, each just the thing itself (e.g. "baseball caps", "a second Lehi").
Stay within the panel's approved action and people below; never add events, people, or text to the image.
"""


def pending(root):
    """[(chapter store, panel_id, flag)] for flags whose panel still shows the flagged image, in book order.

    Chapters whose scenes changed since assembly are skipped until re-rendered (their panels must be re-planned
    first). A flag whose panel has since been redrawn anyway is settled into the history, so the reader shows it as
    redrawn instead of the flag silently going nowhere."""
    out = []
    for chapter in chapters(Store(root)).values():
        if chapter.read("final/manifest.json")["scene_stamp"] != Pipeline(chapter, None).stamp():
            continue
        for panel_id, flag in sorted(flags(chapter).items()):
            record_path = chapter.path(f"images/{panel_id}.json")
            if not record_path.exists():
                continue
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record["image_hash"] == flag["image_hash"]:
                out.append((chapter, panel_id, flag))
            else:
                _settle(chapter, panel_id, flag, record, None, "Panel was redrawn after this flag")
    return out


def _settle(store, panel_id, flag, record, fixes, how):
    """Move a flag to the chapter's history against the drawing that replaced the flagged one."""
    entry = {"panel_id": panel_id, "note": flag["note"], "flagged": flag.get("time"), "corrections": fixes,
             "old_image": flag["image_path"], "new_image": record["path"], "new_image_hash": record["image_hash"],
             "redrawn": datetime.now(timezone.utc).isoformat(), "how": how}
    store.write(HISTORY, history(store) + [entry])
    current = flags(store)
    current.pop(panel_id, None)
    store.path(FLAGS).write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return entry


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


def redraw(store, panel_id, flag, image_provider, text_provider=None, library=None):
    """Redraw one flagged panel and reassemble its chapter; returns the history entry. library: the character
    library whose cast resolves the panel's people and places in the new prompt (see cast.py)."""
    pipeline = Pipeline(store, image_provider, library)
    # Panel approvals in these runs are automated; one that went stale (e.g. new character records copied into the
    # chapter) would otherwise block the redraw.
    pipeline.reapprove_panels("AUTOMATED composition approval, renewed for a redraw.")
    panel = next(p for p in pipeline.panels() if p.panel_id == panel_id)
    fixes = flag.get("corrections") or corrections(text_provider, panel, flag["note"])
    # Drawn on its own even if it continued a master shot; panels continuing it, if it is one, follow the redraw.
    full_size = bool(STYLE_NOTE.search(flag["note"]))
    changed = pipeline.generate(panel_id, references=False, corrections={panel_id: fixes}, reuse=False,
                                full_size=full_size)
    record = pipeline.image_record(panel)
    for other in changed:
        pipeline.review("image", other, "approve", "AUTOMATED redraw after reviewer flag: " + flag["note"] +
                        (". Review the new drawing in the reader." if other == panel_id else
                         f". This panel continues {panel_id}'s master shot."))
    # Recorded before assembly, so an assembly problem can't strand a finished drawing.
    entry = _settle(store, panel_id, flag, record, fixes, "Redrawn from the reviewer's note" +
                    (" at full size" if full_size else ""))
    store.event("redraw", panel_id=panel_id, note=flag["note"], image=record["path"])
    pipeline.assemble()
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
