"""
Password reset: everything that can be checked without a database, plus the
security properties that matter most (no account enumeration, no reset-link
poisoning, no token leakage, tokens never stored in the clear).

The database-backed behaviour (single use, expiry, throttling) is covered in
test_password_reset_db.py, which only runs when TEST_DATABASE_URL is set.

Run with:  pytest
"""
import pytest
from fastapi.testclient import TestClient

from app import notifications
from app.accounts import generate_reset_token, hash_reset_token
from app.ratelimit import SlidingWindowLimiter
from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def test_generated_tokens_are_unique_and_hash_to_their_stored_form():
    raw1, hash1 = generate_reset_token()
    raw2, hash2 = generate_reset_token()
    assert raw1 != raw2
    assert hash1 == hash_reset_token(raw1) and hash2 == hash_reset_token(raw2)


def test_the_stored_hash_does_not_contain_the_token():
    raw, stored = generate_reset_token()
    assert raw not in stored
    assert len(stored) == 64  # fits the VARCHAR(64) column shared with Vett


def test_tokens_have_enough_entropy():
    raw, _ = generate_reset_token()
    assert len(raw) >= 40  # token_urlsafe(32) -> 43 characters


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def test_forgot_password_page_renders(client):
    response = client.get("/accounts/forgot-password")
    assert response.status_code == 200
    assert 'name="email"' in response.text


def test_login_page_links_to_forgot_password(client):
    assert 'href="/accounts/forgot-password"' in client.get("/accounts/login").text


def test_reset_page_without_a_token_shows_an_error_not_a_form(client):
    response = client.get("/accounts/reset-password")
    assert response.status_code == 200
    assert "missing its token" in response.text
    assert 'name="password"' not in response.text
    assert "/accounts/forgot-password" in response.text


def test_reset_page_never_leaks_the_token_via_referrer_or_cache(client):
    response = client.get("/accounts/reset-password")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_reset_page_loads_nothing_from_third_parties(client):
    # A third-party request would receive the token-bearing URL as its Referer.
    html = client.get("/accounts/reset-password", params={"token": "x"}).text
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html


# ---------------------------------------------------------------------------
# Request validation (rejected before any database access)
# ---------------------------------------------------------------------------

def test_forgot_password_rejects_obviously_bad_email(client):
    response = client.post("/accounts/forgot-password", data={"email": "not-an-email"})
    assert response.status_code == 400
    assert "valid email" in response.json()["message"]


def test_reset_rejects_short_password_before_checking_the_token(client):
    response = client.post("/accounts/reset-password", data={"token": "whatever", "password": "short"})
    assert response.status_code == 400
    assert "at least 8" in response.json()["message"]


def test_database_outage_is_a_503_not_a_leak_or_a_500(client):
    # With no database the lookup fails. The caller must get a clean retryable
    # error, and it must be the same whatever address was submitted.
    for address in ("exists@example.com", "nobody@example.com"):
        response = client.post("/accounts/forgot-password", data={"email": address})
        assert response.status_code == 503
        assert "message" in response.json()


# ---------------------------------------------------------------------------
# Reset-link poisoning
# ---------------------------------------------------------------------------

def test_reset_links_use_the_configured_base_url_never_the_host_header(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://sg.example.test")
    assert notifications.app_url("/accounts/reset-password?token=t") == \
        "https://sg.example.test/accounts/reset-password?token=t"


def test_no_base_url_means_no_link_at_all(monkeypatch):
    # Better to send no link than to guess one from the incoming request.
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    assert notifications.app_url("/accounts/reset-password?token=t") is None


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

class Recorder:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, to, subject, html_body):
        self.sent.append((to, subject, html_body))
        return True


@pytest.fixture
def recorder(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(notifications, "get_email_sender", lambda: r)
    monkeypatch.setenv("APP_BASE_URL", "https://sg.example.test")
    return r


@pytest.mark.anyio
async def test_reset_email_contains_the_link_and_the_expiry(recorder):
    await notifications.notify_password_reset("a@example.com", "https://sg.example.test/accounts/reset-password?token=abc")
    to, subject, body = recorder.sent[0]
    assert to == "a@example.com"
    assert "Reset" in subject
    assert "token=abc" in body
    assert "1 hour" in body


@pytest.mark.anyio
async def test_password_changed_notice_is_sent_with_a_warning_for_unexpected_changes(recorder):
    await notifications.notify_password_changed("a@example.com")
    body = recorder.sent[0][2]
    assert "wasn&#x27;t you" in body or "wasn't you" in body
    assert "https://sg.example.test/accounts/login" in body


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def test_limiter_blocks_after_the_limit_and_recovers_after_the_window():
    limiter = SlidingWindowLimiter(max_events=3, window_seconds=60)
    assert [limiter.allow("ip", now=t) for t in (0, 1, 2)] == [True, True, True]
    assert limiter.allow("ip", now=3) is False
    assert limiter.allow("ip", now=59) is False
    assert limiter.allow("ip", now=61) is True   # the event at t=0 has aged out


def test_limiter_tracks_keys_independently():
    limiter = SlidingWindowLimiter(max_events=1, window_seconds=60)
    assert limiter.allow("a", now=0) is True
    assert limiter.allow("b", now=0) is True
    assert limiter.allow("a", now=1) is False


def test_blocked_attempts_do_not_extend_the_block():
    limiter = SlidingWindowLimiter(max_events=1, window_seconds=60)
    assert limiter.allow("ip", now=0) is True
    for t in range(1, 50):
        assert limiter.allow("ip", now=t) is False
    assert limiter.allow("ip", now=61) is True