import asyncio
import logging
from datetime import datetime, timezone

from storage.trade_log import close_trade, fetch_open_trades

logger = logging.getLogger("PositionMonitor")

_alerted_orphan_coins: set[str] = set()


def _extract_open_positions(user_state: dict) -> dict[str, float]:
    """Returns {coin: abs_size} for all coins with a non-zero open position."""
    positions = {}
    for asset_position in user_state.get("assetPositions", []):
        position = asset_position.get("position", {})
        coin = position.get("coin", "")
        size = float(position.get("szi", "0"))
        if coin and abs(size) > 0:
            positions[coin] = abs(size)
    return positions


def _get_close_fills(fills: list, coin: str, side: str, since_ms: float) -> list:
    """Returns close fills for a coin after since_ms, sorted oldest-first."""
    expected_dir = "Close Long" if side == "LONG" else "Close Short"
    matching = [
        f for f in fills
        if f.get("coin") == coin
        and f.get("time", 0) > since_ms
        and expected_dir in f.get("dir", "")
    ]
    return sorted(matching, key=lambda f: f["time"])


def _find_matching_trade(open_trades: list[dict], fill_px: float) -> dict | None:
    """Returns the trade whose TP or SL price is closest to the fill price."""
    best_trade = None
    best_distance = float("inf")
    for trade in open_trades:
        distance = min(
            abs(float(trade["tp_px"]) - fill_px),
            abs(float(trade["sl_px"]) - fill_px),
        )
        if distance < best_distance:
            best_distance = distance
            best_trade = trade
    return best_trade


def _determine_close_status(trade: dict, close_px: float) -> str:
    tp_px = float(trade["tp_px"])
    sl_px = float(trade["sl_px"])
    midpoint = (tp_px + sl_px) / 2

    # Trigger orders fill at market after the trigger price is reached, so the actual
    # fill can land slightly beyond the trigger (e.g. a LONG TP sell that triggers at
    # $2059 may fill at $2056). Strict threshold comparison mislabels these as MANUAL.
    # Use the TP/SL midpoint as the dividing line instead.
    if trade["side"] == "LONG":
        return "TP" if close_px >= midpoint else "SL"
    else:
        return "TP" if close_px <= midpoint else "SL"


def _format_close_notification(trade: dict, close_px: float, pnl: float, status: str) -> str:
    entry_px = float(trade["entry_px"])
    size = float(trade["size"])
    side = trade["side"]
    direction_emoji = "🟢" if side == "LONG" else "🔴"
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_pct = (pnl / (entry_px * size)) * 100 if entry_px > 0 and size > 0 else 0
    pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"

    return (
        f"{direction_emoji} {trade['coin']} {side} CLOSED — {status} @ ${close_px:,.2f}\n\n"
        f"📐 Size: {size} (${size * close_px:,.2f})\n"
        f"💵 Entry: ${entry_px:,.2f} → Exit: ${close_px:,.2f}\n"
        f"📈 PnL: {pnl_sign}${pnl:,.2f} ({pnl_pct_str})"
    )


async def _process_coin_closures(
    coin: str, db_trades: list[dict], fills: list, notify
) -> None:
    """Match close fills to DB trades and close each one individually."""
    side = db_trades[0]["side"]
    since_ms = min(
        datetime.fromisoformat(t["opened_at"]).replace(tzinfo=timezone.utc).timestamp() * 1000
        for t in db_trades
    )

    close_fills = _get_close_fills(fills, coin, side, since_ms)

    if not close_fills:
        logger.warning(f"{coin}: closure detected but no matching fills in user_fills — will retry next cycle")
        return

    pending_trades = list(db_trades)

    for fill in close_fills:
        if not pending_trades:
            break

        fill_px = float(fill["px"])
        pnl = float(fill.get("closedPnl", 0))

        trade = _find_matching_trade(pending_trades, fill_px)
        if trade is None:
            break

        status = _determine_close_status(trade, fill_px)
        await close_trade(trade["id"], pnl, status)
        pending_trades.remove(trade)

        logger.info(
            f"{coin} {side} closed | status={status}"
            f" | entry={trade['entry_px']} exit={fill_px} pnl={pnl:+.2f}"
        )
        await notify(_format_close_notification(trade, fill_px, pnl, status))


async def _check_closed_positions(info, settings, notify) -> None:
    open_trades = await fetch_open_trades()
    if not open_trades:
        return

    user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    hl_positions = _extract_open_positions(user_state)

    trades_by_coin: dict[str, list[dict]] = {}
    for trade in open_trades:
        trades_by_coin.setdefault(trade["coin"], []).append(trade)

    for coin, db_trades in trades_by_coin.items():
        total_db_size = sum(float(t["size"]) for t in db_trades)
        current_hl_size = hl_positions.get(coin, 0.0)
        closed_amount = total_db_size - current_hl_size

        if closed_amount < 1e-6:
            continue

        logger.info(
            f"Closure detected: {coin} | db_total={total_db_size:.6f}"
            f" hl_size={current_hl_size:.6f} closed={closed_amount:.6f}"
        )
        fills = await asyncio.to_thread(info.user_fills, settings.hl_account_address)
        await _process_coin_closures(coin, db_trades, fills, notify)

    for coin, hl_size in hl_positions.items():
        if coin not in trades_by_coin and coin not in _alerted_orphan_coins:
            _alerted_orphan_coins.add(coin)
            msg = f"Untracked position: {coin} size={hl_size} — no DB record. Close manually or wait for next signal."
            logger.warning(msg)
            await notify(msg)


async def run_position_monitor(info, settings, notify, stop_event: asyncio.Event) -> None:
    logger.info(f"Started — polling every {settings.position_poll_interval_seconds}s")

    while not stop_event.is_set():
        try:
            await _check_closed_positions(info, settings, notify)
        except Exception as error:
            logger.error(f"Poll cycle error: {error}", exc_info=True)

        await asyncio.sleep(settings.position_poll_interval_seconds)
