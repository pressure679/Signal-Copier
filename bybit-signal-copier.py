#!/usr/bin/env python3
"""Telegram -> Bybit USDT perpetual signal copier.

Supports the new signal format:

Pairs: US/USDT 👉 Trade Type = SHORT 🔴 👉 Leverage :- 20x⚡️ Entry = [ 0.0123 TO 0.0123 ]❌ StopLoss :- 0.0127✅ Take profit = [ 0.0121, 0.0119, ... ]

Configuration is intentionally kept outside GitHub:
  pybit-credentials.txt       Bybit API credentials
  telegram-credentials.txt    Telegram API credentials
  telegram-channels.txt       channel IDs/names

Bybit: pybit Unified Trading API, linear (USDT perpetual) contracts.
"""

import asyncio
import hashlib
import os
import re
import time
from collections import OrderedDict
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pybit.unified_trading import HTTP
from telethon import TelegramClient, events

BASE_DIR = Path(__file__).resolve().parent
BYBIT_CREDENTIALS = BASE_DIR / "pybit-credentials.txt"
TELEGRAM_CREDENTIALS = BASE_DIR / "telegram-credentials.txt"
CHANNELS_FILE = BASE_DIR / "telegram-channels.txt"
SESSION_NAME = "signal_copier_bybit"

CATEGORY = "linear"
ACCOUNT_TYPE = "UNIFIED"
RISK_PCT = 0.005          # 0.5% account risk per signal
DEFAULT_LEVERAGE = 20
ENTRY_ORDER_TYPE = "Limit"
ENTRY_TIME_IN_FORCE = "GTC"
POSITION_IDX = 0          # one-way position mode

# The copier will listen to every channel in telegram-channels.txt.
# The new channel can therefore be added without changing this program.


def load_kv_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing credentials/config file: {path}")
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        values[key.strip().lower()] = value.strip().strip('"').strip("'")
    return values


def load_bybit_credentials() -> Tuple[str, str, bool]:
    cfg = load_kv_file(BYBIT_CREDENTIALS)
    key = cfg.get("api_key") or cfg.get("key")
    secret = cfg.get("api_secret") or cfg.get("secret")
    if not key or not secret:
        raise RuntimeError(
            f"{BYBIT_CREDENTIALS} must contain api_key=... and api_secret=..."
        )
    testnet = cfg.get("testnet", "false").lower() in {"1", "true", "yes", "on"}
    return key, secret, testnet


def load_telegram_credentials() -> Tuple[int, str, Optional[str]]:
    cfg = load_kv_file(TELEGRAM_CREDENTIALS)
    api_id = cfg.get("api_id") or os.getenv("TELEGRAM_API_ID")
    api_hash = cfg.get("api_hash") or os.getenv("TELEGRAM_API_HASH")
    phone = cfg.get("phone") or os.getenv("TELEGRAM_PHONE")
    if not api_id or not api_hash:
        raise RuntimeError(
            f"{TELEGRAM_CREDENTIALS} must contain api_id=... and api_hash=..."
        )
    return int(api_id), api_hash, phone


def load_channels() -> Dict[int, str]:
    if not CHANNELS_FILE.exists():
        raise FileNotFoundError(
            f"Missing {CHANNELS_FILE}. Run telegram-channel-id.py first or create it."
        )
    channels: Dict[int, str] = {}
    for raw in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        channel_id, name = line.split("=", 1)
        try:
            # Telegram channel IDs are commonly stored without the -100 prefix.
            n = int(channel_id.strip())
            if n > 0 and n < 10**12:
                n = -1000000000000 + n
            channels[n] = name.strip() or str(n)
        except ValueError:
            continue
    if not channels:
        raise RuntimeError(f"No channels configured in {CHANNELS_FILE}")
    return channels


def normalize_symbol(raw: str) -> str:
    s = raw.upper().strip()
    s = s.replace(" ", "")
    s = s.replace("/", "")
    s = s.replace("-", "")
    return s


def extract_numbers(text: str) -> List[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]


