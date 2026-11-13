import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path("data/nameless.db")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet. Safe to run on every start."""
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS welcome_settings (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER,
                message    TEXT,
                enabled    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS study_guard (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER,
                mode       TEXT NOT NULL DEFAULT 'warn',
                enabled    INTEGER NOT NULL DEFAULT 0
            )
            """
        )
