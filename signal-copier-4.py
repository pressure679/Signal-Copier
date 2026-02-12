#!/usr/bin/env python3
"""
signal-copier-from-scratch.py

Minimal focused Telegram -> MT5 Signal Copier for two channels.

Features implemented:
- Listens to three Telegram channels:
    * -1001914952549  (King SMC Paid)
    * -1002095470635  (SMC GOLD EXPERT Paid)
    * -1003465604702  (CRT Signals Team)
    * -1002529563894  (Forex Gold Expert ( Paid ))
    * -1002143819549  (SMC XAUUSD(GOLD)(PAID))
    * -1002469668344  (SMC TradeZone)
    * -1003408288564  (SMC Gold Special)
- Parses signal formats described by user for all channels.
- Places market orders (BUY NOW / SELL NOW) and pending limit orders (BUY LIMIT / SELL LIMIT).
- Uses SL from signal message as the initial SL when placing orders.
- Supports single TP (or multiple TP lines) and places TP as the order TP.
- Stores a mapping from provider message id -> orders/tickets so reply actions can reference them.
- Supports reply commands:
    * "Close" / "Close the trade" (reply to provider message) — closes open positions placed by that signal
    * "Delete this Limit" / "Order Delete" (reply) — attempts to remove pending orders created for that signal
    * "Use breakeven" / "Use BE" / "Use Breakeven" / "Use BE" (reply) / "Use break-even" / "sl move on entry point" — move SL to entry for positions of that signal
    * "Half close" / "Half lots close" - Close half of the trade
    * "Cancel this" - 
- Auto-detects MT5 symbol names (matches prefix like XAUUSD to XAUUSD+, XAUUSD.s, etc)
- Adds the provider message id to the order comment so mapping across restarts is easier to implement later.
- **TEST ON DEMO FIRST**. Some MT5 calls (especially removing pending orders) may require broker-specific parameters.

Limitations & notes:
- This is intentionally focused: it does NOT implement TP watching, trailing SL, scaling, or advanced grid logic.
- Deleting pending orders uses TRADE_ACTION_REMOVE with 'order' field — some brokers might need adjustments.
- The script prints actions to stdout; consider adding logging to file later.
"""

import re
import time
import asyncio
from typing import Optional, Dict, Any, List
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from collections import defaultdict, OrderedDict
import math
import hashlib
import requests
from bs4 import BeautifulSoup

try:
    import MetaTrader5 as mt5
except Exception as e:
    print("MetaTrader5 import failed:", e)
    raise

try:
    from telethon import TelegramClient, events
except Exception as e:
    print("Telethon import failed:", e)
    raise

# -------------------- CONFIG --------------------
API_ID = 
API_HASH = ""
PHONE = ""

# Channel IDs
KING_SMC_ID = -1001914952549          # 𝙆𝙞𝙣𝙜 𝙎𝙈𝘾 (🄿🄰🄸🄳 )
SMC_GOLD_ID = -1002095470635          # SMC GOLD EXPERT (Paid)
CRT_Signals_Team_ID = -1003465604702  # CRT Signals Team(Vip)
Forex_Gold_Expert_ID = -1002529563894 # Forex Gold Expert ( Paid )
SMC_XAUUSD_ID = -1002143819549        # SMC XAUUSD(GOLD)(PAID)
SMC_TZ_ID = -1002469668344
SMC_Gold_Special_ID = -1003408288564
# Signal_Test_ID = -1003600107157     # Test channel

# VIP_CHANNELS = [KING_SMC_ID, SMC_GOLD_ID, Forex_Gold_Expert_ID, CRT_Signals_Team_ID]
VIP_CHANNELS = [KING_SMC_ID]
provider_name = ["King SMC", "SMC Gold Expert", "Forex Gold Expert", "CRT Signals Team", "SMC XAUUSD(GOLD)", "SMC TZ", "SMC Gold Special"]
channel_id_provider_name_map = {
    1914952549: "King SMC",
    2095470635: "SMC Gold Expert",
    2529563894: "Forex Gold Exper",
    3465604702: "CRT Signals Team",
    2143819549: "SMC XAUUSD",
    2469668344: "SMC TZ",
    3408288564: "SMC Gold Special"
    # 3600107157: "Signal Test"
}

mt5.initialize()

MAGIC = 987654321  # magic number for orders
RISK_PCT = 0.01   # 1.0% risk
MIN_LOT = 0.01
DEFAULT_MAX_LOT = 100.0

# Handler messages RE
CLOSE_CMD_RE = re.compile(r'\bclose\b', re.IGNORECASE)
CLOSEn_CMD_RE = re.compile(r'\b(close the trade|trade close|closed|close)\b', re.IGNORECASE)
DELETE_LIMIT_RE = re.compile(r'\b(delete this limit|order delete|delete order|cancel this|delete limit|delete this|delete buy limit|delete sell limit|delete)\b', re.IGNORECASE)
USE_BE_RE = re.compile(r'\b(sl move to entry point|sl move on entry point|use breakeven|use be|use break-even|use break even|set breakeven)\b', re.IGNORECASE)
HALF_CLOSE_RE = re.compile(r'\b(half close|half lots close|haff close)\b', re.IGNORECASE)

