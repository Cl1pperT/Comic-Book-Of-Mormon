"""Resolving scene labels to library records for image prompts, the picture-first diffusion prompt, and the
settings comparison tool. Synthetic records exercise software only; the real-library checks guard its data file."""
import json
from pathlib import Path
import pytest
from bom_comic.cast import Cast, auto_tag, within
from bom_comic.comic import DEFAULT_CONTINUITY, build_prompt, diffusion_prompts
from bom_comic.models import Claim, Panel
from bom_comic.pipeline import Pipeline
from bom_comic.providers import PlaceholderImages
from bom_comic.storage import Store

LIBRARY = Path(__file__).resolve().parent.parent / "portraits" / "book-of-mormon"


def cast():
    records = {"Nephi": {"visual_design_choices": ["A tall young man, around 600 BC.", "Deep blue mantle."]},
               "Nephi (son of Helaman)": {"visual_design_choices": ["A lean prophet in a white robe."]},
               "Laman": {"visual_design_choices": ["Ochre tunic."]}, "Lemuel": {"visual_design_choices": ["Green tunic."]},
               "Townspeople": {"group": True, "visual_design_choices": ["Varied wool tunics."]}}
    data = {"tags": {"Nephi": "tall young man, deep blue mantle"},
            "aliases": [{"labels": ["Nephi"], "from": "Helaman 1", "to": "Helaman 16", "name": "Nephi (son of Helaman)"},
                        {"labels": ["Nephi's brethren"], "from": "1 Nephi 2", "to": "1 Nephi 18", "name": ["Laman", "Lemuel"]}],
            "fallbacks": [{"pattern": r"\bpeople\b", "name": "Townspeople"}],
            "places": {"keywords": [{"match": "jerusalem", "name": "Jerusalem"}, {"match": "heaven", "name": "__none__"},
                                    {"match": "^(?:the )?land$", "name": "__region__"}],
                       "regions": [{"from": "1 Nephi 2", "to": "1 Nephi 7", "name": "Valley"},
                                   {"from": "1 Nephi 8", "to": "1 Nephi 8", "name": "__none__"}]}}
    locations = {"Jerusalem": {"visual_design_choices": ["Limestone hill city"]},
                 "Valley": {"visual_design_choices": ["Green valley by a river"]}}
    return Cast(records, locations, data)


def test_labels_resolve_by_chapter_before_exact_names():
    c = cast()
    assert c.people(["Nephi"], ("1 Nephi", 3)) == ["Nephi"]
    assert c.people(["Nephi"], ("Helaman", 5)) == ["Nephi (son of Helaman)"]  # the exact name would be wrong here
    assert c.people(["Nephi’s brethren"], ("1 Nephi", 3)) == ["Laman", "Lemuel"]  # curly quote, list alias
    assert c.people(["Nephi's brethren"], ("Alma", 3)) == ["Nephi's brethren"]  # out of range: kept as written
    assert c.people(["Laman and Lemuel"], ("1 Nephi", 3)) == ["Laman", "Lemuel"]  # composite, part by part
    assert c.people(["Nephi and his companions"], ("1 Nephi", 3)) == ["Nephi", "his companions"]
    assert c.people(["Laman (the eldest)"], ("1 Nephi", 3)) == ["Laman"]  # parenthetical note dropped
    assert c.people(["unnamed people"], ("1 Nephi", 3)) == ["Townspeople"]  # fallback, only after exact names
    assert c.people(["the angel", "the Lord"], ("1 Nephi", 3)) == ["the angel", "the Lord"]  # conventions, not records
    assert c.people(["the narrator"], ("1 Nephi", 3)) == ["Nephi"]  # the chapter's first-person narrator


def test_records_carry_a_short_tag_without_era_boilerplate_or_full_stops():
    c = cast()
    assert c.character("Nephi")["visual_tag"] == "tall young man, deep blue mantle"
    assert c.character("Laman")["visual_tag"] == "Ochre tunic"
    assert c.character("Nobody") == {}
    tag = auto_tag({"visual_design_choices": ["An Israelite man, around 600 BC.", "Olive skin. Never a giant.",
                                              "Mesoamerican-inspired New World dress; nothing European."]})
    assert tag == "An Israelite man, Olive skin" and "." not in tag


