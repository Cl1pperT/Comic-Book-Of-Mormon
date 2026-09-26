import json
import math
from pathlib import Path
from .models import Panel

NEGATIVE = ["No additional named characters or unlisted events", "No modern objects or buildings",
            "No lettering, captions, logos, or speech bubbles in the image",
            "Never show Jesus Christ, the Father, a descending person, or a divine silhouette",
            "No events after 3 Nephi 11:7", "No unsupported doctrinal symbolism",
            "Do not alter approved important character identities or numbers",
            "No gore, parody, superhero imagery, humor, or fantasy embellishment",
            "Not a photograph or live-action film still; must read as drawn/painted comic illustration with visible linework"]

DEFAULT_CONTINUITY = {
    "characters": {}, "locations": {},
    "visual_style": {"description": "Reverent, hand-inked graphic novel illustration with visible linework and paint/ink "
                     "texture — never photoreal, never a photograph or film still. Architecture, when shown, is "
                     "Mesoamerican/Andean-inspired: stepped stone pyramid-temples, turquoise fretwork accents, terraced "
                     "stonework — never European or Asian. Vivid illustrated color, blue skies, warm sun, lush jungle "
                     "greenery — except storm, destruction, and darkness scenes, which stay desaturated and dramatic.",
                     "locked_traits": ["Hand-inked/painted comic illustration with visible linework; never photographic or photorealistic.",
                                       "Architecture is always Mesoamerican/Andean in style, never European or Asian.",
                                       "Color is vivid and saturated except in storm/destruction/darkness scenes."]}}

# A small, fixed shot vocabulary drives both the requested image aspect ratio and the page
# layout. Aspect values are the exact strings Gemini's image_config.aspect_ratio accepts.
SHOT_ASPECT = {"splash": "3:4", "wide": "16:9", "tall": "2:3", "medium": "4:3", "close": "1:1"}
SHOT_CAMERA = {
    "splash": "Full-page wide establishing view",
    "wide": "Wide establishing view",
    "tall": "Vertical framing, low or high angle",
    "medium": "Medium shot, eye level",
    "close": "Close, intimate framing on faces and gesture",
}
SHOT_MOOD = {
    "splash": "Reverent, monumental",
    "wide": "Reverent, expansive",
    "tall": "Reverent, dramatic",
    "medium": "Reverent, serious",
    "close": "Reverent, intimate",
}
SHOT_COMPOSITION = {
    "splash": "Full-bleed, single dominant focal point",
    "wide": "Wide horizontal framing, horizon-led composition",
    "tall": "Vertical framing, strong verticals",
    "medium": "Clear focal point, restrained detail",
    "close": "Tight two-shot or single reaction, faces readable",
}


def classify_shot(scene):
    """Pick a panel shape from fields the scene already carries after validation/approval.

    Deterministic and reviewable: it never reads scripture text or invents anything, only
    counts and flags already on the approved Scene (importance, dialogue, character count,
    lettering length). Every branch is keyed to content, not position, so two neighboring
    scenes that both deserve the same shape (e.g. two short, single-character beats) land
    on the same shot and can pair into a two-column row instead of only ever alternating.
    """
    if scene.importance == "major" or "3 Nephi 11:7" in scene.refs:
        return "splash"
    dialogue_words = sum(len(c.text.split()) for c in scene.spoken_dialogue)
    narration_words = sum(len(c.text.split()) for c in scene.narration)
    if scene.spoken_dialogue and 1 <= len(scene.characters) <= 2 and dialogue_words + narration_words <= 30:
        return "close"
    if not scene.characters or len(scene.characters) >= 4:
        return "wide"
    # Small cast, narration-led beat: a short, iconic moment reads well as a tall panel;
    # a longer one needs a medium panel's extra width to hold its lettering comfortably.
    return "tall" if narration_words and narration_words <= 12 else "medium"


def plan(scenes, target_pages=10):
    if not scenes:
        raise ValueError("No scenes to plan")
    panels, page, slot = [], 1, 0
    per_page = min(3, max(1, math.ceil(len(scenes) / target_pages)))
    for i, scene in enumerate(scenes, 1):
        major = scene.importance == "major" or "3 Nephi 11:7" in scene.refs
        if slot and (major or slot >= per_page):
            page, slot = page + 1, 0
        slot += 1
        shot = classify_shot(scene)
        panels.append(Panel(panel_id=f"panel_{i:03d}", scene_id=scene.scene_id,
            page=page, panel_number=slot, refs=scene.refs, characters_visible=scene.characters,
            location=scene.locations, action=scene.summary, shot=shot,
            mood=SHOT_MOOD[shot], camera=SHOT_CAMERA[shot], composition=SHOT_COMPOSITION[shot],
            dialogue=scene.spoken_dialogue,
            narration=scene.narration, visual_facts=scene.explicit_facts,
            visual_inferences=scene.reasonable_visual_inferences,
            creative_details=scene.unspecified_visual_details,
            prohibited=scene.prohibited_inventions))
        if major:
            page, slot = page + 1, 0
    return panels