SYMBOL_EXTRACT_RE = re.compile(r'#([A-Z]{3,6})', re.IGNORECASE)

def extract_symbol_from_text(text: str):
    m = SYMBOL_EXTRACT_RE.search(text)
    if not m:
        return None
    return m.group(1).upper()

def _extract_float(line: str) -> Optional[float]:
    """
    Extract the LAST float-like number from a line.
    Handles:
      TP1. 4460.000
      TP 2. 4478.919
      SL. 4417.910
      TP : 2,020.11
    """
    # Normalize commas (European formatting safety)
    clean = line.replace(",", "")

    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", clean)
    if not matches:
        return None

    try:
        return float(matches[-1])  # ✅ LAST number
    except ValueError:
        return None

def decimals_count(x) -> int:
    s = str(x)
    return len(s.split(".")[1]) if "." in s else 0

# -------------------- Utilities --------------------
def find_mt5_symbol(symbol_short: str) -> Optional[str]:
    if not symbol_short:
        return None
    s = symbol_short.strip().upper()
    # direct lookup
    info = mt5.symbol_info(s)
    if info is not None:
        return s
    # try to find a symbol starting with s
    try:
        syms = mt5.symbols_get()
    except Exception:
        syms = []
    for sym in syms:
        name = getattr(sym, "name", None) or getattr(sym, "symbol", None) or ""
        if not name:
            continue
        if name.upper() == s:
            return name
    for sym in syms:
        name = getattr(sym, "name", None) or getattr(sym, "symbol", None) or ""
        if name and name.upper().startswith(s):
            return name
    return s  # fallback

def account_balance() -> float:
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("Cannot fetch account info")
    return float(info.balance)

def risk_per_lot(symbol: str, sl_price_diff: float) -> float:
    info = mt5.symbol_info(symbol)
    ticks = sl_price_diff / info.trade_tick_size
    return ticks * info.trade_tick_value

# -------------------- MT5 order helpers --------------------
def place_market_order(symbol: str=None, direction: str=None, volume: float=None, sl: float=None, tp: float=None, comment: str=None):
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError("No tick for symbol " + symbol)
    price = float(tick.ask if direction == "BUY" else tick.bid)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl or 0.0,
        "tp": tp or 0.0,
        "deviation": 50,
        "magic": MAGIC,
        "comment": comment,
        "type_filling": mt5.ORDER_FILLING_IOC
    }
    result = mt5.order_send(req)
    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
        print(
            "Failed to place market order for",
            symbol,
            "retcode:",
            getattr(result, "retcode", None),
        )
    return result

def place_pending_limit(symbol: str=None, direction: str=None, price: float=None, volume: float=None, sl: float=None, tp: float=None, comment: str=None):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print("No tick data")
        return None

    if direction == "BUY" and price >= tick.ask:
        print("Invalid BUY LIMIT price:", price, "ask:", tick.ask + ", price >= tick.ask")
        return None

    if direction == "SELL" and price <= tick.bid:
        print("Invalid SELL LIMIT price:", price, "bid:", tick.bid + ", price <= tick.bid")
        return None
    order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl or 0.0,
        "tp": tp or 0.0,
        # "deviation": 50,
        "magic": MAGIC,
        "comment": comment
        # "type_filling": mt5.ORDER_FILLING_FOK
        # "type_filling": getattr(mt5, "ORDER_FILLING_RETURN", 2)
    }
    res = mt5.order_send(req)
    if res is None:
        print("LIMIT order_send returned None")
        return None

    # print("LIMIT retcode:", res.retcode, res.comment)

    if res.retcode != mt5.TRADE_RETCODE_DONE:
        print("LIMIT order failed:", res.retcode, res.comment)
        return None

    return res

def close_position_by_id(ticket):
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        print("[CLOSE] Position not found:", ticket)
        return False

    pos = pos[0]

    # 🔒 Safety: only close bot positions
    if pos.magic != MAGIC:
        return False

    order_type = (
        mt5.ORDER_TYPE_SELL
        if pos.type == mt5.POSITION_TYPE_BUY
        else mt5.ORDER_TYPE_BUY
    )

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "position": ticket,
        "volume": pos.volume,
        "type": order_type,
        "magic": MAGIC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(req)

    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
        print(
            "[CLOSE] Failed to close position",
            ticket,
            "retcode:",
            getattr(result, "retcode", None),
        )
        return False

    # print("[CLOSE] Closed position:", ticket)
    return True