def test_places_resolve_and_unspecified_ones_continue_the_scene():
    c = cast()
    where = ("1 Nephi", 3)
    settings = c.settings([["Jerusalem"], ["Unspecified location"], ["house of Laban"], ["the land"], ["Unspecified"]],
                          where)
    assert settings == [["Jerusalem"], ["Jerusalem"], ["house of Laban", "Valley"], ["Valley"], ["Valley"]]
    assert c.settings([["Unspecified"], ["Jerusalem"]], where) == [["Jerusalem"], ["Jerusalem"]]  # takes the next
    assert c.settings([["Unspecified"]], where) == [["Valley"]]  # nothing to continue: the era's region
    assert c.settings([["the heavens"]], where) == [["the heavens"]]  # not a place to put a region on
    assert c.settings([["a field"], ["Unspecified"]], ("1 Nephi", 8)) == [["a field"], ["a field"]]  # a dream: no region
    # The chapter's own location record wins.
    assert c.settings([["Jerusalem"]], where, {"Jerusalem": {"visual_design_choices": ["x"]}}) == [["Jerusalem"]]


def test_ranges_cover_whole_books_and_chapters():
    assert within(("Alma", 17), {"from": "Alma 17", "to": "Alma 26"})
    assert not within(("Alma", 27), {"from": "Alma 17", "to": "Alma 26"})
    assert within(("Mosiah", 3), {"from": "Words of Mormon 1", "to": "Moroni 10"})
    assert within(("Omni", 1), {"from": "Omni", "to": "Omni"}) and within(("Alma", 1), {})


def test_real_library_resolves_the_panels_that_went_wrong():
    c = Cast.load(LIBRARY)
    assert c.people(["Nephi and his companions (the narrator’s “we”)"], ("1 Nephi", 4)) == ["Nephi", "Laman", "Lemuel", "Sam"]
    assert c.people(["the servant of Laban"], ("1 Nephi", 4)) == ["Zoram"]
    assert c.people(["Alma"], ("Mosiah", 18)) == ["Alma the Elder"]
    assert c.people(["Alma"], ("Alma", 30)) == ["Alma the Younger (high priest)"]
    assert c.people(["Moroni"], ("Alma", 46)) == ["Captain Moroni"]
    assert c.people(["Moroni"], ("Ether", 12)) == ["Moroni (son of Mormon)"]
    assert c.people(["the people"], ("Alma", 18)) == ["Lamanite people"]
    assert c.settings([["Unspecified location"]], ("1 Nephi", 1)) == [["Jerusalem (c. 600 BC)"]]
    records = c.records()
    for name in list(c.tags) + [n for e in c.aliases for n in ([e["name"]] if isinstance(e["name"], str) else e["name"])]:
        assert name in records, name  # every tag and alias names a real record
    for entry in c.keywords + [dict(e, match="") for e in c.regions]:
        assert entry["name"] in c.locations or entry["name"] in ("__none__", "__region__"), entry["name"]
    for tag in c.tags.values():
        assert "." not in tag and len(tag.split()) <= 30


def approved_pipeline(tmp_path):
    """A one-panel run, approved through panel review, whose scene shows "Unnamed people"."""
    from test_pipeline import FakeAI, ready
    source = tmp_path / "input.txt"
    source.write_text("3 Nephi 8:1 TEST FIXTURE: People gathered.\n3 Nephi 8:2 TEST FIXTURE: A voice was heard.\n")
    p = Pipeline(Store(tmp_path / "run"), FakeAI())
    p.init(source, "3 Nephi 8:1", "3 Nephi 8:2")
    p.analyze()
    p.validate()
    ready(p)
    return p


def panel(**update):
    base = Panel(panel_id="panel_001", scene_id="scene_001", page=1, panel_number=1, weight=1, refs=["1 Nephi 3:1"],
                 characters_visible=["Laman", "Lemuel"], location=["Jerusalem"],
                 action="Laman and Lemuel beat Nephi with a rod. They say he wants to rule over them.",
                 shot="medium", mood="Reverent", camera="Medium shot", composition="Clear focal point", dialogue=[],
                 narration=[Claim(text="TEST", refs=["1 Nephi 3:1"])], visual_facts=[
                     Claim(text="A fact the summary already states.", refs=["1 Nephi 3:1"])],
                 visual_inferences=["The rod can be shown raised; the angel is not yet visible."],
                 creative_details=[], prohibited=["Do not depict Nephi's parents", "No weapons besides the rod",
                                                  "No events beyond the cited verses"])
    return base.model_copy(update=update)


def test_diffusion_prompt_is_a_picture_with_each_look_beside_its_name():
    c = cast()
    continuity = {**DEFAULT_CONTINUITY, "characters": {n: c.character(n) for n in ("Laman", "Lemuel", "Nephi")},
                  "locations": {"Jerusalem": {"visual_design_choices": ["Limestone hill city"]}}}
    positive, negative = diffusion_prompts(build_prompt(panel(), continuity, "4:3"))
    # The first sentence of the summary and the de-hedged inference; no motives, no negated clause, no facts.
    assert positive.startswith("Scene: Laman and Lemuel beat Nephi with a rod; The rod raised. ")
    assert "rule over them" not in positive and "angel" not in positive and "A fact" not in positive
    assert "Exactly two people: Laman (Ochre tunic); Lemuel (Green tunic)." in positive
    assert "Setting: Jerusalem (Limestone hill city)" in positive
    assert "crowd, extra people" in negative
    # Only concrete prohibitions reach the negative prompt, and never a visible person's name.
    assert "weapons besides the rod" in negative
    assert "events beyond" not in negative and "parents" not in negative
    # A group or an unknown label means no headcount.
    positive, negative = diffusion_prompts(build_prompt(panel(characters_visible=["Laman", "Some strangers"]),
                                                        continuity, "4:3"))
    assert "People: Laman (Ochre tunic); Some strangers." in positive and "extra people" not in negative


