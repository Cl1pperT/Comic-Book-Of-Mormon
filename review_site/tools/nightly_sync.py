"""The review-site steps of the nightly job (scripts\\nightly.cmd runs these around the render):

    python -m review_site.tools.nightly_sync pull       # before rendering: accepted issues -> flags, so tonight redraws them
    python -m review_site.tools.nightly_sync publish    # after rendering: redrawn chapters -> the site, as waiting versions

Each step is only a convenience: it never stops the night's work. It skips (and says why) when the site address or token
isn't set, or when another render job (ComicBOM Control) holds the lock, and tries a second time if the site is slow to
wake up. Anything else is printed and left for the next night. Chapters published here arrive as *waiting* versions: you
still choose when each one goes live (Admin > Chapters)."""
import os
import sys
import time
from pathlib import Path

ROOT = Path("runs/book")
NAMES = ("REVIEW_SITE_URL", "REVIEW_TOOL_TOKEN")


def user_environment(name):
    """The variable from the Windows user environment: a scheduled task may start without the newest values."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except (ImportError, OSError):
        return None


def ready():
    """True when the site address and token are available (set into this process's environment if they weren't)."""
    for name in NAMES:
        if not os.environ.get(name):
            value = user_environment(name)
            if value:
                os.environ[name] = value
    missing = [name for name in NAMES if not os.environ.get(name)]
    if missing:
        print(f"Skipping the review-site step: {', '.join(missing)} not set.")
    return not missing


def busy():
    """Another render job (a ComicBOM Control session) is working in runs/book."""
    from bom_comic import book
    return book.held(ROOT / "control.lock")


def run(step, attempts=2, pause=30, root=ROOT):
    """Run 'pull' or 'publish'. Returns 0 when done or deliberately skipped, 1 when it failed."""
    from . import publish, pull
    if not ready():
        return 0
    if busy():
        print("Skipping the review-site step: ComicBOM Control has a render running.")
        return 0
    main = {"pull": pull.main, "publish": publish.main}[step]
    for attempt in range(1, attempts + 1):
        try:
            main(["--runs", str(root)])
            return 0
        except SystemExit as exc:           # the tools exit with a message on bad input
            if exc.code in (None, 0):
                return 0
            print(f"{step}: {exc.code}")
            return 1
        except Exception as exc:            # network trouble, a half-written page...
            print(f"{step} attempt {attempt}/{attempts} failed: {type(exc).__name__}: {exc}")
            if attempt < attempts:
                time.sleep(pause)
    return 1


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("pull", "publish"):
        sys.exit(__doc__)
    sys.exit(run(args[0]))


if __name__ == "__main__":
    main()
