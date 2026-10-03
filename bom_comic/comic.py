import json
import math
import re
from pathlib import Path
from .analysis import vague_speaker
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


CORRECTIONS_HEADING = "REVIEWER CORRECTIONS"
VISION_HEADING = "DREAM OR VISION"

# Fixed art conventions for heavenly figures, matched on the scene's free-form labels. They are design choices, not
# scripture, and they win over records or scene prohibitions that would contradict them (e.g. "no invented wings").
# Applied when the prompt is built, not stored in records, so drawings made before them don't go stale.
# Entries: (kind, label pattern, subject words whose look-prohibitions give way, convention record, display name for
# the image model or None to keep the label).
HEAVENLY = [
    # Christ keeps the look the renders have given him; a fixed name also stops "the Lamb of God" being drawn as a
    # lamb. No subjects: prohibitions about him (e.g. no halo) still apply.
    ("christ", re.compile(r"^(?:the\s+)?(?:Jesus|Christ|Lamb of God|Son of God|Redeemer|Messiah|One descending)\b",
                          re.I), (), {
        "visual_design_choices": ["Jesus Christ, a man clothed in a white robe."],
        "locked_traits": ["Always the same man in a white robe."],
        "visual_tag": "a man clothed in a white robe"}, "Jesus Christ"),
    ("spirit", re.compile(r"^(?:[Tt]he\s+)?(?:Spirit\b|Holy Ghost\b)"), ("spirit", "holy ghost"), {
        "visual_design_choices": ["Shown only as a tall column of soft, radiant white light: no body, face, "
                                  "or human features of any kind."],
        "locked_traits": ["Always the same column of white light, never a person."],
        "visual_tag": "a tall column of soft, radiant white light"}, None),
    ("god", re.compile(r"^(?:the\s+)?(?:God(?: the Father)?|Lord God|Lord)\b(?!['’]|\s+of\b)", re.I),
     ("god", "lord", "father"), {
        "visual_design_choices": ["A person made entirely of brilliant white light: a human outline so bright that "
                                  "no face, features, or clothing can be made out."],
        "locked_traits": ["Always the same figure of pure light, never a detailed face or body."],
        "visual_tag": "a person made entirely of brilliant white light, a glowing human outline"}, None),
    # Every angel is the same figure, so it stays consistent from panel to panel (a crowd of angels shares it). No
    # wings: scripture never gives angels wings, and drawn wings bled onto the people beside them. A man, as the text
    # calls an angel "he" (1 Nephi 3:30; 1 Nephi 11:11 "in the form of a man"); the golden glow sets him apart from
    # Christ's plain white robe.
    ("angel", re.compile(r"^(?:(?:the|an|a)\s+)?(?:numberless\s+)?(?:concourses?\s+of\s+)?angels?\b", re.I),
     ("angel",), {
        "visual_design_choices": ["A heavenly messenger: a radiant man of about thirty with a calm, kind face, in "
                                  "shining white robes, his whole figure glowing with soft golden-white light, standing "
                                  "in the air a little above the ground."],
        "locked_traits": ["Every angel has this same face, shining white robes, and golden glow in every panel.",
                          "No wings, ever."],
        "visual_tag": "a radiant man in shining white robes, his whole figure glowing with soft golden-white light, "
                      "standing in the air a little above the ground"}, None),
]
# Objects drawn the same way wherever a panel's action names them, applied when the prompt is built like HEAVENLY.
# Entries: (pattern over the action and inferences, description for the image model, things it must not look like,
# and optionally a pattern that keeps the entry out of a panel even when the first one matches).
CHURCH = re.compile(r"\bgreat and abominable church\b|\babominable church\b|\bwhore of all the earth\b|"
                    r"\bmother of (?:abominations|harlots)\b|\bchurch of the devil\b|\bgreat church\b", re.I)
