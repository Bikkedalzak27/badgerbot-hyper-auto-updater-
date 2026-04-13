import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

REQUIRED_KEYS = [
    "HL_ACCOUNT_ADDRESS",
    "HL_API_PRIVATE_KEY",
    "BADGERBOT_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_AUTHORIZED_USER_ID",
]


@dataclass
class Settings:
    hl_account_address: str
    hl_api_private_key: str
    badgerbot_api_key: str
    position_size_pct: float
    position_size_usd: float | None
    risk_pct: float | None
    max_signal_age_seconds: int
    max_price_deviation_pct: float
    telegram_bot_token: str
    telegram_authorized_user_id: int
    position_poll_interval_seconds: int


def load_settings() -> Settings:
    load_dotenv()

    missing = [key for key in REQUIRED_KEYS if not os.getenv(key)]
    if missing:
        print(
            f"ERROR: Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        print("Copy .env.example to .env and fill in all values.", file=sys.stderr)
        sys.exit(1)

    return Settings(
        hl_account_address=os.environ["HL_ACCOUNT_ADDRESS"],
        hl_api_private_key=os.environ["HL_API_PRIVATE_KEY"],
        badgerbot_api_key=os.environ["BADGERBOT_API_KEY"],
        position_size_pct=float(os.getenv("POSITION_SIZE_PCT", "0.10")),
        position_size_usd=float(v) if (v := os.getenv("POSITION_SIZE_USD")) else None,
        risk_pct=float(v) if (v := os.getenv("RISK_PCT")) else None,
        max_signal_age_seconds=int(os.getenv("MAX_SIGNAL_AGE_SECONDS", "1200")),
        max_price_deviation_pct=float(os.getenv("MAX_PRICE_DEVIATION_PCT", "0.01")),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_authorized_user_id=int(os.environ["TELEGRAM_AUTHORIZED_USER_ID"]),
        position_poll_interval_seconds=int(os.getenv("POSITION_POLL_INTERVAL_SECONDS", "15")),
    )
