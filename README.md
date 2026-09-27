# Book of Mormon comic prototype

A Python CLI for a human-reviewed comic covering **3 Nephi 8–10 and 11:1–7**. Local scripture determines the story. The final scene ends with Christ's introduction in 11:7; no panel may visually reveal Jesus but may imply a heavenly figure 

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

There is deliberately no default model. The text models must support structured JSON responses; the image model must support native `generate_content` image output. Validation is always a separate call; its model can differ from the analyzer. Google's SDK integration is isolated in `bom_comic/providers.py`, including `generate_image(prompt, reference_images=None, output_path=None, aspect_ratio=None)`. `aspect_ratio` is the panel's frame shape from the page layout (see Page layout below). It's sent as the nearest ratio Gemini supports in `image_config.aspect_ratio`, and assembly trims the small difference. Reference image paths are supported at provider level for future richer continuity tooling; the initial CLI uses text continuity records.

The implementation follows [Google's image generation documentation](https://ai.google.dev/gemini-api/docs/generate-content/image-generation) and [official Python SDK](https://github.com/googleapis/python-genai). If a selected future model uses a different API, update the adapter. Live calls incur provider costs and require network access. No live provider calls are made by tests.

## Local image generation (ComfyUI + Flux)

`generate --local` renders panels on your own GPU through a running [ComfyUI](https://github.com/comfyanonymous/ComfyUI) server instead of Gemini. Analysis and validation still use Gemini. Setup (tested on an RTX 3060 12GB):

1. Install ComfyUI (the Windows portable build works) and the [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF) custom node.
2. Place models: `flux1-dev-Q8_0.gguf` in `models/unet`, `t5-v1_1-xxl-encoder-Q8_0.gguf` and `clip_l.safetensors` in `models/clip`, and the Flux `ae.safetensors` in `models/vae`.
3. Start ComfyUI, then run `bom-comic --run runs/my-run generate --local`.

The recorded prompt is exactly the one Gemini would get. `diffusion_prompts()` in `comic.py` rewrites it deterministically for Flux. Every negated clause ("No lettering…", "never photoreal") moves into a negative prompt, because diffusion models tend to draw whatever a prompt names. Panel content comes first, because Flux weights early tokens most. A panel inference that asks for lettering or speech bubbles is dropped from the positive prompt, since the locked negative forbids them. Nothing is added. The positive/negative pair, seed and full workflow are saved under `api/`. The seed derives from the revision name, so each revision is reproducible and a regeneration differs.

Optional `.env` overrides: `COMFYUI_URL` (default `http://127.0.0.1:8188`), `COMFYUI_UNET`, `COMFYUI_T5`, `COMFYUI_STEPS` (24), `COMFYUI_CFG` (2.0) and `COMFYUI_GUIDANCE` (2.5). `cfg` above 1 is what makes Flux honor the negative prompt. At 1.0 the negative prompt is ignored and renders are about twice as fast, but modern objects crept in during testing. Expect about 3 minutes per panel on a 12GB card at the defaults. Flux.1 [dev] weights are under a non-commercial license.

## Run a small portion first

All commands accept `--run PATH` **before** the subcommand. Start with a short contiguous portion from your own source, then use the default full range in a new run.

```sh
python main.py --run runs/small init data/my-scripture.json --start '3 Nephi 8:5' --end '3 Nephi 8:7'
python main.py --run runs/small analyze
python main.py --run runs/small validate
python main.py --run runs/small review scene
python main.py --run runs/small review scene scene_001 --decision approve
# Repeat individually for every generated scene ID.
python main.py --run runs/small plan
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

Panel story fields are locked to their approved scene. Edit camera, composition, mood, shot, weight, page, or panel number in `panels.json`, then review panels again. Change story content in scenes instead. Panel approvals also cover continuity/style input. The CLI prints the final image prompt during panel review so design details cannot bypass review.

`shot` (`splash`, `wide`, `tall`, `medium`, `close`) sets the camera/mood/composition text and the frame shape the layout prefers. `weight` (1–6) is how much of a page the panel deserves. See Page layout below. `plan` sets both from the scene's already-approved content, and you can edit either by hand. A page's weights may not exceed 6. Approvals and images depend on a panel's frame *shape*, not its page or position. Moving a panel whose frame keeps its shape needs no new approval or render. A change that reshapes a frame does.

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
- `comic.py`: deterministic one-scene-per-panel planning, page layout, structured prompts, Pillow assembly. No new story-writing call occurs here.

### Page layout

Pages are laid out first, and each panel is then rendered at its frame's exact shape, so art is never letterboxed. Local renders need no cropping, apart from rounding. Gemini only accepts a fixed list of ratios, so its images are trimmed slightly to fit.

**Weight.** A page holds 6 weight units, so it carries 1 to 6 panels. `classify_weight` decides from approved scene data. A `major` scene, or the closing 11:7 quote, gets 6 (a full page). Other scenes get 1–3 depending on how much lettering they carry, because captions sit on the art. `plan` then measures every panel's real captions in its real frame. If a panel's captions would cover more than 45% of the frame, `plan` raises that panel's weight and lays the pages out again. Anything `plan` produces can therefore be assembled.

**Pagination.** Panels fill a page in story order until the next one wouldn't fit in the 6 units. A single small panel left alone on a page, for example just before a full-page moment, borrows the previous page's last panel.

**Flow.** Within a page, panels run left to right and wrap to a new row, like reading order, with at most 3 per row. Rows span the full width with thin gutters. `page_frames` tries every set of row breaks and keeps the one whose frames best match three things: each panel's weight (area), its shot's preferred shape, and a slight preference for side-by-side panels. Frames stay between 0.4:1 and 2.5:1 so they remain renderable. Weight orders panel sizes, but when it conflicts with a renderable shape, the shape wins.

**Lettering.** Narration captions sit in the panel's top-left corner, and speech (speaker label plus quote) sits bottom-right, so reading runs corner to corner. Each box takes the narrowest of a few widths that keeps it under 30% of the frame's height. Scripture references go in the last box, and the page's verse range and number run along the bottom margin.

| Shot | When | Preferred shape |
|---|---|---|
| `splash` | `importance == "major"`, or the closing 11:7 quote | 3:4 |
| `wide` | No visible characters, or four or more | 16:9 |
| `close` | Dialogue between one or two characters, ≤30 words | 1:1 |
| `tall` | A small cast, narration-only, ≤12 words | 2:3 |
| `medium` | A small cast, narration-only, longer beat | 4:3 |

Shot and weight classifiers are simple deterministic functions (`classify_shot`, `classify_weight`), not LLM calls. They add no cost, no invention risk and no new review gate. Their output is an ordinary, human-editable field on each panel.
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

Assembly draws each page from the same layout `plan` used (see Page layout above): panels edge to edge in thin-guttered rows, with caption boxes on the art. If hand edits leave a panel's lettering too big for its frame, assembly refuses rather than bury the art. Lettering is set in bundled EB Garamond (`bom_comic/fonts/`, SIL Open Font License — see `bom_comic/fonts/OFL.txt`) so pages render identically everywhere. The manifest preserves panel → scene → verse provenance. Only reviewed images are assembled. `generate --placeholder` can test layout without API calls, at each frame's shape; placeholders are visibly marked and must still pass the same explicit CLI review gates.

## Tests

`pytest -q` covers parsing, complete range selection, JSON round trips, typed models, prompt constraints, environment configuration, path containment, invented speech, rejection/warning behavior, stale approvals, panel story locking, modified image detection, regeneration history, and an offline end-to-end PDF build. Mocked LLM results test orchestration, not the quality of Gemini's reasoning or art.

## Prepared full-story source

The supplied EPUB has been extracted to `data/full-scripture.json` and `data/full-scripture.txt`, with all 6,604 numbered verses across the Book of Mormon. The comic prototype still selects its 73-verse range, 3 Nephi 8:1–11:7. See [full-story source and workflow](data/FULL_STORY.md) for that story's provenance and cliffhanger boundary. Reproduce extraction with `python -m bom_comic.epub data/BookOfMormon.epub`.

For a full run, `python main.py --run runs/full-story preview` writes `review/STORYBOARD.md`, `review/draft-panels.json`, and `review/draft-prompts.json` after validation. These are review artifacts only; they cannot bypass the production scene/panel approvals. `validate --workers 4` can perform up to four independent checks concurrently (default: one); use fewer workers if your API quota is limited.

If validation is interrupted by rate limits, wait for the provider's indicated retry interval, then use `validate --resume`. Only successfully parsed responses with the identical model, schema, and full prompt are reused. Modified scenes and affected chronology checks are sent for fresh validation. Resume does not grant human approval.