def delete_specific_pending_for_symbol(symbol: str, provider: str, entry: float=None, sl: float=None, tp: float=None):
    # scan pending orders and remove those matching our magic and comment containing provider_msg_id
    pend = []
    try:
        pend = mt5.orders_get(symbol=symbol)
    except Exception as e:
        print(f"[{provider}][Delete limit] orders_get error:", e)
    removed = 0
    for p in pend:
        try:
            # match by magic or by comment containing provider id
            if getattr(p, "magic", None) == MAGIC and p.comment == provider and p.price == entry and p.sl == sl and p.tp == tp:
                ticket = getattr(p, "ticket", None) or getattr(p, "order", None)
                if ticket:
                    result = remove_pending_order(int(ticket))
                    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                        print(
                            f"[{provider}][Delete limit] Failed to remove limit for ticket",
                            ticket,
                            "retcode:",
                            getattr(result, "retcode", None),
                        )
                    print(f"[{provider}][Delete limit] Removed pending order", ticket, "->", res)
                    removed += 1
        except Exception as e:
            print(f"[{provider}][Delete limit] Error removing pending:", e)
    if removed == 0:
        print(f"[{provider}][Delete limit] No pending order removed")

def delete_pending_for_symbol(symbol: str, provider: str):
    # scan pending orders and remove those matching our magic and comment containing provider_msg_id
    try:
        pend = mt5.orders_get(symbol=symbol)
    except Exception as e:
        print("orders_get error:", e)
        pend = []
    removed = 0
    for p in pend:
        try:
            # match by magic or by comment containing provider id
            if getattr(p, "magic", None) == MAGIC and p.comment == provider:
                ticket = getattr(p, "ticket", None) or getattr(p, "order", None)
                if ticket:
                    result = remove_pending_order(int(ticket))
                    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                        print(
                            "Failed to remove limit for ticket",
                            ticket,
                            "retcode:",
                            getattr(result, "retcode", None),
                        )
                    print("Removed pending order", ticket, "->", res)
                    removed += 1
        except Exception as e:
            print("Error removing pending:", e)
    if removed == 0:
        print("No pending orders removed for King SMC")

def close_positions_for_symbol(symbol: str, provider: str) -> bool:
    print(f"[{provider}][CLOSE] Processing close positions for {symbol}")

    positions = mt5.positions_get()
    if not positions:
        print(f"[{provider}][CLOSE] No open positions for {symbol}")
        return

    for p in positions:
        # Restrict to your bot's trades
        if p.magic != 987654321:
            continue

        # Restrict to symbol only
        if not symbol == p.symbol:
            continue

        if not p.comment == provider:
            continue

        entry_price = p.price_open
        ticket = p.ticket   # ✅ THIS is the position id

        order_type = (
            mt5.ORDER_TYPE_SELL
            if p.type == mt5.POSITION_TYPE_BUY
            else mt5.ORDER_TYPE_BUY
        )

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "position": ticket,
            "volume": p.volume,
            "type": order_type,
            "magic": MAGIC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(req)

        if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
            print(
                f"[{provider}][CLOSE] Failed to close position for {symbol}",
                ticket,
                "retcode:",
                getattr(result, "retcode", None),
            )
            return

        print(f"[{provider}][CLOSE] Closed {symbol} position: {ticket}")
    return

def close_specific_position_for_symbol(symbol: str, provider: str, tp: float=None):
    # scan pending orders and remove those matching our magic and comment containing provider_msg_id
    positions = []
    try:
        positions = mt5.positions_get(symbol=symbol)
    except Exception as e:
        print(f"[{provider}][Close] positions_get error:", e)
    removed = 0
    for p in positions:
        try:
            # match by magic or by comment containing provider id
            if getattr(p, "magic", None) == MAGIC and p.comment == provider and p.tp == tp and p.symbol == symbol:
                # ticket = getattr(p, "ticket", None) or getattr(p, "order", None)
                ticket = (
                    getattr(p, "ticket", None)
                    or getattr(p, "position", None)
                    or getattr(p, "deal", None)
                    or getattr(p, "order", None)
                )
                if ticket:
                    result = close_position_by_id(int(ticket))
                    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                        print(
                            f"[{provider}][Close] Failed to close position for ticket",
                            ticket,
                            "retcode:",
                            getattr(result, "retcode", None),
                        )
                    print(f"[{provider}][Close] Closed position", ticket, "->", res)
                    removed += 1
        except Exception as e:
            print(f"[{provider}][Close] Error closing position:", e)
    if removed == 0:
        print(f"[{provider}][Close] No positions closed for SMC XAUUSD")

def update_sl(ticket, symbol, sl_price, tp_price):
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol,
        "sl": sl_price,
        "tp": tp_price,
        # "tp": 0.0,  # no TP used
        "magic": 987654321,
    }

    result = mt5.order_send(req)
    if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
        print(
            "Failed to update sl for ",
            symbol,
            "retcode:",
            getattr(result, "retcode", None),
        )

