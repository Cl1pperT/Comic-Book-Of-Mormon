"""Chapter guides (opener card, cover, dreams and visions), cover pages, master shots, and renders sized to their
frames. Synthetic words exercise software only; they are not scripture or an adaptation."""
import json
import pytest
from bom_comic import chapter as guide
from bom_comic.comic import diffusion_prompts, frames, master_crop, shot_groups
from bom_comic.models import ChapterIntro, Claim, Scene, SceneBatch, Speech, Verdict
from bom_comic.pipeline import Pipeline
from bom_comic.providers import PlaceholderImages, target_pixels
from bom_comic.storage import Store

TEXT = "TEST FIXTURE words for a software test"


def scene(i, characters=("Ann", "Ben"), locations=("Hall",), speech=False, words=6):
    ref = f"3 Nephi 8:{i}"
    claim = dict(text=" ".join(TEXT.split()[:words]), refs=[ref])
    return Scene(scene_id=f"scene_{i:03d}", title=f"Test {i}", refs=[ref], summary=f"Ann and Ben stand in the hall {i}.",
                 characters=list(characters), locations=list(locations), explicit_facts=[Claim(**claim)],
                 spoken_dialogue=[Speech(speaker="Ann", **claim)] if speech else [],
                 narration=[] if speech else [Claim(**claim)])


SCENES = [scene(1), scene(2, speech=True), scene(3, speech=True), scene(4, characters=("Cal",))]
INTRO = {"title": "Ann and Ben Talk", "recap": "", "opener": "Ann and Ben meet in a hall and talk.",
         "cover": {"moment": "Ann and Ben stand in a hall.", "characters": ["Ann", "Ben"], "location": "Hall",
                   "refs": ["3 Nephi 8:1"]},
         "visions": [{"kind": "vision", "seer": "Ann", "first_ref": "3 Nephi 8:3", "last_ref": "3 Nephi 8:4"}]}


class Fake(PlaceholderImages):
    """Text and image model in one: writes the fixed scenes and guide, passes every audit, counts renders."""
    def __init__(self, intro=INTRO, verdicts=("PASS",)):
        self.intro, self.verdicts, self.renders, self.sizes, self.asked = intro, list(verdicts), [], [], []

    def structured(self, prompt, schema, tag, kind="text"):
        self.asked.append((tag, prompt))
        if schema is SceneBatch:
            return SceneBatch(scenes=SCENES)
        if schema is ChapterIntro:
            return ChapterIntro.model_validate(self.intro)
        if tag.startswith("intro"):
            return Verdict(status=self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0], issues=["x"])
        return Verdict(status="PASS")

    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None, size=None):
        self.renders.append(prompt)
        self.sizes.append(size)
        return super().generate_image(prompt, reference_images, output_path, aspect_ratio)


