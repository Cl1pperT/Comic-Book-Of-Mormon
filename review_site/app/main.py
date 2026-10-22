"""The web app. Run it with:  uvicorn review_site.app.main:create_app --factory"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect
from starlette.middleware.sessions import SessionMiddleware

from .config import HERE, load_settings
from .db import make_engine, make_session_factory
from .deps import LoginRequired, render
from .media import MediaStore
from .routers import admin, auth, reviewer, tool_api
from .security import RateLimiter
from .services.assignment import ensure_meta


def create_app(settings=None, create_tables=False):
    settings = settings or load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    if create_tables:   # tests and quick local runs; production uses migrations (python -m review_site.manage upgrade)
        from .db import Base
        Base.metadata.create_all(engine)
    app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.media = MediaStore(settings.media_dir)
    app.state.login_limiter = RateLimiter()
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax",
                       https_only=settings.secure_cookies, max_age=60 * 60 * 24 * 30, session_cookie="bomreview")
    app.mount("/static", StaticFiles(directory=str(HERE / "app" / "static")), name="static")
    for module in (auth, reviewer, admin, tool_api):
        app.include_router(module.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if "text/html" in response.headers.get("content-type", ""):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Please log in again."}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        request.session["next"] = target if request.method == "GET" else "/"
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/api/", "/media/")):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, message=str(exc.detail), code=exc.status_code)

    if inspect(engine).has_table("meta"):   # absent until migrations have run
        with app.state.session_factory() as db:
            ensure_meta(db)
            db.commit()
    return app