OBJECTS = [
    # The Liahona (1 Nephi 16:10, 16:29; Alma 37:38): "a round ball of curious workmanship... of fine brass", two
    # spindles within, writing on them. "The compass" in 1 Nephi 18 must not bring a modern magnetic compass along.
    (re.compile(r"\bLiahona\b|\bthe compass\b|\bball\b|\bspindles?\b|\bdirector\b", re.I),
     "The Liahona: a small round ball of fine polished brass, small enough to hold in two hands, its surface finely "
     "engraved with intricate curling patterns, open at the top to show two slender brass spindles inside, faint "
     "engraved writing along the spindles",
     ["giant ball", "boulder-sized ball", "modern magnetic compass", "compass rose", "compass needle dial",
      "clock face", "glass globe", "crystal ball"]),
    # The plates the records are kept on (brass plates, Nephi's plates of ore, Mormon's and the Jaredite plates):
    # engraved metal sheets, never a printed book. One look for all of them, in a gold-bronze between brass and gold.
    # "Record" counts only as a thing kept, not "bear record" (testify); "plates" only plural, so "plate armor" and
    # "breastplate" don't. "Book" is left alone: Lehi's book (1 Nephi 1) and the Bible (1 Nephi 13) are books.
    (re.compile(r"\bplates\b|\bengravings?\b|(?<!bear )(?<!bore )(?<!bears )\brecords? (?:of|engraved|engraven|kept)\b|"
                r"\b(?:the|his|her|their|our|my|these|those|this|its|sacred|father[’']?s)\s+records?\b(?! that)", re.I),
     # Sized against the people holding them: some drawings made them table-sized slabs.
     "The plates: a small squared stack of stiff, flat sheets of warm gold-bronze metal, each sheet about the size "
     "of a man's two hands side by side, the whole stack a few inches thick and small enough to carry under one "
     "arm, held together along one edge by three metal rings, each sheet engraved with rows of small characters; "
     "any writing on them is done with a pointed metal stylus",
     ["modern book", "open book", "two-page spread", "curved or bending pages", "hardcover book", "leather-bound book",
      "printed book", "paper pages", "codex", "spiral binding", "quill", "ink pen", "ink", "giant plates",
      "oversized metal slabs", "plates as large as a table", "huge tablets"],
     # A panel showing a real book (the Bible is "a record of the Jews", 1 Nephi 13:23), testifying ("bears record"),
     # or a figure of speech ("graven upon his palms") is not about the plates.
     re.compile(r"\bbook\b|\bbears? record\b|\bbore record\b|\bgraven upon\b|\bfigurative\b", re.I)),
    # The church of the devil (1 Nephi 13:4-9, 14:9-17; 2 Nephi 28:18): scripture's "whore of all the earth" and
    # "mother of abominations" name a church, so it is never drawn as a woman or one figure. Its look comes from the
    # verses' gold, silver, silks, scarlets and fine-twined linen and its sitting "upon many waters"; no building, so
    # it carries no real church's (or the Nephites' own) architecture or symbols.
    (CHURCH,
     "The great and abominable church, shown as a vast proud throng: crowds of people in costly scarlet, purple and "
     "gold clothing amid heaps of gold, silver and fine silks, spread across dark, many waters under a shadowed sky",
     ["a woman seated on the waters", "a single robed figure", "glowing figure", "halo", "cathedral", "church building",
      "cross", "clergy robes", "religious symbols"]),
]
# Labels for the church of the devil in VISIBLE PEOPLE: drawn by its OBJECTS entry, not as a person. "Harlots" goes
# with it only in a panel about the church (1 Nephi 13:7), where the throng shows the worldliness the verse names.
_CHURCH_PEOPLE = re.compile(r"^(?:the\s+)?harlots$", re.I)


def symbolic(name, action=""):
    """Whether a VISIBLE PEOPLE label stands for something drawn as a symbol (OBJECTS) rather than a person."""
    return bool(CHURCH.search(name) or (_CHURCH_PEOPLE.match(name.strip()) and CHURCH.search(action)))
# A prohibition about one of these figures that mentions how it looks contradicts its convention.
_APPEARANCE = ("wing", "halo", "glow", "light", "radian", "robe", "white", "form", "body", "appearance", "figure",
               "face", "feature", "visib", "depict", "shape", "gender", "woman", "female")


def heavenly(name):
    """(kind, subject words, convention record, display name) for a heavenly figure's label, or None."""
    for kind, pattern, subjects, record, display in HEAVENLY:
        if pattern.match(name.strip()):
            return kind, subjects, record, display
    return None


def _contradicts(text, subjects):
    low = text.lower()
    return any(s in low for s in subjects) and any(a in low for a in _APPEARANCE)


def _conventional(name, record):
    """A character record with its heavenly convention applied (unchanged for everyone else)."""
    match = heavenly(name)
    if not match:
        return record
    _, subjects, convention, _ = match
    # In the figure's own record the subject is implied, so any rule about its look gives way to the convention.
    kept = [t for t in record.get("locked_traits", []) if not any(a in t.lower() for a in _APPEARANCE)]
    out = {**record, "visual_design_choices": convention["visual_design_choices"],
           "locked_traits": convention["locked_traits"] + kept}
    # The diffusion prompt's short look (the design text is phrased partly as negations, which it can't use).
    out.pop("visual_tag", None)
    if convention.get("visual_tag"):
        out["visual_tag"] = convention["visual_tag"]
    return out


