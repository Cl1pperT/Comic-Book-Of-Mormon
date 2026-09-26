"""Offline software demonstration. All content is explicitly synthetic, not scripture."""
import argparse
from pathlib import Path
from bom_comic.storage import Store
from bom_comic.pipeline import Pipeline
from bom_comic.providers import PlaceholderImages
from bom_comic.models import SceneBatch, Scene, Claim, Verdict

class FixtureProvider(PlaceholderImages):
    def structured(self, prompt, schema, tag, kind="text"):
        if schema is Verdict:
            return Verdict(status="PASS WITH WARNINGS", issues=["Synthetic software fixture; not scriptural validation"])
        return SceneBatch(scenes=[Scene(scene_id="scene_001", title="SYNTHETIC SOFTWARE TEST",
            refs=["3 Nephi 8:1"], summary="A group stands in an unspecified place.",
            explicit_facts=[Claim(text="A group stands in an unspecified place.", refs=["3 Nephi 8:1"])],
            narration=[Claim(text="SYNTHETIC TEST: A group stands in an unspecified place.", refs=["3 Nephi 8:1"])])])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="runs/offline-demo")
    args = parser.parse_args()
    store = Store(args.run)
    source = store.path("synthetic-input.txt")
    source.write_text("3 Nephi 8:1 SYNTHETIC TEST: A group stands in an unspecified place.\n")
    pipeline = Pipeline(store, FixtureProvider())
    pipeline.init(source, "3 Nephi 8:1", "3 Nephi 8:1")
    pipeline.analyze()
    pipeline.validate()
    # Automated approvals exist ONLY here for synthetic software fixtures.
    pipeline.review("scene", "scene_001", "approve", "Synthetic test only")
    pipeline.plan(1)
    pipeline.review("panel", "panel_001", "approve", "Synthetic test only")
    pipeline.generate()
    pipeline.review("image", "panel_001", "approve", "Synthetic placeholder only")
    print(pipeline.assemble())
