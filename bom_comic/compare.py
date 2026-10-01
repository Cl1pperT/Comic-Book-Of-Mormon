"""Render the same panels under different ComfyUI settings, side by side at their size on the page.

Use it to judge a speed-up before adopting it, e.g. the smaller Q5 GGUF model against the Q8 one:

    python -m bom_comic.compare --run runs/book/1-nephi/004 --panels panel_003 panel_007 panel_013 \\
        --variant q8 --variant q5:unet=flux1-dev-Q5_K_S.gguf

A variant is NAME or NAME:key=value,... with keys unet, t5, steps, cfg, guidance, and size (unset keys keep the
COMFYUI_* settings). size is "frame" (render at the panel's size on the page), "frame-min-0.65" (the same, but never
below 0.65 megapixels), or a number of megapixels. Every variant draws each panel from the same prompt and the same seed, so only the setting differs.
Variants run one after another, so each model loads once; the first render of each variant includes that load and is
left out of its median time.

Writes runs/compare/<time>/: each render, timings.json, and sheet.png with one row per panel: the panel's current
drawing (made from the older prompt with another seed) and then each variant. Nothing in the chapter changes.
--prompts-only prints the prompts ComfyUI would get and renders nothing.
"""
import argparse
import datetime
import hashlib
import json
import statistics
import time
from pathlib import Path
from .comic import diffusion_prompts, frames, _fonts
from .storage import Store

SETTINGS = {"unet": str, "t5": str, "steps": int, "cfg": float, "guidance": float, "size": str}


def pixels_for(size, frame):
    """Render pixels for a size setting and the panel's frame (x, y, w, h) on the page."""
    if size == "frame" or size.startswith("frame-min-"):
        floor = float(size[len("frame-min-"):]) * 1e6 if size.startswith("frame-min-") else 0
        return int(max(frame[2] * frame[3], floor))
    return int(float(size) * 1e6)
LABEL_HEIGHT = 56


def parse_variant(text):
    name, _, rest = text.partition(":")
    overrides = {}
    for item in filter(None, rest.split(",")):
        key, _, value = item.partition("=")
        if key not in SETTINGS or not value:
            raise ValueError(f"Bad variant setting {item!r}; use {', '.join(SETTINGS)}")
        overrides[key] = SETTINGS[key](value)
    return name, overrides


def seed_for(run, panel_id):
    return int(hashlib.sha256(f"{Path(run).as_posix()}|{panel_id}".encode()).hexdigest()[:12], 16)


