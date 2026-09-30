"""Who and where a panel shows, resolved to the character library's records.

Scenes are written chapter by chapter with free-text labels ("Alma", "Nephi's brethren", "the people"), so most don't
match a record by name, and the same bare name means different people in different books (the Alma of Mosiah 18 is
not the Alma of Alma 30). The library's cast.json maps labels to records by chapter range, gives each record a short
visual tag, and maps places to setting records, with an era region for scenes whose place is unspecified.

Resolution happens only when an image prompt is built. It never edits scenes, so approvals and existing drawings stay
current; a panel drawn after this change simply gets a better-informed prompt.

Report how many labels resolve:  python -m bom_comic.cast [--root runs/book] [--library portraits/book-of-mormon]
"""
import json
import re
from pathlib import Path

BOOKS = ["1 Nephi", "2 Nephi", "Jacob", "Enos", "Jarom", "Omni", "Words of Mormon", "Mosiah", "Alma", "Helaman",
         "3 Nephi", "4 Nephi", "Mormon", "Ether", "Moroni"]
UNSPECIFIED = re.compile(r"^(?:unspecified|unknown|not specified|none|n/a)\b", re.I)
NO_PLACE = "__none__"  # a keyword match meaning "not a place to draw" (the heavens, a dream): no region is added
REGION = "__region__"  # a keyword match meaning "somewhere generic in this era" (the land, a tent): the region
_NARRATOR = re.compile(r"^(?:the\s+)?(?:unnamed\s+)?(?:first[- ]person\s+)?(?:narrator|speaker)$", re.I)
_SPLIT = re.compile(r",\s*|\s+and\s+|\s*&\s*")
# Words that add nothing to a place's setting record ("the land of Zarahemla" is just Zarahemla).
_FILLER = {"the", "a", "an", "of", "in", "near", "by", "at", "on", "to", "and", "s", "land", "lands", "city", "place",
           "area", "region", "borders", "parts", "part", "unspecified", "setting", "location", "river", "valley"}


def chapter_of(where):
    """("Alma", 17) for "Alma 17" or ("Alma", 17); ("Alma", None) for a whole book."""
    if isinstance(where, str):
        head, _, tail = where.rpartition(" ")
        return (head, int(tail)) if tail.isdigit() else (where, None)
    return tuple(where)


def position(where):
    book, chapter = chapter_of(where)
    return BOOKS.index(book), chapter


def within(where, entry):
    """Whether a chapter falls in an entry's optional "from"/"to" range (a bare book name covers the whole book)."""
    if not (entry.get("from") or entry.get("to")):
        return True
    if where is None:
        return False
    book, chapter = position(where)
    low = position(entry["from"]) if entry.get("from") else (0, 0)
    high = position(entry["to"]) if entry.get("to") else (len(BOOKS), None)
    return (low[0], low[1] or 0) <= (book, chapter or 0) <= (high[0], high[1] or 10_000)


