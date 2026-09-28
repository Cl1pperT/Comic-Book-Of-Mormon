"""Staged scene analysis: several small single-purpose model passes, with code enforcing every
mechanical rule (verse coverage, reference format, exact quotes, lettering length, major count).

Built so small local models only make judgment calls; anything code can guarantee, code does."""
import json
from typing import List, Literal
from pydantic import BaseModel, ConfigDict, Field, create_model
from .analysis import RULES
from .models import Claim, Scene, Speech

WINDOW = 12            # verses shown to the segmenter at once
MAX_SCENE_VERSES = 6   # longer segments are split
MAX_WORDS = 65
REPAIRS = 2


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _one_of(values):
    # A Literal becomes a JSON-schema enum, so constrained decoding can only emit these values.
    return Literal[tuple(values)]


def _norm(text):
    return " ".join(text.split())


def segment_schema(numbers):
    Break = create_model("Break", __base__=_Strict, first_verse=(_one_of(numbers), ...), title=(str, ...))
    return create_model("Segments", __base__=_Strict, scenes=(List[Break], Field(min_length=1)))


def details_schema(refs):
    Fact = create_model("Fact", __base__=_Strict, text=(str, ...), refs=(List[_one_of(refs)], Field(min_length=1)))
    return create_model("Details", __base__=_Strict, summary=(str, ...), characters=(List[str], ...),
                        locations=(List[str], ...), explicit_facts=(List[Fact], Field(min_length=1)),
                        reasonable_visual_inferences=(List[str], ...), unspecified_visual_details=(List[str], ...),
                        doctrinal_or_story_notes=(List[str], ...), prohibited_inventions=(List[str], ...))


def lettering_schema(refs):
    Line = create_model("Line", __base__=_Strict, verse=(_one_of(refs), ...), text=(str, ...))
    Said = create_model("Said", __base__=_Strict, verse=(_one_of(refs), ...), speaker=(str, ...), text=(str, ...))
    return create_model("Lettering", __base__=_Strict, narration=(List[Line], ...), dialogue=(List[Said], ...))


def importance_schema(count):
    return create_model("Importance", __base__=_Strict,
                        major=(List[_one_of(range(1, count + 1))], Field(max_length=1)))


def _ask(provider, prompt, schema, tag, retries=1):
    """One structured call; a garbled or timed-out answer gets one fresh try before failing the stage."""
    from .errors import ProviderError
    for attempt in range(retries + 1):
        try:
            return provider.structured(prompt, schema, tag if attempt == 0 else f"{tag}_retry{attempt}")
        except ProviderError:
            if attempt == retries:
                raise


def _source(verses):
    return json.dumps([{"ref": v.ref, "text": v.text} for v in verses], ensure_ascii=False)


def segment(provider, window, tag):
    """Scene boundaries only. Code turns breaks into a gap-free partition of the window."""
    numbers = [v.verse for v in window]
    prompt = (RULES + "\n\nTask: split these verses into chronological comic scenes, one visual moment each, "
              "usually 1 to 4 verses. Return only where each scene starts (its first verse number) and a short "
              "title. Put a new scene wherever the place, time, or main action changes.\nVerses:\n" + _source(window))
    result = _ask(provider, prompt, segment_schema(numbers), tag)
    titles = {b.first_verse: b.title for b in result.scenes}
    starts = sorted(set(titles) | {numbers[0]})
    groups = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else numbers[-1] + 1
        members = [v for v in window if start <= v.verse < end]
        for k in range(0, len(members), MAX_SCENE_VERSES):
            groups.append({"verses": members[k:k + MAX_SCENE_VERSES], "title": titles.get(start, "Scene")})
    return groups


def details(provider, group, labels, tag):
    verses = group["verses"]
    refs = [v.ref for v in verses]
    prompt = (RULES + "\n\nTask: describe this one comic scene. summary: what is visibly happening, only from "
              "these verses. characters: every VISIBLE person or group; locations: every VISIBLE setting. "
              "Known labels (reuse exactly when that person, group, or place is present): " + json.dumps(labels)
              + ". explicit_facts: facts stated in these verses, each citing its verse. Keep inferences, creative "
              "details, and prohibited inventions separate and brief.\nScene title: " + group["title"]
              + "\nVerses:\n" + _source(verses))
    return _ask(provider, prompt, details_schema(refs), tag)


def lettering_errors(result, by_ref):
    errors, words = [], 0
    items = [("Narration", i, line) for i, line in enumerate(result.narration, 1)] + \
            [("Dialogue", i, said) for i, said in enumerate(result.dialogue, 1)]
    if not items:
        errors.append("Add at least one exact excerpt as narration or dialogue.")
    for kind, i, item in items:
        text = _norm(item.text)
        words += len(text.split())
        if "..." in text or "…" in text:
            errors.append(f"{kind} {i} contains an ellipsis; use one continuous exact excerpt instead.")
        elif not text or text not in _norm(by_ref[item.verse]):
            errors.append(f"{kind} {i} is not an exact excerpt of {item.verse}. That verse reads: {by_ref[item.verse]}")
    if words > MAX_WORDS:
        errors.append(f"Lettering totals {words} words; the limit is {MAX_WORDS}. Use shorter excerpts or fewer of them.")
    return errors


