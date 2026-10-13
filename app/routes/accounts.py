"""
Accounts: signup, login, logout, and a dashboard where a logged-in user can
issue their own API key instead of needing the founder to run
scripts/create_api_key.py for every new customer. This is the "self-serve"
piece the earlier billing decision depends on.

Session handling matches Vett's exactly (see app/accounts.py) - same
cookie name, same token format, same secret env var - so turning on true
cross-product SSO later is a config change, not a rewrite.
"""
import logging
import os
import secrets

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Request, Response, Cookie, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.accounts import (
    hash_password, verify_password, make_session_token, decode_session_token,
    generate_reset_token, hash_reset_token,
)
from app.db import (
    create_user, get_user_by_email, get_user_by_id,
    create_api_client, list_api_clients_for_user, plan_defaults,
    get_active_plan_for_user, describe_plan, plan_label,
    create_password_reset, password_reset_is_valid, consume_password_reset,
)
from app.notifications import (
    notify_welcome, notify_key_created, notify_password_reset, notify_password_changed, app_url,
)
from app.ratelimit import SlidingWindowLimiter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["accounts"])

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"))

MAX_LABEL_LENGTH = 100
MIN_PASSWORD_LENGTH = 8

# Per-IP cap on reset requests. Deliberately generous: behind a proxy that isn't
# configured to forward client IPs, every visitor shares one address, and a tight
# limit would then block everyone. The real protection against email flooding is
# the per-account limit in db.create_password_reset (3 links per hour).
_forgot_ip_limiter = SlidingWindowLimiter(max_events=30, window_seconds=3600)

FORGOT_PASSWORD_MESSAGE = ("If an account exists for that email, a reset link is on its way. "
                           "The link expires in 1 hour.")
INVALID_RESET_MESSAGE = "This reset link is invalid, expired, or has already been used."



async def get_optional_web_user(dodman_session: str | None) -> dict | None:
    if not dodman_session:
        return None
    user_id = decode_session_token(dodman_session)
    if not user_id:
        return None
    return await get_user_by_id(user_id)


def _set_session_cookie(resp: Response, user_id: str) -> None:
    token = make_session_token(user_id)
    resp.set_cookie(
        key="dodman_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=(os.getenv("APP_ENV", "production") == "production"),
        max_age=60 * 60 * 24 * 7,  # 7 days - matches Vett
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/signup", response_class=HTMLResponse, include_in_schema=False)
async def signup_page(request: Request):
    return templates.TemplateResponse(request, "accounts_signup.html")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "accounts_login.html")


@router.get("/forgot-password", response_class=HTMLResponse, include_in_schema=False)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "accounts_forgot_password.html")


@router.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
async def reset_password_page(request: Request, token: str | None = None):
    """Checks the link up front so someone with a dead link is told immediately,
    rather than after typing a new password. The page is served with
    Referrer-Policy: no-referrer and no caching, because the secret token is in
    the URL and must not leak to the third-party font host or into a cache."""
    error = None
    if not token:
        error = "This reset link is missing its token. Please request a new one."
    else:
        try:
            if not await password_reset_is_valid(hash_reset_token(token)):
                error = INVALID_RESET_MESSAGE
        except Exception as e:
            logger.error(f"Could not check reset token: {e}")
            error = "We couldn't check this link right now. Please try again in a moment."

    resp = templates.TemplateResponse(request, "accounts_reset_password.html", {
        "token": None if error else token, "error": error,
    })
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(request: Request, dodman_session: str | None = Cookie(default=None)):
    user = await get_optional_web_user(dodman_session)
    if not user:
        return RedirectResponse(url="/accounts/login", status_code=303)
    clients = await list_api_clients_for_user(user["id"])
    # Plan cards are generated from the same table the limits are enforced
    # from (app/db.py PLAN_DEFAULTS), so the dashboard can't advertise
    # numbers the app doesn't actually apply.
    plans = [{"key": k, "label": plan_label(k), "summary": describe_plan(k)} for k in ("starter", "pro")]
    return templates.TemplateResponse(request, "accounts_dashboard.html", {
        "user": user, "clients": clients, "plans": plans, "free_summary": describe_plan("free"),
    })


# ---------------------------------------------------------------------------
# Actions (called via fetch() from the pages above)
# ---------------------------------------------------------------------------

@router.post("/signup", include_in_schema=False)
async def signup_action(background_tasks: BackgroundTasks, email: str = Form(...), password: str = Form(...)):
    email = email.lower().strip()
    if len(password) < 8:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    duplicate = HTTPException(
        status_code=409,
        detail="An account with that email already exists. Try logging in instead.",
    )

    existing = await get_user_by_email(email)
    if existing:
        raise duplicate

    try:
        user_id = await create_user(email, hash_password(password))
    except asyncpg.UniqueViolationError:
        # Two signups for the same email raced past the check above - the
        # database's unique constraint is the real guard, so report it as
        # the duplicate it is rather than as a service outage.
        raise duplicate
    except Exception as e:
        logger.error(f"Signup failed for {email}: {e}")
        raise HTTPException(status_code=503, detail="Could not create account. Please try again shortly.")

    resp = RedirectResponse(url="/accounts/dashboard", status_code=303)
    _set_session_cookie(resp, user_id)
    background_tasks.add_task(notify_welcome, email)
    logger.info(f"New account created: {email}")
    return resp


