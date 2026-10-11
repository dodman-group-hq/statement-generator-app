"""
FastAPI entry point. Run with: uvicorn main:app --host 0.0.0.0 --port 8000
(Procfile does exactly this - see Procfile for the real production command.)
"""
import os
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import init_pool, close_pool, init_schema
from app.observability import init_error_tracking
from app.billing import setup_billing
from app.routes.generate_statements import router as statement_generator_router, templates as sg_templates, STATIC_DIR
from app.routes.accounts import router as accounts_router

load_dotenv()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '25'))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_error_tracking(app_env=os.getenv('APP_ENV', 'production'))

    database_ready = False
    try:
        await init_pool()
        # Don't crash the whole app if the database happens to be unreachable
        # at boot (e.g. Neon is still waking from autosuspend) - requests
        # will just get a clear 503 from the auth layer until it's reachable.
        await init_schema()
        database_ready = True
    except Exception as e:
        logger.error(f"Could not initialize database at startup: {e}")

    # Billing needs a live pool (its engine is built around it), so it can
    # only start once the database is up. It mounts /billing onto the app
    # here, at startup - which is why the 404 page below is an exception
    # handler and not a catch-all route: a catch-all registered at import
    # time would sit BEFORE these late-mounted routes and swallow them.
    billing_task = await setup_billing(app) if database_ready else None

    yield

    if billing_task is not None:
        billing_task.cancel()
    await close_pool()


app = FastAPI(
    title="Statement Generator API",
    description="Merge a Word template with a spreadsheet to generate personalized, optionally password-protected PDF statements - one per row.",
    version="2.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

landing_templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "app", "templates"))


def _url_for_static(name: str, **kwargs) -> str:
    """Matches Flask's url_for('static', filename=...) call shape exactly,
    so the existing (already-reviewed) templates don't need editing just
    because the framework changed under them."""
    if name == 'static':
        return f"/static/{kwargs.get('filename', '')}"
    return ""


def _get_flashed_messages(*args, **kwargs) -> list:
    """Flask's flash() is never actually called anywhere in this app - this
    is a no-op stand-in so the dead template code referencing it doesn't
    raise a Jinja UndefinedError."""
    return []


for _t in (sg_templates, landing_templates):
    _t.env.globals['url_for'] = _url_for_static
    _t.env.globals['get_flashed_messages'] = _get_flashed_messages


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """Reject oversized uploads with a clear error instead of a hung
    request. FastAPI/Starlette has no built-in MAX_CONTENT_LENGTH the way
    Flask does, so this is a small middleware doing the same job."""
    content_length = request.headers.get('content-length')
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=413,
            content={"message": f"Upload too large - please keep files under {MAX_UPLOAD_MB}MB."},
        )
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Keep the same {"message": ...} response shape the Flask version used,
    for any client code that already expects it - HTTPException(detail=...)
    becomes {"message": detail} instead of FastAPI's default {"detail": ...}.

    Also renders the friendly 404 page for URLs that match no route at all.
    Starlette signals that case with the stock detail "Not Found"; a route
    that deliberately raises its own 404 (e.g. "Invalid or expired download
    ID.") has a custom detail and still gets JSON."""
    if exc.status_code == 404 and exc.detail == "Not Found":
        return landing_templates.TemplateResponse(request, "404.html", status_code=404)

    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"message": detail})


app.include_router(statement_generator_router, prefix="/api")
app.include_router(accounts_router, prefix="/accounts")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request):
    return landing_templates.TemplateResponse(request, "landing_page.html")