"""
Transactional email for Statement Generator.

Scope: emails a person NEEDS because of something they did or because their
account state changed - welcome, new API key created, approaching/reaching a
plan limit. (Billing receipts and subscription notices are sent by the shared
dodman_billing package, through the same sender defined here. Password reset
will use it too.) Nothing here is marketing: no newsletters, no promotions.
Marketing email would need consent and an unsubscribe mechanism and should be
built separately.

Delivery goes through Resend's HTTP API. If RESEND_API_KEY / RESEND_FROM
aren't set (local dev, tests) the sender just logs that an email WOULD have
been sent - recipient and subject only, never the body, since bodies can
contain links and personal data.

Every notify_* function is safe to fire in the background: it never raises,
so a mail outage can never fail the request that triggered it.
"""
import html
import logging
import math
import os

import httpx

from app.db import plan_label

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
PRODUCT_NAME = "Statement Generator"
COMPANY_NAME = "Dodman Group"

# Warn when this fraction of the monthly job allowance has been used.
LIMIT_WARNING_FRACTION = 0.8


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

class EmailSender:
    """Implements the same `async send(to, subject, html) -> bool` interface the
    dodman_billing package expects, so one instance serves the whole app."""

    def __init__(self, api_key: str, from_addr: str):
        self._api_key = api_key
        self._from = from_addr

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._from)

    async def send(self, to: str, subject: str, html_body: str) -> bool:
        if not self.enabled:
            logger.info("Email delivery not configured - would have sent %r to %s", subject, to)
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    RESEND_API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json={"from": self._from, "to": [to], "subject": subject, "html": html_body},
                )
                resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Sending email failed: to=%s subject=%r", to, subject)
            return False


_sender: EmailSender | None = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = EmailSender(os.getenv("RESEND_API_KEY", ""), os.getenv("RESEND_FROM", ""))
    return _sender


def _app_url(path: str) -> str | None:
    """Absolute URL for a link in an email, or None if APP_BASE_URL isn't set
    (a relative link is useless in an inbox, so the button is simply omitted)."""
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return f"{base}{path}" if base else None


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