def normalize(label):
    """Case, curly quotes, spacing and a leading article don't distinguish labels."""
    text = (label or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = " ".join(text.lower().split()).strip(" .")
    return re.sub(r"^(?:the|a|an)\s+", "", text)


def _clauses(text):
    return [c.strip(" .,") for c in re.split(r"[;—]|(?<=\.)\s+", text) if c.strip(" .,")]


_NEGATION = re.compile(r"\b(?:no|not|never|nothing|without)\b|n't\b", re.I)
_BOILERPLATE = re.compile(r"inspired|european|asian|skin tones? vary|skin colou?r|signature|watermark|artist mark|"
                          r"in every panel|every time", re.I)


def auto_tag(record, limit=30):
    """A short look for a record the cast file has no tag for: its design choices without era boilerplate or
    negations, cut at a clause boundary. Never contains a full stop, so prompts can split on ". "."""
    out, words = [], 0
    for clause in _clauses(" ".join(record.get("visual_design_choices", []))):
        clause = re.sub(r",?\s*around 600 BC", "", clause).strip(" ,:")
        if not clause or _NEGATION.search(clause) or _BOILERPLATE.search(clause):
            continue
        n = len(clause.split())
        if out and words + n > limit:
            break
        out.append(clause)
        words += n
    return ", ".join(out).replace(".", "")


class Cast:
    def __init__(self, records=None, locations=None, data=None):
        data = data or {}
        self.library = dict(records or {})
        self.extra = data.get("records", {})
        self.locations = dict(locations or {})
        self.tags = data.get("tags", {})
        self.aliases = [dict(entry, labels={normalize(label) for label in entry["labels"]})
                        for entry in data.get("aliases", [])]
        self.patterns = [dict(entry, regex=re.compile(entry["pattern"], re.I)) for entry in data.get("patterns", [])]
        self.fallbacks = [dict(entry, regex=re.compile(entry["pattern"], re.I)) for entry in data.get("fallbacks", [])]
        places = data.get("places", {})
        self.keywords = [dict(entry, regex=re.compile(entry["match"], re.I)) for entry in places.get("keywords", [])]
        self.regions = places.get("regions", [])

    @classmethod
    def load(cls, library):
        library = Path(library)
        read = lambda name: json.loads((library / name).read_text(encoding="utf-8")) if (library / name).exists() else {}
        return cls(read("characters.json"), read("locations.json"), read("cast.json"))

    # People -----------------------------------------------------------------------------------------------------

    def records(self, known=None):
        return {**self.extra, **self.library, **(known or {})}

    def _one(self, label, where, known):
        """Record name(s) for one label, or None. Chapter-ranged aliases come before exact names, since a bare
        "Nephi" in Helaman is not the Nephi the library record of that name draws."""
        key = normalize(label)
        for entry in self.aliases:
            if key in entry["labels"] and within(where, entry):
                return entry["name"]
        for entry in self.patterns:
            if entry["regex"].search(key) and within(where, entry):
                return entry["name"]
        names = self.records(known)
        if label in names:
            return label
        exact = {normalize(name): name for name in names}.get(key)
        if exact:
            return exact
        # Only after every exact name: generic crowds ("unnamed listeners") and minor people of a culture.
        for entry in self.fallbacks:
            if entry["regex"].search(key) and within(where, entry):
                return entry["name"]
        return None

    def resolve(self, label, where=None, known=None):
        """(record names, unnamed remainder) for a visible-people label; ([], []) when it can't be resolved.
        Heavenly figures never resolve: they are drawn by fixed conventions, not records. A composite label
        ("Nephi and his companions") resolves part by part and keeps the parts that name no record."""
        from .analysis import narrator
        from .comic import heavenly
        if heavenly(label):
            return [], []
        found = self._one(label, where, known)
        if found is None and where and _NARRATOR.match(normalize(label)):
            teller = narrator(*chapter_of(where))
            if len(teller.split()) <= 4:  # a named narrator, not a description ("each record keeper in turn...")
                found = self._one(teller, where, known)
        base = re.sub(r"\s*\([^)]*\)", "", label).strip()
        if found is None and base and base != label:
            found = self._one(base, where, known)
        if found is not None:
            return ([found] if isinstance(found, str) else list(found)), []
        names, rest = [], []
        parts = [part.strip() for part in _SPLIT.split(base) if part.strip()]
        if len(parts) > 1:
            for part in parts:
                named = self._one(part, where, known)
                if named is None:
                    rest.append(part)
                else:
                    names += [named] if isinstance(named, str) else list(named)
        return (names, rest) if names else ([], [])

    def people(self, labels, where=None, known=None):
        """The panel's visible people as record names, in order; an unresolved label is kept as it is."""
        out = []
        for label in labels:
            names, rest = self.resolve(label, where, known)
            out += names + rest if names else [label]
        return list(dict.fromkeys(out))

    def character(self, name, known=None):
        """The record a prompt draws a person from, with its short visual tag; {} for an unknown label."""
        record = self.records(known).get(name)
        if not record:
            return {}
        tag = self.tags.get(name) or auto_tag(record)
        return {**record, "visual_tag": tag} if tag else dict(record)

    # Places -----------------------------------------------------------------------------------------------------

    def region(self, where):
        """The era's setting for a chapter; None for dream and vision chapters (a region named __none__)."""
        for entry in self.regions:
            if within(where, entry):
                return None if entry["name"] == NO_PLACE else entry["name"]
        return None

    def place(self, label, where=None, known=None):
        """(setting names, unspecified) for one location label. A place the chapter's own continuity has a record
        for keeps it; otherwise keywords, then the library's records by name. A specific place with no record keeps
        its label and gains its era's region, so its architecture and dress still fit the time."""
        if label in (known or {}):
            return [label], False
        key = normalize(label)
        for entry in self.keywords:
            match = entry["regex"].search(key)
            if match and within(where, entry):
                if entry["name"] == NO_PLACE:
                    return ([] if UNSPECIFIED.match(key) else [label]), False
                name = self.region(where) if entry["name"] == REGION else entry["name"]
                # "City wall of Zarahemla" or "Temple" says more than its setting record's name; "land of
                # Zarahemla" doesn't.
                words = lambda text: set(re.findall(r"[a-z]+", normalize(text or ""))) - _FILLER
                return ([label, name] if words(label) - words(name) else [name]), False
        by_key = {normalize(name): name for name in self.locations}
        if key in by_key:
            return [by_key[key]], False
        if not key or UNSPECIFIED.match(key):
            return [], True
        return [label, self.region(where)], False

    def settings(self, panel_locations, where=None, known=None):
        """Each panel's setting names, in order. A panel whose place is unspecified continues the previous panel's
        setting (a scene rarely moves without saying so), else takes the next one's, else the era's region."""
        resolved = []
        for labels in panel_locations:
            names, unspecified = [], True
            for label in labels:
                found, blank = self.place(label, where, known)
                names += [name for name in found if name]
                unspecified = unspecified and blank
            resolved.append(None if unspecified and not names else list(dict.fromkeys(names)))
        region = self.region(where)
        out = []
        for i, names in enumerate(resolved):
            if names is None:
                before = [r for r in resolved[:i] if r]
                after = [r for r in resolved[i + 1:] if r]
                names = before[-1] if before else after[0] if after else [region] if region else []
            out.append(names)
        return out

    def location(self, name, known=None):
        return (known or {}).get(name) or self.locations.get(name) or {}


def report(root="runs/book", library="portraits/book-of-mormon"):
    """Coverage of the written book's labels, by exact record name and after resolution."""
    from .comic import heavenly
    from .models import Scene
    cast = Cast.load(library)
    records = set(cast.records())
    stats = {"scenes": 0, "people_before": 0, "people_after": 0, "unspecified_before": 0, "setting_after": 0}
    unresolved = {}
    for path in sorted(Path(root).glob("*/*/scenes.json")):
        source = json.loads((path.parent / "source.json").read_text(encoding="utf-8"))
        where = (source[0]["book"], source[0]["chapter"])
        scenes = [Scene.model_validate(s) for s in json.loads(path.read_text(encoding="utf-8"))]
        for scene, places in zip(scenes, cast.settings([s.locations for s in scenes], where)):
            stats["scenes"] += 1
            mortal = [c for c in scene.characters if not heavenly(c)]
            stats["people_before"] += all(c in records for c in mortal)
            shown = cast.people(mortal, where)
            stats["people_after"] += all(n in records for n in shown)
            for name in shown:
                if name not in records:
                    unresolved[name] = unresolved.get(name, 0) + 1
            stats["unspecified_before"] += all(UNSPECIFIED.match(normalize(l)) for l in scene.locations)
            stats["setting_after"] += any(p in cast.locations for p in places)
    return stats, sorted(unresolved.items(), key=lambda item: -item[1])


def main():
    import argparse
    parser = argparse.ArgumentParser(description="How many scene labels resolve to library records")
    parser.add_argument("--root", default="runs/book")
    parser.add_argument("--library", default="portraits/book-of-mormon")
    parser.add_argument("--top", type=int, default=30, help="Show this many of the commonest unresolved labels")
    args = parser.parse_args()
    stats, unresolved = report(args.root, args.library)
    n = stats["scenes"] or 1
    print(f"{stats['scenes']} scenes")
    print(f"Every visible person drawn from a record: {stats['people_before'] / n:.0%} by exact name, "
          f"{stats['people_after'] / n:.0%} after resolution")
    print(f"Place unspecified: {stats['unspecified_before'] / n:.0%}. "
          f"Drawn from a setting record after resolution: {stats['setting_after'] / n:.0%}")
    print("Commonest unresolved labels:")
    for label, count in unresolved[:args.top]:
        print(f"  {count:4d}  {label}")


if __name__ == "__main__":
    main()