def move_sl_to_entry(symbol: str, provider: str, entry: float=None):
    print(f"[{provider}][BE] Processing move SL to entry for {symbol}")

    positions = mt5.positions_get()
    if not positions:
        print(f"[{provider}][BE] No open positions for {symbol}")
        return

    moved = 0

    for p in positions:
        # 🔒 Only bot trades
        if p.magic != 987654321:
            continue

        # 🔒 Provider-specific filtering
        # King SMC / Forex providers
        if p.symbol != symbol:
            continue

        if p.comment != provider:
            continue

        entry_price = p.price_open
        # ticket = p.ticket
        if entry:
            if entry_price != entry:
                continue

        # Skip if SL already at (or beyond) entry
        if p.sl == entry_price:
            continue

        print(
            f"[{provider}][BE] Moving SL → entry {entry_price} "
            f"for {p.symbol}, ticket {p.ticket}"
        )

        update_sl(p.ticket, p.symbol, p.price_open, p.tp)
        moved += 1

    if moved == 0:
        print(f"[{provider}][BE] No eligible positions found for {symbol}")

def _strip_parentheses(s: str) -> str:
    out = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


# -------------------- Parsing signal --------------------
def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    # raw = _clean_text(text)
    lines_all = [ln.strip() for ln in text.split("\n") if ln.strip()]

    if not lines_all:
        return None

    # 1) Remove TradingView / http lines completely
    lines: List[str] = []
    for ln in lines_all:
        up = ln.lower()
        if up.startswith("http://") or up.startswith("https://") or "tradingview.com/x/" in up:
            continue
        if ln == "":
            continue
        lines.append(ln)

    if not lines:
        return None

    # 2) Find symbol line (first starting with '#', else first token)
    symbol = None
    start_idx = 0
    for idx, ln in enumerate(lines):
        m = re.search(r"#\s*([A-Za-z]{3,10})", ln)
        if m:
            sym_token = m.group(1).upper()
            if sym_token in ("GOLD", "XAU"):
                symbol = "XAUUSD"
            elif sym_token in ("USOIL"):
                symbol = "USOUSD"
            elif sym_token == "BTCUSD":
                return None
            else:
                symbol = sym_token
            start_idx = idx + 1
            break

    if not symbol:
        # fallback: treat first line as symbol-ish
        m2 = re.search(r"([A-Za-z]{3,10})", lines[0])
        if not m2:
            return None
        sym_token = m2.group(1).upper()
        if sym_token in ("GOLD", "XAU"):
            symbol = "XAUUSD"
        elif sym_token in ("USOIL"):
            symbol = "USOUSD"
        elif sym_token == "BTCUSD":
            return None
        else:
            symbol = sym_token
        start_idx = 1

    direction = None   # "BUY"/"SELL"
    mode = None        # "LIMIT"/"MARKET"
    entry = None
    sl = None
    tp = None

    # 3) Parse for direction/mode/entry/SL/TP
    for ln in lines:
        ln_u = ln.upper()

        # Direction + mode + entry line, e.g. "SELL Limit :2.02011"
        if "BUY" in ln_u or "SELL" in ln_u:
            if "BUY" in ln_u:
                direction = "BUY"
            if "SELL" in ln_u or "SEL" in ln_u:
                direction = "SELL"

            if "LIMIT" in ln_u or "LIMTE" in ln_u or "LIMITE" in ln_u:
                mode = "LIMIT"
            elif "NOW" in ln_u or "MARKET" in ln_u:
                mode = "MARKET"

            val = _extract_float(ln)
            if symbol == "XAUUSD" and decimals_count(val) > 2:
                val = float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if val is not None:
                entry = val
            continue

        # SL line regardless of colon spacing
        if "SL" in ln_u:
            clean_ln = _strip_parentheses(ln)
            val = _extract_float(clean_ln)
            if symbol == "XAUUSD" and decimals_count(val) > 2:
                val = float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if val is not None:
                sl = val
            continue

        # TP line(s)
        if "TP" in ln_u:
            clean_ln = _strip_parentheses(ln)
            val = _extract_float(clean_ln)
            if symbol == "XAUUSD" and decimals_count(val) > 2:
                val = float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if val is not None:
                tp = val
                # print(f"[DEBUG] TP found: f{tp}")
            continue

    # LIMIT but no entry? Try any number in remaining lines
    if mode == "LIMIT" and entry is None:
        val = None
        for ln in lines:
            ln_u = ln.upper()
            if "BUY" in ln_u or "SELL" in ln_u or "LIMIT" in ln_u or "LIMTE" in ln_u or "LIMITE" in ln_u:
                val = _extract_float(ln)
            if symbol == "XAUUSD" and decimals_count(val) > 2:
                val = float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if val is not None:
                entry = val
                break

    if not direction or not sl or not tp or not mode:
        """
        print("[DEBUG PARSE]",
            "symbol=", symbol,
            "direction=", direction,
            "mode=", mode,
            "entry=", entry,
            "sl=", sl,
            "tp=", tp)
        """
        return None

    return {
        "symbol": symbol + "+",
        "direction": direction,
        "mode": mode,
        "entry": entry,
        "sl": sl,
        "tp": tp,
    }

def extract_tv_url(text):
    m = re.search(r"https://www\.tradingview\.com/x/\w+/?", text)
    return m.group(0) if m else None

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

