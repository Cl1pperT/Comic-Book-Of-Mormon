import json
import math
import re
from pathlib import Path
from .models import Panel

NEGATIVE = ["No additional named characters or unlisted events", "No modern objects or buildings",
            "No lettering, captions, logos, or speech bubbles in the image",
            "No events beyond the approved scene's cited verses", "No unsupported doctrinal symbolism",
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
                                       "Color is vivid and saturated except in storm/destruction/darkness scenes.",
                                       "Armor is quilted cotton, hide, wood, or leather, never metal plate or chainmail."]}}

# Frame shapes come from the page layout; a shot only states the width/height ratio it prefers,
# which steers how the layout groups panels into rows.
SHOT_PREFERENCE = {"splash": 0.75, "wide": 1.78, "tall": 0.67, "medium": 1.33, "close": 1.0}
PAGE_UNITS = 6
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


def classify_weight(scene):
    """Share of a six-unit page: 6 = full page, 1 = one sixth. Pure function of approved data."""
    if scene.importance == "major" or "3 Nephi 11:7" in scene.refs:
        return 6
    # Lettering sits on the art, so text-heavy panels need more area to leave the art visible.
    words = sum(len(c.text.split()) for c in scene.spoken_dialogue + scene.narration)
    return 3 if words > 90 else 2 if words > 45 else 1


def plan(scenes):
    if not scenes:
        raise ValueError("No scenes to plan")
    weights = [classify_weight(scene) for scene in scenes]
    draw, fonts = _measure()
    # Heavier weight means a bigger frame, so repeat until every panel's lettering fits its frame.
    while True:
        panels = _paginate(scenes, weights)
        layout = frames(panels)
        tight = [i for i, p in enumerate(panels) if _lettering(draw, p, layout[p.panel_id], fonts) is None]
        if not tight:
            return panels
        for i in tight:
            if weights[i] == PAGE_UNITS:
                raise ValueError(f"Too much lettering for {panels[i].panel_id} even on a full page; split its scene")
            weights[i] += 1


def _paginate(scenes, weights):
    groups = [[]]
    for i, weight in enumerate(weights):
        if groups[-1] and sum(weights[j] for j in groups[-1]) + weight > PAGE_UNITS:
            groups.append([])
        groups[-1].append(i)
    # A lone small panel (e.g. just before a full-page moment) borrows its predecessor's last panel.
    for prev, group in zip(groups, groups[1:]):
        if len(group) == 1 and weights[group[0]] < PAGE_UNITS and len(prev) >= 3 \
                and weights[prev[-1]] + weights[group[0]] <= PAGE_UNITS:
            group.insert(0, prev.pop())
    position = {i: (page, slot) for page, group in enumerate(groups, 1) for slot, i in enumerate(group, 1)}
    panels = []
    for i, (scene, weight) in enumerate(zip(scenes, weights)):
        shot = classify_shot(scene)
        page, slot = position[i]
        panels.append(Panel(panel_id=f"panel_{i + 1:03d}", scene_id=scene.scene_id, page=page, panel_number=slot,
            weight=weight, refs=scene.refs, characters_visible=scene.characters,
            location=scene.locations, action=scene.summary, shot=shot,
            mood=SHOT_MOOD[shot], camera=SHOT_CAMERA[shot], composition=SHOT_COMPOSITION[shot],
            dialogue=scene.spoken_dialogue,
            narration=scene.narration, visual_facts=scene.explicit_facts,
            visual_inferences=scene.reasonable_visual_inferences,
            creative_details=scene.unspecified_visual_details,
            prohibited=scene.prohibited_inventions))
    return panels


PORTRAITS_HEADING = "CHARACTER REFERENCE PORTRAITS"
PORTRAIT_ASPECT = "3:4"
GROUP_ASPECT = "4:3"


def is_group(record):
    """A group record (a people, army, or faction) gets a costume sheet of several people, not one face."""
    return bool(record) and bool(record.get("group"))


def portrait_aspect(record):
    return GROUP_ASPECT if is_group(record) else PORTRAIT_ASPECT