@router.post("/login", include_in_schema=False)
async def login_action(email: str = Form(...), password: str = Form(...)):
    row = await get_user_by_email(email)
    invalid = HTTPException(status_code=401, detail="Incorrect email or password.")
    if not row:
        raise invalid
    if not verify_password(password, row["password_hash"]):
        raise invalid

    resp = RedirectResponse(url="/accounts/dashboard", status_code=303)
    _set_session_cookie(resp, str(row["id"]))
    return resp


@router.post("/forgot-password", include_in_schema=False)
async def forgot_password_action(request: Request, background_tasks: BackgroundTasks, email: str = Form(...)):
    """Always answers the same way whether or not the email has an account, so
    this can't be used to find out who is registered. The email itself is sent in
    the background so response time doesn't give it away either."""
    client_ip = request.client.host if request.client else "unknown"
    if not _forgot_ip_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    email = email.lower().strip()
    if not email or len(email) > 320 or "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    try:
        user = await get_user_by_email(email)
        if user:
            raw_token, token_hash = generate_reset_token()
            if await create_password_reset(str(user["id"]), token_hash):
                link = app_url(f"/accounts/reset-password?token={raw_token}")
                if link:
                    background_tasks.add_task(notify_password_reset, user["email"], link)
                elif os.getenv("APP_ENV", "production") != "production":
                    logger.info(f"[dev] APP_BASE_URL not set; password reset link for {email}: "
                                f"/accounts/reset-password?token={raw_token}")
                else:
                    logger.error("APP_BASE_URL is not set - cannot build a password reset link, so no email was sent.")
    except Exception as e:
        logger.error(f"Password reset request failed: {e}")
        raise HTTPException(status_code=503, detail="Could not process your request right now. Please try again shortly.")

    return JSONResponse({"message": FORGOT_PASSWORD_MESSAGE})


@router.post("/reset-password", include_in_schema=False)
async def reset_password_action(background_tasks: BackgroundTasks, token: str = Form(...), password: str = Form(...)):
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    invalid = HTTPException(status_code=400, detail=INVALID_RESET_MESSAGE)
    token_hash = hash_reset_token(token)
    try:
        # Cheap check first so junk tokens don't cost a bcrypt hash each.
        if not await password_reset_is_valid(token_hash):
            raise invalid
        user_id = await consume_password_reset(token_hash, hash_password(password))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password reset failed: {e}")
        raise HTTPException(status_code=503, detail="Could not reset your password right now. Please try again shortly.")

    if user_id is None:  # lost a race with another submission of the same link
        raise invalid

    user = await get_user_by_id(user_id)
    if user:
        background_tasks.add_task(notify_password_changed, user["email"])
    logger.info(f"Password reset completed for user {user_id}")
    return JSONResponse({"message": "Your password has been updated. You can now log in."})


@router.post("/logout", include_in_schema=False)
async def logout_action():
    resp = RedirectResponse(url="/accounts/login", status_code=303)
    resp.delete_cookie(key="dodman_session", httponly=True, samesite="lax")
    return resp


@router.post("/dashboard/create-key", include_in_schema=False)
async def create_key_action(background_tasks: BackgroundTasks, label: str = Form(...), dodman_session: str | None = Cookie(default=None)):
    """Called via fetch() from the dashboard and returns JSON, NOT a rendered
    page. Previously this re-rendered the whole dashboard from the POST, so
    refreshing the browser re-submitted the form and silently minted another
    key. The raw key now exists only in this one response; the page shows it
    once and a refresh simply reloads the dashboard (GET) without it."""
    user = await get_optional_web_user(dodman_session)
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired. Please log in again.")

    label = (label.strip() or f"{user['email']}'s key")[:MAX_LABEL_LENGTH]
    # New keys start with whatever the user is currently entitled to - a paying
    # customer's new key must not come out with free-plan limits.
    plan = await get_active_plan_for_user(user["id"])
    limits = plan_defaults(plan)

    # statement_api_clients.label is UNIQUE across ALL clients, not per user,
    # so two different users naming a key "Production" would collide. On a
    # collision, retry once with a short random suffix instead of failing.
    final_label = label
    raw_key = None
    for attempt in range(2):
        try:
            raw_key = await create_api_client(final_label, plan=plan, user_id=user["id"], **limits)
            break
        except asyncpg.UniqueViolationError:
            final_label = f"{label[:MAX_LABEL_LENGTH - 7]} ({secrets.token_hex(2)})"
        except Exception as e:
            logger.error(f"Self-serve key creation failed for user {user['id']}: {e}")
            raise HTTPException(status_code=503, detail="Could not create a key right now. Please try again shortly.")

    if raw_key is None:
        raise HTTPException(status_code=503, detail="Could not create a key right now. Please try again shortly.")

    background_tasks.add_task(notify_key_created, user["email"], final_label)

    # Re-read so the response reflects exactly what was stored (created_at, status).
    clients = await list_api_clients_for_user(user["id"])
    created = next((c for c in clients if c["label"] == final_label), None)

    return JSONResponse({
        "key": raw_key,
        "label": final_label,
        "plan": plan,
        "is_active": True,
        "created_at": created["created_at"].isoformat() if created else None,
    }, headers={"Cache-Control": "no-store"})