"""
Database access for API clients (who's allowed to call this service) and jobs
(one row per generation run, used for usage limits and audit history).

Uses a real asyncpg connection pool, created once at app startup (see
main.py's lifespan) and closed at shutdown - same pattern Vett uses, so the
whole product suite has one consistent operational story rather than each
product inventing its own. The pool handles reconnecting after Neon's
autosuspend; a failed query on a stale connection surfaces as a normal
retryable error rather than something this module needs to special-case.
"""
import os
import hashlib
import secrets
import logging
from contextlib import asynccontextmanager

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


# ---------------------------------------------------------------------------
# Plan defaults - the single source of truth for what a plan is allowed to do.
# Used by BOTH the CLI (scripts/create_api_key.py) and self-serve signup
# (app/routes/accounts.py), so the two can never drift apart again.
#
#   free  -> 5 jobs/month, 25 rows/job, password protection OFF
#   other -> unlimited jobs and rows, password protection ON
#
# These are starting points: callers can still override any single value.
# ---------------------------------------------------------------------------
FREE_PLAN_DEFAULTS = {
    "monthly_job_limit": 5,
    "max_rows_per_job": 25,
    "password_protection_allowed": False,
}
OTHER_PLAN_DEFAULTS = {
    "monthly_job_limit": None,
    "max_rows_per_job": None,
    "password_protection_allowed": True,
}


def plan_defaults(plan: str) -> dict:
    """Returns a fresh copy, so callers can't accidentally mutate the shared defaults."""
    return dict(FREE_PLAN_DEFAULTS if plan == "free" else OTHER_PLAN_DEFAULTS)


def _dsn() -> str:
    dsn = os.getenv('DATABASE_URL')
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy your Neon connection string "
            "(Neon dashboard -> Connection Details) into the DATABASE_URL env var."
        )
    return dsn


async def init_pool() -> asyncpg.Pool:
    """Call once, from the FastAPI lifespan startup. Idempotent - safe to
    call again (returns the existing pool) if something calls it twice."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_dsn(), min_size=2, max_size=10, command_timeout=15)
        logger.info("Database pool created.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed.")


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized - init_pool() must run at app startup first.")
    return _pool


@asynccontextmanager
async def get_connection(client_id: int | None = None):
    """When client_id is given, the connection is pinned to that tenant for
    its whole transaction (via a session-local Postgres setting), and Row-
    Level Security on statement_jobs enforces it - so even a future query
    against that table that forgets its own WHERE client_id = ... clause
    still can't read or write another tenant's rows. This is enforced by
    Postgres itself, not by this function remembering to filter correctly.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if client_id is not None:
                # set_config (not a raw `SET ... = $1`) is what accepts a
                # normal parameterized value here, so this can never become
                # a SQL-injection vector even though client_id ultimately
                # traces back to a request. Must run *inside* the
                # transaction (is_local=true scopes to "rest of the current
                # transaction" - it has no effect if set before one starts).
                await conn.execute("SELECT set_config('app.current_client_id', $1, true)", str(client_id))
            yield conn


