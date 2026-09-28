import json
import hashlib
import re
from uuid import uuid4
from .models import Scene, SceneBatch, Panel, Verse
from .storage import digest
from .analysis import RULES, analyze, validate_scene
from .comic import (DEFAULT_CONTINUITY, PAGE_UNITS, LOCATION_ASPECT, plan, build_prompt, build_portrait_prompt,
    build_location_prompt, portrait_eligible, location_eligible, portrait_aspect, is_group, assemble, frames,
    frame_aspect)
from .scripture import load, select

class Pipeline:
    def __init__(self, store, provider=None):
        self.store, self.provider = store, provider

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

    def analyze(self, identifier=None, staged=False):
        if staged and not identifier:
            from .staged import analyze_staged
            scenes = analyze_staged(self.provider, self.verses(), known_characters=list(self.continuity()["characters"]),
                                    known_locations=list(self.continuity()["locations"]))
        elif identifier:
            scenes = self.scenes()
            index = next((i for i, s in enumerate(scenes) if s.scene_id == identifier), None)
            if index is None:
                raise ValueError("Unknown scene")
            old = scenes[index]
            verses = [v for v in self.verses() if v.ref in old.refs]
            prompt = RULES + "\nRegenerate exactly one scene using only these verses. Preserve all refs. Return one \n"
            result = self.provider.structured(prompt + json.dumps({
                "source": [v.model_dump() for v in verses], "previous_draft": old.model_dump()}),
                SceneBatch, f"regenerate_{identifier}_{uuid4().hex[:12]}")
            if len(result.scenes) != 1 or result.scenes[0].refs != old.refs:
                raise ValueError("Individual scene regeneration must preserve its verse coverage")
            scenes[index] = result.scenes[0]
            scenes[index].scene_id = identifier
        else:
            scenes = analyze(self.provider, self.verses(), known_characters=list(self.continuity()["characters"]),
                             known_locations=list(self.continuity()["locations"]))
        self.store.write("scenes.json", [s.model_dump() for s in scenes])
        self.store.event("analyze", count=len(scenes))

    def validate(self, workers=1):
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
        if workers == 1:
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
        self.store.write("review/draft-prompts.json", {
            p.panel_id: build_prompt(p, self.continuity(), frame_aspect(layout[p.panel_id])) for p in panels})
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

    def panel_stamp(self, panel, aspects=None):
        # Page position and weight only matter through the frame shape, so moving a panel
        # without reshaping its frame keeps its approval and image.
        aspect = (aspects or self.aspects())[panel.panel_id]
        value = {"panel": panel.model_dump(exclude={"page", "panel_number", "weight"}), "aspect": aspect,
                 "continuity": self.continuity(), "source": self.stamp()}
        # Attached portraits and location references steer the art, so panel approval covers them.
        # Omitted when there are none, so runs without them keep their approvals.
        portraits = self.panel_portraits(panel)
        if portraits:
            value["portraits"] = {name: [entry["path"], entry["image_hash"]] for name, entry in portraits.items()}
        places = self.panel_locations(panel)
        if places:
            value["locations"] = {name: [entry["path"], entry["image_hash"]] for name, entry in places.items()}
        return digest(value)

    def portrait_prompt(self, name):
        return self.reference_prompt("characters", name)

    def portrait_names(self):
        """Characters in the scenes whose continuity record can seed a reference portrait."""
        return self.reference_names("characters")

    def portrait_index(self):
        return self.reference_index("characters")

    def portrait(self, name, index=None):
        """The current portrait for a character, or None if it has none or its record opted out."""
        return self.reference("characters", name, index)

    def panel_portraits(self, panel):
        return self.panel_references("characters", panel.characters_visible)

    def portraits(self, identifier=None):
        """Render one reference portrait per eligible character, reusing current ones."""
        return self.render_references("characters", identifier)

    def panel_locations(self, panel):
        return self.panel_references("locations", panel.location)

    def location_references(self, identifier=None):
        """Render one reference image per eligible location, reusing current ones."""
        return self.render_references("locations", identifier)

    # Characters get portraits and locations get establishing views; both follow the same rules.
    def reference_prompt(self, kind, name):
        continuity = self.continuity()
        return REFERENCE_KINDS[kind]["prompt"](name, continuity[kind][name], continuity["visual_style"])

    def reference_names(self, kind):
        records = self.continuity()[kind]
        field = REFERENCE_KINDS[kind]["scene_field"]
        names = dict.fromkeys(name for scene in self.scenes() for name in getattr(scene, field))
        return [name for name in names if REFERENCE_KINDS[kind]["eligible"](records.get(name))]

    def reference_index(self, kind):
        path = REFERENCE_KINDS[kind]["folder"] + "/index.json"
        return self.store.read(path) if self.store.path(path).exists() else {}

    def reference(self, kind, name, index=None):
        """The current reference image for a record, or None if it has none or its record opted out."""
        entry = (self.reference_index(kind) if index is None else index).get(name)
        if entry is None or not REFERENCE_KINDS[kind]["eligible"](self.continuity()[kind].get(name)):
            return None
        actual = hashlib.sha256(self.store.path(entry["path"]).read_bytes()).hexdigest()
        if entry["stamp"] != digest(self.reference_prompt(kind, name)) or actual != entry["image_hash"]:
            raise ValueError(f"Reference image for {name} is stale or modified; run {REFERENCE_KINDS[kind]['command']} again")
        return entry

    def panel_references(self, kind, names):
        index = self.reference_index(kind)
        found = {name: self.reference(kind, name, index) for name in names}
        return {name: entry for name, entry in found.items() if entry}

    def render_references(self, kind, identifier=None):
        spec = REFERENCE_KINDS[kind]
        names = self.reference_names(kind)
        if identifier and identifier not in names:
            raise ValueError(f"No {spec['label']} record for {identifier!r} in the scenes")
        index = self.reference_index(kind)
        made = []
        for name in names:
            if identifier and name != identifier:
                continue
            # Batch runs reuse current images; explicit --id always renders a new revision.
            if not identifier:
                try:
                    if self.reference(kind, name, index):
                        continue
                except ValueError:
                    pass
            prompt = self.reference_prompt(kind, name)
            slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or kind
            path = self.store.path(f"{spec['folder']}/{slug}_{uuid4().hex[:12]}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            record = self.continuity()[kind][name]
            self.provider.generate_image(prompt, output_path=path, aspect_ratio=spec["aspect"](record))
            index[name] = {"path": str(path.relative_to(self.store.root)), "stamp": digest(prompt),
                           "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": prompt}
            self.store.write(f"{spec['folder']}/index.json", index)
            self.store.event(spec["event"], name=name, path=index[name]["path"])
            made.append(name)
        return made

    def image_record(self, panel, aspects=None):
        record = self.store.read(f"images/{panel.panel_id}.json")
        actual = hashlib.sha256(self.store.path(record["path"]).read_bytes()).hexdigest()
        if record["panel_stamp"] != self.panel_stamp(panel, aspects) or actual != record["image_hash"]:
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

    def main_characters(self, panel):
        """Visible named individuals whose record can seed a portrait; these need identity-preserving art."""
        records = self.continuity()["characters"]
        return [name for name in panel.characters_visible
                if portrait_eligible(records.get(name)) and not is_group(records.get(name))]

    def generate(self, identifier=None, skip_main=False):
        panels = self.panels()
        aspects = self.aspects()
        if identifier and identifier not in {p.panel_id for p in panels}:
            raise ValueError("Unknown panel")
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
            portraits = self.panel_portraits(panel)
            places = self.panel_locations(panel)
            prompt = build_prompt(panel, self.continuity(), aspect, list(portraits), list(places))
            self.store.write(f"prompts/{name}.json", {"panel": panel.model_dump(), "aspect": aspect, "prompt": prompt,
                "portraits": {n: e["path"] for n, e in portraits.items()},
                "locations": {n: e["path"] for n, e in places.items()}})
            path = self.store.path(f"images/{name}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            records = self.continuity()["characters"]
            references = [(f"{n} (group costume sheet)" if is_group(records.get(n)) else n, self.store.path(e["path"]))
                          for n, e in portraits.items()]
            references += [(f"{n} (location reference)", self.store.path(e["path"])) for n, e in places.items()]
            self.provider.generate_image(prompt, reference_images=references or None, output_path=path, aspect_ratio=aspect)
            record = {"path": str(path.relative_to(self.store.root)), "panel_stamp": self.panel_stamp(panel, aspects),
                      "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": f"prompts/{name}.json"}
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


REFERENCE_KINDS = {
    "characters": {"folder": "portraits", "scene_field": "characters", "eligible": portrait_eligible,
                   "prompt": build_portrait_prompt, "aspect": portrait_aspect, "command": "portraits",
                   "event": "portrait", "label": "portrait-eligible", "bible": "characters.json"},
    "locations": {"folder": "locations", "scene_field": "locations", "eligible": location_eligible,
                  "prompt": build_location_prompt, "aspect": lambda record: LOCATION_ASPECT, "command": "locations",
                  "event": "location_reference", "label": "reference-eligible location", "bible": "locations.json"},
}


def character_bible(store, provider, identifier=None, kind="characters"):
    """Render a reference image for every eligible record in a folder's characters.json (or, with
    kind="locations", its locations.json), independent of any story run.

    Uses the folder's style.json (or the default style) and reuses images whose prompt is unchanged."""
    spec = REFERENCE_KINDS[kind]
    records = store.read(spec["bible"])
    style = store.read("style.json") if store.path("style.json").exists() else DEFAULT_CONTINUITY["visual_style"]
    names = [name for name, record in records.items() if spec["eligible"](record)]
    if identifier and identifier not in names:
        raise ValueError(f"No {spec['label']} record for {identifier!r} in {spec['bible']}")
    index = store.read("index.json") if store.path("index.json").exists() else {}
    made = []
    for name in names:
        if identifier and name != identifier:
            continue
        prompt = spec["prompt"](name, records[name], style)
        entry = index.get(name)
        # Batch runs reuse current images; explicit --id always renders a new revision.
        if not identifier and entry and entry["stamp"] == digest(prompt) and store.path(entry["path"]).exists():
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or kind
        path = store.path(f"{slug}_{uuid4().hex[:12]}.png")
        provider.generate_image(prompt, output_path=path, aspect_ratio=spec["aspect"](records[name]))
        index[name] = {"path": path.name, "stamp": digest(prompt),
                       "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": prompt}
        store.write("index.json", index)
        made.append(name)
    return made