def build_prompt(panel, continuity, aspect, portraits=()):
    w, h = (int(n) for n in aspect.split(":"))
    references = []
    if portraits:
        # Sent as the attached images, in this order; diffusion_prompts drops this section.
        records = continuity["characters"]
        people = [name for name in portraits if not is_group(records.get(name))]
        groups = [name for name in portraits if is_group(records.get(name))]
        lines = ["The attached images are, in order, references for: " + "; ".join(portraits) + "."]
        if people:
            lines.append("Reference portraits of individuals: " + "; ".join(people) + ". Draw each of these people"
                         " with the same face, build, hair, skin tone and clothing as their portrait.")
        if groups:
            lines.append("Costume sheets for groups: " + "; ".join(groups) + ". Dress and equip members of each group"
                         " as their sheet shows, but give every member their own face; a sheet shows no particular"
                         " individual, so never repeat one face across a crowd.")
        lines.append("Use references only for appearance: do not copy their pose, framing, lighting or plain"
                     " background, and do not add anyone because a reference is attached.")
        references = [PORTRAITS_HEADING + "\n" + " ".join(lines)]
    return "\n\n".join([
        "Visualize only this approved panel. You do not decide the story.",
        "GLOBAL STYLE\n" + json.dumps(continuity["visual_style"]),
        "LOCATION CONSISTENCY (design choices are not scripture)\n" + json.dumps(
            {name: continuity["locations"].get(name, {}) for name in panel.location}),
        "CHARACTER CONSISTENCY (design choices are not scripture)\n" + json.dumps(
            {name: continuity["characters"].get(name, {}) for name in panel.characters_visible}),
        *references,
        "APPROVED ACTION\n" + panel.action,
        "VISIBLE PEOPLE\n" + json.dumps(panel.characters_visible),
        "LOCATION\n" + json.dumps(panel.location),
        "EXPLICIT SCRIPTURAL FACTS\n" + json.dumps([c.model_dump() for c in panel.visual_facts]),
        "REASONABLE VISUAL INFERENCES\n" + json.dumps(panel.visual_inferences),
        "UNSPECIFIED CREATIVE DETAILS\n" + json.dumps(panel.creative_details),
        "PANEL SHAPE\n" + f"{panel.shot} panel; compose for a frame {w / h:.2f} times as wide as it is tall",
        "MOOD / COMPOSITION / CAMERA\n" + " / ".join([panel.mood, panel.composition, panel.camera]),
        "NEGATIVE CONSTRAINTS\n" + "\n".join(NEGATIVE + panel.prohibited)])


def portrait_eligible(record):
    """A character record can seed a reference portrait unless it opts out or gives nothing to draw from."""
    return bool(record) and record.get("reference_portrait", True) is not False and bool(
        record.get("scriptural_facts") or record.get("visual_design_choices"))


def build_portrait_prompt(name, record, style):
    """One character's reference portrait. Same sections as a panel prompt, so local rendering works too."""
    if is_group(record):
        return build_group_prompt(name, record, style)
    return "\n\n".join([
        "Draw one character reference portrait. It is a design reference used to keep this person looking the "
        "same across comic panels; it depicts no scene or event.",
        "GLOBAL STYLE\n" + json.dumps(style),
        "LOCATION CONSISTENCY (design choices are not scripture)\n{}",
        "CHARACTER CONSISTENCY (design choices are not scripture)\n" + json.dumps({name: record}),
        "APPROVED ACTION\n" + f"Reference portrait of {name}, alone, waist up, facing three-quarters toward the viewer",
        "VISIBLE PEOPLE\n" + json.dumps([name]),
        "LOCATION\n[]",
        "EXPLICIT SCRIPTURAL FACTS\n[]",
        "REASONABLE VISUAL INFERENCES\n[]",
        "UNSPECIFIED CREATIVE DETAILS\n" + json.dumps(["Plain, softly lit neutral background"]),
        "PANEL SHAPE\nReference portrait; compose for a frame 0.75 times as wide as it is tall",
        "MOOD / COMPOSITION / CAMERA\nCalm, neutral expression / Single figure centered, face and clothing clearly readable / Waist-up, eye level",
        "NEGATIVE CONSTRAINTS\n" + "\n".join(NEGATIVE + [
            "No other people in the image",
            "Do not invent an appearance the record doesn't state or allow; for a heavenly or divine figure, "
            "depict only appearance its scriptural facts state"])])


