import json
import math
from .models import Panel

NEGATIVE = ["No additional named characters or unlisted events", "No modern objects or buildings",
            "No lettering, captions, logos, or speech bubbles in the image",
            "Never show Jesus Christ, the Father, a descending person, or a divine silhouette",
            "No events after 3 Nephi 11:7", "No unsupported doctrinal symbolism",
            "Do not alter approved important character identities or numbers",
            "No gore, parody, superhero imagery, humor, or fantasy embellishment"]

DEFAULT_CONTINUITY = {
    "characters": {}, "locations": {},
    "visual_style": {"description": "Serious, reverent, realistic cinematic graphic novel. Restrained colors, expressive composition, no gratuitous violence.",
                     "locked_traits": ["Consistent ink treatment and naturalistic proportions"]}}


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
        panels.append(Panel(panel_id=f"panel_{i:03d}", scene_id=scene.scene_id,
            page=page, panel_number=slot, refs=scene.refs, characters_visible=scene.characters,
            location=scene.locations, action=scene.summary, dialogue=scene.spoken_dialogue,
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
        "MOOD / COMPOSITION / CAMERA\n" + " / ".join([panel.mood, panel.composition, panel.camera]),
        "NEGATIVE CONSTRAINTS\n" + "\n".join(NEGATIVE + panel.prohibited)])


def assemble(store, panels, images):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    font = ImageFont.load_default(size=23)
    pages = []
    for number in sorted({p.page for p in panels}):
        group = [p for p in panels if p.page == number]
        page = Image.new("RGB", (1400, 2000), "#eee8da")
        draw = ImageDraw.Draw(page)
        cell = 1860 // len(group)
        for index, panel in enumerate(group):
            y = 35 + index * cell
            lines = []
            texts = [f"{s.speaker}: {s.text}" for s in panel.dialogue] + [s.text for s in panel.narration]
            texts.append("; ".join(panel.refs))
            for text in texts:
                line = ""
                for word in text.split():
                    candidate = (line + " " + word).strip()
                    if draw.textlength(candidate, font=font) > 1270:
                        lines.append(line)
                        line = word
                    else:
                        line = candidate
                lines.append(line)
            text_height = len(lines) * 30 + 20
            image_height = cell - text_height - 25
            if image_height < 140:
                raise ValueError(f"Too much lettering on {panel.panel_id}; reduce text or panels per page")
            with Image.open(store.path(images[panel.panel_id]["path"])) as im:
                fit = ImageOps.contain(im.convert("RGB"), (1320, image_height))
                page.paste(fit, (40 + (1320 - fit.width) // 2, y))
            draw.multiline_text((55, y + image_height + 10), "\n".join(lines), font=font, fill="#171717", spacing=7)
        draw.text((650, 1940), str(number), font=font, fill="#171717")
        path = store.path(f"pages/page_{number:03d}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        page.save(path)
        pages.append(page)
    target = store.path("final/comic.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(target, save_all=True, append_images=pages[1:], resolution=150)
    return target
