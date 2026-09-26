import json
import hashlib
from uuid import uuid4
from .models import Scene, SceneBatch, Panel, Verse
from .storage import digest
from .analysis import RULES, analyze, validate_scene
from .comic import DEFAULT_CONTINUITY, plan, build_prompt, assemble
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

    def analyze(self, identifier=None):
        if identifier:
            scenes = self.scenes()
            index = next((i for i, s in enumerate(scenes) if s.scene_id == identifier), None)
            if index is None:
                raise ValueError("Unknown scene")
            old = scenes[index]
            verses = [v for v in self.verses() if v.ref in old.refs]
            prompt = RULES + "\nRegenerate exactly one scene using only these verses. Preserve all refs. Return one scene.\n"
            result = self.provider.structured(prompt + json.dumps({
                "source": [v.model_dump() for v in verses], "previous_draft": old.model_dump()}),
                SceneBatch, f"regenerate_{identifier}_{uuid4().hex[:12]}")
            if len(result.scenes) != 1 or result.scenes[0].refs != old.refs:
                raise ValueError("Individual scene regeneration must preserve its verse coverage")
            scenes[index] = result.scenes[0]
            scenes[index].scene_id = identifier
        else:
            scenes = analyze(self.provider, self.verses())
        self.store.write("scenes.json", [s.model_dump() for s in scenes])
        self.store.event("analyze", count=len(scenes))

    def validate(self):
        scenes, verses = self.scenes(), self.verses()
        if len({s.scene_id for s in scenes}) != len(scenes):
            raise ValueError("Duplicate scene IDs")
        results = {}
        for index, scene in enumerate(scenes):
            verdict = validate_scene(self.provider, scene, verses, scenes[index - 1] if index else None)
            results[scene.scene_id] = verdict.model_dump()
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

    def plan(self, pages):
        self.require_scenes()
        panels = plan(self.scenes(), pages)
        self.store.write("panels.json", {"scene_stamp": self.stamp(), "panels": [p.model_dump() for p in panels]})
        self.store.event("plan", pages=len({p.page for p in panels}))

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
        return panels

    def panel_stamp(self, panel):
        return digest({"panel": panel.model_dump(), "continuity": self.continuity(), "source": self.stamp()})

    def image_record(self, panel):
        record = self.store.read(f"images/{panel.panel_id}.json")
        actual = hashlib.sha256(self.store.path(record["path"]).read_bytes()).hexdigest()
        if record["panel_stamp"] != self.panel_stamp(panel) or actual != record["image_hash"]:
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

    def generate(self, identifier=None):
        panels = self.panels()
        if identifier and identifier not in {p.panel_id for p in panels}:
            raise ValueError("Unknown panel")
        for panel in panels:
            if identifier and panel.panel_id != identifier:
                continue
            review = self.approvals().get("panel:" + panel.panel_id, {})
            if review.get("decision") != "approve" or review.get("stamp") != self.panel_stamp(panel):
                raise ValueError(f"Review panel composition/continuity before generation: {panel.panel_id}")
            # Batch generation resumes; explicit --id always regenerates with a new revision.
            if not identifier and self.store.path(f"images/{panel.panel_id}.json").exists():
                try:
                    self.image_record(panel)
                    continue
                except ValueError:
                    pass
            revision = uuid4().hex[:12]
            name = f"{panel.panel_id}_{revision}"
            prompt = build_prompt(panel, self.continuity())
            self.store.write(f"prompts/{name}.json", {"panel": panel.model_dump(), "prompt": prompt})
            path = self.store.path(f"images/{name}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.provider.generate_image(prompt, output_path=path)
            record = {"path": str(path.relative_to(self.store.root)), "panel_stamp": self.panel_stamp(panel),
                      "image_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "prompt": f"prompts/{name}.json"}
            self.store.write(f"images/{panel.panel_id}.json", record)
            self.store.event("generate", panel_id=panel.panel_id, **record)

    def assemble(self):
        panels = self.panels()
        images = {}
        for panel in panels:
            panel_review = self.approvals().get("panel:" + panel.panel_id, {})
            if panel_review.get("decision") != "approve" or panel_review.get("stamp") != self.panel_stamp(panel):
                raise ValueError(f"Panel {panel.panel_id} requires current composition/continuity approval")
            record = self.image_record(panel)
            review = self.approvals().get("image:" + panel.panel_id, {})
            if review.get("decision") != "approve" or review.get("stamp") != digest(record):
                raise ValueError(f"Image {panel.panel_id} requires human visual/scripture review")
            images[panel.panel_id] = record
        path = assemble(self.store, panels, images)
        self.store.write("final/manifest.json", {"scene_stamp": self.stamp(), "panels": [p.model_dump() for p in panels], "images": images})
        self.store.event("assemble", path=str(path))
        return path