def build_group_prompt(name, record, style):
    """A group's costume sheet: several clearly different people sharing the group's dress and equipment."""
    return "\n\n".join([
        "Draw one costume reference sheet for a group of people. It is a design reference used to keep this group's "
        "dress, grooming and equipment consistent across comic panels; it depicts no scene or event and no "
        "particular individual.",
        "GLOBAL STYLE\n" + json.dumps(style),
        "LOCATION CONSISTENCY (design choices are not scripture)\n{}",
        "CHARACTER CONSISTENCY (design choices are not scripture)\n" + json.dumps({name: record}),
        "APPROVED ACTION\n" + f"Costume reference sheet of {name}: four members standing side by side, full body, "
        "each clearly a different person in age, build and face, all in the same group dress and equipment",
        "VISIBLE PEOPLE\n" + json.dumps([name]),
        "LOCATION\n[]",
        "EXPLICIT SCRIPTURAL FACTS\n[]",
        "REASONABLE VISUAL INFERENCES\n[]",
        "UNSPECIFIED CREATIVE DETAILS\n" + json.dumps(["Plain, softly lit neutral background"]),
        "PANEL SHAPE\nCostume sheet; compose for a frame 1.33 times as wide as it is tall",
        "MOOD / COMPOSITION / CAMERA\nNeutral standing poses / Four figures in a row, clothing and equipment clearly readable / Full body, eye level",
        "NEGATIVE CONSTRAINTS\n" + "\n".join(NEGATIVE + [
            "No identical faces or twins",
            "No combat, wounds, or action; figures simply stand",
            "Do not invent dress or equipment the record doesn't state or allow"])])


_NEGATED = re.compile(r"^(?:no|never|not|avoid|without|do not)\s", re.I)
_LEAD = re.compile(r"^(?:(?:no|never|not|avoid|without|do not)\s+)?(?:visually\s+)?(?:(?:show|depict|alter|imply)(?:ing)?\s+)?", re.I)
_TAIL = re.compile(r",\s*(?:no|never|not)\s+", re.I)
# Locked by NEGATIVE; a panel inference mentioning them would make a diffusion model draw text.
_FORBIDDEN = ("lettering", "caption", "speech bubble", "logo")


def _clauses(text):
    return [c.strip(" .") for c in re.split(r"[;—]|(?<=\.)\s+", text) if c.strip(" .")]


def _sort_clauses(texts, positive, negative, constraints=False):
    for text in texts:
        for clause in _clauses(text):
            head, *tails = _TAIL.split(clause)
            negated = bool(_NEGATED.match(head)) or (constraints and not head.lower().startswith("must "))
            for target, item in [(negative if negated else positive, head)] + [(negative, t) for t in tails]:
                if target is negative:
                    item = _LEAD.sub("", item)
                else:
                    item = ", ".join(p for p in item.split(", ") if not any(w in p.lower() for w in _FORBIDDEN))
                if item and item not in target:
                    target.append(item)


def prompt_section(prompt, heading):
    """One section's body from a build_prompt() prompt, or None."""
    for block in prompt.split("\n\n"):
        head, _, body = block.partition("\n")
        if head == heading:
            return body
    return None


def visible_people(prompt):
    """The VISIBLE PEOPLE list from a build_prompt() prompt."""
    body = prompt_section(prompt, "VISIBLE PEOPLE")
    return json.loads(body) if body else []


_ARCHITECTURE_CLAUSE = re.compile(r"^architecture\b", re.I)
_BUILT_WORDS = ("temple", "pyramid", "court", "palace", "city", "cities", "house", "building", "tower",
    "wall", "gate", "room", "chamber", "throne", "altar", "doorway", "dwelling", "town", "village",
    "fortress", "structure", "hall", "stairway", "ruins", "prison", "market")