def compare(run, panel_ids, variants, out=None, library="portraits/book-of-mormon", provider_factory=None):
    """Render each panel under each (name, overrides) variant; returns the output folder."""
    from .pipeline import Pipeline
    from .providers import ComfyUI
    library = library if library and Path(library, "cast.json").exists() else None
    pipeline = Pipeline(Store(run), None, library)
    panels = {p.panel_id: p for p in pipeline.panels()}
    unknown = [pid for pid in panel_ids if pid not in panels]
    if unknown:
        raise ValueError(f"Unknown panels in {run}: {', '.join(unknown)}")
    aspects, layout, shown = pipeline.aspects(), frames(list(panels.values())), pipeline.shown(list(panels.values()))
    out = Path(out or Path("runs/compare") / datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    store = Store(out)
    make = provider_factory or (lambda: ComfyUI(store))
    timings = {}
    for name, overrides in variants:
        provider = make()
        for key, value in overrides.items():
            if key != "size":
                setattr(provider, key, value)
        renders = {}
        for pid in panel_ids:
            if "size" in overrides:
                provider.pixels = pixels_for(overrides["size"], layout[pid])
            prompt = pipeline.prompt_for(panels[pid], aspects[pid], shown=shown)
            path = store.path(f"{name}/{pid}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            provider.generate_image(prompt, output_path=path, aspect_ratio=aspects[pid], seed=seed_for(run, pid))
            renders[pid] = round(time.monotonic() - started, 1)
            print(f"{name} {pid}: {renders[pid]}s", flush=True)
        warm = list(renders.values())[1:] or list(renders.values())
        timings[name] = {"settings": {k: overrides.get(k, getattr(provider, k, None)) for k in SETTINGS},
                         "seconds": renders,
                         "median_seconds": statistics.median(warm)}
    store.write("timings.json", {"run": str(run), "panels": panel_ids, "variants": timings})
    sheet(pipeline, panel_ids, [name for name, _ in variants], layout, store, timings)
    return out


def sheet(pipeline, panel_ids, names, layout, store, timings):
    """One row per panel at its size on the page: the current drawing, then each variant."""
    from PIL import Image, ImageDraw, ImageOps
    fonts = _fonts()
    columns = ["current"] + names
    width = max(layout[pid][2] for pid in panel_ids)
    gutter = 16
    heights = [layout[pid][3] for pid in panel_ids]
    canvas = Image.new("RGB", (gutter + len(columns) * (width + gutter),
                               LABEL_HEIGHT + sum(h + gutter for h in heights) + gutter), "#efe9dc")
    draw = ImageDraw.Draw(canvas)
    for c, column in enumerate(columns):
        x = gutter + c * (width + gutter)
        label = "current drawing (older prompt, other seed)" if column == "current" else \
            f"{column}: median {timings[column]['median_seconds']}s per panel"
        draw.text((x, LABEL_HEIGHT - 16), label, font=fonts["ref"], fill="#1f1c18", anchor="ls")
    y = LABEL_HEIGHT
    for pid, height in zip(panel_ids, heights):
        size = (layout[pid][2], height)
        for c, column in enumerate(columns):
            if column == "current":
                record = store_path_of_current(pipeline, pid)
            else:
                record = store.path(f"{column}/{pid}.png")
            if record and Path(record).exists():
                with Image.open(record) as im:
                    canvas.paste(ImageOps.fit(im.convert("RGB"), size, Image.LANCZOS), (gutter + c * (width + gutter), y))
        draw.text((gutter, y + height + gutter - 3), pid, font=fonts["ref"], fill="#7a705f", anchor="ls")
        y += height + gutter
    canvas.save(store.path("sheet.png"))


def store_path_of_current(pipeline, panel_id):
    record = pipeline.store.path(f"images/{panel_id}.json")
    if not record.exists():
        return None
    return pipeline.store.path(json.loads(record.read_text(encoding="utf-8"))["path"])


def main():
    parser = argparse.ArgumentParser(description="Render panels under different ComfyUI settings, side by side")
    parser.add_argument("--run", required=True, help="A planned chapter, e.g. runs/book/1-nephi/004")
    parser.add_argument("--panels", nargs="+", help="Panel IDs (default: the first six)")
    parser.add_argument("--variant", action="append", default=[],
                        help="NAME or NAME:key=value,... (keys: unet, t5, steps, cfg, guidance); repeat per variant")
    parser.add_argument("--out", help="Output folder (default runs/compare/<time>)")
    parser.add_argument("--library", default="portraits/book-of-mormon")
    parser.add_argument("--prompts-only", action="store_true", help="Print the ComfyUI prompts; render nothing")
    args = parser.parse_args()
    from .config import Config
    from .pipeline import Pipeline
    Config.load()
    library = args.library if Path(args.library, "cast.json").exists() else None
    pipeline = Pipeline(Store(args.run), None, library)
    panels = pipeline.panels()
    ids = args.panels or [p.panel_id for p in panels[:6]]
    if args.prompts_only:
        aspects, shown = pipeline.aspects(), pipeline.shown(panels)
        for panel in panels:
            if panel.panel_id in ids:
                positive, negative = diffusion_prompts(pipeline.prompt_for(panel, aspects[panel.panel_id], shown=shown))
                print(f"{panel.panel_id}\n  POSITIVE: {positive}\n  NEGATIVE: {negative}\n")
        return
    variants = [parse_variant(v) for v in args.variant] or [("current-settings", {})]
    print(compare(args.run, ids, variants, args.out, args.library) / "sheet.png")


if __name__ == "__main__":
    main()
