# Book of Mormon comic prototype

A Python CLI for a human-reviewed comic covering **3 Nephi 8–10 and 11:1–7**. Local scripture determines the story. The final scene ends with the Father's introduction in 11:7; no panel may visually reveal Jesus, the Father, or a descending heavenly figure.

This repository contains a working pipeline, not a finished or certified accurate comic. No authoritative scripture, API credentials, or chosen Gemini models were supplied. Bring your own local source. The offline demo uses conspicuously synthetic test text and placeholder images; it is not a scripture adaptation.

## Install

Python 3.11+ (3.12 recommended; use an OpenSSL-based installation):

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[test]'
cp .env.example .env
pytest -q
python -m examples.demo
```

The demo creates `runs/offline-demo/final/comic.pdf`. Use a different `--run` directory when repeating it. It makes no API calls. Only the synthetic demo and tests automate approvals.

## Supply scripture

JSON input is an array of `{ "book": "3 Nephi", "chapter": 8, "verse": 1, "text": "..." }` objects. See `examples/source-format.json` for structure; replace its placeholder. Plain UTF-8 text also works, with one fully referenced verse per line:

```text
3 Nephi 8:1 <paste the complete verse here>
3 Nephi 8:2 <paste the complete verse here>
```

Do not include angle-bracket placeholders in real input. The parser rejects duplicates, missing verses in the selected range, malformed lines, and references beyond this prototype's scope. It does not authenticate the text or silently fetch missing verses. A full input needs 73 verses: 25 in chapter 8, 22 in 9, 19 in 10, and the first 7 in 11. Books outside this prototype are intentionally unsupported. Every source snapshot is hashed; changing it requires a new run.

## Configure Gemini

Set these in `.env` (never commit this file):

```dotenv
GEMINI_API_KEY=your-key
GEMINI_TEXT_MODEL=your-selected-text-model
GEMINI_VALIDATOR_MODEL=your-selected-validation-model
GEMINI_IMAGE_MODEL=your-selected-image-model
```

There is deliberately no default model. The text models must support structured JSON responses; the image model must support native `generate_content` image output. Validation is always a separate call; its model can differ from the analyzer. Google's SDK integration is isolated in `bom_comic/providers.py`, including `generate_image(prompt, reference_images=None, output_path=None)`. Reference image paths are supported at provider level for future richer continuity tooling; the initial CLI uses text continuity records.

The implementation follows [Google's image generation documentation](https://ai.google.dev/gemini-api/docs/generate-content/image-generation) and [official Python SDK](https://github.com/googleapis/python-genai). If a selected future model uses a different API, update the adapter. Live calls incur provider costs and require network access. No live provider calls are made by tests.

## Run a small portion first

All commands accept `--run PATH` **before** the subcommand. Start with a short contiguous portion from your own source, then use the default full range in a new run.

```sh
python main.py --run runs/small init data/my-scripture.json --start '3 Nephi 8:5' --end '3 Nephi 8:7'
python main.py --run runs/small analyze
python main.py --run runs/small validate
python main.py --run runs/small review scene
python main.py --run runs/small review scene scene_001 --decision approve
# Repeat individually for every generated scene ID.
python main.py --run runs/small plan --pages 10
python main.py --run runs/small review panel
python main.py --run runs/small review panel panel_001 --decision approve
# Repeat individually for every panel after reading its prompt and continuity details.
python main.py --run runs/small generate
python main.py --run runs/small review image
# Open each printed PNG path and compare it against its source and approved panel.
python main.py --run runs/small review image panel_001 --decision approve --note 'Compared image with supplied verses'
# Repeat individually for every image.
python main.py --run runs/small assemble
```

For the full prototype:

```sh
python main.py --run runs/full init data/my-scripture.json
python main.py --run runs/full analyze
python main.py --run runs/full validate
```

Then follow the same individual review, plan, generate, image-review, and assembly commands. The analyzer processes chapter-bounded chunks of at most six verses. It is directed to preserve storm/destruction details, distinguish teachings from visible events, preserve the later temple transition, and stop at 11:7. All selected verses must be represented by scene references. The final scene must quote the complete supplied 11:7 introduction. Reviewers still need to check that every important beat is actually represented: reference coverage alone cannot prove this.

## Review and correction

`review scene` prints the source, scenes, and validation results. Compare every factual claim and every speaker with the cited verses. `PASS WITH WARNINGS` needs an explanatory `--note` to approve. `REJECT` cannot be approved. Reject an individual item with `--decision reject`.

Edit `scenes.json` with any text editor, then rerun `validate`, approve scenes again, and rerun `plan`. `analyze` regenerates all scenes. Regenerate one scene with `analyze --id scene_001`, then validate and review again. Its ID and verse coverage must remain unchanged. Saved prompts/responses and archived JSON revisions support inspecting both attempts.

Panel story fields are locked to their approved scene. Edit camera, composition, mood, page, or panel number in `panels.json`, then review panels again. Change story content in scenes instead. Panel approvals also cover continuity/style input. The CLI prints the final image prompt during panel review so design details cannot bypass review.

After viewing an image, explicitly approve or reject it. Check location, people, clothing continuity, unsupported symbols or actions, divine figures, accidental lettering, and consistency with the cited scripture. Image review is human, not an automatic claim of fidelity. To regenerate a rejected or unsatisfactory panel:

```sh
python main.py --run runs/small generate --id panel_001
python main.py --run runs/small review image panel_001
python main.py --run runs/small review image panel_001 --decision approve
```

Regeneration keeps old PNGs and prompt records and invalidates the prior image approval. Batch `generate` resumes by skipping current images. Changed scenes, panel composition, continuity, or image bytes invalidate dependent approvals. Approvals are workflow safeguards for a trusted local editor, not tamper-proof signatures. Use one process per run directory.

## Architecture and accuracy boundaries

- `scripture.py`: strict local parsing, range selection, reference retention.
- `models.py`: validated JSON schemas. Explicit facts carry evidence refs; reasonable inference and creative detail have separate fields.
- `analysis.py`: small-chunk LLM analysis and independent semantic validation. Deterministic checks reject invented lettering, unsupported references, visible divine characters, excessive lettering, and backward scene starts.
- `comic.py`: deterministic one-scene-per-panel planning, structured prompts, Pillow assembly. No new story-writing call occurs here. Major scenes get a full page; other pages have at most three panels. A target of ten pages is a pacing preference, not a mandate; scene count and major moments may put the result outside 8–15 pages.
- `pipeline.py`: stage orchestration and content-based review gates.
- `providers.py`: injectable Gemini text/image adapter and offline placeholder provider.
- `storage.py`: atomic JSON writes, bounded output paths, timestamped history.

An LLM validator can miss errors. These safeguards make unsupported additions harder to introduce, but cannot prove doctrinal or visual correctness. Human source comparison is mandatory. Dialogue and narration use exact source substrings, a deliberately conservative restriction beyond the brief's allowance for close paraphrase. A quote's speaker and context still require semantic review. For long speeches, split into short scenes with at most 65 total lettering words. The app does not promise exhaustive transcription of all speeches.

## Continuity and style

Each run has editable `continuity/characters.json`, `locations.json`, and `visual_style.json`. Character/location objects are keyed by the exact approved scene label. Use entries such as:

```json
{
  "Unnamed crowd": {
    "scriptural_facts": [],
    "visual_design_choices": ["Muted earth-tone garments; no individual identities"],
    "locked_traits": ["Reuse the same garment palette"]
  }
}
```

Only add facts with supporting verse references, for example `{ "text": "...", "refs": ["3 Nephi 11:1"] }`. Exact faces, costume details, and architecture belong in design choices, never facts. Do not create named crowd members. Style defaults are reverent, cinematic, realistic, and restrained. Global negative constraints exclude modern details, invented events, gratuitous gore, later chapters, and divine figures. Text continuity improves guidance but cannot guarantee identical designs between images; review and regeneration remain necessary.

## Saved outputs and assembly

Within each run:

```text
source.json, selection.json        authoritative selected snapshot and hash
scenes.json, validation.json       scenes, evidence, independent verdicts
panels.json, reviews.json          layout and individual approval decisions
continuity/                       reusable design records and global style
api/                              model prompts and raw SDK responses
prompts/                          versioned structured image instructions
images/                           versioned PNGs and current-image records
pages/                            assembled PNG pages
final/comic.pdf, manifest.json     printable comic and panel-to-source trace
history.jsonl                     timestamps, reviews, generations, errors
archive/                          previous versions of overwritten JSON artifacts
```

Raw API responses can be large because they include image payloads. Keep run directories private if your input is private. API keys are not written to artifacts. Errors record stage and exception type; Gemini API errors include a credential-redacted message, HTTP status, and recovery hint. Other provider exception bodies are suppressed. Failed stages can be rerun independently; there are no automatic paid retries.

Assembly fits images without cropping, wraps lettering into separate boxes beneath each panel, prints scripture refs and page numbers, and writes PNG pages plus a PDF. It refuses overcrowded lettering. The manifest preserves panel → scene → verse provenance. Only reviewed images are assembled. `generate --placeholder` can test layout without API calls; placeholders are visibly marked and must still pass the same explicit CLI review gates.

## Tests

`pytest -q` covers parsing, complete range selection, JSON round trips, typed models, prompt constraints, environment configuration, path containment, invented speech, rejection/warning behavior, stale approvals, panel story locking, modified image detection, regeneration history, and an offline end-to-end PDF build. Mocked LLM results test orchestration, not the quality of Gemini's reasoning or art.