def diffusion_prompts(prompt):
    # Diffusion models can't obey "no X" (naming X invites it), so negated clauses move to the
    # negative prompt. Nothing is added; preamble, creative-latitude and shape lines are dropped.
    sections, current = {}, None
    for block in prompt.split("\n\n"):
        head, _, body = block.partition("\n")
        if head in _SECTIONS:
            current, sections[head] = head, body
        elif current:
            sections[current] += "\n\n" + block
    style = json.loads(sections["GLOBAL STYLE"])
    people = json.loads(sections["VISIBLE PEOPLE"])
    places = json.loads(sections["LOCATION"])
    location_records = json.loads(sections["LOCATION CONSISTENCY (design choices are not scripture)"])
    # Flux weights early tokens most, so the panel's own content leads and global style follows.
    positive, negative = ["Scene: " + sections["APPROVED ACTION"].strip(" .\n")], []
    facts = [c["text"] for c in json.loads(sections["EXPLICIT SCRIPTURAL FACTS"])]
    inferences = json.loads(sections["REASONABLE VISUAL INFERENCES"])
    creative = json.loads(sections["UNSPECIFIED CREATIVE DETAILS"])
    _sort_clauses(inferences + facts, positive, negative)
    if people:
        positive.append("People: " + ", ".join(people))
    if places:
        positive.append("Setting: " + ", ".join(places))
    positive.append(sections["MOOD / COMPOSITION / CAMERA"].replace(" / ", ", "))
    # "Architecture ..." style rules only describe what a building should look like if the panel's
    # own content already calls for one; asserted unconditionally they put a temple in every panel,
    # including open-country scenes. Gate them on whether anything here actually mentions a structure.
    location_text = [text for record in location_records.values()
                      for text in record.get("scriptural_facts", []) + record.get("visual_design_choices", [])
                      + record.get("locked_traits", [])]
    location_text = [t["text"] if isinstance(t, dict) else t for t in location_text]
    built = any(word in text.lower() for text in places + facts + inferences + creative + location_text
                + [sections["APPROVED ACTION"]] for word in _BUILT_WORDS)
    style_positive, style_negative = [], []
    _sort_clauses([style.get("description", ""), *style.get("locked_traits", [])], style_positive, style_negative)
    architecture, other_style = [], []
    for clause in style_positive:
        (architecture if _ARCHITECTURE_CLAUSE.match(clause) else other_style).append(clause)
    positive += other_style + (architecture if built else [])
    negative += style_negative
    if not built:
        negative.append("buildings, temples, pyramids, palaces, or other man-made structures")
    for key in ("LOCATION CONSISTENCY (design choices are not scripture)", "CHARACTER CONSISTENCY (design choices are not scripture)"):
        for record in json.loads(sections[key]).values():
            facts = [f["text"] if isinstance(f, dict) else f for f in record.get("scriptural_facts", [])]
            _sort_clauses(facts + record.get("visual_design_choices", []) + record.get("locked_traits", []), positive, negative)
    for line in sections["NEGATIVE CONSTRAINTS"].splitlines():
        _sort_clauses([line], positive, negative, constraints=True)
    return ". ".join(positive) + ".", ", ".join(negative)


_SECTIONS = {"GLOBAL STYLE", "LOCATION CONSISTENCY (design choices are not scripture)",
             "CHARACTER CONSISTENCY (design choices are not scripture)", "APPROVED ACTION", "VISIBLE PEOPLE",
             "LOCATION", "EXPLICIT SCRIPTURAL FACTS", "REASONABLE VISUAL INFERENCES", "UNSPECIFIED CREATIVE DETAILS",
             "PANEL SHAPE", "MOOD / COMPOSITION / CAMERA", "NEGATIVE CONSTRAINTS", PORTRAITS_HEADING}


# Page geometry in pixels at 150 dpi. Panels fill the live area edge to edge with thin gutters.
PAGE_SIZE = (1400, 2000)
MARGIN, BOTTOM, GUTTER, BORDER, PAD = 48, 84, 14, 3, 14
MAX_ROW, MIN_ASPECT, MAX_ASPECT, FLOW_COST = 3, 0.4, 2.5, 0.35
MAX_COVER = 0.45
PAPER, INK, CAPTION, BALLOON, MUTED = "#efe9dc", "#1f1c18", "#f6ecd0", "#ffffff", "#7a705f"
LINE = {"speaker": 24, "body": 31, "gap": 8, "ref": 26}
FONTS = Path(__file__).parent / "fonts"


def _clamped(targets, lows, highs, total):
    """Scale targets to sum to total, pinning any that leave [low, high] and rescaling the rest."""
    pinned = {}
    while True:
        free = [i for i in range(len(targets)) if i not in pinned]
        if not free:
            break
        scale = (total - sum(pinned.values())) / sum(targets[i] for i in free)
        out = {i: lows[i] if targets[i] * scale < lows[i] else highs[i] for i in free
               if not lows[i] <= targets[i] * scale <= highs[i]}
        if not out:
            break
        pinned.update(out)
    heights = [pinned.get(i, targets[i] * scale if free else 0) for i in range(len(targets))]
    return [h * total / sum(heights) for h in heights]


