"""Log in, log out, join with an invite code, and admin-issued password reset links."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import csrf_protect, current_user, flash, get_db, render
from ..services import accounts

router = APIRouter()


def _ip(request):
    return request.client.host if request.client else "?"


def _safe_next(request):
    target = request.session.pop("next", "/")
    return target if target.startswith("/") and not target.startswith("//") else "/"


@router.get("/login")
def login_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login(request: Request, email: str = Form(""), password: str = Form(""), db=Depends(get_db)):
    limiter, key = request.app.state.login_limiter, (_ip(request), email.strip().lower())
    if limiter.blocked(key):
        return render(request, "login.html", status_code=429, error="Too many tries. Please wait a few minutes.",
                      email=email)
    user = accounts.authenticate(db, email, password)
    if user is None:
        limiter.fail(key)
        return render(request, "login.html", status_code=400, error="That email and password don't match.",
                      email=email)
    limiter.reset(key)
    db.commit()
    next_url = _safe_next(request)
    request.session.clear()
    request.session["uid"] = user.id
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/join")
def join_page(request: Request, code: str = "", db=Depends(get_db), user=Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "join.html", code=code.strip().upper())


@router.post("/join", dependencies=[Depends(csrf_protect)])
def join(request: Request, code: str = Form(""), email: str = Form(""), display_name: str = Form(""),
         password: str = Form(""), credit_ok: str = Form(""), db=Depends(get_db)):
    limiter, key = request.app.state.login_limiter, ("join", _ip(request))
    if limiter.blocked(key):
        return render(request, "join.html", status_code=429, error="Too many tries. Please wait a few minutes.",
                      code=code)
    try:
        user = accounts.register(db, code, email, display_name, password, bool(credit_ok))
        db.commit()
    except (accounts.AccountError, ValueError) as exc:
        db.rollback()
        limiter.fail(key)
        return render(request, "join.html", status_code=400, error=str(exc), code=code, email=email,
                      display_name=display_name, credit_ok=bool(credit_ok))
    request.session.clear()
    request.session["uid"] = user.id
    flash(request, f"Welcome, {user.display_name}!", "ok")
    return RedirectResponse("/", status_code=303)


@router.get("/reset/{token}")
def reset_page(request: Request, token: str, db=Depends(get_db)):
    if accounts.reset_for(db, token) is None:
        return render(request, "error.html", status_code=404, code=404,
                      message="That reset link has expired or was already used. Ask for a new one.")
    return render(request, "reset.html", token=token)


@router.post("/reset/{token}", dependencies=[Depends(csrf_protect)])
def reset(request: Request, token: str, password: str = Form(""), db=Depends(get_db)):
    row = accounts.reset_for(db, token)
    if row is None:
        return render(request, "error.html", status_code=404, code=404,
                      message="That reset link has expired or was already used. Ask for a new one.")
    try:
        user = accounts.finish_reset(db, row, password)
        db.commit()
    except accounts.AccountError as exc:
        db.rollback()
        return render(request, "reset.html", status_code=400, token=token, error=str(exc))
    request.session.clear()
    request.session["uid"] = user.id
    flash(request, "Your password has been changed.", "ok")
    return RedirectResponse("/", status_code=303)