def build_prompt(panel, continuity, aspect, portraits=(), corrections=None, vision=None):
    """corrections: {"add": [...], "avoid": [...]} from a reviewer's flag on an earlier drawing of this panel.
    vision: {"kind": "dream" or "vision", "seer": ...} when the panel shows what someone sees in one (chapter.py)."""
    w, h = (int(n) for n in aspect.split(":"))
    # The church of the devil is drawn from its OBJECTS entry, never as one of the people in the frame.
    panel = panel.model_copy(update={"characters_visible": [n for n in panel.characters_visible
                                                            if not symbolic(n, panel.action)]})
    # Subjects of the heavenly figures in this panel: prohibitions about how they look give way to the conventions.
    subjects = tuple(s for name in panel.characters_visible if heavenly(name) for s in heavenly(name)[1])
    # An angel's glow bleeds onto everyone else in the frame; say who has it. Merged into the corrections so it sits
    # near the front of the prompt.
    mortals = [name for name in panel.characters_visible if not heavenly(name)]
    if mortals and any((heavenly(name) or [None])[0] == "angel" for name in panel.characters_visible):
        staging = {"add": ["Only the angel glows; " + ", ".join(mortals) + " are ordinary earthly people in plain "
                           "daylight colors"], "avoid": ["glowing people", "winged people"]}
        corrections = {key: staging[key] + list((corrections or {}).get(key, [])) for key in ("add", "avoid")}
    references = []
    if corrections and (corrections.get("add") or corrections.get("avoid")):
        references.append(CORRECTIONS_HEADING + "\n" + json.dumps(
            {"add": corrections.get("add", []), "avoid": corrections.get("avoid", [])}))
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
        references.append(PORTRAITS_HEADING + "\n" + " ".join(lines))
    return "\n\n".join([
        "Visualize only this approved panel. You do not decide the story.",
        "GLOBAL STYLE\n" + json.dumps(continuity["visual_style"]),
        "LOCATION CONSISTENCY (design choices are not scripture)\n" + json.dumps(
            {name: continuity["locations"].get(name, {}) for name in panel.location}),
        "CHARACTER CONSISTENCY (design choices are not scripture)\n" + json.dumps(
            {name: _conventional(name, continuity["characters"].get(name, {}))
             for name in panel.characters_visible}),
        *references,
        *([VISION_HEADING + "\n" + json.dumps(vision)] if vision else []),
        "APPROVED ACTION\n" + panel.action,
        "VISIBLE PEOPLE\n" + json.dumps(panel.characters_visible),
        "LOCATION\n" + json.dumps(panel.location),
        "EXPLICIT SCRIPTURAL FACTS\n" + json.dumps([c.model_dump() for c in panel.visual_facts]),
        "REASONABLE VISUAL INFERENCES\n" + json.dumps(panel.visual_inferences),
        "UNSPECIFIED CREATIVE DETAILS\n" + json.dumps(panel.creative_details),
        "PANEL SHAPE\n" + f"{panel.shot} panel; compose for a frame {w / h:.2f} times as wide as it is tall",
        "MOOD / COMPOSITION / CAMERA\n" + " / ".join([panel.mood, panel.composition, panel.camera]),
        "NEGATIVE CONSTRAINTS\n" + "\n".join(NEGATIVE + [p for p in panel.prohibited if not _contradicts(p, subjects)])])


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


# The approved prompt's rules are written for a reader ("No events beyond the approved scene's cited verses"); an
# image model can't act on those, and naming a thing in a negative prompt only helps when the thing is concrete. So
# the diffusion negative is this fixed list of things the renders actually drift into, plus only the concrete parts of
# the scene's own prohibitions and records ("weapons", "halo", "domes").
# Wings are in every panel's list: no one in the book has them, and Flux otherwise adds them near any glow.
DIFFUSION_NEGATIVE = ["winged people", "angel wings", "feathered wings",
                      "text", "lettering", "captions", "speech bubbles", "logo", "watermark", "signature",
                      "photograph", "photorealistic", "3d render", "modern clothing", "modern objects", "eyeglasses",
                      "backpacks", "zippers", "wristwatches", "baseball caps", "gore", "blood", "superhero costume",
                      "cartoon caricature"]
_ABSTRACT = re.compile(
    r"\b(?:verses?|scriptur\w*|texts?|textual|invent\w*|events?|narrat\w*|impl(?:y|ied|ying|ies)|identif\w*|assign\w*|"
    r"appearance|describ\w*|stat(?:e|ed|es)|specif\w*|present|presence|speech|speak\w*|words?|spoken|story|"
    r"doctrin\w*|symbol\w*|future|past|later|earlier|depict\w*|portray\w*|show\w*|nam(?:e|ed|es|ing)|label\w*|"
    r"interpret\w*|literal\w*|metaphor\w*|reported|beyond|unsupported|approved|identit\w*|numbers?|chapters?|"
    r"sources?|records?|unless|except|only|should|would|could|may|might|must|possib\w*|feared|visibl[ey]|"
    r"physical\w*|bodily|direct\w*|quot\w*|dialogue|off-screen|conveyed|outside|clarif\w*|indicat\w*|suggest\w*|"
    r"contradict\w*|accura\w*|diagnos\w*|condition|anachronis\w*|embellish\w*|fantasy|parody|humor)\b", re.I)