def _row_frames(rows):
    """Widths within a row follow weight; row heights follow weight too, kept renderable."""
    width, height = PAGE_SIZE
    area_w, area_h = width - 2 * MARGIN, height - MARGIN - BOTTOM - GUTTER * (len(rows) - 1)
    widths = []
    for row in rows:
        avail, weight = area_w - GUTTER * (len(row) - 1), sum(p.weight for p in row)
        cols = [round(avail * p.weight / weight) for p in row[:-1]]
        widths.append(cols + [avail - sum(cols)])
    heights = _clamped([sum(p.weight for p in row) for row in rows],
                       [max(w) / MAX_ASPECT for w in widths], [min(w) / MIN_ASPECT for w in widths], area_h)
    heights = [round(h) for h in heights[:-1]] + [area_h - sum(round(h) for h in heights[:-1])]
    frames, y = [], MARGIN
    for cols, rh in zip(widths, heights):
        x = MARGIN
        for w in cols:
            frames.append((x, y, w, rh))
            x += w + GUTTER
        y += rh + GUTTER
    return frames


def page_frames(group):
    """Flow panels left to right, wrapping to a new row; pick the row breaks whose frame
    shapes best match each shot's preferred shape. Returns (x, y, w, h) per panel, in order."""
    best = None
    for mask in range(2 ** (len(group) - 1)):
        rows, row = [], [group[0]]
        for i, panel in enumerate(group[1:]):
            if mask >> i & 1:
                rows.append(row)
                row = []
            row.append(panel)
        rows.append(row)
        if max(len(r) for r in rows) > MAX_ROW:
            continue
        frames = _row_frames(rows)
        area, weight = sum(w * h for *_, w, h in frames), sum(p.weight for p in group)
        # Comics flow sideways: a lone panel in a row costs a little unless it's alone on the page.
        cost = FLOW_COST * sum(len(r) == 1 for r in rows) if len(group) > 1 else 0.0
        for panel, (_, _, w, h) in zip(group, frames):
            cost += math.log(w / h / SHOT_PREFERENCE[panel.shot]) ** 2
            cost += math.log((w * h / area) / (panel.weight / weight)) ** 2
            cost += 0 if MIN_ASPECT - 0.01 <= w / h <= MAX_ASPECT + 0.01 else 10
        if best is None or cost < best[0]:
            best = (cost, frames)
    return best[1]


def frames(panels):
    result = {}
    for number in sorted({p.page for p in panels}):
        group = sorted((p for p in panels if p.page == number), key=lambda p: p.panel_number)
        result.update(zip((p.panel_id for p in group), page_frames(group)))
    return result


def frame_aspect(frame):
    w, h = frame[2], frame[3]
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"


def _fonts():
    from PIL import ImageFont
    load = lambda name, size: ImageFont.truetype(str(FONTS / f"EBGaramond-{name}.ttf"), size)
    return {"body": load("Regular", 25), "speaker": load("SemiBold", 18), "ref": load("Italic", 19),
            "running": load("Regular", 19), "folio": load("Italic", 24)}


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
    book, first = refs[0].rsplit(" ", 1)
    last = refs[-1].rsplit(" ", 1)[1]
    if first == last:
        return f"{book} {first}"
    (c1, _), (c2, v2) = first.split(":"), last.split(":")
    return f"{book} {first}–{v2 if c1 == c2 else last}"


BOX_WIDTHS = (0.5, 0.65, 0.8, 1.0)


def _tracked_width(draw, text, font, tracking=2):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * max(0, len(text) - 1)


