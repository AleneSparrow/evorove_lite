import sqlite3
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

current_run_id = ContextVar("current_run_id", default=None)

ROOT = Path(__file__).resolve().parent.parent


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def migrate(path):
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        for file in sorted((ROOT / "migrations").glob("*.sql")):
            if not db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (file.name,)).fetchone():
                # executescript controls its own transaction, so include the ledger atomically.
                sql = file.read_text()
                db.executescript("BEGIN IMMEDIATE;\n" + sql +
                                 "\nINSERT INTO schema_migrations VALUES ('" + file.name + "'); COMMIT;")
