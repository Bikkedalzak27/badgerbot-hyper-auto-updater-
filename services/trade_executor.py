import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import Settings
from services.signal_consumer import log_signal, validate_signal
from storage.trade_log import insert_trade, update_trade_status

logger = logging.getLogger("TradeExecutor")

Notifier = Callable[[str], Awaitable[None]]

LEVERAGE_CONFIG_PATH = Path(__file__).parent.parent / "config" / "coin_leverage.json"

_sz_decimals_cache: dict[str, int] = {}


def load_leverage_config() -> dict:
    with open(LEVERAGE_CONFIG_PATH) as file:
        return json.load(file)


def build_exchange(settings: Settings) -> Exchange:
    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL
    wallet = eth_account.Account.from_key(settings.hl_api_private_key)
    # account_address required — without it Exchange uses API wallet address (empty account)
    return Exchange(wallet, api_url, account_address=settings.hl_account_address)


def _fetch_sz_decimals(info: Info) -> dict[str, int]:
    meta = info.meta()
    return {asset["name"]: asset["szDecimals"] for asset in meta["universe"]}


async def ensure_sz_decimals_cached(info: Info) -> None:
    global _sz_decimals_cache
    if not _sz_decimals_cache:
        _sz_decimals_cache = await asyncio.to_thread(_fetch_sz_decimals, info)


def _fetch_account_equity(info: Info, address: str) -> float:
    # Unified accounts: spot USDC is the total collateral pool; perps equity is only the
    # margin portion. Standard accounts: perps equity is the full pool; spot USDC is 0.
    # max() returns the correct total for both account types.
    user_state = info.user_state(address)
    perps_equity = float(user_state.get("marginSummary", {}).get("accountValue", 0))
    spot_state = info.spot_user_state(address)
    spot_usdc = 0.0
    for balance in spot_state.get("balances", []):
        if balance["coin"] == "USDC":
            spot_usdc = float(balance["total"])
            break
    return max(perps_equity, spot_usdc)


async def fetch_account_equity(info: Info, address: str) -> float:
    return await asyncio.to_thread(_fetch_account_equity, info, address)


async def fetch_mark_price(info: Info, coin: str) -> float:
    all_mids = await asyncio.to_thread(info.all_mids)
    if coin not in all_mids:
        raise ValueError(f"Coin {coin} not found in mark prices")
    return float(all_mids[coin])


def calculate_position_size(
    equity: float, mark_price: float, position_size_pct: float, sz_decimals: int
) -> float:
    notional = equity * position_size_pct
    return round(notional / mark_price, sz_decimals)


def calculate_risk_size(
    equity: float, risk_pct: float, entry_price: float,
    sl_price: float, batch_size: int, sz_decimals: int,
) -> float:
    risk_per_signal = equity * risk_pct / batch_size
    price_distance = abs(entry_price - sl_price)
    if price_distance == 0:
        return 0.0
    return round(risk_per_signal / price_distance, sz_decimals)


async def _validate_and_size(
    signal: dict, info: Info, settings: Settings, leverage_config: dict,
    batch_size: int = 1,
) -> tuple[float, float, float, int, str] | None:
    """Returns (mark_price, size, equity, leverage, rejection_reason) or None on fetch error.
    rejection_reason is empty string if valid."""
    coin = signal["coin_symbol"]
    leverage = leverage_config.get(coin, leverage_config.get("DEFAULT", 3))
    try:
        mark_price = await fetch_mark_price(info, coin)
    except Exception as error:
        logger.error(f"Failed to fetch mark price | coin={coin} | {error}")
        return None
    rejection = validate_signal(signal, mark_price, settings)
    if rejection:
        return mark_price, 0, 0, leverage, rejection
    equity = await fetch_account_equity(info, settings.hl_account_address)
    if equity <= 0:
        logger.error(f"Account equity is zero — skipping | coin={coin}")
        return mark_price, 0, 0, leverage, "zero equity"
    await ensure_sz_decimals_cached(info)
    sz_decimals = _sz_decimals_cache.get(coin, 4)
    if settings.risk_pct is not None:
        entry_price = float(signal["price"])
        sl_price = float(signal["sl_price"])
        size = calculate_risk_size(
            equity, settings.risk_pct, entry_price, sl_price, batch_size, sz_decimals
        )
        logger.info(
            f"Risk sizing | coin={coin} | risk={settings.risk_pct*100:.1f}%"
            f" | batch={batch_size} | entry={entry_price} | sl={sl_price}"
            f" | distance={abs(entry_price - sl_price):.2f} | size={size}"
        )
    elif settings.position_size_usd is not None:
        notional = settings.position_size_usd * leverage
        size = round(notional / mark_price, sz_decimals)
    else:
        size = calculate_position_size(equity, mark_price, settings.position_size_pct, sz_decimals)
    if size <= 0:
        logger.warning(f"Calculated size is zero — skipping | coin={coin}")
        return mark_price, 0, 0, leverage, "zero size"
    notional = size * mark_price
    if notional < 10.0:
        logger.warning(
            f"Below $10 minimum notional (${notional:.2f}) — skipping | coin={coin}"
        )
        return mark_price, 0, 0, leverage, f"below $10 min (${notional:.2f})"
    return mark_price, size, equity, leverage, ""