_NEGATION = re.compile(r"\b(?:no|not|never|nothing|without|neither|nor)\b|n't\b", re.I)
_HEDGE = re.compile(r"\b(?:can|could|may|might|should|would|will)\s+(?:also\s+)?(?:be\s+)?(?:shown|depicted|drawn|"
                    r"portrayed|represented|visualized|illustrated|seen)(?:\s+as)?\s*", re.I)
_META = re.compile(r"off-?screen|\brepresent|visual beat|\bpanel\b|without assigning|\bdescribed\b|\bimplied\b|"
                   r"\bconvey|\bindicat|\bsuggest|\bsymboli|\bmetaphor|\bcaption|\blettering|\binvent", re.I)
_NUMBERS = ["", "one", "two", "three", "four", "five", "six"]
# A style line that points the reader at the records instead of describing a look.
_STYLE_META = re.compile(r"\b(?:described|record|records)\b", re.I)


def _moment(action, inferences, limit=55):
    """What the panel shows, as concrete as the approved scene allows: the first sentence of its summary (later
    sentences carry motives and reported speech, e.g. "They desire his company so the Jews will not learn of their
    flight"), then its visual inferences with the hedges ("can be shown") removed. Negated and meta clauses are
    dropped: they describe what not to draw, which a positive prompt can only invite. Joined with semicolons, so the
    prompt's sentences stay one per section."""
    first = re.split(r"(?<=[.!?])\s+", action.strip())[0] if action.strip() else ""
    parts = []
    for clause in _clauses(first) + [c for text in inferences for c in _clauses(text)]:
        if _NEGATION.search(clause) or _META.search(clause):
            continue
        clause = _HEDGE.sub("", clause).strip(" ,.")
        low = clause.lower()
        if not clause or any(low in p.lower() or p.lower() in low for p in parts):
            continue
        if parts and sum(len(p.split()) for p in parts) + len(clause.split()) > limit:
            break
        parts.append(clause)
    return "; ".join(parts) if parts else action.strip(" .\n")


def _concrete(clause, names):
    """The thing a prohibition names, if it is something an image model can steer away from, else None. Mentions of
    the panel's own people are stripped ("halo on the Lamb of God" -> "halo"); a prohibition about a person who is
    in the panel is dropped, since negating a name suppresses the person."""
    text = _LEAD.sub("", clause.strip(" .")).strip(" ,")
    text = re.sub(r"^(?:any|an?|the)\s+", "", text, flags=re.I)
    for name in names:
        text = re.sub(r"\s*\b(?:on|for|of|around|over|near|upon|to|with|from|about)\s+(?:the\s+)?" + re.escape(name)
                      + r"(?:'s|’s)?\b", "", text, flags=re.I)
    if not text or len(text.split()) > 7 or _ABSTRACT.search(text) or re.search(r"[\d:\"“”]", text):
        return None
    if any(re.search(r"\b" + re.escape(word) + r"\b", text, re.I) for name in names for word in _name_words(name)):
        return None
    # Someone not in the panel ("Nephi's parents", "the Lamb of God"): naming a person can't steer a drawing away.
    if re.search(r"\b[A-Z][\w-]*['’]s\b", text) or re.search(r"\s[A-Z]", text):
        return None
    return text


def _name_words(name):
    """The words of a label that identify someone ("Nephi", "Lamb", "Christ"), not "the" or "of"."""
    return [w for w in re.findall(r"[A-Z][\w'’-]+", name) if w not in ("The", "A", "An")] or [name]


def _look(record, limit):
    """A person's or place's look in a few words, from its visual tag or its design choices."""
    from .cast import auto_tag
    tag = record.get("visual_tag") or auto_tag(record, limit)
    return tag.replace(". ", ", ").strip(" .")


# A short style statement placed ahead of the scene in the diffusion prompt (compared with and without in
# runs/compare/style-anchor). It isn't part of the art stamp, so changing it doesn't make existing drawings stale.
# Positive wording only: "never a photograph" is already in the negative prompt.
STYLE_ANCHOR = "Hand-inked graphic novel illustration, bold black ink linework, painted comic color"


