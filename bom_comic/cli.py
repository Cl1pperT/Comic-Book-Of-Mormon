import argparse
import json
from pathlib import Path
from .config import Config
from .errors import ProviderError
from .storage import Store
from .pipeline import Pipeline, character_bible
from .providers import ComfyUI, Gemini, Ollama, PlaceholderImages

DEFAULT_LIBRARY = "portraits/book-of-mormon"


def main():
    parser = argparse.ArgumentParser(description="Source-grounded, human-reviewed scripture comic pipeline")
    parser.add_argument("--run", default="runs/prototype", help="Artifact directory")
    parser.add_argument("--library", default=DEFAULT_LIBRARY,
                        help="Character library whose cast.json resolves scene labels in image prompts "
                             f"(default {DEFAULT_LIBRARY} when present; 'none' to use exact labels only)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("source")
    init.add_argument("--start", default="3 Nephi 8:1")
    init.add_argument("--end", default="3 Nephi 11:7")
    analyzer = commands.add_parser("analyze")
    analyzer.add_argument("--id", help="Regenerate one scene, preserving its references")
    analyzer.add_argument("--local", action="store_true", help="Reason with a local Ollama server (OLLAMA_URL/OLLAMA_TEXT_MODEL) instead of Gemini")
    analyzer.add_argument("--claude", action="store_true", help="Reason with Claude (ANTHROPIC_API_KEY, CLAUDE_TEXT_MODEL) instead of Gemini")
    analyzer.add_argument("--codex", action="store_true", help="Reason with the Codex CLI signed in with ChatGPT (no API key; uses the plan's limits)")
    analyzer.add_argument("--resume", action="store_true", help="Reuse chunks already analyzed with the identical model and prompt")
    analyzer.add_argument("--chunk-size", type=int, default=6, help="Verses per call; 0 sends each whole chapter in one call")
    analyzer.add_argument("--staged", action="store_true", help="Several small single-purpose passes with code-enforced rules (for smaller models)")
    validator = commands.add_parser("validate")
    validator.add_argument("--resume", action="store_true", help="Reuse successful checks only when model, schema, and complete prompt are unchanged")
    validator.add_argument("--workers", type=int, choices=range(1, 5), default=1, help="Concurrent independent API checks; default 1")
    validator.add_argument("--local", action="store_true", help="Reason with a local Ollama server (OLLAMA_URL/OLLAMA_VALIDATOR_MODEL) instead of Gemini")
    validator.add_argument("--claude", action="store_true", help="Check with Claude (ANTHROPIC_API_KEY, CLAUDE_VALIDATOR_MODEL) instead of Gemini")
    validator.add_argument("--batch", action="store_true", help="Audit each chapter's scenes in one call instead of one call per scene")
    validator.add_argument("--codex", action="store_true", help="Check with the Codex CLI signed in with ChatGPT (no API key; uses the plan's limits)")
    validator.add_argument("--offline", metavar="MODEL", help="No model calls: reuse saved verdicts from MODEL where unchanged, flag the rest for human review")
    commands.add_parser("plan")
    commands.add_parser("preview", help="Save a review-only storyboard and draft prompts")
    review = commands.add_parser("review")
    review.add_argument("kind", choices=["scene", "panel", "image", "intro", "cover"])
    review.add_argument("id", nargs="?")
    review.add_argument("--decision", choices=["approve", "reject"])
    review.add_argument("--note", default="")
    portrait_cmd = commands.add_parser("portraits", help="Render one reference portrait per character record")
    portrait_cmd.add_argument("--id", help="Re-render one character's portrait (its exact name in characters.json)")
    portrait_cmd.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    portrait_cmd.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    portrait_cmd.add_argument("--adopt", metavar="LIBRARY", help="Reuse approved portraits from a character library folder instead of rendering")
    bible = commands.add_parser("bible", help="Render portraits for every record in --run's characters.json, no story run needed")
    bible.add_argument("--id", help="Re-render one character's portrait (its exact name in characters.json)")
    bible.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    bible.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    generation = commands.add_parser("generate")
    generation.add_argument("--id", help="Regenerate one panel; retains old image revisions")
    generation.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    generation.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    generation.add_argument("--skip-main", action="store_true", help="Only render panels with no main (portrait-eligible, named) character")
    generation.add_argument("--kontext", action="store_true", help="With --local, attach reference portraits (FLUX.1 Kontext); by default local drafts are plain Flux from the text records")
    cover_cmd = commands.add_parser("cover", help="Render the chapter's cover art from its guide (intro.json)")
    cover_cmd.add_argument("--redo", action="store_true", help="Render a new revision even if the current one fits")
    cover_cmd.add_argument("--local", action="store_true", help="Render with ComfyUI")
    cover_cmd.add_argument("--placeholder", action="store_true", help="Offline placeholder art")
    commands.add_parser("assemble")
    book = commands.add_parser("book", help="Write (not render) the book's scenes chapter by chapter with Codex, one run per chapter")
    book.add_argument("--root", default="runs/book", help="Folder holding one run per chapter")
    book.add_argument("--start", help="First chapter, e.g. '1 Nephi 1' (finished chapters are skipped either way)")
    book.add_argument("--max-chapters", type=int, help="Stop after writing this many chapters")
    book.add_argument("--repair", action="store_true", help="First re-repair written chapters that still have rejected scenes")
    book.add_argument("--speakers", action="store_true", help="First name speakers labelled like 'Unidentified speaker'")
    book.add_argument("--continuity", action="store_true",
                      help="Only read each chapter for story continuity and repair the scenes that confuse")
    book.add_argument("--intros", action="store_true",
                      help="Only write each chapter's reader's guide (cover moment, opener card, dream/vision ranges)")
    compiler = commands.add_parser("compile", help="Stitch every assembled chapter under --run into one PDF, in book order")
    compiler.add_argument("--out", help="Output file (default: <run>/comic-progress.pdf)")
    reader = commands.add_parser("read", help="Read the assembled draft in a browser and flag panels to re-render")
    reader.add_argument("--port", type=int, default=8765)
    reader.add_argument("--no-browser", action="store_true", help="Print the address without opening a browser")
    args = parser.parse_args()
    store = Store(args.run)
    try:
        if args.command in ("validate", "plan", "preview", "review", "portraits", "generate", "cover", "assemble") and not store.path("scenes.json").exists():
            raise ValueError("No scenes.json yet. Run analyze successfully before this stage.")
        provider = None
        if args.command in ("analyze", "validate"):
            if getattr(args, "local", False):
                Config.load()
                provider = Ollama(store)
            elif getattr(args, "offline", None):
                from .providers import CachedChecks
                provider = CachedChecks(store, args.offline)
            elif getattr(args, "codex", False):
                Config.load()
                from .codex import Codex
                provider = Codex(store)
            elif getattr(args, "claude", False):
                Config.load()
                from .claude import Claude
                provider = Claude(store)
            else:
                provider = Gemini(Config.load(), store)
        elif args.command == "portraits" and args.adopt:
            provider = None
        elif args.command in ("portraits", "bible", "generate", "cover"):
            if getattr(args, "placeholder", False):
                provider = PlaceholderImages()
            elif getattr(args, "local", False):
                Config.load()
                provider = ComfyUI(store)
            else:
                provider = Gemini(Config.load(), store)
        if provider is not None:
            provider.reuse_responses = getattr(args, "resume", False)
        library = None if args.library in (None, "", "none") or not Path(args.library, "cast.json").exists() else args.library
        pipeline = Pipeline(store, provider, library)
        if args.command == "bible":
            for name in character_bible(store, provider, args.id):
                print(f"{name}: {store.path(store.read('index.json')[name]['path'])}")
        elif args.command == "init":
            pipeline.init(args.source, args.start, args.end)
        elif args.command == "analyze":
            pipeline.analyze(args.id, staged=args.staged, chunk_size=args.chunk_size)
        elif args.command == "validate":
            print(json.dumps(pipeline.validate(args.workers, batch=args.batch), indent=2))
        elif args.command == "plan":
            pipeline.plan()
        elif args.command == "preview":
            print(pipeline.preview())
        elif args.command == "review":
            if args.decision:
                if not args.id:
                    raise ValueError("A decision requires an individual scene/panel ID")
                pipeline.review(args.kind, args.id, args.decision, args.note)
            elif args.kind == "intro":
                print(json.dumps({"guide": store.read("intro.json"), "source": store.read("source.json")}, indent=2))
            elif args.kind == "cover":
                print(store.path(pipeline.cover_record()["path"]))
            elif args.kind == "scene":
                print(json.dumps({"source": store.read("source.json"), "scenes": store.read("scenes.json"),
                    "validation": store.read("validation.json") if store.path("validation.json").exists() else "not validated"}, indent=2))
            else:
                aspects = pipeline.aspects()
                for panel in pipeline.panels():
                    if args.id and panel.panel_id != args.id:
                        continue
                    print(panel.model_dump_json(indent=2))
                    if args.kind == "image":
                        print(store.path(pipeline.image_record(panel, aspects)["path"]))
                    else:
                        portraits = pipeline.panel_portraits(panel)
                        for name, entry in portraits.items():
                            print(f"Reference portrait for {name}: {store.path(entry['path'])}")
                        print(pipeline.prompt_for(panel, aspects[panel.panel_id], portraits))
        elif args.command == "portraits" and args.adopt:
            for name in pipeline.adopt_portraits(args.adopt):
                print(f"{name}: {store.path(pipeline.portrait_index()[name]['path'])} (adopted)")
        elif args.command == "portraits":
            for name in pipeline.portraits(args.id):
                print(f"{name}: {store.path(pipeline.portrait_index()[name]['path'])}")
        elif args.command == "generate":
            # An explicit --id draws that panel on its own, even if it would continue a master shot.
            pipeline.generate(args.id, skip_main=args.skip_main, references=args.kontext or not args.local,
                              reuse=not args.id)
        elif args.command == "cover":
            record = pipeline.cover(regenerate=args.redo)
            if record is None:
                raise ValueError("No usable chapter guide; write one with `book --intros` first")
            print(store.path(record["path"]))
            print("Review it, then: review intro chapter --decision approve; review cover cover --decision approve")
        elif args.command == "assemble":
            print(pipeline.assemble())
        elif args.command == "book":
            Config.load()
            from .book import check_continuity, fix_speakers, repair_blocked, write_book, write_intros
            if args.continuity:
                fixed, reason = check_continuity(args.root)
                print(f"Repaired continuity in {len(fixed)} chapters; stopped: {reason}")
                return
            if args.intros:
                guides, reason = write_intros(args.root)
                print(f"Wrote {len(guides)} chapter guides; stopped: {reason}")
                return
            if args.speakers:
                named, reason = fix_speakers(args.root)
                print(f"Named speakers in {len(named)} chapters; stopped: {reason}")
            if args.repair:
                repaired, reason = repair_blocked(args.root)
                print(f"Repaired {len(repaired)} chapters; stopped: {reason}")
            written, reason = write_book(args.root, start=args.start, max_chapters=args.max_chapters)
            print(f"Wrote {len(written)} chapters; stopped: {reason}")
        elif args.command == "compile":
            from .book import compile_pdf
            path, names, pages = compile_pdf(args.run, args.out)
            print(f"{path}: {pages} pages, {len(names)} chapters ({names[0]} to {names[-1]})")
        elif args.command == "read":
            from .reader import serve
            serve(store, args.port, not args.no_browser)
    except Exception as exc:
        # Avoid logging provider exceptions that may contain credentials or request URLs.
        store.event("error", stage=args.command, error_type=type(exc).__name__)
        if isinstance(exc, (ProviderError, ValueError, FileNotFoundError, KeyError)):
            parser.exit(1, f"{exc}\n")
        parser.exit(1, f"{type(exc).__name__}: stage failed. Check configuration and saved artifacts.\n")
