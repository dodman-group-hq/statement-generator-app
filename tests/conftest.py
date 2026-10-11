"""
Shared test setup. This file runs before any test module imports the app.

The environment is pinned here, BEFORE `main` is imported, for two reasons:

1. app/accounts.py refuses to start in production without SESSION_SECRET_KEY,
   so tests must run as development with a known secret.
2. main.py calls load_dotenv(), which would otherwise pull a real DATABASE_URL
   (e.g. your Neon string) from a local .env file and let the test suite
   connect to - and write to - a real database. load_dotenv() never overrides
   a variable that already exists, so setting these to empty strings here
   guarantees the tests run with no database and no error tracking.

Tests in this suite therefore cover everything that works WITHOUT a
database: pages, input validation, auth rejection, and pure logic. Anything
that needs Postgres (signup, login, job creation) belongs in a separate
integration suite pointed at a throwaway database.
"""
import os

os.environ["APP_ENV"] = "development"
os.environ["SESSION_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["DATABASE_URL"] = ""
os.environ["SENTRY_DSN"] = ""