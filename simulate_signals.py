"""
Simulate three ETH LONG signals by injecting them directly into the trade pipeline.

Each signal fires one minute apart at the current mark price with different TP/SL offsets.
Order size is always the minimum allowed for the coin (10^(-szDecimals)), bypassing
the equity-based sizing so the order always triggers regardless of account balance.

WARNING: This places REAL orders on Hyperliquid on whatever network .env specifies.

Run from the hyperbot/ directory:
    .venv/bin/python simulate_signals.py
"""

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
)
from storage.trade_log import init_trade_log, insert_trade, update_trade_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s",
    force=True,
)
logger = logging.getLogger("Simulator")

COIN = "ETH"
MINIMUM_NOTIONAL_USD = 11.0  # HL enforces $10 minimum notional; pad to $11 for safety

SIGNAL_TEMPLATES = [
    {"tp_offset": 0.05, "sl_offset": -0.03},
    {"tp_offset": 0.04, "sl_offset": -0.02},
    {"tp_offset": 0.06, "sl_offset": -0.04},
]
DELAY_BETWEEN_SIGNALS_SECONDS = 60


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
        "mode": "LONG",
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
    }


async def execute_with_fixed_size(
    signal: dict, size: float, info, exchange, leverage_config: dict, notify, address: str
) -> None:
    coin = signal["coin_symbol"]
    tp_price = float(signal["tp_price"])
    sl_price = float(signal["sl_price"])
    leverage = leverage_config.get(coin, leverage_config.get("DEFAULT", 3))

    fill_price = await _enter_position(exchange, coin, is_long=True, size=size, leverage=leverage)
    if fill_price is None:
        logger.error(f"Entry failed | coin={coin}")
        return

    trade_id = await insert_trade(coin, "LONG", size, fill_price, tp_price, sl_price)
    tp_ok, sl_ok = await _place_tpsl_orders(exchange, coin, False, size, tp_price, sl_price)

    if not tp_ok or not sl_ok:
        await update_trade_status(trade_id, "UNPROTECTED")
        logger.error(f"POSITION UNPROTECTED | coin={coin} | trade_id={trade_id}")
        await notify(f"⚠️ UNPROTECTED: {coin} LONG @ ${fill_price:,.2f} — TP/SL failed!")
    else:
        logger.info(f"Trade complete | entry={fill_price} | TP={tp_price:.2f} | SL={sl_price:.2f}")
        post = await asyncio.to_thread(_fetch_post_trade_state, info, address, coin)
        notional = size * fill_price
        liq_str = f"${post['liq_px']:,.2f}" if post["liq_px"] else "N/A"
        margin_pct = (post["margin_used"] / post["account_value"] * 100) if post["account_value"] > 0 else 0
        await notify(
            f"🟢 {coin} LONG OPENED\n\n"
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


async def run_simulation() -> None:
    settings = load_settings()
    network = "TESTNET" if settings.hl_use_testnet else "MAINNET"
    logger.info(f"Network: {network}")
    logger.info(f"Simulating {len(SIGNAL_TEMPLATES)} {COIN} LONG signals, {DELAY_BETWEEN_SIGNALS_SECONDS}s apart")

    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL
    info = Info(api_url, skip_ws=True)
    exchange = build_exchange(settings)
    leverage_config = load_leverage_config()

    bot_state = BotState()
    telegram_bot = TelegramBot(settings, info, exchange, bot_state)

    await init_trade_log()
    min_size = await fetch_min_size(info, COIN)

    async with telegram_bot._app:
        for index, template in enumerate(SIGNAL_TEMPLATES):
            signal = await build_signal(info, template)
            logger.info(
                f"Signal {index + 1}/{len(SIGNAL_TEMPLATES)}:"
                f" {COIN} LONG @ {signal['price']:.2f}"
                f" | TP: {signal['tp_price']:.2f}"
                f" | SL: {signal['sl_price']:.2f}"
                f" | size: {min_size}"
            )

            await execute_with_fixed_size(
                signal, min_size, info, exchange, leverage_config, telegram_bot.send,
                settings.hl_account_address
            )

            if index < len(SIGNAL_TEMPLATES) - 1:
                logger.info(f"Waiting {DELAY_BETWEEN_SIGNALS_SECONDS}s before next signal...")
                await asyncio.sleep(DELAY_BETWEEN_SIGNALS_SECONDS)

    logger.info("Simulation complete.")


if __name__ == "__main__":
    asyncio.run(run_simulation())
