"""Request helpers: database session, logged-in user, role checks, CSRF, templates."""
from datetime import timezone

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import constants
from .config import HERE
from .models import User
from .security import csrf_token, same

templates = Jinja2Templates(directory=str(HERE / "app" / "templates"))


class LoginRequired(Exception):
    pass


def get_db(request: Request):
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db=Depends(get_db)):
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.is_active:
        request.session.pop("uid", None)
        return None
    return user


def require_user(user=Depends(current_user)):
    if user is None:
        raise LoginRequired()
    return user


def require_role(role):
    needed = constants.ROLE_RANK[role]

    def check(user=Depends(require_user)):
        if constants.ROLE_RANK[user.role] < needed:
            raise HTTPException(403, "You don't have access to that.")
        return user
    return check


require_editor = require_role("editor")
require_admin = require_role("admin")


async def csrf_protect(request: Request):
    """Forms send a hidden `csrf` field; the page's JavaScript sends an `X-CSRF-Token` header."""
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        sent = (await request.form()).get("csrf", "")
    if not same(str(sent), request.session.get("csrf", "")) or not request.session.get("csrf"):
        raise HTTPException(403, "Your session expired. Please reload the page and try again.")


def flash(request, message, kind="info"):
    request.session["flash"] = [kind, message]


def when(value, fmt="%b %d, %Y %H:%M"):
    return value.astimezone(timezone.utc).strftime(fmt) + " UTC" if value else ""


def render(request: Request, name, status_code=200, **context):
    context.setdefault("user", None)
    context["csrf"] = csrf_token(request.session)
    context["flash"] = request.session.pop("flash", None)
    context["c"] = constants
    context["settings"] = request.app.state.settings
    return templates.TemplateResponse(request, name, context, status_code=status_code)


templates.env.globals["when"] = when
templates.env.filters["when"] = when


def paragraphs(text):
    """Plain text -> HTML paragraphs (escaped), for user-written descriptions."""
    return Markup("<br>".join(str(escape(line)) for line in (text or "").splitlines()))


templates.env.filters["paragraphs"] = paragraphs
