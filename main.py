import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import Settings, load_settings
from services.position_monitor import run_position_monitor
from services.signal_consumer import connect_and_listen
from services.telegram_bot import BotState, TelegramBot
from services.trade_executor import build_exchange, load_leverage_config, make_signal_handler, safe_spot_meta
from storage.trade_log import fetch_open_trades, init_trade_log, insert_trade, repair_trade_tpsl, update_trade_status

LOGS_DIR = Path(__file__).parent / "logs"


def configure_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / "hyperbot.log"

    file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - [%(name)s] %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - [%(name)s] %(message)s")
    )

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler], force=True)


def connect_to_hyperliquid(settings: Settings) -> Info:
    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL
    return Info(api_url, skip_ws=True, spot_meta=safe_spot_meta(api_url))


async def create_info(settings: Settings, logger: logging.Logger) -> Info:
    try:
        return await asyncio.to_thread(connect_to_hyperliquid, settings)
    except Exception as error:
        logger.error(f"Failed to connect to Hyperliquid: {error}")
        sys.exit(1)


def fetch_spot_usdc_balance(info: Info, address: str) -> float:
    spot_state = info.spot_user_state(address)
    for balance in spot_state.get("balances", []):
        if balance["coin"] == "USDC":
            return float(balance["total"])
    return 0.0


def resolve_account_equity(perps_equity: float, info: Info, address: str) -> float:
    # Unified accounts keep USDC in spot; marginSummary shows $0 until active perps positions exist
    if perps_equity > 0:
        return perps_equity
    return fetch_spot_usdc_balance(info, address)


def format_open_positions(asset_positions: list) -> str:
    open_positions = [
        p for p in asset_positions
        if "position" in p and float(p["position"]["szi"]) != 0
    ]
    if not open_positions:
        return "0 open positions"
    lines = [
        f"  {p['position']['coin']} | size={p['position']['szi']} | entry={p['position'].get('entryPx', 'N/A')}"
        for p in open_positions
    ]
    return f"{len(lines)} open position(s):\n" + "\n".join(lines)


def _extract_tpsl_for_coin(all_orders: list, coin: str, logger: logging.Logger) -> tuple[float, float]:
    coin_triggers = [o for o in all_orders if o.get("coin") == coin and o.get("isTrigger")]
    tp_px = next(
        (float(o["triggerPx"]) for o in coin_triggers if "profit" in o.get("orderType", "").lower()),
        0.0,
    )
    sl_px = next(
        (float(o["triggerPx"]) for o in coin_triggers if "stop" in o.get("orderType", "").lower()),
        0.0,
    )
    return tp_px, sl_px


async def _fetch_all_orders(info: Info, settings: Settings, logger: logging.Logger) -> list:
    try:
        raw_orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.error(f"Reconciliation: failed to fetch open orders: {error}")
        return []
    all_orders = []
    for order in raw_orders:
        all_orders.append(order)
        all_orders.extend(order.get("children", []))
    return all_orders


async def _reconcile_orphaned_positions(
    info: Info, settings: Settings, asset_positions: list, logger: logging.Logger
) -> None:
    open_trades = await fetch_open_trades()
    db_coins = {t["coin"] for t in open_trades}

    hl_positions = {}
    for p in asset_positions:
        pos = p.get("position", {})
        coin = pos.get("coin", "")
        if coin and float(pos.get("szi", 0)) != 0:
            hl_positions[coin] = pos

    orphaned = {c: p for c, p in hl_positions.items() if c not in db_coins}
    unprotected = [t for t in open_trades if t["status"] == "UNPROTECTED"]

    if not orphaned and not unprotected:
        return

    all_orders = await _fetch_all_orders(info, settings, logger)

    for coin, pos in orphaned.items():
        szi = float(pos["szi"])
        side = "LONG" if szi > 0 else "SHORT"
        size = abs(szi)
        entry_px = float(pos.get("entryPx") or 0)
        tp_px, sl_px = _extract_tpsl_for_coin(all_orders, coin, logger)

        trade_id = await insert_trade(coin, side, size, entry_px, tp_px, sl_px)

        if tp_px == 0.0 or sl_px == 0.0:
            await update_trade_status(trade_id, "UNPROTECTED")
            logger.warning(
                f"Recovered untracked position: {coin} {side} {size} @ ${entry_px}"
                f" | TP/SL not found — marked UNPROTECTED"
            )
        else:
            logger.info(
                f"Recovered untracked position: {coin} {side} {size} @ ${entry_px}"
                f" | TP: ${tp_px} | SL: ${sl_px}"
            )

    for trade in unprotected:
        coin = trade["coin"]
        tp_px, sl_px = _extract_tpsl_for_coin(all_orders, coin, logger)
        if tp_px == 0.0 or sl_px == 0.0:
            logger.warning(f"UNPROTECTED trade id={trade['id']} {coin} — TP/SL still not found on HL")
            continue
        await repair_trade_tpsl(trade["id"], tp_px, sl_px)
        logger.info(
            f"Repaired UNPROTECTED trade id={trade['id']} {coin}"
            f" | TP: ${tp_px} | SL: ${sl_px}"
        )


async def run_startup_check(settings: Settings, info: Info, logger: logging.Logger) -> None:
    network_label = "TESTNET" if settings.hl_use_testnet else "MAINNET"
    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL
    logger.info(f"Connecting to Hyperliquid {network_label} ({api_url})")

    try:
        user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    except Exception as error:
        logger.error(f"Failed to connect to Hyperliquid: {error}")
        sys.exit(1)

    margin_summary = user_state.get("marginSummary", {})
    perps_equity = float(margin_summary.get("accountValue", 0))
    account_equity = await asyncio.to_thread(
        resolve_account_equity, perps_equity, info, settings.hl_account_address
    )
    asset_positions = user_state.get("assetPositions", [])

    logger.info(f"Connected to Hyperliquid {network_label}")
    logger.info(
        f"Account: {settings.hl_account_address} | Equity: ${account_equity:,.2f}"
    )
    logger.info(format_open_positions(asset_positions))

    await init_trade_log()
    logger.info("Trade log initialized")
    await _reconcile_orphaned_positions(info, settings, asset_positions, logger)
    logger.info("All services ready. Starting loop...")


async def main() -> None:
    configure_logging()
    logger = logging.getLogger("Main")

    settings = load_settings()
    info = await create_info(settings, logger)
    await run_startup_check(settings, info, logger)

    exchange = await asyncio.to_thread(build_exchange, settings)
    leverage_config = load_leverage_config()
    bot_state = BotState()
    telegram_bot = TelegramBot(settings, info, exchange, bot_state, leverage_config)
    signal_handler = make_signal_handler(
        info, exchange, settings, leverage_config, telegram_bot.send, bot_state
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Starting signal consumer, position monitor, and Telegram bot...")
    results = await asyncio.gather(
        connect_and_listen(signal_handler, settings, stop_event, notify=telegram_bot.send),
        telegram_bot.run(stop_event),
        run_position_monitor(info, settings, telegram_bot.send, stop_event, exchange),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Service exited with exception: {result}", exc_info=result)

    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
