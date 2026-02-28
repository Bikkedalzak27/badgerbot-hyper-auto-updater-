import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import Settings, load_settings
from storage.trade_log import init_trade_log

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

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


def connect_to_hyperliquid(settings: Settings) -> Info:
    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL
    return Info(api_url, skip_ws=True)


def format_open_positions(asset_positions: list) -> str:
    open_positions = [
        p for p in asset_positions if float(p["position"]["szi"]) != 0
    ]
    if not open_positions:
        return "0 open positions"
    lines = [
        f"  {p['position']['coin']} | size={p['position']['szi']} | entry={p['position'].get('entryPx', 'N/A')}"
        for p in open_positions
    ]
    return f"{len(lines)} open position(s):\n" + "\n".join(lines)


async def run_startup_check(settings: Settings, logger: logging.Logger) -> None:
    network_label = "TESTNET" if settings.hl_use_testnet else "MAINNET"
    api_url = constants.TESTNET_API_URL if settings.hl_use_testnet else constants.MAINNET_API_URL

    logger.info(f"Connecting to Hyperliquid {network_label} ({api_url})")

    try:
        info = await asyncio.to_thread(connect_to_hyperliquid, settings)
        user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    except Exception as error:
        logger.error(f"Failed to connect to Hyperliquid: {error}")
        sys.exit(1)

    margin_summary = user_state.get("marginSummary", {})
    account_equity = float(margin_summary.get("accountValue", 0))
    asset_positions = user_state.get("assetPositions", [])

    logger.info(f"Connected to Hyperliquid {network_label}")
    logger.info(
        f"Account: {settings.hl_account_address} | Equity: ${account_equity:,.2f}"
    )
    logger.info(format_open_positions(asset_positions))

    await init_trade_log()
    logger.info("Trade log initialized")
    logger.info("All services ready. Starting loop...")


async def main() -> None:
    configure_logging()
    logger = logging.getLogger("Main")

    settings = load_settings()
    await run_startup_check(settings, logger)


if __name__ == "__main__":
    asyncio.run(main())
