"""
Tests for transactional email: when limit notices fire, and that user-supplied
text can't inject markup into an email. No network and no database - the
sender is replaced with a recorder.

Run with:  pytest
"""
import pytest

from app import notifications
from app.notifications import limit_notice_kind, render_email


# ---------------------------------------------------------------------------
# When limit notices fire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("used, limit, expected", [
    (1, 5, None),
    (3, 5, None),
    (4, 5, "warning"),      # 80% of 5
    (5, 5, "reached"),
    (6, 5, None),           # already past it - don't repeat the notice
    (80, 100, "warning"),
    (79, 100, None),
    (81, 100, None),        # only the job that CROSSES the line triggers it
    (100, 100, "reached"),
    (1, 1, "reached"),      # 80% of 1 rounds up to 100%: only "reached", never both
    (0, 5, None),
])
def test_limit_notice_kind(used, limit, expected):
    assert limit_notice_kind(used, limit) == expected


@pytest.mark.parametrize("limit", [None, 0, -1])
def test_unlimited_or_invalid_limits_never_notify(limit):
    assert limit_notice_kind(3, limit) is None


def test_each_notice_fires_exactly_once_over_a_month():
    fired = [limit_notice_kind(used, 10) for used in range(1, 13)]
    assert fired.count("warning") == 1
    assert fired.count("reached") == 1


# ---------------------------------------------------------------------------
# Email template
# ---------------------------------------------------------------------------

def test_render_email_escapes_user_supplied_text():
    out = render_email('<b>Hi</b>', ['A key named "<script>alert(1)</script>"'], "Go", "https://x.test/?a=1&b=\"2")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;b&gt;Hi&lt;/b&gt;" in out
    assert 'href="https://x.test/?a=1&amp;b=&quot;2"' in out


def test_render_email_omits_button_without_a_url():
    out = render_email("Hello", ["Body"], "Click me", None)
    assert "Click me" not in out


# ---------------------------------------------------------------------------
# Notifications use the sender and never raise
# ---------------------------------------------------------------------------

class RecordingSender:
    enabled = True

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, to, subject, html_body):
        if self.fail:
            raise RuntimeError("mail provider down")
        self.sent.append((to, subject, html_body))
        return True


@pytest.fixture
def sender(monkeypatch):
    s = RecordingSender()
    monkeypatch.setattr(notifications, "get_email_sender", lambda: s)
    monkeypatch.setenv("APP_BASE_URL", "https://sg.example.test")
    return s


@pytest.mark.anyio
async def test_welcome_email_links_to_the_dashboard(sender):
    await notifications.notify_welcome("new@example.com")
    to, subject, body = sender.sent[0]
    assert to == "new@example.com"
    assert "Welcome" in subject
    assert "https://sg.example.test/accounts/dashboard" in body


@pytest.mark.anyio
async def test_key_created_email_names_the_key_safely(sender):
    await notifications.notify_key_created("a@example.com", "<img src=x>")
    body = sender.sent[0][2]
    assert "<img src=x>" not in body
    assert "&lt;img src=x&gt;" in body


@pytest.mark.anyio
async def test_job_limit_emails_differ_by_kind(sender):
    await notifications.notify_job_limit("a@example.com", "warning", "free", 4, 5)
    await notifications.notify_job_limit("a@example.com", "reached", "free", 5, 5)
    assert "4 of 5" in sender.sent[0][1]
    assert "reached" in sender.sent[1][1]


@pytest.mark.anyio
async def test_notifications_swallow_sender_failures(monkeypatch):
    monkeypatch.setattr(notifications, "get_email_sender", lambda: RecordingSender(fail=True))
    # None of these may raise: a mail outage must never break the request that triggered it.
    await notifications.notify_welcome("a@example.com")
    await notifications.notify_key_created("a@example.com", "Production")
    await notifications.notify_job_limit("a@example.com", "reached", "free", 5, 5)


@pytest.mark.anyio
async def test_limit_notice_for_cli_key_without_an_account_is_a_noop(sender):
    # CLI-issued keys have user_id None: nobody to email.
    await notifications.notify_job_limit_for_client(
        {"user_id": None, "plan": "free", "monthly_job_limit": 5}, used=5)
    assert sender.sent == []


@pytest.mark.anyio
async def test_unconfigured_sender_reports_disabled_and_does_not_raise():
    s = notifications.EmailSender("", "")
    assert s.enabled is False
    assert await s.send("a@example.com", "Subject", "<p>x</p>") is False