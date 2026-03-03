import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import Settings

logger = logging.getLogger("SignalConsumer")

WEBSOCKET_URL = "wss://api.badgerbot.io/signals/ws"
INITIAL_RECONNECT_DELAY_SECONDS = 1
MAX_RECONNECT_DELAY_SECONDS = 60
OFFLINE_ALERT_THRESHOLD_SECONDS = 300

SignalHandler = Callable[[dict], Awaitable[None]]


def build_websocket_url(api_key: str) -> str:
    return f"{WEBSOCKET_URL}?api_key={api_key}"


def parse_signal(raw_message: str) -> dict | None:
    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError as error:
        logger.warning(f"Signal parse error: {error} | raw={raw_message[:200]}")
        return None

    if data.get("event") == "keepalive":
        return None

    required_fields = {"coin_symbol", "price", "tp_price", "sl_price", "mode", "dispatched_at"}
    missing = required_fields - data.keys()
    if missing:
        logger.warning(f"Signal missing fields: {missing} | raw={raw_message[:200]}")
        return None

    if not data.get("tp_price") or not data.get("sl_price"):
        logger.warning(f"Signal has null TP/SL — dropped | coin={data.get('coin_symbol')}")
        return None

    return data


def validate_signal(signal: dict, mark_price: float, settings: Settings) -> bool:
    dispatched_at = datetime.fromisoformat(signal["dispatched_at"]).replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - dispatched_at).total_seconds()

    if age_seconds > settings.max_signal_age_seconds:
        logger.warning(
            f"Signal dropped: stale by {age_seconds:.0f}s | coin={signal['coin_symbol']}"
        )
        return False

    signal_price = float(signal["price"])
    deviation = abs(mark_price - signal_price) / signal_price

    if deviation > settings.max_price_deviation_pct:
        logger.warning(
            f"Signal dropped: price deviation {deviation:.2%} > {settings.max_price_deviation_pct:.2%}"
            f" | coin={signal['coin_symbol']} | signal={signal_price} | mark={mark_price}"
        )
        return False

    return True


async def _listen(websocket_url: str, signal_handler: SignalHandler, settings: Settings) -> None:
    async with websockets.connect(websocket_url) as websocket:
        logger.info(f"Listening for signals on {WEBSOCKET_URL}")
        async for raw_message in websocket:
            signal = parse_signal(raw_message)
            if signal is None:
                continue
            logger.info(
                f"Signal received: {signal['coin_symbol']} {signal['mode']}"
                f" @ {signal['price']} | TP: {signal['tp_price']} | SL: {signal['sl_price']}"
            )
            await signal_handler(signal)


async def connect_and_listen(
    signal_handler: SignalHandler,
    settings: Settings,
    stop_event: asyncio.Event,
    notify=None,
) -> None:
    websocket_url = build_websocket_url(settings.badgerbot_api_key)
    reconnect_delay = INITIAL_RECONNECT_DELAY_SECONDS
    last_connected_at: float | None = None

    while not stop_event.is_set():
        try:
            last_connected_at = asyncio.get_event_loop().time()
            reconnect_delay = INITIAL_RECONNECT_DELAY_SECONDS
            await _listen(websocket_url, signal_handler, settings)
        except ConnectionClosed as error:
            logger.warning(f"WebSocket disconnected: {error}")
        except Exception as error:
            logger.error(f"WebSocket error: {error}")

        if stop_event.is_set():
            break

        elapsed = asyncio.get_event_loop().time() - (last_connected_at or 0)
        if elapsed >= OFFLINE_ALERT_THRESHOLD_SECONDS:
            msg = f"Signal feed offline for {elapsed:.0f}s — check badgerbot.io connection"
            logger.error(msg)
            if notify:
                await notify(msg)
        else:
            logger.warning(f"Reconnecting in {reconnect_delay}s...")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_SECONDS)