def _wrap_label(draw, label, font, width):
    """Speaker labels are letterspaced, so wrap them by their tracked width (e.g. "ONE DESCENDING FROM HEAVEN")."""
    lines, line = [], ""
    for word in label.split():
        trial = f"{line} {word}".strip()
        if line and _tracked_width(draw, trial, font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


def _rows(draw, blocks, ref, fonts, width):
    """Lettered rows for a caption box of a given width, and the box height they need."""
    rows = []
    for speaker, text in blocks:
        if rows:
            rows.append(("gap", ""))
        if speaker:
            rows += [("speaker", line) for line in _wrap_label(draw, speaker.upper(), fonts["speaker"], width - 2 * PAD)]
        rows += [("body", line) for line in _wrap(draw, text, fonts["body"], width - 2 * PAD)]
    if ref:
        rows.append(("ref", ref))
    return 2 * PAD + sum(LINE[kind] for kind, _ in rows), rows


def _box(draw, blocks, ref, fonts, inner_w, target_h):
    """Narrowest caption box (of a few widths) whose height stays under target_h."""
    for fraction in BOX_WIDTHS:
        width = int(inner_w * fraction)
        height, rows = _rows(draw, blocks, ref, fonts, width)
        if height <= target_h:
            break
    return width, height, rows


def _draw_box(draw, rows, box, fonts, fill):
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=fill, outline=INK, width=2)
    baseline = y0 + PAD
    for kind, text in rows:
        baseline += LINE[kind]
        if kind == "speaker":
            _tracked(draw, (x0 + PAD, baseline - 5), text, fonts["speaker"], MUTED, 2, anchor="left")
        elif kind == "body":
            draw.text((x0 + PAD, baseline - 7), text, font=fonts["body"], fill=INK, anchor="ls")
        elif kind == "ref":
            draw.text((x1 - PAD, baseline - 5), text, font=fonts["ref"], fill=MUTED, anchor="rs")


# Placement penalties break ties between equally clear layouts: the default corners first, then a mirrored
# corner, then narration on the bottom edge, then a reshaped box. MAX_TALL caps how tall a narrowed box may grow.
MIRROR, BOTTOM_NARRATION, RESHAPE, MAX_TALL = 1, 2, 3, 0.5


def _caption_options(draw, panel, frame, fonts):
    """Every corner and width each caption box may take, as (box, rows, fill, penalty), default first. Narration
    may sit in any corner, speech in either bottom corner; the words never change."""
    x, y, w, h = frame
    inner_w, inner_h = w - 2 * BORDER, h - 2 * BORDER
    left, top, right, bottom = x + BORDER, y + BORDER, x + w - BORDER, y + h - BORDER
    ref = _ref_range(panel.refs)
    narration = [(None, c.text) for c in panel.narration]
    speech = [(s.speaker, f"“{s.text}”") for s in panel.dialogue]

    def variants(blocks, ref, fill, corners):
        default = _box(draw, blocks, ref, fonts, inner_w, inner_h * 0.3)[0]
        out = []
        for fraction in BOX_WIDTHS:
            bw = int(inner_w * fraction)
            bh, rows = _rows(draw, blocks, ref, fonts, bw)
            if bw != default and bh > inner_h * MAX_TALL:
                continue
            for corner, penalty in corners:
                bx = left if corner.endswith("left") else right - bw
                by = top if corner.startswith("top") else bottom - bh
                out.append(((bx, by, bx + bw, by + bh), rows, fill, penalty + (0 if bw == default else RESHAPE)))
        return sorted(out, key=lambda option: option[3])

    options = []
    if narration:
        options.append(variants(narration, None if speech else ref, CAPTION,
                                [("top-left", 0), ("top-right", MIRROR), ("bottom-left", BOTTOM_NARRATION),
                                 ("bottom-right", BOTTOM_NARRATION + MIRROR)]))
    if speech or not narration:
        options.append(variants(speech, ref, BALLOON, [("bottom-right", 0), ("bottom-left", MIRROR)]))
    return options


def _fits(boxes, frame):
    """Whether caption boxes leave the art readable: no overlap, bounded cover, and narration read before
    speech when both share the bottom edge."""
    x, y, w, h = frame
    if sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) > MAX_COVER * (w - 2 * BORDER) * (h - 2 * BORDER):
        return False
    if len(boxes) == 2:
        (narration, speech) = boxes
        if _overlap(narration, speech):
            return False
        if narration[1] - y >= h / 2 and narration[0] >= speech[0]:
            return False
    return True


def _lettering(draw, panel, frame, fonts):
    """Default caption boxes for a frame, or None if they'd bury the art. Narration sits top-left,
    speech bottom-right, so reading runs corner to corner."""
    boxes = [options[0][:3] for options in _caption_options(draw, panel, frame, fonts)]
    return boxes if _fits([b for b, _, _ in boxes], frame) else None


def _measure():
    from PIL import Image, ImageDraw
    return ImageDraw.Draw(Image.new("RGB", (1, 1))), _fonts()