def build_prompt(panel, continuity):
    return "\n\n".join([
        "Visualize only this approved panel. You do not decide the story.",
        "GLOBAL STYLE\n" + json.dumps(continuity["visual_style"]),
        "LOCATION CONSISTENCY (design choices are not scripture)\n" + json.dumps(
            {name: continuity["locations"].get(name, {}) for name in panel.location}),
        "CHARACTER CONSISTENCY (design choices are not scripture)\n" + json.dumps(
            {name: continuity["characters"].get(name, {}) for name in panel.characters_visible}),
        "APPROVED ACTION\n" + panel.action,
        "VISIBLE PEOPLE\n" + json.dumps(panel.characters_visible),
        "LOCATION\n" + json.dumps(panel.location),
        "EXPLICIT SCRIPTURAL FACTS\n" + json.dumps([c.model_dump() for c in panel.visual_facts]),
        "REASONABLE VISUAL INFERENCES\n" + json.dumps(panel.visual_inferences),
        "UNSPECIFIED CREATIVE DETAILS\n" + json.dumps(panel.creative_details),
        "PANEL SHAPE\n" + f"{panel.shot} panel; compose for a {SHOT_ASPECT[panel.shot]} aspect ratio",
        "MOOD / COMPOSITION / CAMERA\n" + " / ".join([panel.mood, panel.composition, panel.camera]),
        "NEGATIVE CONSTRAINTS\n" + "\n".join(NEGATIVE + panel.prohibited)])


# Page geometry in pixels at 150 dpi. Rows stack full width; art is never cropped.
PAGE_SIZE = (1400, 2000)
MARGIN, TOP, BOTTOM, GUTTER = 80, 110, 130, 34
BORDER, PAD, MIN_ART = 4, 24, 160
PAPER, INK, CAPTION, MATTE, MUTED = "#efe9dc", "#1f1c18", "#f9f5ea", "#161412", "#7a705f"
FONTS = Path(__file__).parent / "fonts"
# Shots that read well side by side: two verticals as a diptych, two close reactions as
# shot/reverse-shot. Everything else (splash/wide/medium, or an unpaired tall/close) stays
# full width. Capped at two columns so lettering stays legible at page size.
PAIRABLE_SHOTS = {"tall", "close"}


def _fonts():
    from PIL import ImageFont
    load = lambda name, size: ImageFont.truetype(str(FONTS / f"EBGaramond-{name}.ttf"), size)
    return {"body": load("Regular", 29), "speaker": load("SemiBold", 20), "ref": load("Italic", 22),
            "running": load("Regular", 21), "folio": load("Italic", 27)}


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def _tracked(draw, xy, text, font, fill, tracking, anchor="center"):
    """Letterspaced small-caps style label; anchor is 'left' or 'center' on the baseline."""
    widths = [draw.textlength(c, font=font) for c in text]
    x, y = xy
    if anchor == "center":
        x -= (sum(widths) + tracking * (len(text) - 1)) / 2
    for char, width in zip(text, widths):
        draw.text((x, y), char, font=font, fill=fill, anchor="ls")
        x += width + tracking


def _ref_range(refs):
    first, last = refs[0].rsplit(" ", 1)[1], refs[-1].rsplit(" ", 1)[1]
    if first == last:
        return f"3 Nephi {first}"
    (c1, _), (c2, v2) = first.split(":"), last.split(":")
    return f"3 Nephi {first}\u2013{v2 if c1 == c2 else last}"


def _caption(draw, panel, fonts, width):
    """Lettering blocks as (kind, text) rows plus the band height they need."""
    rows = []
    for speech in panel.dialogue:
        rows.append(("speaker", speech.speaker.upper()))
        rows += [("body", line) for line in _wrap(draw, f"\u201c{speech.text}\u201d", fonts["body"], width)]
        rows.append(("gap", ""))
    for claim in panel.narration:
        rows += [("body", line) for line in _wrap(draw, claim.text, fonts["body"], width)]
        rows.append(("gap", ""))
    rows.append(("ref", "; ".join(panel.refs)))
    heights = {"speaker": 29, "body": 36, "gap": 11, "ref": 29}
    return rows, 2 * PAD + sum(heights[kind] for kind, _ in rows) - 6, heights


def _rows(group):
    """Group a page's panels into layout rows: matching tall/tall or close/close panels
    pair into a two-column row; everything else is a single full-width row."""
    rows, i = [], 0
    while i < len(group):
        panel = group[i]
        if panel.shot in PAIRABLE_SHOTS and i + 1 < len(group) and group[i + 1].shot == panel.shot:
            rows.append(group[i:i + 2])
            i += 2
        else:
            rows.append(group[i:i + 1])
            i += 1
    return rows


