import json
import re
from pathlib import Path
from .models import Verse
COUNTS = {8: 25, 9: 22, 10: 19, 11: 7}

def coordinate(ref):
    match = re.fullmatch(r"3 Nephi (\d+):(\d+)", ref)
    if not match:
        raise ValueError(f"Invalid reference: {ref}")
    chapter, verse = map(int, match.groups())
    if chapter not in COUNTS or not 1 <= verse <= COUNTS[chapter]:
        raise ValueError(f"Outside prototype scope: {ref}")
    return chapter, verse

def load(path):
    path = Path(path)
    if path.suffix == ".json":
        records = json.loads(path.read_text(encoding="utf-8"))
    else:
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"3 Nephi (\d+):(\d+)\s+(.+)", line.strip())
            if not match:
                raise ValueError(f"Expected '3 Nephi chapter:verse text': {line}")
            records.append(dict(chapter=int(match[1]), verse=int(match[2]), text=match[3]))
    verses = [Verse.model_validate(r) for r in records]
    refs = [v.ref for v in verses]
    if len(refs) != len(set(refs)):
        raise ValueError("Duplicate verses")
    for ref in refs:
        coordinate(ref)
    return sorted(verses, key=lambda v: (v.chapter, v.verse))

def select(verses, start, end):
    lo, hi = coordinate(start), coordinate(end)
    if lo > hi:
        raise ValueError("Range is reversed")
    selected = [v for v in verses if lo <= (v.chapter, v.verse) <= hi]
    expected = [(c, n) for c, count in COUNTS.items() for n in range(1, count + 1) if lo <= (c, n) <= hi]
    if [(v.chapter, v.verse) for v in selected] != expected:
        raise ValueError("Selected range contains missing verses")
    return selected