"""
SYMBOL_RE = re.compile(r"^[A-Z0-9:.]{3,20}$")
def get_symbol_from_tv_share(url):
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.title.string if soup.title else ""
    title = title.strip()

    # Try: SYMBOL: description
    if ":" in title:
        candidate = title.split(":")[1].strip()
        if SYMBOL_RE.match(candidate):
            # return candidate.split(".")[0]
            return candidate

    # Fallback: scan whole title
    # tokens = re.findall(r"[A-Z]{3,10}[A-Z0-9]{0,5}", title)
    # if tokens:
    #     return tokens[1]

    return None
"""
SYMBOL_RE = re.compile(r"^[A-Z]{3,10}[A-Z0-9]{0,5}$")

def get_symbol_from_tv_share(url):
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.title.string.strip() if soup.title else ""

    parts = [p.strip() for p in re.split(r"[:·—\-]", title)]

    for p in parts:
        token = p.split()[0].upper()

        # if token == "TRADINGVIEW":
        #     continue

        if SYMBOL_RE.match(token):
            return token

    return None

# -------------------- Telegram handlers --------------------
client = TelegramClient("smc_signal_copier_session", API_ID, API_HASH, connection_retries=10, request_retries=5)
SMC_TZ_last_opened_trade = None

seen_messages = OrderedDict()
@client.on(events.MessageEdited(chats=VIP_CHANNELS))
@client.on(events.NewMessage(chats=VIP_CHANNELS))
async def process_provider_message(event):
    msg = event.message
    chat_id = event.chat_id
    msg_id = msg.id

    text = (msg.message or "").strip()
    if not text:
        return

    text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    key = (chat_id, msg_id)

    # If we've already seen this exact content → ignore
    if key in seen_messages and seen_messages[key] == text_hash:
        return

    # Store / update latest version
    seen_messages[key] = text_hash
    seen_messages.move_to_end(key)

    # LRU eviction
    if len(seen_messages) > 20_000:
        seen_messages.popitem(last=False)

    # ---- REAL PROCESSING STARTS HERE ----
    is_management = False
    text = event.message.message or ""
    text = text.strip()

    if not text:
        print("Empty message text - nothing to parse.")
        return
    
    if event.chat.id == 1914952549:
        m = re.search(r"#\s*([A-Za-z]{3,10})", text)
        if m:
            sym_token = m.group(1).upper()
            if sym_token in ("GOLD", "XAU"):
                return

    parsed = None

    # SMC Gold Special
    if event.chat.id == 3408288564:
        parsed = parse_signal(text)
    # SMC TradeZone
    if event.chat.id == 2469668344:
        if USE_BE_RE.search(text):
            is_management = True
            # replied = await client.get_messages(event.chat_id, ids=reply_to)
            # parsed = parse_signal(replied.text)
            symbol = extract_symbol_from_text(text)
            # url = extract_tv_url(text)
            # symbol = get_symbol_from_tv_share(url)
            if symbol:
                move_sl_to_entry(symbol + "+", channel_id_provider_name_map[event.chat.id])
            else:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][Use BE] Couldn't extract symbol from text:", text)
            # return
        if CLOSEn_CMD_RE.search(text):
            is_management = True
            # url = extract_tv_url(text)
            # symbol = get_symbol_from_tv_share(url)
            symbol = extract_symbol_from_text(text)
            close_positions_for_symbol(symbol + "+", channel_id_provider_name_map[event.chat.id])
            # print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Trade closed")
        if DELETE_LIMIT_RE.search(text):
            is_management = True
            url = extract_tv_url(text)
            symbol = None
            if url:
                symbol = get_symbol_from_tv_share(url)
            else:
                symbol = extract_symbol_from_text(text)
            if not symbol:
            # if symbol:
            #     sym_token = m.group(1).upper()
            #     if sym_token in ("GOLD", "XAU"):
            #         symbol = "XAUUSD" + "+"
            #     else:
            #         symbol = sym_token + "+"
                replied = await event.get_reply_message()
                if replied:
                    parsed = parse_signal(replied.text)
                    symbol = parsed.get("symbol")
            delete_pending_for_symbol(symbol + "+", channel_id_provider_name_map[event.chat.id])
                # print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Reply message not found")
                # return
            # parsed = parse_signal(replied.text)
            print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Deleted limit for {symbol}+")
            # return
        parsed = parse_signal(text)
    # Forex Gold Expert
    if event.chat.id == 2529563894:
        if CLOSEn_CMD_RE.search(text):
            is_management = True
            close_positions_for_symbol("XAUUSD+", channel_id_provider_name_map[event.chat.id])
            # print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Trade closed")
        if HALF_CLOSE_RE.search(text):
            is_management = True
            # get LIVE MT5 positions
            positions = [
                p for p in (mt5.positions_get() or [])
                if p.symbol.startswith("XAU") and p.magic == MAGIC and p.comment == channel_id_provider_name_map[event.chat.id]
            ]

            if len(positions) < 2:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Not enough positions to half close")
                # return

            # 3️⃣ Close ONE position (e.g. oldest)
            if len(positions) == 2:
                pos_to_close = positions[0].ticket

                if close_position_by_id(pos_to_close):
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Half closed:", pos_to_close)
                else:
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Failed to half close:", pos_to_close)
            # return 
        if USE_BE_RE.search(text):
            is_management = True
            move_sl_to_entry("XAUUSD+", channel_id_provider_name_map[event.chat.id])
            # return 
        parsed = parse_signal(text)
    # SMC XAUUSD
    if event.chat.id == 2143819549:
        if DELETE_LIMIT_RE.search(text):
            is_management = True
            # reply_to = getattr(event.message, "reply_to_msg_id", None)
            # if not reply_to:
            #     print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Close command with no reply — ignoring.")
            #     # return
            # replied = await client.get_messages(event.chat_id, ids=reply_to)
            replied = await event.get_reply_message()
            if not replied:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Reply message not found")
                # return
            parsed = parse_signal(replied.text)
            delete_specific_pending_for_symbol("XAUUSD+", channel_id_provider_name_map[event.chat.id], entry=parsed.get("entry"), sl=parsed.get("sl"), tp=parsed.get("tp"))
            print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Pending limit(s) closed")
        if CLOSEn_CMD_RE.search(text):
            is_management = True
            # reply_to = getattr(event.message, "reply_to_msg_id", None)
            # if not reply_to:
            #     print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Close command with no reply — ignoring.")
            #     # return
            # replied = await client.get_messages(event.chat_id, ids=reply_to)
            replied = await event.get_reply_message()
            if not replied:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Reply message not found")
                # return
            parsed = parse_signal(replied.text)
            # symbol = extract_symbol_from_text(replied.text) + "+"
            close_specific_position_for_symbol(parsed.get("symbol"), channel_id_provider_name_map[event.chat.id], tp=parsed.get("tp"))
            print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Position closed")
        if HALF_CLOSE_RE.search(text):
            is_management = True
            # get LIVE MT5 positions
            # replied = await client.get_messages(event.chat_id, ids=reply_to)
            replied = await event.get_reply_message()
            if not replied:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Reply message not found")
                return
            parsed = parse_signal(replied)
            positions = [
                p for p in (mt5.positions_get() or [])
                if p.symbol == parsed.get("symbol") and p.magic == MAGIC and p.comment == channel_id_provider_name_map[event.chat.id] and p.price == parsed.get("entry") and p.tp == parsed.get("tp")
            ]

            if len(positions) < 2:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Not enough positions to half close")
                # return

            # 3️⃣ Close ONE position (e.g. oldest)
            if len(positions) == 2:
                pos_to_close = positions[0].ticket

                if close_position_by_id(pos_to_close):
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Half closed:", pos_to_close)
                else:
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Failed to half close:", pos_to_close)
            # return
        if USE_BE_RE.search(text):
            is_management = True
            replied = await client.get_messages(event.chat_id, ids=reply_to)
            parsed = parse_signal(replied.text)
            move_sl_to_entry(parsed.get("symbol"), channel_id_provider_name_map[event.chat.id])
            # return
        parsed = parse_signal(text)
    # CRT Signals Team
    if event.chat.id == 3465604702:
        if DELETE_LIMIT_RE.search(text):
            is_management = True
            delete_pending_for_symbol("XAUUSD+", channel_id_provider_name_map[event.chat.id])
            print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Pending limit(s) closed")
        if CLOSEn_CMD_RE.search(text):
            is_management = True
            close_positions_for_symbol("XAUUSD+", channel_id_provider_name_map[event.chat.id])
            # print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Trade closed")
        if HALF_CLOSE_RE.search(text):
            is_management = True
            # get LIVE MT5 positions
            positions = [
                p for p in (mt5.positions_get() or [])
                if p.symbol.startswith("XAU") and p.magic == MAGIC and p.comment == channel_id_provider_name_map[event.chat.id]
            ]

            if len(positions) < 2:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Not enough positions to half close")
                # return

            # 3️⃣ Close ONE position (e.g. oldest)
            if len(positions) == 2:
                pos_to_close = positions[0].ticket

                if close_position_by_id(pos_to_close):
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Half closed:", pos_to_close)
                else:
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Failed to half close:", pos_to_close)

            # return

        if USE_BE_RE.search(text):
            is_management = True
            move_sl_to_entry("XAUUSD+", channel_id_provider_name_map[event.chat.id])
            # return 
        parsed = parse_signal(text)
    # Forex Gold Expert
    if event.chat.id == 2095470635:
        if HALF_CLOSE_RE.search(text):
            is_management = True
            # get LIVE MT5 positions
            positions = [
                p for p in (mt5.positions_get() or [])
                if p.symbol.startswith("XAU") and p.magic == MAGIC and p.comment == "SMC Gold Expert"
            ]

            if len(positions) < 2:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Not enough positions to half close")
                # return

            # 3️⃣ Close ONE position (e.g. oldest)
            if len(positions) == 2:
                pos_to_close = positions[0].ticket

                if close_position_by_id(pos_to_close):
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Half closed:", pos_to_close)
                else:
                    print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Failed to half close:", pos_to_close)

            # return

        if USE_BE_RE.search(text):
            is_management = True
            move_sl_to_entry("XAUUSD+", channel_id_provider_name_map[event.chat.id])
            # return 
        parsed = parse_signal(text)
    # King SMC
    if event.chat.id == 1914952549:
        if CLOSE_CMD_RE.search(text):
            is_management = True
            # reply_to = getattr(event.message, "reply_to_msg_id", None)
            # if not reply_to:
            #     print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Close command with no reply — ignoring.")
            #     # return

            # replied = await client.get_messages(event.chat_id, ids=reply_to)
            replied = await event.get_reply_message()
            if not replied:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Reply message not found")
                return
            symbol = extract_symbol_from_text(replied.text)

            if not symbol:
                print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Could not extract symbol from replied message.")
                # return
            else:
                symbol = symbol + "+"

                close_positions_for_symbol(symbol, "King SMC")
                # print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Positions for {symbol} closed.")
            # return

        if DELETE_LIMIT_RE.search(text):
            is_management = True
            m = re.search(r"#\s*([A-Za-z]{3,10})", text)
            sym = ""
            if m:
                sym_token = m.group(1).upper()
                if sym_token in ("GOLD", "XAU"):
                    sym = "XAUUSD" + "+"
                else:
                    sym = sym_token + "+"
            delete_pending_for_symbol(sym, "King SMC")
            print(f"[{channel_id_provider_name_map[event.chat.id]}][HANDLER] Deleted limit for {sym}")
            # return

        if USE_BE_RE.search(text):
            is_management = True
            # m = re.search(r"#\s*([A-Za-z]{3,10})", text)
            sym = None
            # if m:
            #     sym_token = m.group(1).upper()
            #     # sym = sym_token + "+"
            #     if sym_token in ("GOLD", "XAU"):
            #         sym = "XAUUSD" + "+"
            #     else:
            #         sym = sym_token + "+"
            sym = extract_symbol_from_text(text)
            if not sym:
                replied = await event.get_reply_message()
                sym = extract_symbol_from_text(replied)
            move_sl_to_entry(sym, channel_id_provider_name_map[event.chat.id])
            # return
        parsed = parse_signal(text)
    # signal test
    if event.chat.id == 3600107157:
        parsed = parse_signal(text)

    if not parsed:
        if not is_management:
            print(
                f"[{channel_id_provider_name_map[event.chat.id]}] "
                f"Message not recognized as signal: {text}"
            )
        return

    # place orders according to parsed content
    symbol = parsed.get("symbol")
    direction = parsed.get("direction")  # "BUY" or "SELL" expected (case-insensitive)
    mode = parsed.get("mode")            # "LIMIT" or "MARKET" or None
    entry = parsed.get("entry")          # maybe None for MARKET signals
    sl = parsed.get("sl")
    tp = parsed.get("tp")

    # if (event.chat.id == 2095470635 and (len(entry.split(".")[1]) > 2 or len(sl.split(".")[1]) > 2 or len(tp.split(".")[1])) > 2):
    # if (event.chat.id in (2095470635, 3465604702) and (decimals_count(str(entry)) > 2 or decimals_count(str(sl)) > 2 or decimals_count(str(tp)) > 2)):
    #     entry = float(Decimal(str(entry)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    #     sl    = float(Decimal(str(sl)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    #     tp    = float(Decimal(str(tp)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    # Default flags / values
    price_for_order = None

    # If LIMIT mode and entry exists -> pending order at entry price
    if mode and mode.upper() == "LIMIT" and entry is not None:
        try:
            price_for_order = float(entry)
        except Exception:
            print("Invalid entry price parsed for LIMIT:", entry)
            return
    else:
        # MARKET or no explicit mode: use live tick price for market execution
        tick = mt5.symbol_info_tick(parsed.get("symbol"))
        if tick is None:
            print("Symbol not available in MT5 (no tick):", parsed.get("symbol"))
            return
        # use ask for BUY, bid for SELL
        if direction and direction.upper() == "BUY":
            price_for_order = float(tick.ask)
        else:
            price_for_order = float(tick.bid)

    raw_lots = None
    if sl is not None:
        # convert to float and calculate pips using your helper
        try:
            sl_val = float(sl)
            try:
                sl_val = account_balance()
                # if 2529563894 == event.chat.id:
                #     if abs(price_for_order - sl_val) >= 8:
                #         # sl_val = float(abs(price_for_order - (price_for_order - 5)))
                #         sl_val = (price_for_order - (price_for_order - 5)) if direction == "BUY" else (price_for_order + 5)
                raw_lots = sl_val * RISK_PCT / risk_per_lot(parsed.get("symbol"), abs(price_for_order - sl_val))
                raw_lots = float(Decimal(str(raw_lots)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            except Exception as e:
                print("risk_per_lot failed, falling back to MIN_LOT:", e)
                raw_lots = MIN_LOT
        except Exception as e:
            print("Invalid SL value parsed:", sl, e)
            raw_lots = MIN_LOT
    else:
        # no SL provided, use minimum lot (or you could compute a default)
        raw_lots = MIN_LOT

    # Sanity check: lots must be <= volume_max and >= volume_min
    try:
        info = mt5.symbol_info(parsed.get("symbol"))
        if info and getattr(info, "volume_min", None) is not None and getattr(info, "volume_max", None) is not None:
            if raw_lots < float(info.volume_min):
                # print(f"Computed lots {raw_lots} < volume_min {info.volume_min}, adjusting to min.")
                raw_lots = float(info.volume_min)
            if raw_lots > float(info.volume_max):
                # print(f"Computed lots {raw_lots} > volume_max {info.volume_max}, adjusting to max.")
                raw_lots = float(info.volume_max)
    except Exception as e:
        # pass
        print("Error clamping lot size between max and min:", e)
        return

    def _place_one(provider: str) -> int | None:
        ticket = None
        try:
            vol = None
            if provider in ("SMC Gold Expert", "Forex Gold Expert"):
                vol = max(float(Decimal(str(raw_lots / 2)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), 0.01)
            else:
                vol = raw_lots
            # print(f"[PLACE ORDER] symbol: {symbol}, direction: {direction}, price: {price_for_order}, volume: {vol}, sl: {sl}, tp: {tp}, comment: {provider}")
            if mode == "LIMIT":
                res = place_pending_limit(
                    symbol=parsed.get("symbol"),
                    direction=direction,
                    price=price_for_order,
                    volume=vol,
                    sl=sl,
                    tp=tp,
                    comment=provider,
                )

                # ✅ pending → order ticket
                ticket = getattr(res, "order", None)
                # ticket = res.order
                if not ticket:
                    print(f"[Warn] Failed place_pending_limit, response: {res}")
                    return none

            else:
                # info = mt5.symbol_info(mt5_sym)
                # ticks = int(round(abs(entry - price_for_order) / info.trade_tick_size, 0))
                # if ticks >= 300:
                #     print(f"[{channel_id_provider_name_map[event.chat.id]}] Price difference between signal entry and market price over 300 ticks, too big for placing market order, entry: {entry}, market price: {price_for_order}")
                #     return None
                res = place_market_order(
                    symbol=parsed.get("symbol"),
                    direction=direction,
                    volume=vol,
                    sl=sl,
                    tp=tp,
                    comment=provider,
                )

                # ✅ market → position ticket preferred
                ticket = (
                    getattr(res, "position", None)
                    or getattr(res, "deal", None)
                    or getattr(res, "order", None)
                )

            if not ticket:
                print("[WARN] Order placed but no ticket returned:", res)
                return None

            return ticket

        except Exception as e:
            print("Error placing order:", e)
            return None

    # tickets = []
    ticket = None

    """
    1914952549: "King SMC",
    2095470635: "SMC Gold Expert",
    2529563894: "Forex Gold Exper",
    3465604702: "CRT Signals Team",
    2143819549: "SMC XAUUSD",
    2469668344: "SMC TZ"
    """
    if event.chat.id == 2469668344:
        ticket = _place_one(channel_id_provider_name_map[event.chat.id])
    if event.chat.id == 1914952549:
        ticket = _place_one(channel_id_provider_name_map[event.chat.id])

    if ticket:
        print(f"Signal placed for {channel_id_provider_name_map[event.chat.id]} -> {parsed.get('symbol')}")
        if event.chat.id == 2469668344:
            SMC_TZ_last_opened_trade = parsed.get("symbol")
        # print(f"Market/Limit: {str(parsed.get("mode")).lower()}, buy/sell: {str(parsed.get("direction")).lower()}, entry: {parsed.get("entry")}, sl: {parsed.get("sl")}, tp: {parsed.get("tp")}, lot size: {raw_lots}")
        info = mt5.symbol_info(parsed.get("symbol"))
        print(f"Market/Limit: {mode.lower()}, buy/sell: {direction.lower()}, entry: {price_for_order:.{info.digits}f}, sl: {sl}, tp: {tp}, volume: {raw_lots}")

# -------------------- Main --------------------
async def main():
    await client.start(phone=PHONE)
    # print(f"Listening to channels: {provider_name[0]}, {provider_name[1]}")
    # print(f"Listening to channel(s): {provider_name[0]}, {provider_name[1]}, {provider_name[2]}, {provider_name[3]}")
    print(f"Listening to channel(s): {provider_name[0]}")
    await client.run_until_disconnected()

if __name__ == '__main__':
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Stopping by user")
            break
        except Exception as e:
            print("Runtime error, restarting in 5s:", e)

            time.sleep(5)