def render_email(heading: str, paragraphs: list[str], cta_text: str = "", cta_url: str | None = None,
                 footnote: str = "") -> str:
    """Branded HTML email in Statement Generator's own look (paper, ink, gold
    seal). Everything passed in is HTML-escaped, so a user-supplied value such
    as an API key label can't inject markup into an email."""
    support = os.getenv("SUPPORT_EMAIL", "inquiries@dodman.group")
    body = "".join(
        f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#16213e;">{html.escape(p)}</p>'
        for p in paragraphs
    )
    button = ""
    if cta_text and cta_url:
        button = (
            f'<p style="margin:22px 0 6px;"><a href="{html.escape(cta_url, quote=True)}" '
            'style="display:inline-block;background:#16213e;color:#faf9f4;text-decoration:none;'
            'font-size:15px;padding:12px 26px;border-radius:2px;">'
            f'{html.escape(cta_text)}</a></p>'
        )
    note = (f'<p style="margin:18px 0 0;font-size:12.5px;line-height:1.6;color:#4c5674;">{html.escape(footnote)}</p>'
            if footnote else "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:32px 16px;background:#edeee7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#faf9f4;border:1px solid #d7d3c4;">
    <div style="padding:22px 28px;border-bottom:2px solid #16213e;">
      <p style="margin:0 0 4px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#8a6220;">{html.escape(PRODUCT_NAME)}</p>
      <p style="margin:0;font-size:21px;font-weight:600;font-family:Georgia,'Times New Roman',serif;color:#16213e;">{html.escape(heading)}</p>
    </div>
    <div style="padding:24px 28px;">{body}{button}{note}</div>
    <div style="padding:16px 28px;border-top:1px solid #d7d3c4;font-size:12px;color:#4c5674;">
      {html.escape(COMPANY_NAME)} &middot; {html.escape(PRODUCT_NAME)} &middot; Questions? Email
      <a href="mailto:{html.escape(support, quote=True)}" style="color:#8a6220;">{html.escape(support)}</a>
    </div>
  </div>
</body></html>"""


# ---------------------------------------------------------------------------
# Limit-notice logic (pure, so it's easy to test)
# ---------------------------------------------------------------------------

def limit_notice_kind(used: int, limit: int | None) -> str | None:
    """Which notice, if any, should go out now that `used` jobs have been
    started this month against a monthly `limit`.

    Returns "warning" at exactly 80% of the allowance, "reached" at exactly
    100%, otherwise None. Matching the exact count (not "at or above") means
    each notice goes out once per month, at the job that crosses the line -
    no flag column or extra state needed. A tiny limit where 80% rounds up to
    100% gets only the "reached" notice.
    """
    if limit is None or limit <= 0:
        return None
    if used == limit:
        return "reached"
    warn_at = math.ceil(limit * LIMIT_WARNING_FRACTION)
    if used == warn_at and warn_at < limit:
        return "warning"
    return None


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

async def notify_welcome(email: str) -> None:
    try:
        await get_email_sender().send(
            email,
            f"Welcome to {PRODUCT_NAME}",
            render_email(
                f"Welcome to {PRODUCT_NAME}",
                [
                    "Your account is ready. To start generating statements, create an API key on your dashboard, "
                    "then upload a Word template and a spreadsheet on the generator page.",
                    "You can download a working example template and spreadsheet from the home page.",
                ],
                "Go to your dashboard", _app_url("/accounts/dashboard"),
            ),
        )
    except Exception:
        logger.exception("notify_welcome failed")


async def notify_key_created(email: str, key_label: str) -> None:
    """Security notice: a new key can act as the account, so tell the owner."""
    try:
        await get_email_sender().send(
            email,
            "A new API key was created on your account",
            render_email(
                "New API key created",
                [
                    f'A new API key named "{key_label}" was just created on your {PRODUCT_NAME} account.',
                    "If this was you, there's nothing to do.",
                ],
                "Review your keys", _app_url("/accounts/dashboard"),
                footnote="If you didn't create this key, change your password and email "
                         f"{os.getenv('SUPPORT_EMAIL', 'inquiries@dodman.group')} straight away.",
            ),
        )
    except Exception:
        logger.exception("notify_key_created failed")


async def notify_job_limit(email: str, kind: str, plan: str, used: int, limit: int) -> None:
    try:
        label = plan_label(plan)
        if kind == "warning":
            subject = f"You've used {used} of {limit} statement jobs this month"
            heading = "Approaching your monthly limit"
            paragraphs = [
                f"You've used {used} of the {limit} jobs included in your {label} plan this month.",
                "Your allowance resets at the start of next month. If you need more, you can upgrade at any time.",
            ]
        else:
            subject = "You've reached your monthly statement job limit"
            heading = "Monthly limit reached"
            paragraphs = [
                f"You've used all {limit} jobs included in your {label} plan this month, so new jobs will "
                "be declined until your allowance resets at the start of next month.",
                "Jobs already running are not affected. To keep generating now, upgrade your plan.",
            ]
        await get_email_sender().send(
            email, subject,
            render_email(heading, paragraphs, "View plans", _app_url("/accounts/dashboard")),
        )
    except Exception:
        logger.exception("notify_job_limit failed")


async def notify_job_limit_for_client(client: dict, used: int) -> None:
    """Looks up the account owner's email and sends the right limit notice, if
    one is due. Keys issued from the command line have no login account, so
    there's nobody to email and this quietly does nothing."""
    try:
        kind = limit_notice_kind(used, client.get("monthly_job_limit"))
        if kind is None or not client.get("user_id"):
            return
        from app.db import get_user_by_id  # local import: keeps this module importable without a pool
        user = await get_user_by_id(str(client["user_id"]))
        if not user:
            return
        await notify_job_limit(user["email"], kind, client.get("plan", "free"), used, client["monthly_job_limit"])
    except Exception:
        logger.exception("notify_job_limit_for_client failed")