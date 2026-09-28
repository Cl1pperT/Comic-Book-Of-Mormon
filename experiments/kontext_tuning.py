"""Tune the two-step render on one panel: same prompt and seed, different Kontext guidance values.

Renders into experiments/kontexttest/tuning/<run>/g<guidance>/ only (final panel plus its Flux draft);
nothing enters a run.
Usage: python experiments/kontext_tuning.py tests/nephitest-2 panel_001 2.5 4
"""
import sys
import time
from pathlib import Path
from bom_comic.comic import build_prompt
from bom_comic.pipeline import Pipeline
from bom_comic.providers import ComfyUI
from bom_comic.storage import Store

OUT = Path("experiments/kontexttest/tuning")

run, panel_id, guidances = Pipeline(Store(sys.argv[1])), sys.argv[2], sys.argv[3:] or ["2.5"]
panel = next(p for p in run.panels() if p.panel_id == panel_id)
portraits = run.panel_portraits(panel)
aspect = run.aspects()[panel_id]
prompt = build_prompt(panel, run.continuity(), aspect, list(portraits))
references = [(name, run.store.path(entry["path"])) for name, entry in portraits.items()]
for guidance in guidances:
    store = Store(OUT / Path(sys.argv[1]).name / f"g{guidance}")
    out = store.path(f"{panel_id}.png")  # same file name in every folder, so every variant gets the same seed
    out.parent.mkdir(parents=True, exist_ok=True)
    provider = ComfyUI(store)
    provider.kontext_guidance = float(guidance)
    started = time.monotonic()
    provider.generate_image(prompt, reference_images=references, output_path=out, aspect_ratio=aspect)
    print(f"guidance {guidance}", f"{time.monotonic() - started:.0f}s", out, flush=True)