def diffusion_prompts(prompt):
    """(positive, negative) for a diffusion model from an approved build_prompt() prompt.

    The positive prompt is a picture, not the story: the concrete moment, then each visible person by name with
    their own look beside it (so Flux can tell whose mantle is blue), then the setting and its look, the camera, and
    the style. Scripture facts, motives and summaries stay in the approved prompt for review; they aren't drawable.
    Diffusion models can't obey "no X" (naming X invites it), so negations move to the negative prompt, and only
    the concrete ones (see DIFFUSION_NEGATIVE)."""
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
    action = sections["APPROVED ACTION"]
    location_records = json.loads(sections["LOCATION CONSISTENCY (design choices are not scripture)"])
    character_records = json.loads(sections["CHARACTER CONSISTENCY (design choices are not scripture)"])
    inferences = json.loads(sections["REASONABLE VISUAL INFERENCES"])
    creative = json.loads(sections["UNSPECIFIED CREATIVE DETAILS"])
    # Flux weights early tokens most, so the panel's own content leads and global style follows.
    positive, negative = ["Scene: " + _moment(action, inferences)], []
    # A reviewer's corrections to an earlier drawing come right after the scene, where they carry the most weight.
    corrections = json.loads(sections.get(CORRECTIONS_HEADING, "{}"))
    positive += [item.strip(" .") for item in corrections.get("add", []) if item.strip(" .")]
    negative += [item.strip(" .") for item in corrections.get("avoid", []) if item.strip(" .")]
    vision = json.loads(sections.get(VISION_HEADING, "null"))
    if vision:
        # Seen, not happening: a dream or vision has its own light, so the reader can tell it from the story around it.
        positive.append(f"Seen in {vision['seer']}'s {vision['kind']}: a luminous, dreamlike scene bathed in soft "
                        "golden light, its edges dissolving into glowing haze")
    negative += DIFFUSION_NEGATIVE
    # Named objects look the same in every panel that shows them.
    throng = False
    for pattern, look, unlike, *unless in OBJECTS:
        text = " ".join([action, *inferences])
        if pattern.search(text) and not (unless and unless[0].search(text)):
            positive.append(look)
            negative += unlike
            throng = throng or pattern is CHURCH
    names = list(people)
    if people:
        entries, known = [], True
        for name in dict.fromkeys(people):
            record = character_records.get(name) or {}
            match = heavenly(name)
            # Heavenly figures with a fixed identity go by it (e.g. "the Lamb of God" is drawn as Jesus Christ).
            shown = (match[3] if match else None) or name
            names.append(shown)
            look = _look(record, 45 if match else 30) if record else ""
            if look.lower().startswith(shown.lower()):
                look = look[len(shown):].strip(" ,:")  # "Jesus Christ, a man clothed in a white robe"
            entries.append(f"{shown} ({look})" if look else shown)
            known = known and bool(record) and not match and not is_group(record)
        count = len(entries)
        # Every visible person is a known individual: say how many, so Flux doesn't pad the frame with extras.
        if known and count < len(_NUMBERS) and not throng:  # the church is drawn as a crowd
            head = "One person only" if count == 1 else f"Exactly {_NUMBERS[count]} people"
            positive.append(head + ": " + "; ".join(entries))
            negative += ["crowd", "extra people", "background figures"]
        else:
            positive.append("People: " + "; ".join(entries))
    # "Architecture" rules describe what a building should look like if the panel's own content already calls for
    # one; asserted unconditionally they put a temple in every panel, including open-country scenes. Setting
    # records keep their buildings apart for the same reason.
    unrecorded = [place for place in places if not location_records.get(place)]
    looks = [text for record in location_records.values() for text in record.get("visual_design_choices", [])]
    built = any(word in text.lower() for text in unrecorded + looks + inferences + creative + [action]
                for word in _BUILT_WORDS)
    if places:
        settings = []
        for place in places:
            record = location_records.get(place) or {}
            look = ", ".join(record.get("visual_design_choices", []) + (record.get("architecture", []) if built else []))
            look = look.replace(". ", ", ").strip(" .")
            settings.append(f"{place} ({look})" if look else place)
        positive.append("Setting: " + "; ".join(settings))
    positive.append(sections["MOOD / COMPOSITION / CAMERA"].replace(" / ", ", "))
    style_positive, style_negative = [], []
    _sort_clauses([style.get("description", ""), *style.get("locked_traits", [])], style_positive, style_negative)
    architecture, other_style = [], []
    for clause in style_positive:
        if _STYLE_META.search(clause):
            continue  # "Each character's clothing ... follow their own time and place as described in their record"
        (architecture if _ARCHITECTURE_CLAUSE.match(clause) else other_style).append(clause)
    positive += other_style + (architecture if built else [])
    negative += style_negative
    if not built:
        negative.append("buildings, temples, pyramids, palaces, or other man-made structures")
    # Records' own "never" rules: always a setting's; a person's only when they're alone in the frame, since one
    # person's "no crown" must not strip the crown from the king beside him.
    rules = [text for record in location_records.values() for text in record.get("locked_traits", [])]
    if len(people) == 1:
        rules += [text for record in character_records.values()
                  for text in record.get("visual_design_choices", []) + record.get("locked_traits", [])]
    for text in rules:
        for clause in _clauses(text):
            head, *tails = _TAIL.split(clause)
            for item in ([head] if _NEGATED.match(head) else []) + tails:
                negative.append(_concrete(item, names))
    for line in sections["NEGATIVE CONSTRAINTS"].splitlines():
        if line in NEGATIVE:
            continue  # the global rules, replaced by DIFFUSION_NEGATIVE
        for clause in _clauses(line):
            negative.append(_concrete(clause, names))
    seen, kept = set(), []
    for item in negative:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            kept.append(item)
    if STYLE_ANCHOR:
        # Flux's CLIP encoder reads only the first ~60 words, so a style named only at the end never reaches it.
        positive.insert(0, STYLE_ANCHOR)
    return ". ".join(positive) + ".", ", ".join(kept)


