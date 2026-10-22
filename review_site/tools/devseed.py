"""Set up a local playground: demo accounts plus a few real chapters, opened for review.

    python -m review_site.tools.devseed            # 6 chapters
    python -m review_site.tools.devseed --chapters 20
    uvicorn review_site.app.main:create_app --factory --reload

Log in as admin@example.com, editor@example.com or ann@example.com (password: demo-password-123).
Uses the local database in review_site/data (or REVIEW_DATA_DIR); never touches a deployed site."""
import argparse

from sqlalchemy import select, update

from ..app.config import load_settings
from ..app.db import make_engine, make_session_factory
from ..app.models import Chapter, User
from ..app.services import accounts
from . import publish

PASSWORD = "demo-password-123"
PEOPLE = [("admin@example.com", "Demo Admin", "admin"), ("editor@example.com", "Demo Editor", "editor"),
          ("ann@example.com", "Ann Reviewer", "reviewer"), ("ben@example.com", "Ben Reviewer", "reviewer")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chapters", type=int, default=6)
    parser.add_argument("--runs", default="runs/book")
    args = parser.parse_args(argv)
    settings = load_settings()
    if settings.secure_cookies:
        raise SystemExit("devseed is for local use only; REVIEW_SECURE_COOKIES is set.")
    publish.main(["--local", "--runs", args.runs, "--limit", str(args.chapters)])
    factory = make_session_factory(make_engine(settings.db_url))
    with factory() as db:
        for email, name, role in PEOPLE:
            if not db.scalar(select(User).where(User.email == email)):
                accounts.create_user(db, email, name, PASSWORD, role=role, credit_ok=True)
        db.execute(update(Chapter).values(status="open"))
        db.commit()
    print(f"\nReady. Log in with any of {', '.join(p[0] for p in PEOPLE)} (password {PASSWORD}).")
    print("Start the site:  uvicorn review_site.app.main:create_app --factory --reload")


if __name__ == "__main__":
    main()
