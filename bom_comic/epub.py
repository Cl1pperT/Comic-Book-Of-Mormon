"""Extract explicitly numbered Book of Mormon verses from a Gutenberg EPUB."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from bom_comic.models import Verse
from bom_comic.scripture import BOOKS


def extract(path):
    records, evidence = [], []
    seen = set()
    with ZipFile(path) as archive:
        for member in archive.namelist():
            if not member.endswith(('.xhtml', '.html')):
                continue
            root = ET.fromstring(archive.read(member))
            title = ' '.join(' '.join(''.join(e.itertext()).split()) for e in root.iter()
                             if e.tag.split('}')[-1] in ('h1', 'h2')).upper()
            aliases = {'1 NEPHI': '1 Nephi', '2 NEPHI': '2 Nephi', 'THIRD BOOK OF NEPHI': '3 Nephi',
                       'FOURTH NEPHI': '4 Nephi', 'THE WORDS OF MORMON': 'Words of Mormon',
                       'THE BOOK OF JACOB': 'Jacob', 'THE BOOK OF ENOS': 'Enos',
                       'THE BOOK OF JAROM': 'Jarom', 'THE BOOK OF OMNI': 'Omni',
                       'THE BOOK OF MOSIAH': 'Mosiah', 'THE BOOK OF ALMA': 'Alma',
                       'THE BOOK OF HELAMAN': 'Helaman', 'THE BOOK OF MORMON': 'Mormon',
                       'THE BOOK OF ETHER': 'Ether', 'THE BOOK OF MORONI': 'Moroni'}
            book = next((value for key, value in aliases.items() if key in title), None)
            if book is None:
                for heading in root.iter():
                    if heading.tag.split('}')[-1] not in ('h2', 'h3'):
                        continue
                    heading_text = ' '.join(''.join(heading.itertext()).split())
                    bm = re.match(r'(.+?) Chapter \d+$', heading_text)
                    if bm and bm[1] in BOOKS:
                        book = bm[1]
                        break
            if book is None:
                continue
            chapter = None
            if book in ('Enos', 'Jarom', 'Omni', 'Words of Mormon'):
                chapter = 1
            for element in root.iter():
                tag = element.tag.split('}')[-1]
                text = ' '.join(''.join(element.itertext()).split())
                if tag in ('h2', 'h3'):
                    cm = re.search(r'Chapter (\d+)', text)
                    if cm:
                        chapter = int(cm[1])
                if tag != 'p':
                    continue
                text = re.sub(r'\b(?:1 Nephi|2 Nephi|3 Nephi|4 Nephi|Words of Mormon|Jacob|Enos|Jarom|Omni|Mosiah|Alma|Helaman|Mormon|Ether|Moroni) (?=\d+:\d+)', '', text)
                parts = re.split(r"\s+(?=\d+:\d+\s)", text)
                for part in parts:
                    match = re.fullmatch(r'(\d+):(\d+)\s+(.+)', part)
                    if match:
                        b, c, v, body = book, int(match[1]), int(match[2]), match[3]
                    elif chapter is not None and re.match(r'^\d+:\d+\s', part):
                        vm = re.fullmatch(r'(\d+):(\d+)\s+(.+)', part)
                        b, c, v, body = book, chapter, int(vm[2]), vm[3]
                        if int(vm[1]) != chapter:
                            raise ValueError(f'Chapter mismatch in {member}: {part[:40]}')
                    else:
                        continue
                    verse = Verse(book=b, chapter=c, verse=v, text=body)
                    if verse.ref in seen:
                        raise ValueError(f'Duplicate verse: {verse.ref}')
                    seen.add(verse.ref)
                    records.append(verse)
                    evidence.append({'ref': verse.ref, 'epub_member': member,
                                     'paragraph_sha256': hashlib.sha256(text.encode()).hexdigest()})
    if not records:
        raise ValueError('No numbered verses found')
    records.sort(key=lambda v: (BOOKS.index(v.book), v.chapter, v.verse))
    by_chapter = {}
    for verse in records:
        by_chapter.setdefault((verse.book, verse.chapter), []).append(verse.verse)
    for (book, chapter), numbers in by_chapter.items():
        if numbers != list(range(1, numbers[-1] + 1)):
            raise ValueError(f'Non-contiguous verses in {book} {chapter}: {numbers}')
    return records, evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('epub', type=Path)
    parser.add_argument('--output', type=Path, default=Path('data/full-scripture.json'))
    args = parser.parse_args()
    verses, evidence = extract(args.epub)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps([v.model_dump() for v in verses], indent=2, ensure_ascii=False)+'\n')
    args.output.with_suffix('.txt').write_text('\n'.join(f'{v.ref} {v.text}' for v in verses)+'\n')
    args.output.with_suffix('.provenance.json').write_text(json.dumps({
        'epub': str(args.epub), 'epub_sha256': hashlib.sha256(args.epub.read_bytes()).hexdigest(),
        'normalization': 'Whitespace collapsed; verse prefixes removed; wording and punctuation retained.',
        'verses': evidence}, indent=2)+'\n')
    print(f'Extracted {len(verses)} verses: {verses[0].ref} through {verses[-1].ref}')
