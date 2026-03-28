import asyncio
import logging
from datetime import datetime, timezone

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from config.settings import Settings
from storage.trade_log import close_trade, fetch_open_trades

logger = logging.getLogger("PositionMonitor")

_alerted_orphan_coins: set[str] = set()

# {coin: {"count": int, "next_log_at": int}} — tracks fill retry backoff per coin
_fill_retry_state: dict[str, dict] = {}


async def _cancel_counterpart_order(
    exchange: Exchange, info: Info, settings: Settings,
    trade: dict, close_status: str,
) -> None:
    """Cancel the orphaned TP or SL order after its counterpart was hit."""
    # If TP was hit, cancel the SL. If SL was hit, cancel the TP.
    if close_status == "TP":
        target_px = float(trade["sl_px"])
        label = "SL"
    else:
        target_px = float(trade["tp_px"])
        label = "TP"

    coin = trade["coin"]
    size = float(trade["size"])

    try:
        orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.warning(f"Failed to fetch open orders for {label} cancel | {error}")
        return

    # Match by coin, trigger price (within 0.1%), and size (within 1%)
    for order in orders:
        if order.get("coin") != coin:
            continue
        trigger = float(order.get("triggerPx", 0))
        order_sz = float(order.get("sz", 0))
        if abs(trigger - target_px) / target_px < 0.001 and abs(order_sz - size) / size < 0.01:
            oid = order.get("oid")
            try:
                result = await asyncio.to_thread(exchange.cancel, coin, oid)
                if result.get("status") == "ok":
                    logger.info(f"Cancelled orphaned {label} | coin={coin} | oid={oid} | trigger={trigger}")
                else:
                    logger.warning(f"Cancel {label} returned non-ok | coin={coin} | result={result}")
            except Exception as error:
                logger.warning(f"Failed to cancel {label} | coin={coin} | oid={oid} | {error}")
            return

    logger.info(f"No matching {label} order found to cancel | coin={coin} | target_px={target_px}")


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


def _format_close_notification(trade: dict, close_px: float, pnl: float, status: str, equity: float) -> str:
    entry_px = float(trade["entry_px"])
    size = float(trade["size"])
    side = trade["side"]
    direction_emoji = "⛔" if status == "SL" else ("🟢" if side == "LONG" else "🔴")
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_pct = (pnl / equity) * 100 if equity > 0 else 0
    pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"

    return (
        f"{direction_emoji} {trade['coin']} {side} CLOSED — {status} @ <code>${close_px:,.2f}</code>\n\n"
        f"📐 Size: <code>{size} (${size * close_px:,.2f})</code>\n"
        f"💵 Entry: <code>${entry_px:,.2f}</code> → Exit: <code>${close_px:,.2f}</code>\n"
        f"📈 PnL: <code>{pnl_sign}${pnl:,.2f} ({pnl_pct_str})</code>"
    )


async def _process_coin_closures(
    coin: str, db_trades: list[dict], fills: list, notify, equity: float,
    exchange: Exchange, info: Info, settings: Settings,
) -> None:
    """Match close fills to DB trades and close each one individually."""
    side = db_trades[0]["side"]
    since_ms = min(
        datetime.fromisoformat(t["opened_at"]).replace(tzinfo=timezone.utc).timestamp() * 1000
        for t in db_trades
    )

    close_fills = _get_close_fills(fills, coin, side, since_ms)

    if not close_fills:
        state = _fill_retry_state.setdefault(coin, {"count": 0, "next_log_at": 0})
        state["count"] += 1
        if state["count"] >= state["next_log_at"]:
            logger.warning(
                f"{coin}: closure detected but no matching fills in user_fills"
                f" — retry #{state['count']}, backing off"
            )
            # Log at: 0, 1, 2, 4, 8, then every ~112 cycles (~30min at 16s poll)
            state["next_log_at"] = state["count"] + min(2 ** state["count"], 112)
        return

    _fill_retry_state.pop(coin, None)

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
        await notify(_format_close_notification(trade, fill_px, pnl, status, equity))
        await _cancel_counterpart_order(exchange, info, settings, trade, status)


async def _close_residual_position(
    exchange: Exchange, info: Info, settings: Settings,
    coin: str, residual_size: float, notify,
) -> None:
    """Auto-close a residual position left by rounding across multiple TP/SL fills."""
    logger.info(f"Closing residual position | coin={coin} | size={residual_size}")
    try:
        result = await asyncio.to_thread(exchange.market_close, coin, slippage=0.02)
        if result.get("status") != "ok":
            logger.error(f"Residual close failed | coin={coin} | result={result}")
            await notify(
                f"⚠️ Residual {coin} position (<code>{residual_size}</code>) "
                f"could not be closed — close manually"
            )
            return
        fill_info = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
        close_px = float(fill_info.get("filled", {}).get("avgPx", 0))
        notional = residual_size * close_px
        logger.info(
            f"Residual closed | coin={coin} | size={residual_size}"
            f" | exit={close_px} | notional=${notional:.2f}"
        )
        await notify(
            f"🧹 {coin} residual closed @ <code>${close_px:,.2f}</code>\n"
            f"📐 Size: <code>{residual_size} (${notional:,.2f})</code>\n"
            f"ℹ️ Rounding remainder after all TP/SL fills"
        )
    except Exception as error:
        logger.error(f"Residual close exception | coin={coin} | {error}")
        await notify(
            f"⚠️ Residual {coin} position (<code>{residual_size}</code>) "
            f"close failed — <code>{error}</code>"
        )


async def _check_closed_positions(info, settings, notify, exchange) -> None:
    open_trades = await fetch_open_trades()

    user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    hl_positions = _extract_open_positions(user_state)

    if not open_trades and not hl_positions:
        return

    spot_state = await asyncio.to_thread(info.spot_user_state, settings.hl_account_address)
    perps_equity = float(user_state.get("marginSummary", {}).get("accountValue", 0))
    spot_usdc = next(
        (float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"),
        0.0,
    )
    equity = max(perps_equity, spot_usdc)

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
        await _process_coin_closures(coin, db_trades, fills, notify, equity, exchange, info, settings)

    # Close residual positions left by rounding mismatches across multiple TP/SL fills.
    # Re-fetch open trades since _process_coin_closures may have closed some.
    remaining_trades = await fetch_open_trades()
    remaining_coins = {t["coin"] for t in remaining_trades}

    for coin, hl_size in hl_positions.items():
        if coin in remaining_coins:
            continue
        if coin in trades_by_coin:
            # All DB trades for this coin just closed, but HL position persists — residual.
            await _close_residual_position(
                exchange, info, settings, coin, hl_size, notify
            )
        elif coin not in _alerted_orphan_coins:
            _alerted_orphan_coins.add(coin)
            msg = f"Untracked position: {coin} size=<code>{hl_size}</code> — no DB record. Close manually or wait for next signal."
            logger.warning(msg)
            await notify(msg)


async def run_position_monitor(info, settings, notify, stop_event: asyncio.Event, exchange: Exchange = None) -> None:
    logger.info(f"Started — polling every {settings.position_poll_interval_seconds}s")

    while not stop_event.is_set():
        try:
            await _check_closed_positions(info, settings, notify, exchange)
        except Exception as error:
            logger.error(f"Poll cycle error: {error}", exc_info=True)

        await asyncio.sleep(settings.position_poll_interval_seconds)