@pytest.fixture
def run(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("".join(f"3 Nephi 8:{i} {TEXT} {i}.\n" for i in range(1, 5)))
    p = Pipeline(Store(tmp_path / "run"), Fake())
    p.init(source, "3 Nephi 8:1", "3 Nephi 8:4")
    p.analyze(chunk_size=0)
    p.validate()
    for s in p.scenes():
        p.review("scene", s.scene_id, "approve")
    p.plan()
    for panel in p.panels():
        p.review("panel", panel.panel_id, "approve")
    return p


def approve_images(p):
    for panel in p.panels():
        p.review("image", panel.panel_id, "approve", "software test")


def test_renders_are_sized_to_their_frame(monkeypatch):
    for key in ("COMFYUI_MAX_MEGAPIXELS", "COMFYUI_MIN_MEGAPIXELS", "COMFYUI_RENDER_SCALE"):
        monkeypatch.delenv(key, raising=False)
    assert target_pixels() == 1024 * 1024  # no frame: the maximum, as before
    assert target_pixels((700, 700)) == 700 * 700
    assert target_pixels((200, 200)) == int(0.4 * 1024 * 1024)  # Flux's floor
    assert target_pixels((1400, 2000)) == 1024 * 1024  # a cover is capped
    monkeypatch.setenv("COMFYUI_RENDER_SCALE", "1.5")
    assert target_pixels((700, 700)) == int(700 * 700 * 1.5)


def test_guide_is_written_audited_and_repaired(run):
    record = guide.write(run)
    assert record["verdict"]["status"] == "PASS" and guide.current(run)
    assert guide.read(run.store).title == "Ann and Ben Talk"
    # A rejected draft gets one rewrite, told why; a guide still rejected is never used.
    run.provider = Fake(verdicts=("REJECT", "REJECT"))
    assert guide.write(run)["verdict"]["status"] == "REJECT"
    assert [tag for tag, _ in run.provider.asked] == ["intro", "intro_audit", "intro_repair", "intro_reaudit"]
    assert "fix them" in run.provider.asked[2][1]
    assert guide.read(run.store) is None
    # Code checks come first: refs outside the chapter and over-long text never reach the auditor.
    bad = json.loads(json.dumps(INTRO))
    bad["cover"]["refs"] = ["Alma 1:1"]
    bad["opener"] = "word " * 100
    issues = guide.deterministic_issues(ChapterIntro.model_validate(bad), run.verses())
    assert any("Cover refs" in i for i in issues) and any("opener is 100 words" in i for i in issues)


def test_dreams_and_visions_mark_their_panels(run):
    guide.write(run)
    intro = run.intro()
    panels = run.panels()
    assert run.visions(panels, intro) == {"panel_003": "ANN'S VISION", "panel_004": "ANN'S VISION"}
    positive, _ = diffusion_prompts(run.prompt_for(panels[2], "4:3"))
    assert "Seen in Ann's vision: a luminous, dreamlike scene" in positive
    assert "Seen in" not in diffusion_prompts(run.prompt_for(panels[0], "4:3"))[0]
    run.generate()
    approve_images(run)
    run.assemble()
    captions = run.store.read("final/captions.json")
    # The label goes on the first panel of the run on a page; the frame marks the rest.
    labelled = [pid for pid, c in captions.items() if "vision_label" in c]
    same_page = captions["panel_003"]["page"] == captions["panel_004"]["page"]
    assert labelled == (["panel_003"] if same_page else ["panel_003", "panel_004"])


def test_cover_page_needs_an_approved_guide_and_cover(run, tmp_path):
    from bom_comic import reader
    from bom_comic.book import compile_pdf
    assert run.cover() is None  # no guide yet
    guide.write(run)
    record = run.cover()
    assert run.provider.sizes[-1] == (1400, 2000) and run.cover() == record  # reused while current
    run.generate()
    approve_images(run)
    run.assemble()
    assert run.store.read("final/manifest.json")["cover"] is None  # not reviewed yet: no cover page
    run.review("intro", "chapter", "approve")
    run.review("cover", "cover", "approve")
    run.assemble()
    manifest = run.store.read("final/manifest.json")
    assert manifest["cover"]["intro"]["title"] == "Ann and Ben Talk" and run.store.path("pages/page_000.png").exists()
    assert reader.draft(run.store)["pages"][0] == {"number": 0, "label": "Cover", "panels": []}
    pages = max(p["page"] for p in manifest["panels"])
    assert compile_pdf(run.store.root, tmp_path / "book.pdf")[2] == pages + 1
    # A rewritten guide retires the cover approval until it is drawn and reviewed again.
    run.provider.intro = {**INTRO, "cover": {**INTRO["cover"], "moment": "Ann alone in the hall."}}
    guide.write(run)
    with pytest.raises(ValueError, match="stale"):
        run.cover_record()


def test_master_shots_continue_while_the_text_fits(run):
    panels = run.panels()
    layout = frames(panels)
    # Same people, same place, led by speech: panels 2 and 3 continue panel 1; panel 4 shows someone else.
    assert shot_groups(panels, layout) == {"panel_002": ("panel_001", 1), "panel_003": ("panel_001", 2)}
    # Past two thirds of a page of lettering, the next panel gets a new shot.
    import bom_comic.comic as comic
    original = comic.MASTER_TEXT_SHARE
    try:
        comic.MASTER_TEXT_SHARE = 0.001
        assert shot_groups(panels, layout) == {}
    finally:
        comic.MASTER_TEXT_SHARE = original


def test_master_shot_is_drawn_once_and_followers_follow_its_redraws(run):
    changed = run.generate()
    assert len(run.provider.renders) == 2 and changed == ["panel_001", "panel_002", "panel_003", "panel_004"]
    assert run.provider.sizes[0] is None  # a master is drawn at full size, since its followers crop it
    assert run.provider.sizes[1] is not None  # an ordinary panel at its frame's size
    follower = run.image_record(run.panels()[1])
    assert follower["master"] == "panel_001" and follower["path"] == run.image_record(run.panels()[0])["path"]
    approve_images(run)
    run.assemble()
    assert run.store.read("final/captions.json")["panel_002"]["master_crop"]["master"] == "panel_001"
    # Redrawing the master relinks its followers; redrawing a follower on its own breaks it away.
    changed = run.generate("panel_001", reuse=False)
    assert changed == ["panel_001", "panel_002", "panel_003"]
    assert run.image_record(run.panels()[2])["path"] == run.image_record(run.panels()[0])["path"]
    run.generate("panel_002", reuse=False)
    assert "master" not in run.image_record(run.panels()[1]) and len(run.provider.renders) == 4


def test_crops_move_between_faces():
    faces = [(100, 100, 40, 40), (600, 120, 40, 40)]
    first, second = (master_crop((1000, 600), (400, 400), i, faces) for i in (1, 2))
    assert first[0] < second[0]  # a cut from one speaker to the other
    for box in (first, second, master_crop((1000, 600), (300, 600), 3, None)):
        assert 0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 600


def test_guides_chain_their_recaps_through_a_book(tmp_path):
    from bom_comic.book import write_intros
    source = tmp_path / "input.txt"
    source.write_text("".join(f"3 Nephi {c}:{v} {TEXT}.\n" for c in (8, 9) for v in (1, 2, 3, 4)))
    root = tmp_path / "book"
    for number in (8, 9):
        store = Store(root / "3-nephi" / f"{number:03d}")
        p = Pipeline(store, Fake())
        p.init(source, f"3 Nephi {number}:1", f"3 Nephi {number}:4")
        store.write("scenes.json", [s.model_copy(update={"refs": [r.replace("8:", f"{number}:") for r in s.refs]})
                                    .model_dump() for s in SCENES])
        store.write("status.json", {"book": "3 Nephi", "chapter": number, "counts": {"REJECT": 0}})
    texts = []

    class Writer(Fake):
        def structured(self, prompt, schema, tag, kind="text"):
            if schema is ChapterIntro:
                texts.append(json.loads(prompt[prompt.index("{"):])["previous_chapter_guide"])
                chapter = 8 if len(texts) == 1 else 9
                return ChapterIntro.model_validate({**INTRO, "title": f"Chapter {chapter}", "visions": [],
                                                    "cover": {**INTRO["cover"], "refs": [f"3 Nephi {chapter}:1"]}})
            return super().structured(prompt, schema, tag, kind)
    done, reason = write_intros(root, lambda store: Writer())
    assert reason == "done" and [d[1] for d in done] == [8, 9]
    assert texts[0] is None and texts[1]["title"] == "Chapter 8"
    assert write_intros(root, lambda store: Writer())[0] == []  # current guides are kept
