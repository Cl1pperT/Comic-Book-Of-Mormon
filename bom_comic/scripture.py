import json
import re
from pathlib import Path
from .models import Verse
COUNTS = {8: 25, 9: 22, 10: 19, 11: 7}
BOOKS = ["1 Nephi", "2 Nephi", "Jacob", "Enos", "Jarom", "Omni", "Words of Mormon", "Mosiah", "Alma", "Helaman", "3 Nephi", "4 Nephi", "Mormon", "Ether", "Moroni"]

def coordinate(ref):
    match = re.fullmatch(r"(.+?) (\d+):(\d+)", ref)
    if not match:
        raise ValueError(f"Invalid reference: {ref}")
    book, chapter, verse = match.group(1), int(match.group(2)), int(match.group(3))
    if book not in BOOKS or chapter < 1 or verse < 1:
        raise ValueError(f"Invalid scripture reference: {ref}")
    return BOOKS.index(book), chapter, verse

def load(path):
    path = Path(path)
    if path.suffix == ".json":
        records = json.loads(path.read_text(encoding="utf-8"))
    else:
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"(.+?) (\d+):(\d+)\s+(.+)", line.strip())
            if not match:
                raise ValueError(f"Expected '3 Nephi chapter:verse text': {line}")
            records.append(dict(book=match[1], chapter=int(match[2]), verse=int(match[3]), text=match[4]))
    verses = [Verse.model_validate(r) for r in records]
    refs = [v.ref for v in verses]
    if len(refs) != len(set(refs)):
        raise ValueError("Duplicate verses")
    for ref in refs:
        coordinate(ref)
    return sorted(verses, key=lambda v: coordinate(v.ref))

def select(verses, start, end):
    lo, hi = coordinate(start), coordinate(end)
    if lo > hi:
        raise ValueError("Range is reversed")
    selected = [v for v in verses if lo <= coordinate(v.ref) <= hi]
    if lo[0] == hi[0] == BOOKS.index("3 Nephi") and 8 <= lo[1] <= hi[1] <= 11:
        expected = [(BOOKS.index("3 Nephi"), c, n) for c, count in COUNTS.items() for n in range(1, count + 1) if lo <= (BOOKS.index("3 Nephi"), c, n) <= hi]
    else:
        expected = [coordinate(v.ref) for v in selected]
    actual = [(BOOKS.index(v.book), v.chapter, v.verse) for v in selected]
    if actual != expected:
        raise ValueError("Selected range contains missing verses")
    return selected
