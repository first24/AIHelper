"""Versioned, additive SQLite migrations. Apply inside one caller transaction."""
from .memory_v1 import STATEMENTS


def migrate(state):
    state.db.execute('PRAGMA foreign_keys=ON')
    with state.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS memory_migrations(version INTEGER PRIMARY KEY, applied_at REAL)')
        if not db.execute('SELECT 1 FROM memory_migrations WHERE version=1').fetchone():
            for statement in STATEMENTS:
                db.execute(statement)
            db.execute("INSERT INTO memory_migrations VALUES(1, unixepoch('now'))")