def _px_decimals(exchange: Exchange, coin: str) -> int:
    coin_name = exchange.info.name_to_coin[coin]
    asset = exchange.info.coin_to_asset[coin_name]
    return 6 - exchange.info.asset_to_sz_decimals[asset]


def _round_price(exchange: Exchange, coin: str, px: float) -> float:
    # HL requires prices with at most 5 significant figures.
    # round(mark * 1.05, 2) can produce 6-sig-fig prices (e.g. 2059.21) which are rejected
    # with "Invalid TP/SL price". Apply the same 5g rounding as the SDK's _slippage_price.
    return round(float(f"{px:.5g}"), _px_decimals(exchange, coin))


def _trigger_limit_px(exchange: Exchange, coin: str, is_buy: bool, trigger_px: float) -> float:
    # limit_px must be aggressive (worse than trigger) so the order always fills when triggered.
    slippage = 0.05
    adjusted = trigger_px * (1 + slippage if is_buy else 1 - slippage)
    return round(float(f"{adjusted:.5g}"), _px_decimals(exchange, coin))


def _fetch_post_trade_state(info: Info, address: str, coin: str) -> dict:
    user_state = info.user_state(address)
    spot_state = info.spot_user_state(address)
    margin = user_state.get("marginSummary", {})
    margin_used = float(margin.get("totalMarginUsed", 0))
    spot_usdc = next(
        (float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"),
        0.0,
    )
    account_value = spot_usdc
    available = spot_usdc - margin_used
    liq_px = None
    for ap in user_state.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == coin and float(pos.get("szi", 0)) != 0:
            raw_liq = pos.get("liquidationPx")
            if raw_liq:
                liq_px = float(raw_liq)
            break
    return {
        "account_value": account_value,
        "margin_used": margin_used,
        "withdrawable": available,
        "liq_px": liq_px,
    }


async def _enter_position(
    exchange: Exchange, coin: str, is_long: bool, size: float, leverage: int
) -> float | None:
    await asyncio.to_thread(exchange.update_leverage, leverage, coin, True)
    logger.info(f"Leverage set: {coin} {leverage}x cross")
    entry_result = await asyncio.to_thread(exchange.market_open, coin, is_long, size, None, 0.02)
    if entry_result.get("status") != "ok":
        logger.error(f"Market order failed | coin={coin} | result={entry_result}")
        return None
    try:
        fill_price = float(entry_result["response"]["data"]["statuses"][0]["filled"]["avgPx"])
    except (KeyError, IndexError, TypeError) as error:
        logger.error(f"Order did not fill | coin={coin} | {error}")
        return None
    logger.info(f"Entry filled @ {fill_price} | coin={coin}")
    return fill_price


def _tpsl_order_ok(result: dict, label: str, coin: str) -> bool:
    # Top-level "ok" is necessary but not sufficient — the API silently fails
    # trigger orders by embedding the error in response.data.statuses[0].
    if result.get("status") != "ok":
        logger.error(f"{label} placement failed | coin={coin} | result={result}")
        return False
    try:
        inner = result["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        logger.error(f"{label} placement — unreadable inner status | coin={coin} | result={result}")
        return False
    # Trigger orders return the string "waitingForTrigger" on success.
    if inner == "waitingForTrigger":
        return True
    if isinstance(inner, dict):
        if "error" in inner:
            logger.error(f"{label} placement inner error | coin={coin} | error={inner['error']}")
            return False
        if "resting" in inner or "filled" in inner:
            return True
    logger.error(f"{label} placement — unexpected status | coin={coin} | inner={inner}")
    return False


async def _place_tpsl_orders(
    exchange: Exchange, coin: str, closing_is_buy: bool, size: float,
    tp_price: float, sl_price: float,
) -> tuple[bool, bool]:
    # Round trigger prices to 5 significant figures — HL rejects prices with 6+ sig figs.
    tp_price = _round_price(exchange, coin, tp_price)
    sl_price = _round_price(exchange, coin, sl_price)
    tp_limit = _trigger_limit_px(exchange, coin, closing_is_buy, tp_price)
    sl_limit = _trigger_limit_px(exchange, coin, closing_is_buy, sl_price)
    tp_type = {"trigger": {"triggerPx": tp_price, "isMarket": True, "tpsl": "tp"}}
    sl_type = {"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}}
    # positionTpsl grouping: send both TP and SL together as a pair on the existing position.
    # exchange.order() uses grouping="na" which HL rejects for trigger orders.
    def _make_order(limit_px, order_type):
        return {"coin": coin, "is_buy": closing_is_buy, "sz": size,
                "limit_px": limit_px, "order_type": order_type, "reduce_only": True}

    tp_ok, sl_ok = False, False
    try:
        result = await asyncio.to_thread(
            exchange.bulk_orders,
            [_make_order(tp_limit, tp_type), _make_order(sl_limit, sl_type)],
            None, "positionTpsl"
        )
        statuses = result.get("response", {}).get("data", {}).get("statuses", [{}, {}])
        tp_result = {"status": result.get("status"), "response": {"type": "order", "data": {"statuses": [statuses[0]]}}}
        sl_result = {"status": result.get("status"), "response": {"type": "order", "data": {"statuses": [statuses[1] if len(statuses) > 1 else {}]}}}
        tp_ok = _tpsl_order_ok(tp_result, "TP", coin)
        sl_ok = _tpsl_order_ok(sl_result, "SL", coin)
        if tp_ok:
            logger.info(f"TP placed @ {tp_price} (limit={tp_limit}) | coin={coin}")
        if sl_ok:
            logger.info(f"SL placed @ {sl_price} (limit={sl_limit}) | coin={coin}")
    except Exception as error:
        logger.error(f"TP/SL placement exception | coin={coin} | {error}")
    return tp_ok, sl_ok


async def execute_signal(
    signal: dict, info: Info, exchange: Exchange,
    settings: Settings, leverage_config: dict,
    notify: Notifier | None = None,
    batch_size: int = 1,
) -> None:
    coin = signal["coin_symbol"]
    is_long = signal["mode"] == "LONG"
    direction = "LONG" if is_long else "SHORT"
    tp_price = float(signal["tp_price"])
    sl_price = float(signal["sl_price"])

    sizing = await _validate_and_size(signal, info, settings, leverage_config, batch_size)
    if sizing is None:
        log_signal({"coin": coin, "side": direction, "outcome": "error", "reason": "fetch failed"})
        return
    mark_price, size, _, leverage, rejection = sizing
    if rejection:
        log_signal({"coin": coin, "side": direction, "outcome": "rejected", "reason": rejection})
        if notify:
            await notify(f"⏭ {coin} {direction} skipped — {rejection}")
        return
    logger.info(
        f"EXECUTING: {coin} {'LONG' if is_long else 'SHORT'}"
        f" | size={size} | notional=${size * mark_price:.2f} | leverage={leverage}x"
    )

    fill_price = await _enter_position(exchange, coin, is_long, size, leverage)
    if fill_price is None:
        log_signal({"coin": coin, "side": direction, "outcome": "error", "reason": "order not filled"})
        if notify:
            await notify(f"⚠️ {coin} {direction} — order placed but did not fill")
        return

    # Write trade record BEFORE TP/SL — Financial Safety Rule #4
    trade_id = await insert_trade(coin, direction, size, fill_price, tp_price, sl_price)

    tp_ok, sl_ok = await _place_tpsl_orders(exchange, coin, not is_long, size, tp_price, sl_price)

    direction_emoji = "🟢" if is_long else "🔴"

    if not tp_ok or not sl_ok:
        await update_trade_status(trade_id, "UNPROTECTED")
        logger.error(f"POSITION UNPROTECTED — TP/SL failed | coin={coin} | trade_id={trade_id}")
        if notify:
            await notify(f"⚠️ UNPROTECTED: {coin} {direction} @ ${fill_price:,.2f} — TP/SL placement failed!")
        return

    log_signal({"coin": coin, "side": direction, "outcome": "filled", "entry": fill_price, "size": size})
    logger.info(f"Trade complete | coin={coin} | entry={fill_price} | TP={tp_price} | SL={sl_price}")
    if notify:
        post = await asyncio.to_thread(_fetch_post_trade_state, info, settings.hl_account_address, coin)
        notional = size * fill_price
        liq_str = f"${post['liq_px']:,.2f}" if post["liq_px"] else "N/A"
        margin_pct = (post["margin_used"] / post["account_value"] * 100) if post["account_value"] > 0 else 0
        await notify(
            f"{direction_emoji} {coin} {direction} OPENED\n\n"
            f"📐 Size: {size} (${notional:,.2f})\n"
            f"💵 Entry: ${fill_price:,.2f}\n"
            f"✅ TP: ${tp_price:,.2f}\n"
            f"⛔ SL: ${sl_price:,.2f}\n"
            f"⚡ Leverage: {leverage}x\n"
            f"💀 Liq: {liq_str}\n\n"
            f"🏦 Account Value: ${post['account_value']:,.2f}\n"
            f"🎢 Margin Used: ${post['margin_used']:,.2f} ({margin_pct:.1f}%)\n"
            f"💰 Available: ${post['withdrawable']:,.2f}"
        )


def make_signal_handler(
    info: Info, exchange: Exchange, settings: Settings, leverage_config: dict,
    notify: Notifier | None = None, bot_state=None,
):
    async def handler(signal: dict, batch_size: int = 1) -> None:
        if bot_state and bot_state.paused:
            logger.info(f"Bot paused — signal dropped | coin={signal.get('coin_symbol')}")
            return
        try:
            await execute_signal(signal, info, exchange, settings, leverage_config, notify, batch_size)
        except Exception as error:
            logger.error(f"Unexpected error in execute_signal | {error}", exc_info=True)
    return handler
