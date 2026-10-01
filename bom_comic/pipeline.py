import json
import hashlib
import re
from uuid import uuid4
from .models import Scene, SceneBatch, Panel, Verse
from .storage import digest
from .analysis import RULES, analyze, speaker_note, unquote, validate_batch, validate_scene
from .comic import (DEFAULT_CONTINUITY, PAGE_UNITS, plan, build_prompt, build_portrait_prompt,
    portrait_eligible, portrait_aspect, is_group, assemble, frames, frame_aspect)
from .scripture import load, select

# Kontext drops or blends faces past a few reference images, so a panel attaches at most this many.
MAX_REFERENCES = 3
# Lettering and page placement aren't drawn, so changing them doesn't make a drawing stale.
ART_EXCLUDE = {"page", "panel_number", "weight", "narration", "dialogue"}
# A drawing still fits a frame whose shape changed by up to this much; assembly crops the difference.
ASPECT_TOLERANCE = 0.04


# Panels render at their size on the page, within these bounds: below 0.65 MP Flux starts to lose faces and
# coherence, and above ~1 MP (its native scale) it's slower without looking better at 150 dpi. Tested on 1 Nephi 3
# (runs/compare/render-size): 138 s a panel against 197 s at a flat 1 MP, no visible loss at page size.
RENDER_MIN_PIXELS, RENDER_MAX_PIXELS = 650_000, 1024 * 1024


def render_pixels(frame):
    """Pixels to render a panel at, from its frame (x, y, w, h) on the page."""
    return int(min(max(frame[2] * frame[3], RENDER_MIN_PIXELS), RENDER_MAX_PIXELS))


def _similar(a, b):
    ratio = lambda aspect: (lambda w, h: w / h)(*(int(n) for n in aspect.split(":")))
    return abs(ratio(a) / ratio(b) - 1) <= ASPECT_TOLERANCE

