"""
Route tests against the real FastAPI app, using Starlette's TestClient.

These run with no database (see conftest.py): the lifespan logs a warning
that the database couldn't be initialised and carries on, exactly as it
would if Neon were unreachable at boot. So every test here is about
behaviour that must hold regardless of the database.

Run with:  pytest
Needs:     pip install pytest httpx
"""
import io

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="module")
def client():
    # Used as a context manager so the app's lifespan startup/shutdown runs.
    with TestClient(app) as c:
        yield c


def _fake_upload_files():
    return {
        "template_file": ("template.docx", io.BytesIO(b"not a real docx"), "application/octet-stream"),
        "data_file": ("data.xlsx", io.BytesIO(b"not a real xlsx"), "application/octet-stream"),
    }


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/",
    "/api/statement_generator",
    "/api/terms",
    "/api/privacy",
    "/accounts/login",
    "/accounts/signup",
])
def test_public_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Statement Generator" in response.text


def test_landing_page_links_to_signup_and_generator(client):
    html = client.get("/").text
    assert 'href="/accounts/signup"' in html
    assert 'href="/api/statement_generator"' in html


def test_legal_pages_use_real_contact_address(client):
    for path in ("/api/terms", "/api/privacy"):
        html = client.get(path).text
        assert "inquiries@dodman.group" in html
        assert "hello@dodmangroup.com" not in html


def test_unknown_path_returns_404_page(client):
    response = client.get("/this/page/does/not/exist")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]


def test_openapi_docs_are_served(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/process_statement" in schema["paths"]


# ---------------------------------------------------------------------------
# API authentication
# ---------------------------------------------------------------------------

def test_process_statement_requires_api_key(client):
    response = client.post("/api/process_statement", files=_fake_upload_files())
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["message"]


def test_progress_requires_api_key(client):
    assert client.get("/api/progress/some-id").status_code == 401


def test_download_requires_api_key(client):
    assert client.get("/api/download_statement/some-id").status_code == 401


def test_key_lookup_failure_is_a_503_not_a_500(client):
    # With no database available, validating a key fails. Callers should get
    # a clean, retryable 503 - not an unhandled 500.
    response = client.get("/api/progress/some-id", headers={"X-API-Key": "sg_live_anything"})
    assert response.status_code == 503
    assert "message" in response.json()


def test_error_responses_use_message_shape(client):
    # main.py rewrites FastAPI's default {"detail": ...} into {"message": ...}.
    body = client.get("/api/progress/some-id").json()
    assert "message" in body
    assert "detail" not in body


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def test_dashboard_redirects_to_login_without_session(client):
    response = client.get("/accounts/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/accounts/login"


def test_dashboard_rejects_a_forged_session_cookie(client):
    # A well-formed but wrongly-signed token must not be accepted.
    forged = "00000000-0000-0000-0000-000000000000:nonce:" + "0" * 64
    response = client.get(
        "/accounts/dashboard", cookies={"dodman_session": forged}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/accounts/login"


def test_signup_rejects_short_password_before_touching_the_database(client):
    response = client.post("/accounts/signup", data={"email": "a@example.com", "password": "short"})
    assert response.status_code == 400
    assert "8 characters" in response.json()["message"]


def test_create_key_requires_a_session(client):
    response = client.post("/accounts/dashboard/create-key", data={"label": "Production"})
    assert response.status_code == 401
    assert "message" in response.json()