def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    compact = " ".join(text.replace("\u00a0", " ").split())
    pair_m = re.search(r"Pairs?\s*:\s*([A-Za-z0-9._/-]+)", compact, re.I)
    side_m = re.search(r"Trade\s*Type\s*=\s*(LONG|SHORT|BUY|SELL)", compact, re.I)
    lev_m = re.search(r"Leverage\s*[:-]+\s*(\d+(?:\.\d+)?)\s*x", compact, re.I)
    entry_m = re.search(r"Entry\s*=\s*\[\s*([0-9.]+)\s*(?:TO|-)\s*([0-9.]+)\s*\]", compact, re.I)
    sl_m = re.search(r"Stop\s*Loss\s*[:-]+\s*([0-9.]+)", compact, re.I)
    tp_m = re.search(r"Take\s*profit\s*=\s*\[\s*([^\]]+)\]", compact, re.I)

    if not (pair_m and side_m and entry_m and sl_m and tp_m):
        return None

    symbol = normalize_symbol(pair_m.group(1))
    side_raw = side_m.group(1).upper()
    side = "Buy" if side_raw in {"BUY", "LONG"} else "Sell"
    leverage = float(lev_m.group(1)) if lev_m else DEFAULT_LEVERAGE
    entry_low = float(entry_m.group(1))
    entry_high = float(entry_m.group(2))
    stop_loss = float(sl_m.group(1))
    take_profits = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", tp_m.group(1))]
    if not take_profits:
        return None

    return {
        "symbol": symbol,
        "side": side,
        "leverage": leverage,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
    }


def decimal_places(step: str) -> int:
    d = Decimal(str(step))
    return max(0, -d.as_tuple().exponent)


def floor_to_step(value: float, step: str) -> str:
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    result = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
    return format(result, "f")