class Pipeline:
    def __init__(self, store, provider=None, library=None):
        """library: a character library folder (e.g. portraits/book-of-mormon) whose cast.json and locations.json
        resolve scene labels to records when image prompts are built (see cast.py). Without one, prompts use the
        run's own continuity records by exact label, as before."""
        self.store, self.provider, self.library = store, provider, library
        self._cast = None

    def init(self, source, start, end):
        if self.store.path("source.json").exists():
            raise ValueError("Run already initialized; use a new run directory")
        verses = select(load(source), start, end)
        self.store.write("source.json", [v.model_dump() for v in verses])
        self.store.write("selection.json", {"start": start, "end": end, "source_hash": digest([v.model_dump() for v in verses])})
        for name, value in DEFAULT_CONTINUITY.items():
            self.store.write(f"continuity/{name}.json", value)
        self.store.event("init", start=start, end=end)

    def verses(self):
        data = self.store.read("source.json")
        selection = self.store.read("selection.json")
        if digest(data) != selection["source_hash"]:
            raise ValueError("Source snapshot changed; initialize a new run")
        return select([Verse.model_validate(v) for v in data], selection["start"], selection["end"])

    def scenes(self):
        return [Scene.model_validate(s) for s in self.store.read("scenes.json")]

    def continuity(self):
        return {key: self.store.read(f"continuity/{key}.json") for key in DEFAULT_CONTINUITY}

    def stamp(self):
        return digest({"source": [v.model_dump() for v in self.verses()],
                       "scenes": [s.model_dump() for s in self.scenes()]})

    def analyze(self, identifier=None, staged=False, chunk_size=6):
        if staged and not identifier:
            from .staged import analyze_staged
            scenes = analyze_staged(self.provider, self.verses(), known_characters=list(self.continuity()["characters"]))
        elif identifier:
            scenes = self.scenes()
            index = next((i for i, s in enumerate(scenes) if s.scene_id == identifier), None)
            if index is None:
                raise ValueError("Unknown scene")
            old = scenes[index]
            verses = [v for v in self.verses() if v.ref in old.refs]
            prompt = (RULES + "\nRegenerate exactly one scene using only these verses. Preserve all refs."
                      + speaker_note(verses[0].book, verses[0].chapter) + " Return one \n")
            request = {"source": [v.model_dump() for v in verses], "previous_draft": old.model_dump()}
            # Without the audit's reasons, a rewrite tends to repeat the same mistake.
            if self.store.path("validation.json").exists():
                issues = self.store.read("validation.json")["results"].get(identifier, {}).get("issues")
                if issues:
                    request["fix_these_audit_issues"] = issues
            result = self.provider.structured(prompt + json.dumps(request),
                SceneBatch, f"regenerate_{identifier}_{uuid4().hex[:12]}", "repair")
            if len(result.scenes) != 1 or result.scenes[0].refs != old.refs:
                raise ValueError("Individual scene regeneration must preserve its verse coverage")
            scenes[index] = unquote(result.scenes[0])
            scenes[index].scene_id = identifier
        else:
            continuity = self.continuity()
            scenes = analyze(self.provider, self.verses(), chunk_size, known_characters=list(continuity["characters"]),
                             known_locations=list(continuity["locations"]))
        self.store.write("scenes.json", [s.model_dump() for s in scenes])
        self.store.event("analyze", count=len(scenes))

    def validate(self, workers=1, batch=False):
        """batch=True audits each chapter's scenes in one call instead of one call per scene."""
        scenes, verses = self.scenes(), self.verses()
        known = list(self.continuity()["characters"])
        if len({s.scene_id for s in scenes}) != len(scenes):
            raise ValueError("Duplicate scene IDs")
        if not 1 <= workers <= 4:
            raise ValueError("Validation workers must be between 1 and 4")
        def check(index):
            scene = scenes[index]
            verdict = validate_scene(self.provider, scene, verses, scenes[index - 1] if index else None, known)
            return scene.scene_id, verdict.model_dump()
        if batch:
            results, previous = {}, None
            chapters = {}
            for scene in scenes:
                chapters.setdefault(scene.refs[0].rsplit(":", 1)[0], []).append(scene)
            for group in chapters.values():
                verdicts = validate_batch(self.provider, group, verses, previous, known)
                results.update({sid: v.model_dump() for sid, v in verdicts.items()})
                previous = group[-1]
        elif workers == 1:
            results = dict(check(index) for index in range(len(scenes)))
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = dict(pool.map(check, range(len(scenes))))
        # Source coverage cannot prove faithfulness, but prevents silent omission of whole verses.
        covered = {ref for s in scenes for ref in s.refs}
        missing = [v.ref for v in verses if v.ref not in covered]
        if missing:
            raise ValueError("Scenes omit source verses: " + ", ".join(missing))
        if verses[-1].ref == "3 Nephi 11:7":
            last = scenes[-1]
            if "3 Nephi 11:7" not in last.refs or not any(
                q.text == verses[-1].text for q in last.spoken_dialogue):
                results[last.scene_id] = {"status": "REJECT", "issues": ["Final scene must end with the complete supplied 11:7 introduction as speech"]}
        self.store.write("validation.json", {"stamp": self.stamp(), "results": results})
        self.store.event("validate", results=results)
        return results

    def revalidate(self, scene_ids):
        """Re-audit only these scenes (e.g. after rewriting them), keeping every other scene's verdict, so a
        re-check can't flip scenes that already passed. Consecutive scenes share one batch call."""
        scenes, verses = self.scenes(), self.verses()
        known = list(self.continuity()["characters"])
        results = dict(self.store.read("validation.json")["results"])
        wanted = set(scene_ids)
        groups, group = [], []
        for i, scene in enumerate(scenes):
            if scene.scene_id in wanted:
                group.append((i, scene))
            elif group:
                groups.append(group)
                group = []
        if group:
            groups.append(group)
        for group in groups:
            first = group[0][0]
            verdicts = validate_batch(self.provider, [scene for _, scene in group], verses,
                                      scenes[first - 1] if first else None, known)
            results.update({sid: v.model_dump() for sid, v in verdicts.items()})
        results = {s.scene_id: results[s.scene_id] for s in scenes}
        self.store.write("validation.json", {"stamp": self.stamp(), "results": results})
        self.store.event("revalidate", scenes=sorted(wanted), results={sid: results[sid] for sid in sorted(wanted)})
        return results

    def validation(self):
        value = self.store.read("validation.json")
        if value["stamp"] != self.stamp():
            raise ValueError("Scene/source edits require validate again")
        return value

    def approvals(self):
        return self.store.read("reviews.json") if self.store.path("reviews.json").exists() else {}

    def require_scenes(self):
        report = self.validation()
        reviews = self.approvals()
        for scene in self.scenes():
            verdict = report["results"][scene.scene_id]
            expected = digest({"report": report, "scene": scene.scene_id})
            record = reviews.get("scene:" + scene.scene_id, {})
            if verdict["status"] == "REJECT" or record.get("decision") != "approve" or record.get("stamp") != expected:
                raise ValueError(f"Scene {scene.scene_id} needs validation and human approval")

    def plan(self):
        self.require_scenes()
        panels = plan(self.scenes())
        self.store.write("panels.json", {"scene_stamp": self.stamp(), "panels": [p.model_dump() for p in panels]})
        self.store.event("plan", pages=len({p.page for p in panels}))

    def preview(self):
        """Create review-only planning artifacts without granting production approval."""
        report = self.validation()
        scenes = self.scenes()
        panels = plan(scenes)
        layout = frames(panels)
        self.store.write("review/draft-panels.json", {
            "status": "DRAFT — NOT APPROVED FOR GENERATION", "scene_stamp": self.stamp(),
            "panels": [p.model_dump() for p in panels]})
        shown = self.shown(panels)
        self.store.write("review/draft-prompts.json", {
            p.panel_id: build_prompt(*shown[p.panel_id], frame_aspect(layout[p.panel_id])) for p in panels})
        lines = ["# Full-story review draft", "",
                 "Not approved for generation. Compare every scene to its cited source before approving.", "",
                 f"{len(scenes)} scenes / {len(panels)} panels / {len({p.page for p in panels})} draft pages", ""]
        source = {v.ref: v.text for v in self.verses()}
        for scene, panel in zip(scenes, panels):
            result = report["results"][scene.scene_id]
            lines.extend([f"## {scene.scene_id}: {scene.title}", "",
                f"Draft page {panel.page}, panel {panel.panel_number}, weight {panel.weight}/{PAGE_UNITS} — **{result['status']}**", "",
                scene.summary, "", "References: " + "; ".join(scene.refs), ""])
            for issue in result["issues"]:
                lines.append("- Review issue: " + issue)
            for label, entries in [("Explicit scriptural facts", [c.text for c in scene.explicit_facts]),
                                   ("Reasonable visual inferences", scene.reasonable_visual_inferences),
                                   ("Creative visual details", scene.unspecified_visual_details),
                                   ("Story notes", scene.doctrinal_or_story_notes)]:
                lines.extend(["", f"**{label}**", ""] + ["- " + e for e in entries])
            lines.extend(["", "**Lettering**", ""])
            lines.extend([f"- {q.speaker}: {q.text}" for q in scene.spoken_dialogue])
            lines.extend(["- Narration: " + q.text for q in scene.narration])
            lines.extend(["", "**Supplied verses**", ""])
            lines.extend([f"> **{ref}** {source[ref]}\n" for ref in scene.refs])
        path = self.store.path("review/STORYBOARD.md")
        path.write_text("\n".join(lines), encoding="utf-8")
        self.store.event("preview", panels=len(panels), pages=len({p.page for p in panels}))
        return path

    def panels(self):
        self.require_scenes()
        data = self.store.read("panels.json")
        if data["scene_stamp"] != self.stamp():
            raise ValueError("Scenes changed; replan")
        panels = [Panel.model_validate(p) for p in data["panels"]]
        scenes = {s.scene_id: s for s in self.scenes()}
        if len({p.panel_id for p in panels}) != len(panels) or [p.scene_id for p in panels] != list(scenes):
            raise ValueError("Panels must preserve scene order and one panel per scene")
        for p in panels:
            s = scenes[p.scene_id]
            pairs = [(p.refs, s.refs), (p.characters_visible, s.characters), (p.location, s.locations),
                     (p.action, s.summary), (p.dialogue, s.spoken_dialogue), (p.narration, s.narration),
                     (p.visual_facts, s.explicit_facts), (p.visual_inferences, s.reasonable_visual_inferences),
                     (p.creative_details, s.unspecified_visual_details), (p.prohibited, s.prohibited_inventions)]
            if any(a != b for a, b in pairs):
                raise ValueError("Edit story content in scenes.json, then validate and plan again")
        if [(p.page, p.panel_number) for p in panels] != sorted(set((p.page, p.panel_number) for p in panels)):
            raise ValueError("Panel positions must be unique and chronological")
        for page in {p.page for p in panels}:
            if sum(p.weight for p in panels if p.page == page) > PAGE_UNITS:
                raise ValueError(f"Page {page} holds more than {PAGE_UNITS} weight units")
        return panels

    def aspects(self):
        return {pid: frame_aspect(frame) for pid, frame in frames(self.panels()).items()}

    def cast(self):
        if self.library is None:
            return None
        if self._cast is None:
            from .cast import Cast
            self._cast = Cast.load(self.library)
        return self._cast

    def where(self):
        """(book, chapter) the run starts in; labels resolve by chapter ("Alma" in Mosiah 18 is Alma the Elder)."""
        first = self.store.read("source.json")[0]
        return first["book"], first["chapter"]

    def shown(self, panels):
        """{panel_id: (panel, continuity)} as the image prompt should see each panel: visible people resolved to
        the library's records (with their visual tags) and each place resolved to a setting, an unspecified one
        continuing the previous panel's. Only the prompt uses this. Stamps, approvals and portraits keep the
        panel's own labels, so resolving differently never makes an approved panel or its drawing stale."""
        continuity = self.continuity()
        cast = self.cast()
        if cast is None:
            return {panel.panel_id: (panel, continuity) for panel in panels}
        where = self.where()
        known_people, known_places = continuity["characters"], continuity["locations"]
        settings = cast.settings([panel.location for panel in panels], where, known_places)
        out = {}
        for panel, places in zip(panels, settings):
            people = cast.people(panel.characters_visible, where, known_people)
            characters = dict(known_people)
            for name in people:
                record = cast.character(name, known_people)
                if record:
                    characters[name] = record
            locations = {**known_places, **{name: cast.location(name, known_places) for name in places}}
            out[panel.panel_id] = (panel.model_copy(update={"characters_visible": people, "location": places}),
                                   {**continuity, "characters": characters, "locations": locations})
        return out

    def prompt_for(self, panel, aspect, portraits=(), corrections=None, shown=None):
        """The image prompt for one panel, drawn from its resolved people and setting."""
        shown = shown or self.shown(self.panels())
        view, continuity = shown[panel.panel_id]
        return build_prompt(view, continuity, aspect, list(portraits), corrections)

    def panel_stamp(self, panel, aspects=None):
        # Page position and weight only matter through the frame shape, so moving a panel
        # without reshaping its frame keeps its approval and image.
        aspect = (aspects or self.aspects())[panel.panel_id]
        value = {"panel": panel.model_dump(exclude={"page", "panel_number", "weight"}), "aspect": aspect,
                 "continuity": self.continuity(), "source": self.stamp()}
        # Attached portraits steer the art, so panel approval covers them. Omitted when there are
        # none, so runs without portraits keep their approvals.
        portraits = self.panel_portraits(panel)
        if portraits:
            value["portraits"] = {name: [entry["path"], entry["image_hash"]] for name, entry in portraits.items()}
        return digest(value)

    def art_stamp(self, panel):
        """What a panel's drawing depends on: its content except the lettering (added at assembly) and its place on
        the page, its own verses, the continuity, and any attached portraits. Unlike panel_stamp it ignores the
        chapter's other scenes, so repairing one scene doesn't make every drawing in the chapter stale."""
        return self._art_stamp(panel)

    def _art_stamp(self, panel, whole_continuity=False):
        # Version 2 covers only the records the panel draws from (its visible people, its locations, the style), so
        # adding or editing someone else's record doesn't restale it. whole_continuity gives version 1, which
        # records made before that change were stamped with.
        continuity = self.continuity()
        if not whole_continuity:
            continuity = {"characters": {n: continuity["characters"].get(n) for n in panel.characters_visible},
                          "locations": {n: continuity["locations"].get(n) for n in panel.location},
                          "visual_style": continuity["visual_style"]}
        source = {v.ref: v.text for v in self.verses()}
        value = {"panel": panel.model_dump(exclude=ART_EXCLUDE), "verses": [source.get(ref) for ref in panel.refs],
                 "continuity": continuity}
        portraits = self.panel_portraits(panel)
        if portraits:
            value["portraits"] = {name: [entry["path"], entry["image_hash"]] for name, entry in portraits.items()}
        return digest(value)

    def upgrade_image_records(self):
        """Give drawings recorded before art stamps existed an art stamp and aspect, if they're still current by
        the old rule. Run before a chapter's scenes change, or its drawings will look stale under the old rule."""
        if not self.store.path("panels.json").exists():
            return 0
        aspects, upgraded, carried = self.aspects(), 0, False
        reviews = self.approvals()
        for panel in self.panels():
            path = self.store.path(f"images/{panel.panel_id}.json")
            if not path.exists():
                continue
            record = self.store.read(f"images/{panel.panel_id}.json")
            before = dict(record)
            if "art_stamp" not in record:
                if record["panel_stamp"] != self.panel_stamp(panel, aspects):
                    continue
                record.update(art_stamp=self.art_stamp(panel), aspect=aspects[panel.panel_id], art_version=2)
            elif record.get("art_version") != 2:
                if record["art_stamp"] != self._art_stamp(panel, whole_continuity=True):
                    continue
                record.update(art_stamp=self.art_stamp(panel), art_version=2)
            if record != before:
                self.store.write(f"images/{panel.panel_id}.json", record)
                upgraded += 1
            # The image approval covers the record, so carry an approval of an earlier form of it forward.
            earlier = [before, {k: v for k, v in before.items() if k not in ("art_stamp", "aspect", "art_version")}]
            review = reviews.get("image:" + panel.panel_id)
            if review and review.get("stamp") != digest(record) and review.get("stamp") in [digest(e) for e in earlier]:
                review["stamp"] = digest(record)
                carried = True
        if carried:
            self.store.write("reviews.json", reviews)
        return upgraded

    def portrait_prompt(self, name):
        continuity = self.continuity()
        return build_portrait_prompt(name, continuity["characters"][name], continuity["visual_style"])

    def portrait_names(self):
        """Characters in the scenes whose continuity record can seed a reference portrait."""
        records = self.continuity()["characters"]
        names = dict.fromkeys(name for scene in self.scenes() for name in scene.characters)
        return [name for name in names if portrait_eligible(records.get(name))]

    def portrait_index(self):
        return self.store.read("portraits/index.json") if self.store.path("portraits/index.json").exists() else {}

    def portrait(self, name, index=None):
        """The current portrait for a character, or None if it has none or its record opted out."""
        entry = (self.portrait_index() if index is None else index).get(name)
        if entry is None or not portrait_eligible(self.continuity()["characters"].get(name)):
            return None
        actual = hashlib.sha256(self.store.path(entry["path"]).read_bytes()).hexdigest()
        if entry["stamp"] != digest(self.portrait_prompt(name)) or actual != entry["image_hash"]:
            raise ValueError(f"Portrait for {name} is stale or modified; run portraits again")
        return entry

    def panel_portraits(self, panel):
        index = self.portrait_index()
        found = {name: self.portrait(name, index) for name in panel.characters_visible}
        found = {name: entry for name, entry in found.items() if entry}
        # Over the limit, keep the people the panel is about: speakers, then those named in the action,
        # individuals before group costume sheets, then the order they're listed. The rest go by text alone.
        speakers = {speech.speaker for speech in panel.dialogue}
        records = self.continuity()["characters"]
        ranked = sorted(found, key=lambda name: (name not in speakers, name not in panel.action,
                                                 is_group(records.get(name)), panel.characters_visible.index(name)))
        kept = set(ranked[:MAX_REFERENCES])
        return {name: entry for name, entry in found.items() if name in kept}

    def portraits(self, identifier=None):
        """Render one reference portrait per eligible character, reusing current ones."""
        names = self.portrait_names()
        if identifier and identifier not in names:
            raise ValueError(f"No portrait-eligible character record for {identifier!r} in the scenes")
        index = self.portrait_index()
        made = []
        for name in names:
            if identifier and name != identifier:
                continue
            # Batch runs reuse current portraits; explicit --id always renders a new revision.
            if not identifier:
                try:
                    if self.portrait(name, index):
                        continue
                except ValueError:
                    pass
            prompt = self.portrait_prompt(name)
            slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "character"
            path = self.store.path(f"portraits/{slug}_{uuid4().hex[:12]}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            record = self.continuity()["characters"][name]
            self.provider.generate_image(prompt, output_path=path, aspect_ratio=portrait_aspect(record))
            index[name] = {"path": str(path.relative_to(self.store.root)), "stamp": digest(prompt),
                           "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": prompt}
            self.store.write("portraits/index.json", index)
            self.store.event("portrait", name=name, path=index[name]["path"])
            made.append(name)
        return made

    def adopt_portraits(self, library):
        """Use a character library's approved portraits instead of rendering new faces.

        Only adopts when the run's record matches the library record the portrait was drawn from."""
        import shutil
        from pathlib import Path
        library = Path(library)
        records = json.loads((library / "characters.json").read_text(encoding="utf-8"))
        library_index = json.loads((library / "index.json").read_text(encoding="utf-8"))
        index = self.portrait_index()
        adopted = []
        for name in self.portrait_names():
            entry, source = library_index.get(name), library / library_index.get(name, {}).get("path", "")
            if not entry or not source.is_file():
                continue
            if records.get(name) != self.continuity()["characters"][name]:
                raise ValueError(f"{name}'s record differs from the library's; copy it over first or render with portraits")
            path = self.store.path(f"portraits/{source.name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, path)
            index[name] = {"path": str(path.relative_to(self.store.root)), "stamp": digest(self.portrait_prompt(name)),
                           "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "prompt": entry["prompt"], "adopted_from": str(source)}
            adopted.append(name)
        self.store.write("portraits/index.json", index)
        self.store.event("adopt_portraits", library=str(library), names=adopted)
        return adopted

    def image_record(self, panel, aspects=None):
        """The panel's current drawing. It stays current while its art stamp matches and its frame keeps nearly
        the same shape (assembly crops the small difference); older records fall back to the full panel stamp."""
        record = self.store.read(f"images/{panel.panel_id}.json")
        actual = hashlib.sha256(self.store.path(record["path"]).read_bytes()).hexdigest()
        if "art_stamp" in record:
            aspect = (aspects or self.aspects())[panel.panel_id]
            stamp = self.art_stamp(panel) if record.get("art_version") == 2 else self._art_stamp(panel, True)
            current = record["art_stamp"] == stamp and _similar(record["aspect"], aspect)
        else:
            current = record["panel_stamp"] == self.panel_stamp(panel, aspects)
        if not current or actual != record["image_hash"]:
            raise ValueError("Image is stale or modified; regenerate and review")
        return record

    def review(self, kind, identifier, decision, note=""):
        if kind == "scene":
            report = self.validation()
            verdict = report["results"][identifier]
            if decision == "approve" and verdict["status"] == "REJECT":
                raise ValueError("Rejected scenes must be corrected and validated")
            if decision == "approve" and verdict["status"] == "PASS WITH WARNINGS" and not note:
                raise ValueError("Explain acceptance of warnings with --note")
            stamp = digest({"report": report, "scene": identifier})
        else:
            panel = next((p for p in self.panels() if p.panel_id == identifier), None)
            if panel is None:
                raise ValueError("Unknown panel")
            stamp = self.panel_stamp(panel) if kind == "panel" else digest(self.image_record(panel))
        reviews = self.approvals()
        reviews[f"{kind}:{identifier}"] = {"decision": decision, "stamp": stamp, "note": note}
        self.store.write("reviews.json", reviews)
        self.store.event("review", kind=kind, identifier=identifier, decision=decision, note=note)

    def reapprove_panels(self, note):
        """Record `note` as the approval of every panel whose approval went stale (e.g. continuity records were added
        or changed). Used by automated runs, whose panel approvals are automated in the first place."""
        aspects, reviews = self.aspects(), self.approvals()
        for panel in self.panels():
            if reviews.get("panel:" + panel.panel_id, {}).get("stamp") != self.panel_stamp(panel, aspects):
                self.review("panel", panel.panel_id, "approve", note)

    def main_characters(self, panel):
        """Visible named individuals whose record can seed a portrait; these need identity-preserving art."""
        records = self.continuity()["characters"]
        return [name for name in panel.characters_visible
                if portrait_eligible(records.get(name)) and not is_group(records.get(name))]

    def generate(self, identifier=None, skip_main=False, references=True, corrections=None):
        """Render approved panels. With references=False the art is drawn from the text records alone (first
        drafts); reference portraits are for panels flagged later as needing them. corrections maps a panel ID to
        a reviewer's {"add", "avoid"} instructions for redrawing it."""
        panels = self.panels()
        aspects = self.aspects()
        if identifier and identifier not in {p.panel_id for p in panels}:
            raise ValueError("Unknown panel")
        shown = self.shown(panels)
        layout = frames(panels)
        for panel in panels:
            if identifier and panel.panel_id != identifier:
                continue
            if skip_main and self.main_characters(panel):
                continue
            review = self.approvals().get("panel:" + panel.panel_id, {})
            if review.get("decision") != "approve" or review.get("stamp") != self.panel_stamp(panel, aspects):
                raise ValueError(f"Review panel composition/continuity before generation: {panel.panel_id}")
            # Batch generation resumes; explicit --id always regenerates with a new revision.
            if not identifier and self.store.path(f"images/{panel.panel_id}.json").exists():
                try:
                    self.image_record(panel, aspects)
                    continue
                except ValueError:
                    pass
            revision = uuid4().hex[:12]
            name = f"{panel.panel_id}_{revision}"
            aspect = aspects[panel.panel_id]
            portraits = self.panel_portraits(panel) if references else {}
            # A label that resolves to someone else (a bare "Nephi" in Helaman) must not bring the wrong face along.
            portraits = {n: e for n, e in portraits.items() if n in shown[panel.panel_id][0].characters_visible}
            prompt = self.prompt_for(panel, aspect, portraits, (corrections or {}).get(panel.panel_id), shown)
            self.store.write(f"prompts/{name}.json", {"panel": panel.model_dump(), "aspect": aspect, "prompt": prompt,
                "portraits": {n: e["path"] for n, e in portraits.items()}})
            path = self.store.path(f"images/{name}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            records = self.continuity()["characters"]
            attached = [(f"{n} (group costume sheet)" if is_group(records.get(n)) else n, self.store.path(e["path"]))
                        for n, e in portraits.items()]
            pixels = render_pixels(layout[panel.panel_id])
            if hasattr(self.provider, "pixels"):
                self.provider.pixels = pixels
            self.provider.generate_image(prompt, reference_images=attached or None, output_path=path, aspect_ratio=aspect)
            record = {"path": str(path.relative_to(self.store.root)), "panel_stamp": self.panel_stamp(panel, aspects),
                      "art_stamp": self.art_stamp(panel), "aspect": aspect, "art_version": 2, "pixels": pixels,
                      "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": f"prompts/{name}.json",
                      "references": list(portraits)}
            self.store.write(f"images/{panel.panel_id}.json", record)
            self.store.event("generate", panel_id=panel.panel_id, **record)

    def assemble(self):
        panels = self.panels()
        aspects = self.aspects()
        images = {}
        for panel in panels:
            panel_review = self.approvals().get("panel:" + panel.panel_id, {})
            if panel_review.get("decision") != "approve" or panel_review.get("stamp") != self.panel_stamp(panel, aspects):
                raise ValueError(f"Panel {panel.panel_id} requires current composition/continuity approval")
            record = self.image_record(panel, aspects)
            review = self.approvals().get("image:" + panel.panel_id, {})
            if review.get("decision") != "approve" or review.get("stamp") != digest(record):
                raise ValueError(f"Image {panel.panel_id} requires human visual/scripture review")
            images[panel.panel_id] = record
        path = assemble(self.store, panels, images)
        self.store.write("final/manifest.json", {"scene_stamp": self.stamp(), "panels": [p.model_dump() for p in panels], "images": images})
        self.store.event("assemble", path=str(path))
        return path


def character_bible(store, provider, identifier=None):
    """Render a portrait for every eligible record in a folder's characters.json, independent of any story run.

    Uses the folder's style.json (or the default style) and reuses portraits whose prompt is unchanged."""
    records = store.read("characters.json")
    style = store.read("style.json") if store.path("style.json").exists() else DEFAULT_CONTINUITY["visual_style"]
    names = [name for name, record in records.items() if portrait_eligible(record)]
    if identifier and identifier not in names:
        raise ValueError(f"No portrait-eligible record for {identifier!r} in characters.json")
    index = store.read("index.json") if store.path("index.json").exists() else {}
    made = []
    for name in names:
        if identifier and name != identifier:
            continue
        prompt = build_portrait_prompt(name, records[name], style)
        entry = index.get(name)
        # Batch runs reuse current portraits; explicit --id always renders a new revision.
        if not identifier and entry and entry["stamp"] == digest(prompt) and store.path(entry["path"]).exists():
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "character"
        path = store.path(f"{slug}_{uuid4().hex[:12]}.png")
        provider.generate_image(prompt, output_path=path, aspect_ratio=portrait_aspect(records[name]))
        index[name] = {"path": path.name, "stamp": digest(prompt),
                       "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": prompt}
        store.write("index.json", index)
        made.append(name)
    return made
