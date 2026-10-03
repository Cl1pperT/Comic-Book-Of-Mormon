"""The book's title page: Christ descending at Bountiful beneath the title, with a band of character panels below.

Every drawing is made like a panel (comic.build_prompt), from the chapter where its people appear, so the cast's
looks, the heavenly conventions and the house style all apply. Drawings are kept in runs/book/title/images and reused;
--redraw draws one again. The page is composed from them (no model calls) as runs/book/title/page.png, which
compile_pdf puts first.

    python -m bom_comic.title              # draw what's missing (starts ComfyUI if needed), then compose
    python -m bom_comic.title --redraw hero moroni
    python -m bom_comic.title --compose-only
"""
import argparse
import shutil
from pathlib import Path
from .comic import FONTS, INK, MARGIN, PAGE_SIZE, VISION_GOLD, _tracked, build_prompt
from .models import Panel
from .storage import Store

ROOT = Path("runs/book")
LIBRARY = "portraits/book-of-mormon"
TITLE, SUBTITLE = "The Book of Mormon", "Another Testament of Jesus Christ"
HERO_ASPECT = "7:10"
# (key, chapter folder whose cast resolves the label, label, place, action). 3 Nephi 11:8: "they saw a Man descending
# out of heaven; and he was clothed in a white robe".
HERO = ("hero", "3-nephi/011", "Jesus Christ", "Temple in the land Bountiful",
        "Jesus Christ descends out of heaven with his arms outstretched, rays of light breaking through the clouds "
        "behind him, above a great terraced stone temple of ancient South American design with broad stairways, a "
        "multitude gathered on the plaza below looking up in awe")
# Ten figures across the book, in its order: two rows of five.
CHARACTERS = [
    ("lehi", "1-nephi/001", "Lehi", "", "Lehi, an aged prophet, reads from a radiant open book in his vision", "LEHI"),
    ("nephi", "1-nephi/005", "Nephi", "", "Nephi holds the brass plates against his chest, resolute", "NEPHI"),
    ("jared", "ether/003", "The brother of Jared", "", "The brother of Jared holds up small clear stones on a mountaintop",
     "BROTHER OF JARED"),
    ("benjamin", "mosiah/002", "King Benjamin", "", "King Benjamin speaks to his people from a tall wooden tower",
     "KING BENJAMIN"),
    ("abinadi", "mosiah/013", "Abinadi", "", "Abinadi stands bound and unafraid before a king's court", "ABINADI"),
    ("alma", "alma/005", "Alma", "", "Alma preaches to the people with one hand raised", "ALMA"),
    ("ammon", "alma/017", "Ammon", "", "Ammon swings a sling at the waters of Sebus, defending the king's flocks",
     "AMMON"),
    ("moroni_captain", "alma/046", "Captain Moroni", "",
     "Captain Moroni lifts the title of liberty, a torn coat fastened to a pole, high in the wind", "CAPTAIN MORONI"),
    ("samuel", "helaman/016", "Samuel the Lamanite", "", "Samuel the Lamanite stands on a city wall as arrows fly past him",
     "SAMUEL"),
    ("moroni", "moroni/010", "Moroni", "", "Moroni, alone, kneels to hide the gold plates in the earth of a hillside",
     "MORONI"),
]
COLUMNS, PANEL_H, GAP, NAME_H = 5, 280, 14, 34
PANEL_W = (PAGE_SIZE[0] - 2 * MARGIN - (COLUMNS - 1) * GAP) // COLUMNS
PANEL_ASPECT = "8:9"  # about PANEL_W x PANEL_H


def _panel(pipeline, label, place, action, hero):
    return Panel(panel_id="panel_001", scene_id="title", page=1, panel_number=1, weight=6 if hero else 1,
                 refs=[pipeline.verses()[0].ref], characters_visible=[label], location=[place] if place else [],
                 action=action, shot="splash" if hero else "close",
                 mood="Reverent, glorious, monumental" if hero else "Heroic, reverent",
                 camera="Book-cover composition, low angle" if hero else "Waist-up heroic portrait, slight low angle",
                 composition=("Christ at upper center, open sky across the top fifth, the temple across the middle, "
                              "the crowd along the bottom") if hero else "One figure filling the frame",
                 dialogue=[], narration=[], visual_facts=[], visual_inferences=[], creative_details=[], prohibited=[])


def prompt_for(run, label, place, action, aspect, hero=False):
    """The structured prompt for one title-page drawing, drawn like a panel of that chapter."""
    from .pipeline import Pipeline
    pipeline = Pipeline(Store(ROOT / run), None, LIBRARY)
    panel = _panel(pipeline, label, place, action, hero)
    view, continuity = pipeline.shown([panel])[panel.panel_id]
    return build_prompt(view, continuity, aspect)


