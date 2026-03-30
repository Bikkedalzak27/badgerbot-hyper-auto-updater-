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
    status      TEXT NOT NULL DEFAULT 'OPEN',
    entry_fee   REAL,
    close_fee   REAL
)
"""

_TAKER_FEE_RATE = 0.00025  # 0.025% standard HL taker fee


def _init_schema() -> None:
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(_CREATE_TRADES_TABLE)
        for col in ("entry_fee", "close_fee"):
            try:
                connection.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass  # column already exists
        _backfill_fees(connection)
        connection.commit()
    finally:
        connection.close()


def _backfill_fees(connection: sqlite3.Connection) -> None:
    """Estimate fees for existing trades that have NULL fee columns."""
    connection.execute(
        "UPDATE trades SET entry_fee = size * entry_px * ? "
        "WHERE entry_fee IS NULL AND status NOT IN ('OPEN', 'UNPROTECTED')",
        (_TAKER_FEE_RATE,),
    )
    connection.execute(
        "UPDATE trades SET close_fee = size * "
        "CASE WHEN status = 'TP' THEN tp_px "
        "     WHEN status = 'SL' THEN sl_px END * ? "
        "WHERE close_fee IS NULL AND status IN ('TP', 'SL')",
        (_TAKER_FEE_RATE,),
    )


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
    return await asyncio.to_thread(
        _insert_trade, coin, side, size, entry_px, tp_px, sl_px
    )


def _update_trade_status(trade_id: int, status: str) -> None:
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(
            "UPDATE trades SET status = ? WHERE id = ?", (status, trade_id)
        )
        connection.commit()
    finally:
        connection.close()


async def update_trade_status(trade_id: int, status: str) -> None:
    await asyncio.to_thread(_update_trade_status, trade_id, status)


def _repair_trade_tpsl(trade_id: int, tp_px: float, sl_px: float) -> None:
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(
            "UPDATE trades SET tp_px = ?, sl_px = ?, status = 'OPEN' WHERE id = ?",
            (tp_px, sl_px, trade_id),
        )
        connection.commit()
    finally:
        connection.close()


async def repair_trade_tpsl(trade_id: int, tp_px: float, sl_px: float) -> None:
    await asyncio.to_thread(_repair_trade_tpsl, trade_id, tp_px, sl_px)


def _close_trade(
    trade_id: int, pnl: float, status: str, close_fee: float | None = None
) -> None:
    closed_at = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(
            "UPDATE trades SET closed_at = ?, pnl = ?, status = ?, close_fee = ? WHERE id = ?",
            (closed_at, pnl, status, close_fee, trade_id),
        )
        connection.commit()
    finally:
        connection.close()


async def close_trade(
    trade_id: int, pnl: float, status: str, close_fee: float | None = None
) -> None:
    await asyncio.to_thread(_close_trade, trade_id, pnl, status, close_fee)


def _update_entry_fee(trade_id: int, entry_fee: float) -> None:
    connection = sqlite3.connect(str(DB_PATH))
    try:
        connection.execute(
            "UPDATE trades SET entry_fee = ? WHERE id = ?", (entry_fee, trade_id)
        )
        connection.commit()
    finally:
        connection.close()


async def update_entry_fee(trade_id: int, entry_fee: float) -> None:
    await asyncio.to_thread(_update_entry_fee, trade_id, entry_fee)


def net_pnl(trade: dict) -> float | None:
    """Compute net PnL: gross pnl minus entry and close fees."""
    pnl = trade.get("pnl")
    if pnl is None:
        return None
    return pnl - (trade.get("entry_fee") or 0) - (trade.get("close_fee") or 0)


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


def _fetch_closed_trades_since(since_iso: str | None) -> list[dict]:
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    try:
        if since_iso:
            cursor = connection.execute(
                "SELECT * FROM trades WHERE status NOT IN ('OPEN', 'UNPROTECTED')"
                " AND closed_at >= ? ORDER BY closed_at DESC",
                (since_iso,),
            )
        else:
            cursor = connection.execute(
                "SELECT * FROM trades WHERE status NOT IN ('OPEN', 'UNPROTECTED')"
                " ORDER BY closed_at DESC"
            )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


async def fetch_closed_trades_since(since_iso: str | None = None) -> list[dict]:
    return await asyncio.to_thread(_fetch_closed_trades_since, since_iso)
