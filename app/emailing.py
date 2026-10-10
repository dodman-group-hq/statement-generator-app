"""
Transactional email for Statement Generator's own auth flows (verification,
password reset) - separate from billing emails (invoices, subscription
lifecycle), but deliberately reusing the SAME sending infrastructure
(dodman_billing.notifications) and the SAME branded-HTML builder, so there's
one implementation of "how do we send an email" across the whole product
suite, not two.
"""
import os
import logging

from dodman_billing.notifications import ResendEmailSender, NullEmailSender, EmailSender
from dodman_billing.config import BrandingConfig
from dodman_billing.emails import build_lifecycle_email_html

logger = logging.getLogger(__name__)

BRANDING = BrandingConfig(
    product_display_name="Statement Generator",
    support_email=os.getenv("RESEND_FROM", ""),
)


def _get_sender() -> EmailSender:
    api_key = os.getenv("RESEND_API_KEY", "")
    from_addr = os.getenv("RESEND_FROM", "")
    if api_key:
        return ResendEmailSender(api_key, from_addr)
    return NullEmailSender()


async def send_verification_email(to: str, verify_url: str) -> None:
    html = build_lifecycle_email_html(
        BRANDING,
        heading="Verify Your Email",
        body=(
            "<p>Confirm your email address to unlock API key creation.</p>"
            "<p>This link expires in 24 hours.</p>"
        ),
        cta_text="Verify Email", cta_url=verify_url,
    )
    await _get_sender().send(to, "Verify your email - Statement Generator", html)


async def send_password_reset_email(to: str, reset_url: str) -> None:
    html = build_lifecycle_email_html(
        BRANDING,
        heading="Reset Your Password",
        body=(
            "<p>We received a request to reset your password.</p>"
            "<p>This link expires in 1 hour. If you didn't request this, you can ignore this email.</p>"
        ),
        cta_text="Reset Password", cta_url=reset_url,
    )
    await _get_sender().send(to, "Reset your password - Statement Generator", html)