def lettering(provider, group, characters, tag):
    """Exact excerpts for the panel, checked by code, repaired by sending back the specific errors."""
    verses = group["verses"]
    refs = [v.ref for v in verses]
    by_ref = {v.ref: v.text for v in verses}
    schema = lettering_schema(refs)
    prompt = ("Task: choose the words printed on this comic panel. Copy 1 to 3 short excerpts EXACTLY, character "
              f"for character, from the verses below; at most {MAX_WORDS} words in total. Words someone says go in "
              "dialogue with their speaker; everything else is narration. Never paraphrase, never add ellipses. "
              "Visible people: " + json.dumps(characters) + "\nVerses:\n" + _source(verses))
    from .errors import ProviderError
    empty = schema(narration=[], dialogue=[])
    try:
        result = provider.structured(prompt, schema, tag)
    except ProviderError:
        result = None
    for attempt in range(1, REPAIRS + 1):
        errors = lettering_errors(result, by_ref) if result else ["Your previous answer was not valid JSON; answer again."]
        if not errors:
            break
        repair = (prompt + "\n\nYour previous answer:\n" + (result.model_dump_json() if result else "(invalid)")
                  + "\n\nFix only these problems:\n- " + "\n- ".join(errors))
        try:
            result = provider.structured(repair, schema, f"{tag}_repair{attempt}")
        except ProviderError:
            result = None
    result = result or empty
    # Keep whatever is valid after repairs, trimmed to the word limit; the validator still reviews the scene.
    narration, dialogue, words = [], [], 0
    for kind, item in [("n", n) for n in result.narration] + [("d", d) for d in result.dialogue]:
        text = _norm(item.text)
        count = len(text.split())
        if not text or "..." in text or "…" in text or text not in _norm(by_ref[item.verse]) or words + count > MAX_WORDS:
            continue
        words += count
        if kind == "n":
            narration.append(Claim(text=text, refs=[item.verse]))
        else:
            dialogue.append(Speech(text=text, refs=[item.verse], speaker=item.speaker))
    return narration, dialogue


def importance(provider, scenes, tag):
    """At most one full-page moment per chapter, chosen with the whole chapter in view."""
    if len(scenes) < 3:
        return None
    listing = "\n".join(f"{i}. {s.title}: {s.summary}" for i, s in enumerate(scenes, 1))
    prompt = ("A major scene gets a full comic page. Choose at most ONE scene in this chapter that is a true turning "
              "point of the whole story, or none if nothing qualifies.\nScenes:\n" + listing)
    result = _ask(provider, prompt, importance_schema(len(scenes)), tag)
    return result.major[0] - 1 if result.major else None


def analyze_staged(provider, verses, known_characters=()):
    scenes = []
    labels = list(known_characters)
    for chapter in sorted({v.chapter for v in verses}):
        chapter_verses = [v for v in verses if v.chapter == chapter]
        groups = []
        for offset in range(0, len(chapter_verses), WINDOW):
            groups += segment(provider, chapter_verses[offset:offset + WINDOW], f"staged_segment_{chapter}_{offset}")
        chapter_scenes = []
        for n, group in enumerate(groups, 1):
            refs = [v.ref for v in group["verses"]]
            tag = f"staged_{chapter}_{n}"
            info = details(provider, group, labels, f"{tag}_details")
            labels += [name for name in info.characters + info.locations if name not in labels]
            narration, dialogue = lettering(provider, group, info.characters, f"{tag}_lettering")
            chapter_scenes.append(Scene(
                scene_id="scene_0", title=group["title"], refs=refs, summary=info.summary,
                characters=info.characters, locations=info.locations,
                explicit_facts=[Claim(text=f.text, refs=list(f.refs)) for f in info.explicit_facts],
                reasonable_visual_inferences=info.reasonable_visual_inferences,
                unspecified_visual_details=info.unspecified_visual_details,
                spoken_dialogue=dialogue, narration=narration,
                doctrinal_or_story_notes=info.doctrinal_or_story_notes,
                prohibited_inventions=info.prohibited_inventions))
        major = importance(provider, chapter_scenes, f"staged_importance_{chapter}")
        if major is not None:
            chapter_scenes[major].importance = "major"
        scenes += chapter_scenes
    for i, scene in enumerate(scenes, 1):
        scene.scene_id = f"scene_{i:03d}"
    return scenes