def price_to_tick(value: float, tick_size: str) -> str:
    d_value = Decimal(str(value))
    d_tick = Decimal(str(tick_size))
    result = (d_value / d_tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * d_tick
    places = decimal_places(tick_size)
    return f"{result:.{places}f}"


class BybitExecutor:
    def __init__(self) -> None:
        api_key, api_secret, testnet = load_bybit_credentials()
        self.testnet = testnet
        self.session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        self.instrument_cache: Dict[str, Dict[str, str]] = {}

    def instrument(self, symbol: str) -> Dict[str, str]:
        if symbol in self.instrument_cache:
            return self.instrument_cache[symbol]
        response = self.session.get_instruments_info(category=CATEGORY, symbol=symbol)
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise RuntimeError(f"Bybit linear symbol not found: {symbol}")
        info = rows[0]
        lot = info.get("lotSizeFilter", {})
        price = info.get("priceFilter", {})
        result = {
            "qty_step": lot.get("qtyStep", "1"),
            "min_qty": lot.get("minOrderQty", "0"),
            "max_qty": lot.get("maxOrderQty", "999999999"),
            "tick_size": price.get("tickSize", "0.00000001"),
        }
        self.instrument_cache[symbol] = result
        return result

    def balance(self) -> float:
        response = self.session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise RuntimeError("Could not read Bybit unified wallet balance")
        equity = rows[0].get("totalEquity")
        if not equity:
            raise RuntimeError("Bybit returned no totalEquity")
        return float(equity)

    def set_leverage(self, symbol: str, leverage: float) -> None:
        lev = str(int(leverage)) if float(leverage).is_integer() else str(leverage)
        try:
            self.session.set_leverage(
                category=CATEGORY,
                symbol=symbol,
                buyLeverage=lev,
                sellLeverage=lev,
            )
        except Exception as exc:
            # Bybit can reject this when the requested leverage is already set.
            msg = str(exc).lower()
            if "leverage not modified" not in msg and "same" not in msg:
                raise

    def calculate_qty(self, symbol: str, entry: float, sl: float) -> str:
        distance = abs(entry - sl)
        if distance <= 0:
            raise ValueError("Entry and stop loss must be different")
        risk_usdt = self.balance() * RISK_PCT
        qty = risk_usdt / distance
        info = self.instrument(symbol)
        qty_s = floor_to_step(qty, info["qty_step"])
        if float(qty_s) < float(info["min_qty"]):
            raise ValueError(
                f"Calculated quantity {qty_s} is below Bybit minimum {info['min_qty']}"
            )
        if float(qty_s) > float(info["max_qty"]):
            qty_s = info["max_qty"]
        return qty_s

    def place_entry(
        self,
        symbol: str,
        side: str,
        qty: str,
        price: str,
        tp: str,
        sl: str,
        link_id: str,
    ) -> Dict[str, Any]:
        return self.session.place_order(
            category=CATEGORY,
            symbol=symbol,
            side=side,
            orderType=ENTRY_ORDER_TYPE,
            qty=qty,
            price=price,
            timeInForce=ENTRY_TIME_IN_FORCE,
            positionIdx=POSITION_IDX,
            orderLinkId=link_id[:36],
            takeProfit=tp,
            stopLoss=sl,
            tpslMode="Partial",
            tpOrderType="Limit",
            slOrderType="Market",
            tpLimitPrice=tp,
        )

    def copy_signal(self, signal: Dict[str, Any], message_id: int, provider: str) -> None:
        symbol = signal["symbol"]
        side = signal["side"]
        entry_low = signal["entry_low"]
        entry_high = signal["entry_high"]
        sl = signal["stop_loss"]
        tps = signal["take_profits"]
        leverage = signal["leverage"]
        info = self.instrument(symbol)

        # A signal entry range is split between the two endpoints when they differ.
        # With equal endpoints (the supplied format) this produces one entry price.
        if abs(entry_low - entry_high) <= float(info["tick_size"]) / 2:
            entries = [(entry_low, 1.0)]
        else:
            entries = [(entry_low, 0.5), (entry_high, 0.5)]

        self.set_leverage(symbol, leverage)

        total_qty = self.calculate_qty(symbol, (entry_low + entry_high) / 2, sl)
        qty_step = info["qty_step"]
        total_dec = Decimal(total_qty)

        print(
            f"[{provider}] {side.upper()} {symbol} leverage={leverage} "
            f"risk={RISK_PCT:.2%} total_qty={total_qty} entry={entry_low}..{entry_high}"
        )

        # Split the risk equally across all TP targets and entry endpoints.
        pieces = len(tps) * len(entries)
        base_piece = total_dec / Decimal(str(pieces))
        placed = 0
        for entry, entry_weight in entries:
            entry_price = price_to_tick(entry, info["tick_size"])
            for tp in tps:
                qty = floor_to_step(float(base_piece), qty_step)
                if float(qty) < float(info["min_qty"]):
                    print(f"[{provider}] Skipping TP {tp}: split qty {qty} below minimum")
                    continue
                tp_price = price_to_tick(tp, info["tick_size"])
                sl_price = price_to_tick(sl, info["tick_size"])
                link = f"sc-{message_id}-{placed}"
                response = self.place_entry(
                    symbol, side, qty, entry_price, tp_price, sl_price, link
                )
                print(
                    f"[{provider}] order {placed + 1}/{pieces}: entry={entry_price} "
                    f"qty={qty} TP={tp_price} SL={sl_price} -> {response.get('retCode')}"
                )
                if response.get("retCode") != 0:
                    raise RuntimeError(
                        f"Bybit rejected order: {response.get('retMsg', response)}"
                    )
                placed += 1

        if not placed:
            raise RuntimeError("No Bybit orders were placed")


# Telegram processing -------------------------------------------------------
api_id, api_hash, phone = load_telegram_credentials()
CHANNELS = load_channels()
BYBIT = BybitExecutor()

client = TelegramClient(
    SESSION_NAME,
    api_id,
    api_hash,
    connection_retries=10,
    request_retries=5,
)
seen_messages: OrderedDict[Tuple[int, int], str] = OrderedDict()


@client.on(events.NewMessage(chats=list(CHANNELS.keys())))
@client.on(events.MessageEdited(chats=list(CHANNELS.keys())))
async def on_signal(event) -> None:
    text = (event.message.message or "").strip()
    if not text:
        return
    key = (int(event.chat_id), int(event.message.id))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if seen_messages.get(key) == digest:
        return
    seen_messages[key] = digest
    seen_messages.move_to_end(key)
    while len(seen_messages) > 20000:
        seen_messages.popitem(last=False)

    provider = CHANNELS.get(int(event.chat_id), str(event.chat_id))
    parsed = parse_signal(text)
    if not parsed:
        print(f"[{provider}] Ignored non-signal message: {text[:250]}")
        return

    try:
        await asyncio.to_thread(
            BYBIT.copy_signal,
            parsed,
            int(event.message.id),
            provider,
        )
    except Exception as exc:
        print(f"[{provider}] ERROR copying message {event.message.id}: {exc}")


async def main() -> None:
    print("Bybit Telegram signal copier")
    print(f"Bybit testnet: {BYBIT.testnet}")
    print("Listening to:")
    for channel_id, name in CHANNELS.items():
        print(f"  {channel_id}: {name}")
    await client.start(phone=phone)
    await client.run_until_disconnected()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Stopped by user")
            break
        except Exception as exc:
            print(f"Runtime error: {exc}; restarting in 5 seconds")
            time.sleep(5)
