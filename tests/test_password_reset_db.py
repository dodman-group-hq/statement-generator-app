"""
Database-backed password-reset tests. SKIPPED unless TEST_DATABASE_URL points at
a THROWAWAY Postgres database - these tests create users and wipe reset tokens.

    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/sg_test pytest tests/test_password_reset_db.py
"""
import os
import uuid

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set"),
    pytest.mark.anyio,
]


@pytest.fixture
async def db(monkeypatch):
    # conftest blanks DATABASE_URL so ordinary tests can never reach a real
    # database; this opts in explicitly, for this file only.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    from app import db as dbm
    await dbm.init_pool()
    await dbm.init_schema()
    yield dbm
    await dbm.close_pool()


@pytest.fixture
async def user_id(db):
    email = f"reset-{uuid.uuid4().hex[:8]}@example.com"
    return await db.create_user(email, "old-hash")


async def _issue(db, user_id):
    from app.accounts import generate_reset_token
    raw, hashed = generate_reset_token()
    assert await db.create_password_reset(user_id, hashed) is True
    return raw, hashed


async def test_a_valid_token_resets_the_password_exactly_once(db, user_id):
    _, hashed = await _issue(db, user_id)
    assert await db.password_reset_is_valid(hashed) is True

    assert await db.consume_password_reset(hashed, "new-hash") == user_id
    async with db.get_connection() as conn:
        assert await conn.fetchval("SELECT password_hash FROM users WHERE id = $1", user_id) == "new-hash"

    # second use of the same link must fail and must not touch the password
    assert await db.consume_password_reset(hashed, "attacker-hash") is None
    async with db.get_connection() as conn:
        assert await conn.fetchval("SELECT password_hash FROM users WHERE id = $1", user_id) == "new-hash"
    assert await db.password_reset_is_valid(hashed) is False


async def test_two_simultaneous_submissions_cannot_both_succeed(db, user_id):
    import asyncio
    _, hashed = await _issue(db, user_id)
    results = await asyncio.gather(
        db.consume_password_reset(hashed, "hash-a"),
        db.consume_password_reset(hashed, "hash-b"),
    )
    assert sorted(r is None for r in results) == [False, True]


async def test_expired_tokens_are_rejected(db, user_id):
    _, hashed = await _issue(db, user_id)
    async with db.get_connection() as conn:
        await conn.execute("UPDATE password_resets SET expires_at = now() - interval '1 minute' WHERE token_hash = $1", hashed)
    assert await db.password_reset_is_valid(hashed) is False
    assert await db.consume_password_reset(hashed, "new-hash") is None


async def test_unknown_tokens_are_rejected(db):
    assert await db.password_reset_is_valid("0" * 64) is False
    assert await db.consume_password_reset("0" * 64, "new-hash") is None


async def test_a_successful_reset_revokes_the_users_other_links(db, user_id):
    _, first = await _issue(db, user_id)
    _, second = await _issue(db, user_id)
    assert await db.consume_password_reset(first, "new-hash") == user_id
    assert await db.password_reset_is_valid(second) is False


async def test_requests_are_capped_per_account_per_hour(db, user_id):
    from app.accounts import generate_reset_token
    for _ in range(db.RESET_MAX_REQUESTS_PER_HOUR):
        assert await db.create_password_reset(user_id, generate_reset_token()[1]) is True
    assert await db.create_password_reset(user_id, generate_reset_token()[1]) is False


async def test_requesting_another_link_does_not_revoke_the_earlier_one(db, user_id):
    # Otherwise a stranger could kill your link by requesting a new one for your email.
    _, first = await _issue(db, user_id)
    await _issue(db, user_id)
    assert await db.password_reset_is_valid(first) is True