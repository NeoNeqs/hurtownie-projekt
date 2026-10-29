from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

DEFAULT_DSN = "postgresql://warehouse:warehouse@localhost:55432/warehouse"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

ECOSYSTEMS = [
    ("npm", "https://www.npmjs.com", "https://registry.npmjs.org"),
    ("PyPI", "https://pypi.org", "https://pypi.org/simple/"),
    ("crates.io", "https://crates.io", "https://crates.io/api/v1"),
    ("RubyGems", "https://rubygems.org", "https://rubygems.org/api/v1"),
    ("NuGet", "https://www.nuget.org", "https://api.nuget.org/v3/index.json"),
    ("Packagist", "https://packagist.org", "https://repo.packagist.org"),
    ("Hex", "https://hex.pm", "https://hex.pm/api"),
]


def get_dsn() -> str | None:
    return os.getenv("WAREHOUSE_DSN")


def connect(dsn: str | None = None) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn or DEFAULT_DSN, row_factory=dict_row)


def ensure_database(dsn: str | None = None) -> None:
    import psycopg
    from psycopg import conninfo, sql

    params = conninfo.conninfo_to_dict(dsn or DEFAULT_DSN)
    database = params.get("dbname")
    if not database:
        raise ValueError("DSN must include a database name")

    admin = {**params, "dbname": "postgres"}
    with psycopg.connect(**admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )


def apply_migrations(conn: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()

    applied = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    newly_applied = []
    for path in sorted(directory.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
            )
        newly_applied.append(path.name)
    return newly_applied


def seed_ecosystems(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ecosystem (name, homepage_url, registry_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
            ECOSYSTEMS,
        )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warehouse database maintenance")
    parser.add_argument("command", choices=["init", "migrate", "seed"])
    parser.add_argument("--dsn", help="PostgreSQL DSN (defaults to WAREHOUSE_DSN)")
    args = parser.parse_args(argv)

    dsn = args.dsn or get_dsn() or DEFAULT_DSN

    if args.command == "init":
        ensure_database(dsn)

    with connect(dsn) as conn:
        if args.command in {"init", "migrate"}:
            applied = apply_migrations(conn)
            for name in applied:
                print(f"applied {name}")
        seed_ecosystems(conn)

    print(f"database ready: {dsn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
