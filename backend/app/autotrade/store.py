"""SQLite persistence for the autotrade worker.

Keys are stored as Fernet ciphertext blobs. Trades / runs / equity-curve
points are plain rows. The only mutation outside this module happens via
context-managed connections returned by ``connect()`` so we get atomic
commits and the file lock is released ASAP.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Literal


SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY,
    network     TEXT NOT NULL UNIQUE,
    api_key_enc BLOB NOT NULL,
    secret_enc  BLOB NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    running         INTEGER NOT NULL DEFAULT 0,
    network         TEXT    NOT NULL DEFAULT 'mainnet',
    instrument      TEXT    NOT NULL DEFAULT 'linear',
    leverage        REAL    NOT NULL DEFAULT 3.0,
    position_pct    REAL    NOT NULL DEFAULT 2.0,
    halted_reason   TEXT,
    halted_until    INTEGER,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        INTEGER NOT NULL,
    finished_at       INTEGER,
    coins_scanned     INTEGER DEFAULT 0,
    chosen_coin       TEXT,
    chosen_tf         TEXT,
    chosen_direction  TEXT,
    chosen_confidence INTEGER,
    chosen_rr         REAL,
    action            TEXT,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id),
    network       TEXT NOT NULL,
    instrument    TEXT NOT NULL,
    coin          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_price   REAL,
    stop_loss     REAL,
    tp1           REAL,
    tp2           REAL,
    qty           REAL,
    leverage      REAL,
    confidence    INTEGER,
    opened_at     INTEGER NOT NULL,
    closed_at     INTEGER,
    close_price   REAL,
    close_reason  TEXT,
    pnl_usdt      REAL,
    pnl_pct       REAL,
    bybit_order_id TEXT,
    breakeven_moved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equity (
    ts          INTEGER PRIMARY KEY,
    equity_usdt REAL NOT NULL,
    free_usdt   REAL,
    open_positions INTEGER DEFAULT 0,
    network     TEXT NOT NULL DEFAULT 'mainnet'
);
"""


_lock = threading.Lock()


def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        # Ensure singleton bot_state row.
        conn.execute(
            "INSERT OR IGNORE INTO bot_state(id, updated_at) VALUES (1, ?)",
            (int(time.time()),),
        )
        conn.commit()


@contextmanager
def connect(path: str) -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()


# ---- Key storage -----------------------------------------------------------


def upsert_keys(
    path: str,
    network: Literal["mainnet", "testnet"],
    api_key_enc: bytes,
    secret_enc: bytes,
) -> None:
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO keys(network, api_key_enc, secret_enc, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(network) DO UPDATE SET
                api_key_enc = excluded.api_key_enc,
                secret_enc  = excluded.secret_enc,
                created_at  = excluded.created_at
            """,
            (network, api_key_enc, secret_enc, int(time.time())),
        )


def get_keys(path: str, network: str) -> tuple[bytes, bytes] | None:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT api_key_enc, secret_enc FROM keys WHERE network=?",
            (network,),
        ).fetchone()
    if not row:
        return None
    return row["api_key_enc"], row["secret_enc"]


def delete_keys(path: str, network: str | None = None) -> int:
    with connect(path) as conn:
        if network is None:
            cur = conn.execute("DELETE FROM keys")
        else:
            cur = conn.execute("DELETE FROM keys WHERE network=?", (network,))
        return cur.rowcount


def list_networks(path: str) -> list[str]:
    with connect(path) as conn:
        rows = conn.execute("SELECT network FROM keys ORDER BY network").fetchall()
    return [r["network"] for r in rows]


# ---- Bot state -------------------------------------------------------------


def get_state(path: str) -> dict:
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM bot_state WHERE id=1").fetchone()
    if not row:
        return {}
    return dict(row)


def update_state(path: str, **fields: object) -> None:
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values())
    with connect(path) as conn:
        conn.execute(f"UPDATE bot_state SET {cols} WHERE id=1", values)


# ---- Runs / trades ---------------------------------------------------------


def insert_run(path: str, started_at: int) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO runs(started_at) VALUES (?)", (started_at,)
        )
        return int(cur.lastrowid or 0)


def finish_run(path: str, run_id: int, **fields: object) -> None:
    fields["finished_at"] = int(time.time())
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [run_id]
    with connect(path) as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE id=?", values)


def insert_trade(path: str, **fields: object) -> int:
    if "opened_at" not in fields:
        fields["opened_at"] = int(time.time())
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with connect(path) as conn:
        cur = conn.execute(
            f"INSERT INTO trades({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return int(cur.lastrowid or 0)


def update_trade(path: str, trade_id: int, **fields: object) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [trade_id]
    with connect(path) as conn:
        conn.execute(f"UPDATE trades SET {cols} WHERE id=?", values)


def open_trades(path: str) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY opened_at"
        ).fetchall()
    return [dict(r) for r in rows]


def trade_history(path: str, limit: int = 50) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def runs_history(path: str, limit: int = 30) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def realised_pnl(path: str, since_ts: int, network: str) -> float:
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usdt), 0.0) AS pnl
            FROM trades
            WHERE closed_at IS NOT NULL
              AND closed_at >= ?
              AND network = ?
            """,
            (since_ts, network),
        ).fetchone()
    return float(row["pnl"] or 0.0)


def insert_equity(path: str, equity_usdt: float, free_usdt: float, open_positions: int, network: str) -> None:
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO equity(ts, equity_usdt, free_usdt, open_positions, network)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(time.time()), equity_usdt, free_usdt, open_positions, network),
        )


def equity_curve(path: str, limit: int = 200) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, equity_usdt, free_usdt, open_positions, network "
            "FROM equity ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    rows = list(reversed(rows))
    return [dict(r) for r in rows]