_SECTIONS = {"GLOBAL STYLE", "LOCATION CONSISTENCY (design choices are not scripture)",
             "CHARACTER CONSISTENCY (design choices are not scripture)", "APPROVED ACTION", "VISIBLE PEOPLE",
             "LOCATION", "EXPLICIT SCRIPTURAL FACTS", "REASONABLE VISUAL INFERENCES", "UNSPECIFIED CREATIVE DETAILS",
             "PANEL SHAPE", "MOOD / COMPOSITION / CAMERA", "NEGATIVE CONSTRAINTS", PORTRAITS_HEADING,
             CORRECTIONS_HEADING, VISION_HEADING}


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
    if not blocks:
        # A scene with no lettering keeps only its verse tag, sized to the tag, not an empty half-panel box.
        width = min(inner_w, int(draw.textlength(ref or "", font=fonts["ref"])) + 2 * PAD + 2)
        height, rows = _rows(draw, blocks, ref, fonts, width)
        return width, height, rows
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
    # A label that names no one ("UNIDENTIFIED SPEAKER") adds nothing on the page, so the quote stands alone.
    speech = [(None if vague_speaker(s.speaker) else s.speaker, f"“{s.text}”") for s in panel.dialogue]

    def variants(blocks, ref, fill, corners):
        default = _box(draw, blocks, ref, fonts, inner_w, inner_h * 0.3)[0]
        out = []
        for bw in [default] if not blocks else [int(inner_w * fraction) for fraction in BOX_WIDTHS]:
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


# A master shot is drawn once and reused, cropped differently, by the panels that continue it: the same people in
# the same place, talking. Its panels together may carry lettering over at most this share of a page's live area;
# past that the next panel gets a new drawing, so a long speech still turns into pictures, not a page of text.
MASTER_TEXT_SHARE = 2 / 3
# And a drawing appears at most this many times (the master and one continuing crop): a third panel of the same
# conversation gets a drawing of its own, so a page doesn't repeat one picture (the owner's rule after Jacob 3, where
# one drawing filled six panels).
MASTER_MAX_USES = 2


def _live_area():
    width, height = PAGE_SIZE
    return (width - 2 * MARGIN) * (height - MARGIN - BOTTOM)


def _speech_led(panel):
    spoken = sum(len(q.text.split()) for q in panel.dialogue)
    return bool(panel.dialogue) and spoken >= sum(len(c.text.split()) for c in panel.narration)


def shot_groups(panels, layout=None):
    """{panel_id: (master panel_id, index)} for each panel that continues a master shot (index 1, 2, ... in order).

    A panel continues the shot before it when it shows exactly the same people in the same place, is led by speech
    rather than new action, isn't a full-page moment, the shot's lettering so far plus its own stays within
    MASTER_TEXT_SHARE of a page, and the drawing has been used fewer than MASTER_MAX_USES times. A pure function of the approved panels and their layout, so nothing is stored and
    approvals don't change."""
    layout = layout or frames(panels)
    draw, fonts = _measure()
    budget = MASTER_TEXT_SHARE * _live_area()
    groups, master, key, used, index = {}, None, None, 0, 0
    for panel in panels:
        boxes = _lettering(draw, panel, layout[panel.panel_id], fonts) or []
        area = sum((b[2] - b[0]) * (b[3] - b[1]) for b, _, _ in boxes)
        here = (frozenset(panel.characters_visible), tuple(panel.location))
        if master is not None and here == key and panel.characters_visible and _speech_led(panel) \
                and panel.shot != "splash" and used + area <= budget and index + 1 < MASTER_MAX_USES:
            index += 1
            used += area
            groups[panel.panel_id] = (master.panel_id, index)
        else:
            master, key, used, index = panel, here, area, 0
    return groups