def _tier_heights(natural, available):
    """Give every row its natural height if it fits, else cap all rows at one shared level."""
    if sum(natural) <= available:
        return natural
    level, remaining = available / len(natural), available
    for count, height in enumerate(sorted(natural)):
        share = remaining / (len(natural) - count)
        if height >= share:
            level = share
            break
        remaining -= height
    return [min(height, level) for height in natural]


def assemble(store, panels, images):
    from PIL import Image, ImageDraw, ImageFilter, ImageOps
    fonts = _fonts()
    width, height = PAGE_SIZE
    frame_w = width - 2 * MARGIN
    pages = []
    for number in sorted({p.page for p in panels}):
        group = sorted((p for p in panels if p.page == number), key=lambda p: p.panel_number)
        page = Image.new("RGB", PAGE_SIZE, PAPER)
        draw = ImageDraw.Draw(page)
        row_groups = _rows(group)
        rows = []
        for members in row_groups:
            cols = len(members)
            col_w = (frame_w - 2 * BORDER * cols - GUTTER * (cols - 1)) // cols
            entries = []
            for panel in members:
                with Image.open(store.path(images[panel.panel_id]["path"])) as im:
                    art = im.convert("RGB")
                caption_rows, caption_h, heights = _caption(draw, panel, fonts, col_w - 2 * PAD)
                natural_h = col_w * art.height / art.width
                entries.append((panel, art, natural_h, caption_rows, caption_h, heights))
            rows.append((col_w, entries))
        row_caption_h = [max(e[4] for e in entries) for _, entries in rows]
        chrome = sum(3 * BORDER + c for c in row_caption_h) + GUTTER * (len(rows) - 1)
        natural_heights = [max(e[2] for e in entries) for _, entries in rows]
        available = height - TOP - BOTTOM - chrome
        tiers = _tier_heights(natural_heights, available)
        # A lightly filled page (e.g. a single small row) would otherwise float in a wide
        # blank margin; scale rows up toward the available height, capped so a single very
        # wide/short row can't balloon into one dominated by letterbox wash.
        total = sum(tiers)
        if 0 < total < available:
            tiers = [t * min(available / total, 1.8) for t in tiers]
        for (_, entries), tier in zip(rows, tiers):
            if tier < MIN_ART:
                ids = ", ".join(panel.panel_id for panel, *_ in entries)
                raise ValueError(f"Too much lettering on {ids}; reduce text or panels per page")
        used = sum(int(t) for t in tiers) + chrome
        y = TOP + (height - TOP - BOTTOM - used) // 2
        for (col_w, entries), tier, cap_h in zip(rows, tiers, row_caption_h):
            tier = int(tier)
            panel_h = 3 * BORDER + tier + cap_h
            x = MARGIN
            for panel, art, natural_h, caption_rows, caption_h, heights in entries:
                draw.rectangle([x, y, x + col_w + 2 * BORDER - 1, y + panel_h - 1], fill=INK)
                art_box = (x + BORDER, y + BORDER)
                # Letterbox with a dim, defocused wash of the same art; the approved image is never cropped.
                wash = ImageOps.fit(art, (max(1, col_w // 8), max(1, tier // 8))).filter(ImageFilter.GaussianBlur(8))
                page.paste(Image.blend(wash.resize((col_w, tier)), Image.new("RGB", (col_w, tier), MATTE), 0.72), art_box)
                fit = ImageOps.contain(art, (col_w, tier), Image.LANCZOS)
                page.paste(fit, (art_box[0] + (col_w - fit.width) // 2, art_box[1] + (tier - fit.height) // 2))
                top = y + 2 * BORDER + tier
                draw.rectangle([x + BORDER, top, x + BORDER + col_w - 1, top + cap_h - 1], fill=CAPTION)
                tx, baseline = x + BORDER + PAD, top + PAD
                for kind, text in caption_rows:
                    baseline += heights[kind]
                    if kind == "speaker":
                        _tracked(draw, (tx, baseline - 6), text, fonts["speaker"], MUTED, 2.5, anchor="left")
                    elif kind == "body":
                        draw.text((tx, baseline - 8), text, font=fonts["body"], fill=INK, anchor="ls")
                    elif kind == "ref":
                        draw.text((x + BORDER + col_w - PAD, baseline - 6), text, font=fonts["ref"], fill=MUTED, anchor="rs")
                x += col_w + 2 * BORDER + GUTTER
            y += panel_h + GUTTER
        refs = [ref for p in group for ref in p.refs]
        _tracked(draw, (width / 2, TOP - 44), _ref_range(refs).upper(), fonts["running"], MUTED, 4)
        folio_y = height - BOTTOM / 2 + 8
        draw.text((width / 2, folio_y), str(number), font=fonts["folio"], fill=MUTED, anchor="ms")
        for side in (-1, 1):
            draw.line([(width / 2 + side * 34, folio_y - 8), (width / 2 + side * 90, folio_y - 8)], fill=MUTED, width=1)
        path = store.path(f"pages/page_{number:03d}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        page.save(path)
        pages.append(page)
    target = store.path("final/comic.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(target, save_all=True, append_images=pages[1:], resolution=150)
    return target
