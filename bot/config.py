"""Settings loaded from .env, plus the fake-card guard."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

API_BASE = "https://api.demoblaze.com"
WEB_BASE = "https://www.demoblaze.com"

# Well-known public test card numbers. Anything else is refused, so a real card
# can never end up in this demo by accident.
TEST_CARDS = {
    "4111111111111111",
    "4242424242424242",
    "4012888888881881",
    "4000056655665556",
    "5555555555554444",
    "5105105105105100",
    "378282246310005",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class CheckoutDetails:
    name: str
    country: str
    city: str
    card: str
    month: str
    year: str


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    checkout: CheckoutDetails
    api_base: str = API_BASE
    web_base: str = WEB_BASE


def normalize_card(card: str) -> str:
    return re.sub(r"[\s-]", "", card or "")


def check_test_card(card: str) -> str:
    digits = normalize_card(card)
    if digits not in TEST_CARDS:
        raise ConfigError(
            "CARD_NUMBER must be a well-known TEST card number (e.g. 4111 1111 1111 1111). "
            "Real card numbers are refused."
        )
    return digits


def load_settings(env_file: str | Path = ".env", require_credentials: bool = True) -> Settings:
    load_dotenv(env_file, override=False)
    username = os.getenv("DEMOBLAZE_USER", "").strip()
    password = os.getenv("DEMOBLAZE_PASS", "")
    if require_credentials and (not username or not password):
        raise ConfigError(
            "DEMOBLAZE_USER / DEMOBLAZE_PASS are not set. Run `python -m bot signup` "
            "or copy .env.example to .env and fill them in."
        )
    card = check_test_card(os.getenv("CARD_NUMBER", "4111 1111 1111 1111"))
    details = CheckoutDetails(
        name=os.getenv("CARD_NAME", "Demo Buyer"),
        country=os.getenv("CARD_COUNTRY", "Testland"),
        city=os.getenv("CARD_CITY", "Sample City"),
        card=card,
        month=os.getenv("CARD_MONTH", "12"),
        year=os.getenv("CARD_YEAR", "2030"),
    )
    return Settings(
        username=username,
        password=password,
        checkout=details,
        api_base=os.getenv("DEMOBLAZE_API", API_BASE).rstrip("/"),
        web_base=os.getenv("DEMOBLAZE_WEB", WEB_BASE).rstrip("/"),
    )


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Set keys in a .env file, creating it from .env.example when missing."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif Path(".env.example").exists():
        lines = Path(".env.example").read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    pending = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in pending.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