def master_crop(size, frame_size, index, faces=None):
    """The part of a master shot a continuing panel shows: about two thirds of it at the panel's own shape, moved
    to a different face (or third of the picture) each time, so a conversation reads as cuts between speakers."""
    width, height = size
    ratio = frame_size[0] / frame_size[1]
    crop_h = height * 0.7
    crop_w = crop_h * ratio
    if crop_w > width * 0.85:
        crop_w = width * 0.85
        crop_h = crop_w / ratio
    if crop_h > height:
        crop_h, crop_w = height, height * ratio
    faces = sorted(faces or [], key=lambda f: f[0])
    if faces:
        x, y, w, h = faces[(index - 1) % len(faces)]
        cx, cy = x + w / 2, y + h * 0.9  # a little below the eyes, so shoulders and gesture are in frame
    else:
        cx, cy = [(0.5, 0.45), (0.3, 0.45), (0.7, 0.45)][(index - 1) % 3]
        cx, cy = cx * width, cy * height
    left = min(max(0, cx - crop_w / 2), width - crop_w)
    top = min(max(0, cy - crop_h / 2), height - crop_h)
    return round(left), round(top), round(left + crop_w), round(top + crop_h)


# Dream and vision panels: a deep indigo frame with a gold inner line, and a label on the first of a run on each page.
VISION_INK, VISION_GOLD, VISION_BORDER = "#26305e", "#d4b25c", 9


