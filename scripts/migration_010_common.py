from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable

import asyncpg


def database_url_from_args(description: str) -> str:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL DSN; defaults to DATABASE_URL",
    )
    args = parser.parse_args()
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL or --database-url is required")
    return normalize_database_url(database_url)


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace(
            "postgresql+asyncpg://",
            "postgresql://",
            1,
        )
    return database_url


async def relation_exists(conn: asyncpg.Connection, name: str) -> bool:
    return bool(await conn.fetchval("select to_regclass($1) is not null", name))


async def column_exists(
    conn: asyncpg.Connection,
    table: str,
    column: str,
) -> bool:
    return bool(
        await conn.fetchval(
            """
            select exists (
              select 1
              from information_schema.columns
              where table_schema = current_schema()
                and table_name = $1
                and column_name = $2
            )
            """,
            table,
            column,
        )
    )


def run_read_only(
    description: str,
    audit: Callable[[asyncpg.Connection], Awaitable[int]],
) -> None:
    database_url = database_url_from_args(description)

    async def execute() -> int:
        conn = await asyncpg.connect(database_url)
        try:
            async with conn.transaction(readonly=True):
                return await audit(conn)
        finally:
            await conn.close()

    try:
        exit_code = asyncio.run(execute())
    except Exception as exc:
        print(f"BLOCKED: database audit failed ({type(exc).__name__}: {exc})")
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)
