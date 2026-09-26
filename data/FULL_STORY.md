# Full-story source

`full-scripture.json` and `full-scripture.txt` contain exactly **3 Nephi 8:1–11:7** (73 verses), extracted from `data/BookOfMormon.epub`.

The EPUB puts 11:2 inside the 11:1 paragraph. The extractor splits the explicit `3 Nephi 11:2` marker into its own verse. It collapses whitespace and removes verse labels from verse text, retaining wording and punctuation. `full-scripture.provenance.json` records the EPUB checksum, internal XHTML member, and original paragraph checksum for each verse.

Reproduce the extraction:

```sh
python -m bom_comic.epub data/BookOfMormon.epub --output data/full-scripture.json
```

This importer supports the numbered-paragraph Gutenberg format in this EPUB, not arbitrary EPUB layouts.

## Narrative boundary

- Chapter 8: anticipation, storm and specifically described destruction, darkness, lamentation.
- Chapter 9: the unseen voice explains destruction and invites repentance; Christ identifies himself through speech only.
- Chapter 10: silence, the voice speaks again, mourning, darkness disperses, prophecy commentary.
- 10:18–19: the narrator previews the coming ministry. Retain these verses in the source and scene notes; do not illustrate Christ or his ministry here.
- 11:1–2: the later gathering at Bountiful's temple and discussion of Christ and the signs of his death.
- 11:3–6: three hearings of the voice, with understanding on the third.
- 11:7: the Father's introduction is the closing quotation. No visual descent or divine figure.

The voice in the darkness and the voice introducing the Son must not be conflated. Prophecy commentary is narration; do not invent a crowd discussion to illustrate it. Darkness scenes must not introduce visible torches, sunlight, stars, or a supernatural glow. Black panels with lettering are acceptable and avoid implying forbidden light.

## Full run

The selected source is initialized at `runs/full-story`. Scene interpretation and validation use the configured Gemini text models. Read `scenes.json` and `validation.json` before approving any scene. No scene or image is automatically human-approved.

```sh
python main.py --run runs/full-story review scene
python main.py --run runs/full-story review scene scene_001 --decision approve
# Repeat for each scene only after comparing it to the supplied verses.
# PASS WITH WARNINGS requires --note explaining your acceptance.
python main.py --run runs/full-story plan --pages 12
python main.py --run runs/full-story review panel
# Approve each panel ID individually after checking its prompt and continuity.
python main.py --run runs/full-story review panel panel_001 --decision approve
python main.py --run runs/full-story generate
python main.py --run runs/full-story review image
# Inspect and approve each PNG before assembly.
python main.py --run runs/full-story review image panel_001 --decision approve
python main.py --run runs/full-story assemble
```

IDs in these commands are examples, not bulk approvals. If rejected, edit or regenerate the scene, rerun validation, and review again. Image generation incurs API charges and should start only after scene/panel review. The small `runs/first-test` run is separate.

The prepared draft contains 36 panels on 14 pages. Open `runs/full-story/review/STORYBOARD.md` to compare each scene, its proposed lettering, and the full cited verses. Draft prompts and layout JSON are beside it. Chapter 9's consecutive speech passages are condensed into seven black panels with exact excerpts; the full verses and factual claims remain in the review artifacts.

To refresh the draft after edits:

```sh
python main.py --run runs/full-story validate --resume
python main.py --run runs/full-story preview --pages 12
```

`--resume` reuses a successful check only when the model, JSON schema, and complete prompt (including source, scene, and preceding scene) match exactly. It is useful after a rate-limit interruption; it does not waive validation of edits. Requests remain sequential by default.
