"""
ABURide – SQLite database layer.

Provides connection management, schema creation, seed data,
and transactional context managers for safe concurrent access.
"""

from __future__ import annotations

import contextlib
import itertools
import sqlite3
import threading
from typing import Generator

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_db_path: str | None = None
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def configure(path: str) -> None:
    """Set the path to the SQLite database file (or `:memory:`)."""
    global _db_path
    _db_path = path


def _get_path() -> str:
    if _db_path is None:
        raise RuntimeError("Database not configured – call db.configure(path) first")
    return _db_path


def _connect() -> sqlite3.Connection:
    """Return a new connection with standard pragmas applied."""
    conn = sqlite3.connect(_get_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Transactional context managers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def tx() -> Generator[sqlite3.Cursor, None, None]:
    """
    Exclusive write transaction.

    Uses ``BEGIN EXCLUSIVE`` so only one writer can proceed at a time –
    SQLite's equivalent of row-level locking.  Commits on clean exit,
    rolls back on any exception.
    """
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN EXCLUSIVE")
        yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def read() -> Generator[sqlite3.Cursor, None, None]:
    """Read-only (implicit) transaction."""
    conn = _connect()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema & seed data
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    name        TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('student', 'driver', 'admin')),
    balance     INTEGER NOT NULL DEFAULT 0,
    token       TEXT    UNIQUE,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    UNIQUE NOT NULL,
    lat        REAL,
    lon        REAL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vehicles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    capacity  INTEGER NOT NULL CHECK (capacity > 0),
    plate     TEXT,
    vehicle_type TEXT NOT NULL DEFAULT 'bus' CHECK (vehicle_type IN ('bus', 'keke'))
);

CREATE TABLE IF NOT EXISTS fares (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_id INTEGER NOT NULL REFERENCES stops(id),
    dest_id   INTEGER NOT NULL REFERENCES stops(id),
    amount    INTEGER NOT NULL,
    UNIQUE (origin_id, dest_id)
);