def draw(provider, store, redraw=()):
    """Draw the hero and the character panels that are missing (or named in redraw); returns the keys drawn."""
    from .pipeline import _draw
    jobs = [(HERO[0], HERO[1:], HERO_ASPECT, PAGE_SIZE, True)] + \
        [(key, (run, label, place, action), PANEL_ASPECT, (PANEL_W, PANEL_H), False)
         for key, run, label, place, action, _ in CHARACTERS]
    done = []
    for key, (run, label, place, action), aspect, size, hero in jobs:
        path = store.path(f"images/{key}.png")
        if path.exists() and key not in redraw:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        prompt = prompt_for(run, label, place, action, aspect, hero)
        store.write(f"prompts/{key}.json", {"prompt": prompt})
        # Each drawing keeps its own file (ComfyUI's seed comes from the name, so a redraw differs); key.png is the
        # one the page uses.
        revision = store.path(f"images/{key}_{len(list(path.parent.glob(key + '_*.png'))) + 1:02d}.png")
        _draw(provider, prompt, revision, aspect, size=None if hero else size)
        shutil.copyfile(revision, path)
        done.append(key)
        print(f"title: drew {key}", flush=True)
    return done


def compose(root=ROOT):
    """The title page from the drawings: hero full bleed, title and subtitle at the top, panels along the bottom."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    store = Store(Path(root) / "title")
    width, height = PAGE_SIZE
    with Image.open(store.path("images/hero.png")) as im:
        page = ImageOps.fit(im.convert("RGB"), PAGE_SIZE, Image.LANCZOS, centering=(0.5, 0.35))
    rows = -(-len(CHARACTERS) // COLUMNS)
    band_top = height - MARGIN - rows * PANEL_H - (rows - 1) * GAP
    # Dark bands behind the title and the panels, fading into the art.
    shade = Image.new("L", PAGE_SIZE, 0)
    shade_draw = ImageDraw.Draw(shade)
    for y in range(420):
        shade_draw.line([(0, y), (width, y)], fill=int(205 * (1 - y / 420) ** 0.9))
    for y in range(band_top - 160, height):
        shade_draw.line([(0, y), (width, y)], fill=int(225 * min(1, (y - band_top + 160) / 200) ** 1.2))
    page.paste(Image.new("RGB", PAGE_SIZE, INK), (0, 0), shade)
    draw = ImageDraw.Draw(page)
    font = lambda name, size: ImageFont.truetype(str(FONTS / f"EBGaramond-{name}.ttf"), size)
    title_font, sub_font, name_font = font("SemiBold", 118), font("Regular", 40), font("SemiBold", 19)
    # The title fills the width it can; the subtitle sits under a gold rule.
    while draw.textlength(TITLE.upper(), font=title_font) + 6 * (len(TITLE) - 1) > width - 2 * MARGIN:
        title_font = font("SemiBold", title_font.size - 4)
    _tracked(draw, (width / 2, 168), TITLE.upper(), title_font, "#f7efdc", 6)
    draw.line([(width / 2 - 260, 206), (width / 2 + 260, 206)], fill=VISION_GOLD, width=2)
    _tracked(draw, (width / 2, 262), SUBTITLE.upper(), sub_font, "#e9dcb8", 5)
    for i, (key, *_, name) in enumerate(CHARACTERS):
        row, column = divmod(i, COLUMNS)
        x, y = MARGIN + column * (PANEL_W + GAP), band_top + row * (PANEL_H + GAP)
        with Image.open(store.path(f"images/{key}.png")) as im:
            page.paste(ImageOps.fit(im.convert("RGB"), (PANEL_W, PANEL_H), Image.LANCZOS, centering=(0.5, 0.3)), (x, y))
        plate = Image.new("L", (PANEL_W, NAME_H), 175)
        page.paste(Image.new("RGB", (PANEL_W, NAME_H), INK), (x, y + PANEL_H - NAME_H), plate)
        _tracked(draw, (x + PANEL_W / 2, y + PANEL_H - 11), name, name_font, VISION_GOLD, 2)
        draw.rectangle([x, y, x + PANEL_W - 1, y + PANEL_H - 1], outline="#e9dcb8", width=3)
    out = store.path("page.png")
    page.save(out)
    return out


def main():
    parser = argparse.ArgumentParser(description="Draw and compose the book's title page")
    parser.add_argument("--redraw", nargs="*", default=[], help="Keys to draw again: hero, " +
                        ", ".join(c[0] for c in CHARACTERS))
    parser.add_argument("--compose-only", action="store_true", help="Compose from the existing drawings")
    args = parser.parse_args()
    if not args.compose_only:
        from .config import Config
        from .nightly import start_comfy
        from .providers import ComfyUI
        Config.load()
        store = Store(ROOT / "title")
        store.root.mkdir(parents=True, exist_ok=True)
        comfy = start_comfy(store.path("comfyui.log"))
        try:
            draw(ComfyUI(store), store, set(args.redraw))
        finally:
            if comfy:
                comfy.terminate()
    print(compose())


if __name__ == "__main__":
    main()