def test_pipeline_resolves_prompts_without_touching_stamps(tmp_path):
    p = approved_pipeline(tmp_path)
    [only] = p.panels()
    before = (p.panel_stamp(only), p.art_stamp(only))
    library = tmp_path / "library"
    library.mkdir()
    (library / "characters.json").write_text(json.dumps({"Gathered townspeople": {
        "group": True, "visual_design_choices": ["Plain wool tunics."]}}))
    (library / "cast.json").write_text(json.dumps({"aliases": [{"labels": ["Unnamed people"],
                                                                "name": "Gathered townspeople"}]}))
    (library / "locations.json").write_text("{}")
    resolved = Pipeline(p.store, p.provider, library)
    view, continuity = resolved.shown([only])[only.panel_id]
    assert view.characters_visible == ["Gathered townspeople"] and "Gathered townspeople" in continuity["characters"]
    assert "Gathered townspeople (Plain wool tunics)" in diffusion_prompts(resolved.prompt_for(only, "4:3"))[0]
    assert (resolved.panel_stamp(only), resolved.art_stamp(only)) == before  # approvals and drawings stay current
    calls = []
    original = resolved.provider.generate_image
    resolved.provider.generate_image = lambda prompt, **kw: calls.append(prompt) or original(prompt, **kw)
    resolved.generate(only.panel_id)
    assert '"Gathered townspeople"' in calls[0]


def test_comfyui_takes_a_fixed_seed(tmp_path):
    import io
    from PIL import Image
    from bom_comic.providers import ComfyUI
    data = io.BytesIO()
    Image.new("RGB", (8, 8)).save(data, format="PNG")
    history = {"x": {"status": {"status_str": "success"},
                     "outputs": {"save": {"images": [{"filename": "p.png", "subfolder": "", "type": "output"}]}}}}
    sent = []

    def call(path, payload=None, timeout=60):
        if path == "/prompt":
            sent.append(payload["prompt"])
            return json.dumps({"prompt_id": "x"}).encode()
        return json.dumps(history).encode() if path.startswith("/history/") else data.getvalue()
    provider = ComfyUI(Store(tmp_path / "c"), url="http://fake")
    provider._call = call
    prompt = build_prompt(panel(), DEFAULT_CONTINUITY, "4:3")
    for stem in ("a", "b"):
        provider.generate_image(prompt, output_path=tmp_path / f"{stem}.png", aspect_ratio="4:3", seed=7)
    provider.generate_image(prompt, output_path=tmp_path / "c.png", aspect_ratio="4:3")
    seeds = [w["sample"]["inputs"]["seed"] for w in sent]
    assert seeds[0] == seeds[1] == 7 and seeds[2] != 7


def test_compare_renders_every_variant_from_the_same_seed_and_makes_a_sheet(tmp_path):
    from bom_comic.compare import compare, parse_variant
    p = approved_pipeline(tmp_path)
    p.generate()
    assert parse_variant("q5:unet=flux1-dev-Q5_K_S.gguf,cfg=1") == ("q5", {"unet": "flux1-dev-Q5_K_S.gguf", "cfg": 1.0})
    with pytest.raises(ValueError):
        parse_variant("bad:sampler=euler")
    seen = []

    class Fake(PlaceholderImages):
        unet, t5, steps, cfg, guidance = "q8.gguf", "t5.gguf", 24, 2.0, 2.5

        def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None, seed=None,
                           size=None):
            seen.append((self.unet, seed, prompt))
            return super().generate_image(prompt, reference_images, output_path, aspect_ratio)
    out = compare(p.store.root, ["panel_001"], [parse_variant("q8"), parse_variant("q5:unet=q5.gguf")],
                  tmp_path / "compare", library=None, provider_factory=Fake)
    assert [unet for unet, _, _ in seen] == ["q8.gguf", "q5.gguf"]
    assert seen[0][1] == seen[1][1] and seen[0][2] == seen[1][2]  # only the setting differs
    timings = json.loads((out / "timings.json").read_text())
    assert timings["variants"]["q5"]["settings"]["unet"] == "q5.gguf"
    assert (out / "sheet.png").exists() and (out / "q5" / "panel_001.png").exists()
