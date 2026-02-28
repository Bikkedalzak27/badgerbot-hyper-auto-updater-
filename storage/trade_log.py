import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "hyperbot.db"

_CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    coin        TEXT NOT NULL,
    side        TEXT NOT NULL,
    size        REAL NOT NULL,
    entry_px    REAL NOT NULL,
    tp_px       REAL NOT NULL,
    sl_px       REAL NOT NULL,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    pnl         REAL,
    status      TEXT NOT NULL DEFAULT 'OPEN'
)
"""


def _init_schema() -> None:
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(_CREATE_TRADES_TABLE)
        connection.commit()
    finally:
        connection.close()


async def init_trade_log() -> None:
    await asyncio.to_thread(_init_schema)


def _insert_trade(
    coin: str, side: str, size: float, entry_px: float, tp_px: float, sl_px: float
) -> int:
    opened_at = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(DB_PATH))
    try:
        cursor = connection.execute(
            "INSERT INTO trades (coin, side, size, entry_px, tp_px, sl_px, opened_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')",
            (coin, side, size, entry_px, tp_px, sl_px, opened_at),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


async def insert_trade(
    coin: str, side: str, size: float, entry_px: float, tp_px: float, sl_px: float
) -> int:
    return await asyncio.to_thread(_insert_trade, coin, side, size, entry_px, tp_px, sl_px)


def _update_trade_status(trade_id: int, status: str) -> None:
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute("UPDATE trades SET status = ? WHERE id = ?", (status, trade_id))
        connection.commit()
    finally:
        connection.close()


async def update_trade_status(trade_id: int, status: str) -> None:
    await asyncio.to_thread(_update_trade_status, trade_id, status)


def _close_trade(trade_id: int, pnl: float, status: str) -> None:
    closed_at = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(
            "UPDATE trades SET closed_at = ?, pnl = ?, status = ? WHERE id = ?",
            (closed_at, pnl, status, trade_id),
        )
        connection.commit()
    finally:
        connection.close()


async def close_trade(trade_id: int, pnl: float, status: str) -> None:
    await asyncio.to_thread(_close_trade, trade_id, pnl, status)


def _fetch_open_trades() -> list[dict]:
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(
            "SELECT * FROM trades WHERE status IN ('OPEN', 'UNPROTECTED')"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


async def fetch_open_trades() -> list[dict]:
    return await asyncio.to_thread(_fetch_open_trades)


def _fetch_recent_closed_trades(limit: int) -> list[dict]:
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(
            "SELECT * FROM trades WHERE status NOT IN ('OPEN', 'UNPROTECTED')"
            " ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


async def fetch_recent_closed_trades(limit: int = 10) -> list[dict]:
    return await asyncio.to_thread(_fetch_recent_closed_trades, limit)