async def _ensure_jobs_rls(conn: asyncpg.Connection) -> None:
    """Row-Level Security on statement_jobs, scoped to the session's
    app.current_client_id. FORCE makes it apply even to the role that owns
    the table (Postgres exempts table owners from RLS by default, and the
    role in your Neon connection string is typically the owner since it's
    the one that ran CREATE TABLE) - without FORCE, this app's own normal
    connection could silently bypass the policy it just created.
    """
    await conn.execute("ALTER TABLE statement_jobs ENABLE ROW LEVEL SECURITY;")
    await conn.execute("ALTER TABLE statement_jobs FORCE ROW LEVEL SECURITY;")
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                WHERE tablename = 'statement_jobs' AND policyname = 'tenant_isolation'
            ) THEN
                CREATE POLICY tenant_isolation ON statement_jobs
                    USING (client_id = current_setting('app.current_client_id', true)::integer)
                    WITH CHECK (client_id = current_setting('app.current_client_id', true)::integer);
            END IF;
        END
        $$;
    """)


async def init_schema() -> None:
    """Idempotent - safe to call on every app startup."""
    async with get_connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS statement_api_clients (
                id SERIAL PRIMARY KEY,
                label TEXT UNIQUE NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                monthly_job_limit INTEGER,     -- NULL = unlimited
                max_rows_per_job INTEGER,       -- NULL = unlimited
                password_protection_allowed BOOLEAN NOT NULL DEFAULT FALSE,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        await conn.execute("ALTER TABLE statement_api_clients ADD COLUMN IF NOT EXISTS max_rows_per_job INTEGER;")
        await conn.execute("ALTER TABLE statement_api_clients ADD COLUMN IF NOT EXISTS password_protection_allowed BOOLEAN NOT NULL DEFAULT FALSE;")
        # References the SAME users table Vett created (this app shares its
        # database, per the "one users table across products" decision) -
        # CREATE TABLE IF NOT EXISTS is a no-op there since it already
        # exists with these exact columns; it only actually creates
        # anything if this schema is ever bootstrapped against a fresh,
        # empty database in isolation (e.g. local dev).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email VARCHAR(320) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        # Nullable: existing CLI-issued keys (scripts/create_api_key.py)
        # have no associated login account and keep working exactly as
        # before via the X-API-Key header alone. Only self-serve signups
        # (see app/routes/accounts.py) set this.
        await conn.execute("ALTER TABLE statement_api_clients ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_statement_api_clients_user ON statement_api_clients(user_id);")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS statement_jobs (
                temp_id UUID PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES statement_api_clients(id),
                status TEXT NOT NULL DEFAULT 'processing',
                row_count INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at TIMESTAMPTZ
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_client_created ON statement_jobs(client_id, created_at);")
        await _ensure_jobs_rls(conn)
    logger.info("Database schema is up to date.")


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


async def get_client_by_key(raw_key: str) -> dict | None:
    """Returns a dict {id, label, plan, monthly_job_limit, max_rows_per_job,
    password_protection_allowed} or None.

    Not RLS-scoped: this is the auth bootstrap itself, run before we know
    who's calling. It only ever matches the one row whose key_hash equals
    the hash of the exact key supplied, so it can't be used to read or
    enumerate any other client's data.
    """
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, label, plan, monthly_job_limit, max_rows_per_job, password_protection_allowed "
            "FROM statement_api_clients WHERE key_hash = $1 AND is_active = TRUE",
            hash_key(raw_key),
        )
        return dict(row) if row else None


async def count_jobs_this_month(client_id: int) -> int:
    async with get_connection(client_id=client_id) as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM statement_jobs "
            "WHERE client_id = $1 AND created_at >= date_trunc('month', now())",
            client_id,
        )


async def create_job(temp_id: str, client_id: int, row_count: int | None = None) -> None:
    async with get_connection(client_id=client_id) as conn:
        await conn.execute(
            "INSERT INTO statement_jobs (temp_id, client_id, status, row_count) VALUES ($1, $2, 'processing', $3)",
            temp_id, client_id, row_count,
        )


async def update_job_status(temp_id: str, status: str, client_id: int) -> None:
    async with get_connection(client_id=client_id) as conn:
        if status in ('completed', 'error'):
            await conn.execute(
                "UPDATE statement_jobs SET status = $1, completed_at = now() WHERE temp_id = $2",
                status, temp_id,
            )
        else:
            await conn.execute("UPDATE statement_jobs SET status = $1 WHERE temp_id = $2", status, temp_id)


async def create_api_client(label: str, plan: str = 'free', monthly_job_limit: int | None = None,
                             max_rows_per_job: int | None = None,
                             password_protection_allowed: bool = False,
                             user_id: str | None = None) -> str:
    """Creates a new client and returns the RAW api key.

    NOTE: this function stores exactly the limits it is given - it does NOT
    apply plan defaults on its own (None means "unlimited"). Callers should
    unpack plan_defaults(plan) into it, as both scripts/create_api_key.py
    and the self-serve dashboard do.

    The raw key is only ever returned here, at creation time - only its hash
    is stored. If it's lost, the only fix is issuing a new key.

    user_id links this key to a login account (self-serve signup) - omit it
    (as scripts/create_api_key.py does) for a CLI-issued key with no
    associated account, which keeps working via X-API-Key exactly as before.
    """
    raw_key = f"sg_live_{secrets.token_urlsafe(32)}"
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO statement_api_clients "
            "(label, key_hash, plan, monthly_job_limit, max_rows_per_job, password_protection_allowed, user_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            label, hash_key(raw_key), plan, monthly_job_limit, max_rows_per_job, password_protection_allowed, user_id,
        )
    return raw_key


# ---------------------------------------------------------------------------
# User accounts - authenticate against the SAME `users` table Vett uses.
# ---------------------------------------------------------------------------

async def create_user(email: str, password_hash: str) -> str:
    """Returns the new user's id. Raises asyncpg.UniqueViolationError on a
    duplicate email - callers should catch that specifically."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id",
            email.lower().strip(), password_hash,
        )
        return str(row["id"])


async def get_user_by_email(email: str) -> dict | None:
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, password_hash FROM users WHERE email = $1",
            email.lower().strip(),
        )
        return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict | None:
    async with get_connection() as conn:
        row = await conn.fetchrow("SELECT id, email FROM users WHERE id = $1", user_id)
        return dict(row) if row else None


async def list_api_clients_for_user(user_id: str) -> list[dict]:
    """Never returns key_hash or the raw key - only what's safe to show on
    a dashboard. The raw key is shown exactly once, at creation time."""
    async with get_connection() as conn:
        rows = await conn.fetch(
            "SELECT id, label, plan, monthly_job_limit, max_rows_per_job, "
            "password_protection_allowed, is_active, created_at "
            "FROM statement_api_clients WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        return [dict(r) for r in rows]