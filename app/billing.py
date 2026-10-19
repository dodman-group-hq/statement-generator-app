"""
Billing for Statement Generator, built on the shared `dodman_billing` package.

What lives here (and only here) is what is specific to this product:
  - which Paystack plan(s) exist and what each one MEANS (entitlement hooks)
  - how to tell who is calling (session cookie -> current user)
  - the admin secret for the one admin route
  - startup wiring and the reconciliation scheduler

Everything generic (subscribe, callback, webhooks, cancel, invoices,
proration, grace periods) is the package's job.

Design notes
------------
* Entitlements come from plan_defaults() in app/db.py - the same table the
  CLI and self-serve signup use - so "what does the paid plan allow" is
  defined in exactly one place.

* Cancelling does NOT deactivate the user's API keys. It drops them back to
  the free plan's limits. A lapsed customer keeps a working (limited)
  account instead of every integration suddenly returning 401, and because
  `is_active` is never toggled here, a key the user deliberately deactivated
  can't be switched back on by a renewal.

* Only the PAID plans (starter, pro) are registered with Paystack. "free" is
  simply the absence of a subscription, not something anyone pays for. The
  keys and ranks mirror Vett's structure (starter < pro) so upgrade/downgrade
  logic in the shared package behaves identically in both products.

* Billing is OPTIONAL at startup: if its environment variables aren't set
  (local dev, tests) the app runs normally without /billing routes.
"""
import asyncio
import hmac
import logging
import os

import asyncpg
from fastapi import Cookie, Header, HTTPException

from dodman_billing import (
    BillingConfig, BrandingConfig, Plan, PlanRegistry,
    SubscriptionRepository, PaystackClient, SharedUsersResolver,
    BillingEngine, build_billing_router,
)

from app.db import get_pool, plan_defaults
from app.notifications import get_email_sender
from app.observability import capture_service_error

logger = logging.getLogger(__name__)

PRODUCT_SLUG = "statement-generator"

REQUIRED_ENV = (
    "PAYSTACK_SECRET_KEY", "PAYSTACK_WEBHOOK_SECRET", "APP_BASE_URL",
    "PAYSTACK_PLAN_SG_STARTER", "PAYSTACK_PLAN_SG_PRO",
)


# ---------------------------------------------------------------------------
# Entitlements: what a plan means for this product
# ---------------------------------------------------------------------------

async def _apply_plan(conn: asyncpg.Connection, user_id: str, plan_key: str) -> None:
    limits = plan_defaults(plan_key)
    await conn.execute(
        """
        UPDATE statement_api_clients
        SET plan = $1, monthly_job_limit = $2, max_rows_per_job = $3,
            password_protection_allowed = $4
        WHERE user_id = $5
        """,
        plan_key, limits["monthly_job_limit"], limits["max_rows_per_job"],
        limits["password_protection_allowed"], user_id,
    )


class StatementGeneratorEntitlementHooks:
    """Runs inside the SAME transaction as the subscription write, so the
    subscription row and the user's key limits commit or roll back together."""

    async def on_activated(self, conn: asyncpg.Connection, user_id: str, plan_key: str) -> None:
        await _apply_plan(conn, user_id, plan_key)

    async def on_cancelled(self, conn: asyncpg.Connection, user_id: str) -> None:
        await _apply_plan(conn, user_id, "free")


# ---------------------------------------------------------------------------
# Dependencies the billing router needs from this app
# ---------------------------------------------------------------------------

async def get_current_user(dodman_session: str | None = Cookie(default=None)) -> dict:
    """Session-cookie auth for /billing routes. Returns {"id": str, "email": str}."""
    # Imported here, not at module top: app.routes.accounts imports from this
    # module's neighbours, and a top-level import would be circular.
    from app.routes.accounts import get_optional_web_user

    user = await get_optional_web_user(dodman_session)
    if not user:
        raise HTTPException(status_code=401, detail={
            "code": "UNAUTHENTICATED", "message": "Please log in to continue.", "request_id": None,
        })
    return {"id": str(user["id"]), "email": user["email"]}


