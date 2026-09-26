"""Extract this Gutenberg EPUB's explicitly numbered 3 Nephi paragraphs."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from bom_comic.models import Verse
from bom_comic.scripture import select


def extract(path):
    records, evidence = [], []
    with ZipFile(path) as archive:
        for member in archive.namelist():
            if not member.endswith(('.xhtml', '.html')):
                continue
            root = ET.fromstring(archive.read(member))
            headings = [' '.join(''.join(e.itertext()).split()) for e in root.iter()
                        if e.tag.split('}')[-1] in ('h1', 'h2')]
            if 'THIRD BOOK OF NEPHI' not in headings:
                continue
            chapter = None
            for element in root.iter():
                tag = element.tag.split('}')[-1]
                text = ' '.join(''.join(element.itertext()).split())
                if tag == 'h3':
                    match = re.fullmatch(r'3 Nephi Chapter (\d+)', text)
                    chapter = int(match[1]) if match else None
                if tag != 'p' or chapter not in (8, 9, 10, 11):
                    continue
                parts = re.split(r"\s+3 Nephi (?=\d+:\d+\s)", text)
                for part in parts:
                    match = re.fullmatch(r'(\d+):(\d+)\s+(.+)', part)
                    if not match:
                        raise ValueError('Unexpected unnumbered paragraph in selected chapter')
                    c, v = int(match[1]), int(match[2])
                    if c != chapter:
                        raise ValueError('Paragraph/chapter mismatch')
                    if c == 11 and v > 7:
                        continue
                    verse = Verse(chapter=c, verse=v, text=match[3])
                    records.append(verse)
                    evidence.append({'ref': verse.ref, 'epub_member': member,
                                     'paragraph_sha256': hashlib.sha256(text.encode()).hexdigest()})
    records = select(records, '3 Nephi 8:1', '3 Nephi 11:7')
    if len({v.ref for v in records}) != 73:
        raise ValueError('Expected exactly 73 unique verses')
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
