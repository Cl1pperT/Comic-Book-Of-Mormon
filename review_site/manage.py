"""Housekeeping commands. Run from the repo root:

    python -m review_site.manage upgrade                       # create/upgrade the database
    python -m review_site.manage create-user --email you@x.com --name "You" --role admin
    python -m review_site.manage create-invite --uses 5 --note "Family"
    python -m review_site.manage stats
"""
import argparse
import getpass
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from .app.config import HERE, load_settings
from .app.db import make_engine, make_session_factory
from .app.models import Chapter, Issue, Review, User
from .app.services import accounts
from .app.services.assignment import ensure_meta


def alembic_config(settings):
    config = Config(str(HERE / "alembic.ini"))
    config.set_main_option("script_location", str(HERE / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.db_url.replace("%", "%%"))
    return config


def upgrade(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")
    factory = make_session_factory(make_engine(settings.db_url))
    with factory() as db:
        ensure_meta(db)
        db.commit()
    print(f"Database ready: {settings.db_url}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="review_site.manage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("upgrade", help="create or upgrade the database")
    user = sub.add_parser("create-user", help="create an account (asks for the password unless REVIEW_PASSWORD is set)")
    user.add_argument("--email", required=True)
    user.add_argument("--name", required=True)
    user.add_argument("--role", choices=("reviewer", "editor", "admin"), default="reviewer")
    user.add_argument("--can-choose", action="store_true", help="let them pick any chapter")
    invite = sub.add_parser("create-invite", help="print a new invite code")
    invite.add_argument("--uses", type=int, default=1)
    invite.add_argument("--role", choices=("reviewer", "editor", "admin"), default="reviewer")
    invite.add_argument("--note", default="")
    invite.add_argument("--can-choose", action="store_true", help="people who join start pre-approved to pick any chapter")
    sub.add_parser("stats", help="a few counts")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.cmd == "upgrade":
        return upgrade(settings)
    factory = make_session_factory(make_engine(settings.db_url))
    with factory() as db:
        if args.cmd == "create-user":
            password = os.environ.get("REVIEW_PASSWORD") or getpass.getpass("Password (10+ characters): ")
            try:
                created = accounts.create_user(db, args.email, args.name, password, role=args.role,
                                             can_choose=args.can_choose)
            except accounts.AccountError as exc:
                sys.exit(str(exc))
            db.commit()
            print(f"Created {created.role} {created.email}")
        elif args.cmd == "create-invite":
            created = accounts.create_invite(db, None, args.role, args.uses, note=args.note, can_choose=args.can_choose)
            db.commit()
            print(f"Invite code {created.code}  (link: /join?code={created.code})")
        elif args.cmd == "stats":
            print("users", db.scalar(select(func.count()).select_from(User)),
                  "| chapters", db.scalar(select(func.count()).select_from(Chapter)),
                  "| reviews", db.scalar(select(func.count()).select_from(Review).where(Review.state == "submitted")),
                  "| issues", db.scalar(select(func.count()).select_from(Issue)))


if __name__ == "__main__":
    main()