def _vision_frame(page, draw, frame, label, boxes, fonts, art_faces):
    """Mark a dream or vision panel: soft glowing edges, the vision frame, and (when label) a small tab naming whose
    dream or vision it is, placed where it covers no caption box and the fewest faces."""
    from PIL import Image, ImageDraw, ImageFilter
    x, y, w, h = frame
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).rectangle([0, 0, w - 1, h - 1], outline=150, width=max(8, min(w, h) // 14))
    glow = glow.filter(ImageFilter.GaussianBlur(max(6, min(w, h) // 18)))
    page.paste(Image.new("RGB", (w, h), "#fff4d6"), (x, y), glow)
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=VISION_INK, width=VISION_BORDER)
    draw.rectangle([x + VISION_BORDER, y + VISION_BORDER, x + w - 1 - VISION_BORDER, y + h - 1 - VISION_BORDER],
                   outline=VISION_GOLD, width=2)
    if not label:
        return None
    tab_w = int(_tracked_width(draw, label, fonts["speaker"])) + 2 * PAD
    tab_h = 34
    inset = VISION_BORDER + 4
    spots = [(x + (w - tab_w) // 2, y + inset), (x + w - tab_w - inset, y + inset), (x + inset, y + inset),
             (x + (w - tab_w) // 2, y + h - tab_h - inset)]
    faces = [(x + fx, y + fy, x + fx + fw, y + fy + fh) for fx, fy, fw, fh in art_faces or []]
    def cost(spot):
        tab = (spot[0], spot[1], spot[0] + tab_w, spot[1] + tab_h)
        return (sum(_overlap(tab, b) for b in boxes), sum(_overlap(tab, f) for f in faces))
    left, top = min(spots, key=cost)
    draw.rectangle([left, top, left + tab_w, top + tab_h], fill=VISION_INK, outline=VISION_GOLD, width=2)
    _tracked(draw, (left + PAD, top + 23), label, fonts["speaker"], VISION_GOLD, 2, anchor="left")
    return [left, top, left + tab_w, top + tab_h]


def _cover_fonts():
    from PIL import ImageFont
    load = lambda name, size: ImageFont.truetype(str(FONTS / f"EBGaramond-{name}.ttf"), size)
    return {"title": load("SemiBold", 66), "kicker": load("Regular", 24), "heading": load("SemiBold", 20),
            "body": load("Regular", 27)}


def draw_cover(art_path, intro, where):
    """A chapter's cover page: its cover art full bleed, the chapter and its title across the top, and the opener
    card at the bottom in dark ink, set apart from the cream scripture captions."""
    from PIL import Image, ImageDraw, ImageOps
    width, height = PAGE_SIZE
    fonts = _cover_fonts()
    with Image.open(art_path) as im:
        page = ImageOps.fit(im.convert("RGB"), PAGE_SIZE, Image.LANCZOS)
    shade = Image.new("L", PAGE_SIZE, 0)
    shade_draw = ImageDraw.Draw(shade)
    for row in range(330):  # a dark band behind the title, fading into the art
        shade_draw.line([(0, row), (width, row)], fill=int(190 * (1 - row / 330) ** 0.8))
    page.paste(Image.new("RGB", PAGE_SIZE, INK), (0, 0), shade)
    draw = ImageDraw.Draw(page)
    book, chapter = where
    _tracked(draw, (width / 2, 92), f"{book} · Chapter {chapter}".upper(), fonts["kicker"], "#e9dcb8", 4)
    lines = _wrap(draw, intro["title"], fonts["title"], width - 4 * MARGIN)
    for i, line in enumerate(lines[:2]):
        draw.text((width / 2, 178 + i * 76), line, font=fonts["title"], fill=PAPER, anchor="ms")
    inner = width - 2 * MARGIN - 2 * 34
    sections = [(heading, text) for heading, text in (("PREVIOUSLY", intro.get("recap", "")),
                                                      ("IN THIS CHAPTER", intro["opener"])) if text]
    rows = []
    for heading, text in sections:
        rows += [("gap", "")] if rows else []
        rows += [("heading", heading)] + [("body", line) for line in _wrap(draw, text, fonts["body"], inner)]
    step = {"heading": 34, "body": 37, "gap": 14}
    card_h = 2 * 30 + sum(step[kind] for kind, _ in rows)
    top = height - BOTTOM - card_h
    card = Image.new("L", PAGE_SIZE, 0)
    ImageDraw.Draw(card).rectangle([MARGIN, top, width - MARGIN, height - BOTTOM], fill=232)
    page.paste(Image.new("RGB", PAGE_SIZE, INK), (0, 0), card)
    draw.rectangle([MARGIN, top, width - MARGIN, height - BOTTOM], outline=VISION_GOLD, width=2)
    baseline = top + 30
    for kind, text in rows:
        baseline += step[kind]
        if kind == "heading":
            _tracked(draw, (MARGIN + 34, baseline - 8), text, fonts["heading"], VISION_GOLD, 3, anchor="left")
        elif kind == "body":
            draw.text((MARGIN + 34, baseline - 8), text, font=fonts["body"], fill=PAPER, anchor="ls")
    return page


def assemble(store, panels, images, cover=None, visions=None):
    """cover: {"image": cover record, "intro": guide, "where": (book, chapter)} for a cover page (page 0).
    visions: {panel_id: label} for panels that show a dream or vision."""
    from PIL import Image, ImageDraw, ImageOps
    fonts = _fonts()
    width, height = PAGE_SIZE
    layout = frames(panels)
    pages, placements = [], {}
    for number in sorted({p.page for p in panels}):
        group = sorted((p for p in panels if p.page == number), key=lambda p: p.panel_number)
        page = Image.new("RGB", PAGE_SIZE, PAPER)
        draw = ImageDraw.Draw(page)
        labelled = set()
        for panel in group:
            x, y, w, h = frame = layout[panel.panel_id]
            record = images[panel.panel_id]
            crop = None
            with Image.open(store.path(record["path"])) as im:
                im = im.convert("RGB")
                if record.get("master"):
                    # A panel continuing a master shot shows its own part of the master's drawing.
                    crop = master_crop(im.size, (w, h), record.get("crop_index", 1), detect_faces(im))
                    im = im.crop(crop)
                # Art is rendered for this frame's shape; fit only trims rounding differences.
                art = ImageOps.fit(im, (w, h), Image.LANCZOS)
            page.paste(art, (x, y))
            draw.rectangle([x, y, x + w - 1, y + h - 1], outline=INK, width=BORDER)
            if _lettering(draw, panel, frame, fonts) is None:
                raise ValueError(f"Too much lettering for {panel.panel_id}'s frame; raise its weight in panels.json")
            # Words are fixed at plan time; only each box's corner and width are chosen here, once the art shows
            # where faces are.
            faces = detect_faces(art)
            boxes, covered = place_captions(_caption_options(draw, panel, frame, fonts), frame, faces)
            tab = None
            if panel.panel_id in (visions or {}):
                label = visions[panel.panel_id]
                # The label goes on the first panel of each run of the same dream or vision on a page.
                tab = _vision_frame(page, draw, frame, label if label not in labelled else None,
                                    [b for b, _, _ in boxes], fonts, faces)
                labelled.add(label)
            for box, rows, fill in boxes:
                _draw_box(draw, rows, box, fonts, fill)
            placements[panel.panel_id] = {"page": number, "corners": [_corner(b, frame) for b, _, _ in boxes],
                                          "faces": faces, "covered_faces": covered}
            if crop:
                placements[panel.panel_id]["master_crop"] = {"master": record["master"], "box": list(crop)}
            if tab:
                placements[panel.panel_id]["vision_label"] = tab
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
    if cover:
        page = draw_cover(store.path(cover["image"]["path"]), cover["intro"], cover["where"])
        path = store.path("pages/page_000.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        page.save(path)
        pages.insert(0, page)
    store.write("final/captions.json", placements)
    target = store.path("final/comic.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(target, save_all=True, append_images=pages[1:], resolution=150)
    return target
