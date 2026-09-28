import argparse
import json
from .config import Config
from .errors import ProviderError
from .storage import Store
from .pipeline import Pipeline, character_bible
from .providers import ComfyUI, Gemini, Ollama, PlaceholderImages


def main():
    parser = argparse.ArgumentParser(description="Source-grounded, human-reviewed scripture comic pipeline")
    parser.add_argument("--run", default="runs/prototype", help="Artifact directory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("source")
    init.add_argument("--start", default="3 Nephi 8:1")
    init.add_argument("--end", default="3 Nephi 11:7")
    analyzer = commands.add_parser("analyze")
    analyzer.add_argument("--id", help="Regenerate one scene, preserving its references")
    analyzer.add_argument("--local", action="store_true", help="Reason with a local Ollama server (OLLAMA_URL/OLLAMA_TEXT_MODEL) instead of Gemini")
    analyzer.add_argument("--claude", action="store_true", help="Reason with Claude (ANTHROPIC_API_KEY, CLAUDE_TEXT_MODEL) instead of Gemini")
    analyzer.add_argument("--resume", action="store_true", help="Reuse chunks already analyzed with the identical model and prompt")
    analyzer.add_argument("--staged", action="store_true", help="Several small single-purpose passes with code-enforced rules (for smaller models)")
    validator = commands.add_parser("validate")
    validator.add_argument("--resume", action="store_true", help="Reuse successful checks only when model, schema, and complete prompt are unchanged")
    validator.add_argument("--workers", type=int, choices=range(1, 5), default=1, help="Concurrent independent API checks; default 1")
    validator.add_argument("--local", action="store_true", help="Reason with a local Ollama server (OLLAMA_URL/OLLAMA_VALIDATOR_MODEL) instead of Gemini")
    validator.add_argument("--claude", action="store_true", help="Check with Claude (ANTHROPIC_API_KEY, CLAUDE_VALIDATOR_MODEL) instead of Gemini")
    commands.add_parser("plan")
    commands.add_parser("preview", help="Save a review-only storyboard and draft prompts")
    review = commands.add_parser("review")
    review.add_argument("kind", choices=["scene", "panel", "image"])
    review.add_argument("id", nargs="?")
    review.add_argument("--decision", choices=["approve", "reject"])
    review.add_argument("--note", default="")
    portrait_cmd = commands.add_parser("portraits", help="Render one reference portrait per character record")
    portrait_cmd.add_argument("--id", help="Re-render one character's portrait (its exact name in characters.json)")
    portrait_cmd.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    portrait_cmd.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    location_cmd = commands.add_parser("locations", help="Render one reference image per location record")
    location_cmd.add_argument("--id", help="Re-render one location's image (its exact name in locations.json)")
    location_cmd.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    location_cmd.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    bible = commands.add_parser("bible", help="Render portraits for every record in --run's characters.json, no story run needed")
    bible.add_argument("--id", help="Re-render one record's image (its exact name in the JSON file)")
    bible.add_argument("--locations", action="store_true", help="Render --run's locations.json instead of characters.json")
    bible.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    bible.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    generation = commands.add_parser("generate")
    generation.add_argument("--id", help="Regenerate one panel; retains old image revisions")
    generation.add_argument("--placeholder", action="store_true", help="Offline plumbing test only")
    generation.add_argument("--local", action="store_true", help="Render with a local ComfyUI server (COMFYUI_URL) instead of Gemini")
    generation.add_argument("--skip-main", action="store_true", help="Only render panels with no main (portrait-eligible, named) character")
    commands.add_parser("assemble")
    args = parser.parse_args()
    store = Store(args.run)
    try:
        if args.command in ("validate", "plan", "preview", "review", "portraits", "locations", "generate", "assemble") and not store.path("scenes.json").exists():
            raise ValueError("No scenes.json yet. Run analyze successfully before this stage.")
        provider = None
        if args.command in ("analyze", "validate"):
            if getattr(args, "local", False):
                Config.load()
                provider = Ollama(store)
            elif getattr(args, "claude", False):
                Config.load()
                from .claude import Claude
                provider = Claude(store)
            else:
                provider = Gemini(Config.load(), store)
        elif args.command in ("portraits", "locations", "bible", "generate"):
            if getattr(args, "placeholder", False):
                provider = PlaceholderImages()
            elif getattr(args, "local", False):
                Config.load()
                provider = ComfyUI(store)
            else:
                provider = Gemini(Config.load(), store)
        if provider is not None:
            provider.reuse_responses = getattr(args, "resume", False)
        pipeline = Pipeline(store, provider)
        if args.command == "bible":
            kind = "locations" if args.locations else "characters"
            for name in character_bible(store, provider, args.id, kind):
                print(f"{name}: {store.path(store.read('index.json')[name]['path'])}")
        elif args.command == "init":
            pipeline.init(args.source, args.start, args.end)
        elif args.command == "analyze":
            pipeline.analyze(args.id, staged=args.staged)
        elif args.command == "validate":
            print(json.dumps(pipeline.validate(args.workers), indent=2))
        elif args.command == "plan":
            pipeline.plan()
        elif args.command == "preview":
            print(pipeline.preview())
        elif args.command == "review":
            if args.decision:
                if not args.id:
                    raise ValueError("A decision requires an individual scene/panel ID")
                pipeline.review(args.kind, args.id, args.decision, args.note)
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
                        from .comic import build_prompt
                        portraits = pipeline.panel_portraits(panel)
                        for name, entry in portraits.items():
                            print(f"Reference portrait for {name}: {store.path(entry['path'])}")
                        places = pipeline.panel_locations(panel)
                        for name, entry in places.items():
                            print(f"Location reference for {name}: {store.path(entry['path'])}")
                        print(build_prompt(panel, pipeline.continuity(), aspects[panel.panel_id], list(portraits), list(places)))
        elif args.command == "portraits":
            for name in pipeline.portraits(args.id):
                print(f"{name}: {store.path(pipeline.portrait_index()[name]['path'])}")
        elif args.command == "locations":
            for name in pipeline.location_references(args.id):
                print(f"{name}: {store.path(pipeline.reference_index('locations')[name]['path'])}")
        elif args.command == "generate":
            pipeline.generate(args.id, skip_main=args.skip_main)
        elif args.command == "assemble":
            print(pipeline.assemble())
    except Exception as exc:
        # Avoid logging provider exceptions that may contain credentials or request URLs.
        store.event("error", stage=args.command, error_type=type(exc).__name__)
        if isinstance(exc, (ProviderError, ValueError, FileNotFoundError, KeyError)):
            parser.exit(1, f"{exc}\n")
        parser.exit(1, f"{type(exc).__name__}: stage failed. Check configuration and saved artifacts.\n")
