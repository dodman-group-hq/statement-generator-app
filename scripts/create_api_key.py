"""
Issue a new Statement Generator API key.

The raw key is only ever printed here, once - only its hash is stored in the
database, so if it's lost the only fix is issuing a new one for that client.

Plan defaults (used whenever the matching flag is omitted) live in
app/db.py (plan_defaults) so this script and self-serve signup always agree:
    free  -> monthly_job_limit=5,   max_rows_per_job=25,  password protection OFF
    other -> monthly_job_limit=None (unlimited), max_rows_per_job=None (unlimited), password protection ON

These are starting points, not fixed rules - every flag can be overridden
per client, e.g. a generous free trial or a capped paid tier.

Usage:
    python scripts/create_api_key.py "Pilot Client" --plan free
    python scripts/create_api_key.py "Acme Corp" --plan paid
    python scripts/create_api_key.py "Acme Corp" --plan paid --limit 500 --max-rows 1000
    python scripts/create_api_key.py "Internal - Dodman" --plan internal

Requires DATABASE_URL to be set (same Neon connection string as the app uses).
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.db import init_pool, close_pool, init_schema, create_api_client, plan_defaults  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description="Issue a new Statement Generator API key.")
    parser.add_argument("label", help="Human-readable name for this client, e.g. 'Acme Corp'")
    parser.add_argument("--plan", default="free", help="Plan name, e.g. free / paid / internal (default: free)")
    parser.add_argument("--limit", type=int, default=None, help="Monthly job limit. Omit to use the plan default.")
    parser.add_argument("--max-rows", type=int, default=None, help="Max spreadsheet rows per job. Omit to use the plan default.")

    protection_group = parser.add_mutually_exclusive_group()
    protection_group.add_argument("--allow-password-protection", dest="password_protection", action="store_true", default=None,
                                   help="Force-enable password protection regardless of plan default.")
    protection_group.add_argument("--no-password-protection", dest="password_protection", action="store_false",
                                   help="Force-disable password protection regardless of plan default.")

    args = parser.parse_args()

    defaults = plan_defaults(args.plan)
    monthly_job_limit = args.limit if args.limit is not None else defaults["monthly_job_limit"]
    max_rows_per_job = args.max_rows if args.max_rows is not None else defaults["max_rows_per_job"]
    password_protection_allowed = (
        args.password_protection if args.password_protection is not None else defaults["password_protection_allowed"]
    )

    await init_pool()
    try:
        await init_schema()
        raw_key = await create_api_client(
            args.label,
            plan=args.plan,
            monthly_job_limit=monthly_job_limit,
            max_rows_per_job=max_rows_per_job,
            password_protection_allowed=password_protection_allowed,
        )
    finally:
        await close_pool()

    print(f"\nCreated API client '{args.label}' (plan: {args.plan})")
    print(f"  Monthly job limit:    {monthly_job_limit if monthly_job_limit is not None else 'unlimited'}")
    print(f"  Max rows per job:     {max_rows_per_job if max_rows_per_job is not None else 'unlimited'}")
    print(f"  Password protection:  {'allowed' if password_protection_allowed else 'not allowed'}")
    print(f"\nAPI key (save this now - it will not be shown again):\n\n  {raw_key}\n")


if __name__ == "__main__":
    asyncio.run(main())