FACE_MODEL = Path(__file__).parent / "detectors" / "face_detection_yunet_2023mar.onnx"
FACE_MARGIN, FACE_FLAG = 0.2, 0.1


def detect_faces(image):
    """Face rectangles (x, y, w, h) in a PIL image, or None when OpenCV or the YuNet model is unavailable."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    if not FACE_MODEL.exists():
        return None
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    height, width = bgr.shape[:2]
    _, found = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (width, height), 0.5).detect(bgr)
    return [] if found is None else [tuple(int(v) for v in f[:4]) for f in found]


def _overlap(a, b):
    """Shared area of two (x0, y0, x1, y1) rectangles."""
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def place_captions(options, frame, faces):
    """Pick one option per caption box (see _caption_options) covering the least face area. Layouts must still
    fit (_fits); ties go to the lowest placement penalty, so a panel without faces keeps its default corners.
    Returns the chosen (box, rows, fill) list and the faces still covered."""
    from itertools import product
    x, y = frame[:2]
    raw = [(x + fx, y + fy, x + fx + fw, y + fy + fh) for fx, fy, fw, fh in faces or []]
    grown = [(a - FACE_MARGIN * (c - a), b - FACE_MARGIN * (d - b), c + FACE_MARGIN * (c - a), d + FACE_MARGIN * (d - b))
             for a, b, c, d in raw]
    best = None
    for choice in product(*options):
        if choice != tuple(o[0] for o in options) and not _fits([box for box, *_ in choice], frame):
            continue
        key = (sum(_overlap(box, face) for box, *_ in choice for face in grown), sum(o[3] for o in choice))
        if best is None or key < best[0]:
            best = (key, [o[:3] for o in choice])
    placed = best[1]
    covered = [f for f, r in zip(faces or [], raw)
               if sum(_overlap(box, r) for box, _, _ in placed) > FACE_FLAG * (r[2] - r[0]) * (r[3] - r[1])]
    return placed, covered


def _corner(box, frame):
    x, y, w, h = frame
    return ("top" if box[1] - y < h / 2 else "bottom") + "-" + ("left" if box[0] - x < w / 2 else "right")


def assemble(store, panels, images):
    from PIL import Image, ImageDraw, ImageOps
    fonts = _fonts()
    width, height = PAGE_SIZE
    layout = frames(panels)
    pages, placements = [], {}
    for number in sorted({p.page for p in panels}):
        group = sorted((p for p in panels if p.page == number), key=lambda p: p.panel_number)
        page = Image.new("RGB", PAGE_SIZE, PAPER)
        draw = ImageDraw.Draw(page)
        for panel in group:
            x, y, w, h = frame = layout[panel.panel_id]
            with Image.open(store.path(images[panel.panel_id]["path"])) as im:
                # Art is rendered for this frame's shape; fit only trims rounding differences.
                art = ImageOps.fit(im.convert("RGB"), (w, h), Image.LANCZOS)
            page.paste(art, (x, y))
            draw.rectangle([x, y, x + w - 1, y + h - 1], outline=INK, width=BORDER)
            if _lettering(draw, panel, frame, fonts) is None:
                raise ValueError(f"Too much lettering for {panel.panel_id}'s frame; raise its weight in panels.json")
            # Words are fixed at plan time; only each box's corner and width are chosen here, once the art shows
            # where faces are.
            faces = detect_faces(art)
            boxes, covered = place_captions(_caption_options(draw, panel, frame, fonts), frame, faces)
            for box, rows, fill in boxes:
                _draw_box(draw, rows, box, fonts, fill)
            placements[panel.panel_id] = {"page": number, "corners": [_corner(b, frame) for b, _, _ in boxes],
                                          "faces": faces, "covered_faces": covered}
            if covered:
                store.event("caption_overlap", panel_id=panel.panel_id, covered_faces=covered)
        baseline = height - BOTTOM / 2 + 8
        _tracked(draw, (MARGIN, baseline), _ref_range([r for p in group for r in p.refs]).upper(),
                 fonts["running"], MUTED, 3, anchor="left")
        draw.text((width - MARGIN, baseline), str(number), font=fonts["folio"], fill=MUTED, anchor="rs")
        path = store.path(f"pages/page_{number:03d}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        page.save(path)
        pages.append(page)
    store.write("final/captions.json", placements)
    target = store.path("final/comic.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(target, save_all=True, append_images=pages[1:], resolution=150)
    return target