async def verify_internal_secret(x_internal_secret: str = Header(default="")) -> bool:
    """Guards the admin-only reactivate route. If INTERNAL_API_SECRET isn't
    configured the route is simply unusable (always 401) rather than open."""
    expected = os.getenv("INTERNAL_API_SECRET", "")
    if not expected or not hmac.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=401, detail={
            "code": "UNAUTHENTICATED", "message": "Invalid internal secret.", "request_id": None,
        })
    return True


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------

def _build_config() -> BillingConfig:
    return BillingConfig(
        product_slug=PRODUCT_SLUG,
        paystack_secret_key=os.environ["PAYSTACK_SECRET_KEY"],
        # Paystack signs webhooks with your SECRET key, so in practice this is
        # the same value as PAYSTACK_SECRET_KEY.
        paystack_webhook_secret=os.environ["PAYSTACK_WEBHOOK_SECRET"],
        app_base_url=os.environ["APP_BASE_URL"].rstrip("/"),
        branding=BrandingConfig(
            product_display_name="Statement Generator",
            support_email=os.getenv("SUPPORT_EMAIL", "inquiries@dodman.group"),
            brand_color="#a9792c",
        ),
        resend_api_key=os.getenv("RESEND_API_KEY", ""),
        resend_from=os.getenv("RESEND_FROM", ""),
        # This app's own pages (the package defaults match Vett's).
        checkout_success_path="/accounts/dashboard",
        checkout_failure_path="/accounts/dashboard",
        dashboard_path="/accounts/dashboard",
    )


async def setup_billing(app) -> asyncio.Task | None:
    """Builds the engine, mounts /billing, and starts the reconciliation
    loop. Returns the loop's task (cancel it at shutdown), or None if
    billing isn't configured or couldn't start - the rest of the app keeps
    working either way."""
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        logger.warning(f"Billing is disabled - missing environment variables: {', '.join(missing)}")
        return None

    try:
        config = _build_config()
        pool = get_pool()

        plans = PlanRegistry([
            Plan(key="starter", paystack_plan_code=os.environ["PAYSTACK_PLAN_SG_STARTER"], rank=1),
            Plan(key="pro", paystack_plan_code=os.environ["PAYSTACK_PLAN_SG_PRO"], rank=2),
        ])

        repository = SubscriptionRepository(pool, product=config.product_slug)
        # NOTE: if this database is shared with Vett, run Vett's migration
        # (examples/vett_migration_script.py in dodman-billing) BEFORE the
        # first deploy that reaches this line - see the wiring notes.
        await repository.init_schema()

        resolver = SharedUsersResolver(pool)
        # One sender for the whole app (welcome emails, limit warnings, ...) and
        # billing, so there's one place that knows how email is delivered.
        email_sender = get_email_sender()

        engine = BillingEngine(
            config=config,
            plans=plans,
            repository=repository,
            paystack=PaystackClient(config.paystack_secret_key),
            hooks=StatementGeneratorEntitlementHooks(),
            resolve_user_by_email=resolver.user_id_by_email,
            resolve_email_by_user_id=resolver.email_by_user_id,
            email_sender=email_sender,
        )

        router = build_billing_router(
            engine=engine,
            get_current_user=get_current_user,
            verify_internal_secret=verify_internal_secret,
        )
        app.include_router(router, prefix="/billing", tags=["billing"])
        app.state.billing_engine = engine

        logger.info("Billing enabled (product=%s, email=%s).", PRODUCT_SLUG, "resend" if email_sender.enabled else "log-only")
        return asyncio.create_task(_reconciliation_loop(engine))
    except Exception as e:
        capture_service_error(e, where="setup_billing")
        logger.error(f"Billing could not be started; continuing without it: {e}")
        return None


async def _reconciliation_loop(engine: BillingEngine) -> None:
    """Applies due scheduled downgrades and revokes access once a payment
    grace period has expired. Safe to run in-process because this app runs a
    single worker (see Dockerfile) - with several workers each would run its
    own copy and they'd need a lock."""
    interval = int(os.getenv("BILLING_RECONCILE_INTERVAL_SECONDS", "3600"))
    await asyncio.sleep(60)  # let startup finish first
    while True:
        for job in (engine.process_due_plan_changes, engine.enforce_payment_grace_periods):
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                capture_service_error(e, where=f"billing.{job.__name__}")
        await asyncio.sleep(interval)