CREATE TABLE IF NOT EXISTS holds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id   INTEGER NOT NULL REFERENCES users(id),
    driver_id    INTEGER REFERENCES users(id),
    origin_id    INTEGER NOT NULL REFERENCES stops(id),
    dest_id      INTEGER NOT NULL REFERENCES stops(id),
    status       TEXT    NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN (
                             'PENDING','CLAIMED','BOARDED',
                             'COMPLETED','EXPIRED','CANCELLED'
                         )),
    pin          TEXT,
    fare         INTEGER NOT NULL,
    created_at   REAL    NOT NULL,
    expires_at   REAL    NOT NULL,
    claimed_at   REAL,
    boarded_at   REAL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS driver_locations (
    driver_id  INTEGER PRIMARY KEY REFERENCES users(id),
    stop_id    INTEGER REFERENCES stops(id),
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS topups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    amount       INTEGER NOT NULL CHECK (amount > 0),
    reference    TEXT    UNIQUE NOT NULL,
    provider     TEXT    NOT NULL DEFAULT 'paystack',
    status       TEXT    NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING', 'SUCCESS', 'FAILED')),
    created_at   REAL    NOT NULL,
    completed_at REAL
);
"""

# Approximate ABU Zaria coordinates
_STOPS = [
    ("North Gate", 11.1620, 7.6340, 0),
    ("Main Gate",  11.1510, 7.6490, 1),
    ("Complex",    11.1470, 7.6540, 2),
    ("Ribadu",     11.1430, 7.6600, 3),
    ("ICS",        11.1400, 7.6650, 4),
]

_DEFAULT_FARE = 15000  # N150.00 in cents

_SEED_DRIVERS = [
    # (token, name, capacity, vehicle_type, plate)
    ("driver-a-token", "Driver Abdullahi", 4, "keke", "ABU-K-101"),
    ("driver-b-token", "Driver Bello",     14, "bus",  "ABU-B-204"),
]

_ADMIN_TOKEN = "admin-secret-token"


def init() -> None:
    """Create tables and seed data (idempotent)."""
    import time

    conn = _connect()
    cur = conn.cursor()
    try:
        cur.executescript(_SCHEMA)

        # Migration: ensure users table role CHECK constraint includes 'admin'
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        user_table_row = cur.fetchone()
        if user_table_row and user_table_row["sql"] and "'admin'" not in user_table_row["sql"]:
            cur.execute("PRAGMA foreign_keys = OFF")
            cur.execute("""
                CREATE TABLE users_migrated (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    name        TEXT    NOT NULL,
                    role        TEXT    NOT NULL CHECK (role IN ('student', 'driver', 'admin')),
                    balance     INTEGER NOT NULL DEFAULT 0,
                    token       TEXT    UNIQUE,
                    is_active   INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL    NOT NULL
                )
            """)
            cur.execute("PRAGMA table_info(users)")
            existing_cols = {r["name"] for r in cur.fetchall()}
            is_active_expr = "is_active" if "is_active" in existing_cols else "1"
            cur.execute(f"""
                INSERT INTO users_migrated (id, telegram_id, name, role, balance, token, is_active, created_at)
                SELECT id, telegram_id, name, role, balance, token, {is_active_expr}, created_at
                FROM users
            """)
            cur.execute("DROP TABLE users")
            cur.execute("ALTER TABLE users_migrated RENAME TO users")
            cur.execute("PRAGMA foreign_keys = ON")

        # Migration: ensure is_active column on users if table already existed
        try:
            cur.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass

        # Migration: ensure plate column on vehicles if table already existed
        try:
            cur.execute("ALTER TABLE vehicles ADD COLUMN plate TEXT")
        except sqlite3.OperationalError:
            pass

        # Migration: ensure vehicle_type column on vehicles if table already existed
        try:
            cur.execute("ALTER TABLE vehicles ADD COLUMN vehicle_type TEXT NOT NULL DEFAULT 'bus'")
        except sqlite3.OperationalError:
            pass

        # Seed stops
        for name, lat, lon, order in _STOPS:
            cur.execute(
                "INSERT OR IGNORE INTO stops (name, lat, lon, sort_order) "
                "VALUES (?, ?, ?, ?)",
                (name, lat, lon, order),
            )

        # Seed fares between all distinct pairs
        cur.execute("SELECT id FROM stops")
        stop_ids = [r["id"] for r in cur.fetchall()]
        for oid, did in itertools.permutations(stop_ids, 2):
            cur.execute(
                "INSERT OR IGNORE INTO fares (origin_id, dest_id, amount) "
                "VALUES (?, ?, ?)",
                (oid, did, _DEFAULT_FARE),
            )

        now = time.time()

        # Seed admin user
        cur.execute("SELECT id FROM users WHERE token = ?", (_ADMIN_TOKEN,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO users (telegram_id, name, role, balance, token, is_active, created_at) "
                "VALUES (NULL, 'Campus Transport Admin', 'admin', 0, ?, 1, ?)",
                (_ADMIN_TOKEN, now),
            )

        # Seed test drivers + vehicles
        for token, name, capacity, vtype, plate in _SEED_DRIVERS:
            cur.execute("SELECT id FROM users WHERE token = ?", (token,))
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO users (telegram_id, name, role, balance, token, is_active, created_at) "
                    "VALUES (NULL, ?, 'driver', 0, ?, 1, ?)",
                    (name, token, now),
                )
                driver_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO vehicles (driver_id, capacity, vehicle_type, plate) VALUES (?, ?, ?, ?)",
                    (driver_id, capacity, vtype, plate),
                )
            else:
                driver_id = existing["id"]
                # Update vehicle info if present
                cur.execute(
                    "UPDATE vehicles SET vehicle_type = ?, plate = COALESCE(plate, ?) WHERE driver_id = ?",
                    (vtype, plate, driver_id),
                )

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Convenience look-ups
# ---------------------------------------------------------------------------


def stop_id(name: str) -> int:
    """Return the ID of a named stop."""
    with read() as cur:
        cur.execute("SELECT id FROM stops WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Unknown stop: {name!r}")
        return row["id"]
