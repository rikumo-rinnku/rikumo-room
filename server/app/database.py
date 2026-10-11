import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('superadmin', 'admin', 'guest')),
    password_hash TEXT,
    created_at TEXT NOT NULL,
    pending_delete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    invite_code TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('guest', 'admin')),
    max_uses INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS banned_ids (
    guest_id TEXT PRIMARY KEY COLLATE NOCASE,
    livekit_identity TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_logs_created_at ON audit_logs(created_at DESC);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    db_path = Path(settings.database_path)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parents[1] / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        # Keep databases created by earlier development builds usable.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(banned_ids)")}
        if "livekit_identity" not in columns:
            connection.execute("ALTER TABLE banned_ids ADD COLUMN livekit_identity TEXT")
        invite_columns = {row["name"] for row in connection.execute("PRAGMA table_info(invites)")}
        if "invite_code" not in invite_columns:
            connection.execute("ALTER TABLE invites ADD COLUMN invite_code TEXT")
        audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_logs)")}
        for name, definition in {
            "action": "TEXT NOT NULL DEFAULT ''", "category": "TEXT NOT NULL DEFAULT ''",
            "target": "TEXT NOT NULL DEFAULT ''", "summary": "TEXT NOT NULL DEFAULT ''",
            "important": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in audit_columns:
                connection.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {definition}")
        from .audit import classify, fallback_target, summary
        for row in connection.execute("SELECT * FROM audit_logs WHERE action = ''").fetchall():
            event = classify(row["method"], row["path"], row["status_code"])
            target = fallback_target(row["path"])
            connection.execute(
                "UPDATE audit_logs SET action=?,category=?,target=?,summary=?,important=? WHERE id=?",
                (event["action"], event["category"], target, summary(event, target, row["status_code"]), event["important"], row["id"]),
            )
        connection.execute("CREATE INDEX IF NOT EXISTS audit_logs_filter ON audit_logs(important, action, id DESC)")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS banned_ids_livekit_identity "
            "ON banned_ids(livekit_identity) WHERE livekit_identity IS NOT NULL"
        )
