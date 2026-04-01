"""
Simulate trade signals by injecting them directly into the trade pipeline.

By default, fires one ETH LONG and one ETH SHORT (one minute apart) at the current mark
price with a small TP/SL offset. Order size is always the minimum allowed for the coin
(10^(-szDecimals)), bypassing equity-based sizing so the order always triggers regardless
of account balance.

WARNING: This places REAL orders on Hyperliquid on whatever network .env specifies.
         Ensure HL_USE_TESTNET=true in your .env before running.

Run from the project directory:
    .venv/bin/python simulate_signals.py            # one LONG + one SHORT (default)
    .venv/bin/python simulate_signals.py --mode long
    .venv/bin/python simulate_signals.py --mode short
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import load_settings
from services.telegram_bot import BotState, TelegramBot
from services.trade_executor import (
    _enter_position,
    _fetch_post_trade_state,
    _place_tpsl_orders,
    build_exchange,
    load_leverage_config,
    safe_spot_meta,
)
from storage.trade_log import close_trade, init_trade_log, insert_trade, update_trade_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s",
    force=True,
)
logger = logging.getLogger("Simulator")


def _b(text) -> str:
    return f"<code>{text}</code>"


COIN = "ETH"
MINIMUM_NOTIONAL_USD = 11.0  # HL enforces $10 minimum notional; pad to $11 for safety

SIGNAL_TEMPLATES = {
    "long": {"mode": "LONG", "tp_offset": 0.05, "sl_offset": -0.03},
    "short": {"mode": "SHORT", "tp_offset": -0.05, "sl_offset": 0.03},
}
DELAY_BETWEEN_SIGNALS_SECONDS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate BadgerBot trade signals")
    parser.add_argument(
        "--mode",
        choices=["long", "short", "both"],
        default="both",
        help="Which direction(s) to simulate (default: both)",
    )
    return parser.parse_args()


async def fetch_min_size(info: Info, coin: str) -> float:
    meta = await asyncio.to_thread(info.meta)
    for asset in meta["universe"]:
        if asset["name"] == coin:
            sz_decimals = asset["szDecimals"]
            all_mids = await asyncio.to_thread(info.all_mids)
            mark_price = float(all_mids[coin])
            # Use the larger of precision minimum and notional minimum.
            # HL enforces a $10 minimum notional — szDecimals alone is not enough.
            precision_min = round(10 ** (-sz_decimals), sz_decimals)
            notional_min = round(MINIMUM_NOTIONAL_USD / mark_price, sz_decimals)
            min_size = max(precision_min, notional_min)
            logger.info(
                f"Min order size for {coin}: {min_size}"
                f" (szDecimals={sz_decimals}, mark={mark_price:.2f},"
                f" notional=${min_size * mark_price:.2f})"
            )
            return min_size
    raise ValueError(f"Coin {coin} not found in meta")


async def build_signal(info: Info, template: dict) -> dict:
    all_mids = await asyncio.to_thread(info.all_mids)
    mark_price = float(all_mids[COIN])
    return {
        "coin_symbol": COIN,
        "price": mark_price,
        "tp_price": mark_price * (1 + template["tp_offset"]),
        "sl_price": mark_price * (1 + template["sl_offset"]),
        "mode": template["mode"],
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
    }


async def execute_with_fixed_size(
    signal: dict, size: float, info, exchange, leverage_config: dict, notify, address: str
) -> None:
    coin = signal["coin_symbol"]
    is_long = signal["mode"] == "LONG"
    direction = signal["mode"]
    direction_emoji = "🟢" if is_long else "🔴"
    tp_price = float(signal["tp_price"])
    sl_price = float(signal["sl_price"])
    leverage = leverage_config.get(coin, leverage_config.get("DEFAULT", 3))

    fill_price = await _enter_position(exchange, coin, is_long=is_long, size=size, leverage=leverage)
    if fill_price is None:
        logger.error(f"Entry failed | coin={coin}")
        return

    trade_id = await insert_trade(coin, direction, size, fill_price, tp_price, sl_price)
    tp_ok, sl_ok = await _place_tpsl_orders(exchange, coin, not is_long, size, tp_price, sl_price)

    if not tp_ok or not sl_ok:
        await update_trade_status(trade_id, "UNPROTECTED")
        logger.error(f"POSITION UNPROTECTED | coin={coin} | trade_id={trade_id}")
        await notify(f"⚠️ UNPROTECTED: {coin} {direction} @ ${fill_price:,.2f} — TP/SL failed!")
    else:
        logger.info(f"Trade complete | entry={fill_price} | TP={tp_price:.2f} | SL={sl_price:.2f}")
        post = await asyncio.to_thread(_fetch_post_trade_state, info, address, coin)
        notional = size * fill_price
        liq_str = f"${post['liq_px']:,.2f}" if post["liq_px"] else "N/A"
        margin_pct = (post["margin_used"] / post["account_value"] * 100) if post["account_value"] > 0 else 0
        await notify(
            f"{direction_emoji} {coin} {direction} OPENED\n\n"
            f"📐 Size: {size} (${notional:,.2f})\n"
            f"💵 Entry: ${fill_price:,.2f}\n"
            f"🎯 TP: ${tp_price:,.2f}\n"
            f"⛔ SL: ${sl_price:,.2f}\n"
            f"⚡ Leverage: {leverage}x\n"
            f"💀 Liq: {liq_str}\n\n"
            f"🏦 Account Value: ${post['account_value']:,.2f}\n"
            f"🎢 Margin Used: ${post['margin_used']:,.2f} ({margin_pct:.1f}%)\n"
            f"💰 Available: ${post['withdrawable']:,.2f}"
        )


async def run_simulation(mode: str) -> None:
    settings = load_settings()
    network = "TESTNET" if settings.hl_use_testnet else "MAINNET"
    logger.info(f"Network: {network}")

    if mode == "both":
        templates = [SIGNAL_TEMPLATES["long"], SIGNAL_TEMPLATES["short"]]
    else:
        templates = [SIGNAL_TEMPLATES[mode]]

    directions = [t["mode"] for t in templates]
    logger.info(f"Simulating {len(templates)} signal(s): {directions}, {DELAY_BETWEEN_SIGNALS_SECONDS}s apart")

    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL
    info = Info(api_url, skip_ws=True, spot_meta=safe_spot_meta(api_url))
    exchange = build_exchange(settings)
    leverage_config = load_leverage_config()

    bot_state = BotState()
    telegram_bot = TelegramBot(settings, info, exchange, bot_state)

    await init_trade_log()
    min_size = await fetch_min_size(info, COIN)

    async with telegram_bot._app:
        for index, template in enumerate(templates):
            signal = await build_signal(info, template)
            logger.info(
                f"Signal {index + 1}/{len(templates)}:"
                f" {COIN} {template['mode']} @ {signal['price']:.2f}"
                f" | TP: {signal['tp_price']:.2f}"
                f" | SL: {signal['sl_price']:.2f}"
                f" | size: {min_size}"
            )

            await execute_with_fixed_size(
                signal, min_size, info, exchange, leverage_config, telegram_bot.send,
                settings.hl_account_address
            )

            if index < len(templates) - 1:
                logger.info(f"Waiting {DELAY_BETWEEN_SIGNALS_SECONDS}s before next signal...")
                await asyncio.sleep(DELAY_BETWEEN_SIGNALS_SECONDS)

        await close_simulation_trades(info, exchange, settings.hl_account_address, telegram_bot.send)

    logger.info("Simulation complete.")


async def close_simulation_trades(info, exchange, address: str, notify) -> None:
    """Close all positions opened during the simulation and cancel their TP/SL orders."""
    from storage.trade_log import fetch_open_trades

    open_trades = await fetch_open_trades()
    if not open_trades:
        logger.info("No open simulation trades to close.")
        return

    coins = list({t["coin"] for t in open_trades})
    logger.info(f"Auto-closing simulation trades | coins={coins}")

    try:
        all_orders = await asyncio.to_thread(info.frontend_open_orders, address)
    except Exception as error:
        logger.error(f"Failed to fetch open orders for cleanup: {error}")
        all_orders = []

    total_pnl = 0.0
    lines = []

    for coin in coins:
        coin_trades = [t for t in open_trades if t["coin"] == coin]
        side = coin_trades[0]["side"]
        direction_emoji = "🟢" if side == "LONG" else "🔴"

        try:
            result = await asyncio.to_thread(exchange.market_close, coin, slippage=0.02)
            fill_px = float(result["response"]["data"]["statuses"][0]["filled"]["avgPx"])
        except Exception as error:
            logger.error(f"Auto-close failed for {coin}: {error}")
            lines.append(f"{coin} — close failed")
            continue

        oids = [o["oid"] for o in all_orders if o.get("coin") == coin and o.get("isTrigger")]
        if oids:
            try:
                await asyncio.to_thread(
                    exchange.bulk_cancel,
                    [{"coin": coin, "oid": oid} for oid in oids],
                )
                logger.info(f"Cancelled {len(oids)} TP/SL order(s) for {coin}")
            except Exception as error:
                logger.error(f"Failed to cancel TP/SL for {coin}: {error}")

        for trade in coin_trades:
            entry_px = float(trade["entry_px"])
            size = float(trade["size"])
            pnl = (fill_px - entry_px) * size if side == "LONG" else (entry_px - fill_px) * size
            total_pnl += pnl
            await close_trade(trade["id"], pnl, "MANUAL")

        lines.append(f"{direction_emoji} {coin} {side} closed @ ${fill_px:,.2f}")

    pnl_sign = "+" if total_pnl >= 0 else ""
    await notify(
        f"🧹 Simulation complete — trades closed\n\n"
        + "\n".join(lines)
        + f"\n\n💰 Net PnL: {_b(f'{pnl_sign}${total_pnl:,.2f}')}"
    )
    logger.info(f"Simulation cleanup complete | total_pnl={total_pnl:.4f}")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_simulation(args.mode))
