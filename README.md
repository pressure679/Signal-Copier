# 🚀 Telegram → Bybit Signal Copier

The repository contains the original MT5 copier plus a new **Bybit USDT-perpetual copier** for Telegram signals.

## New Bybit copier

Run:

```bash
pip install -r requirements-bybit.txt
python bybit-signal-copier.py
```

The new parser accepts messages such as:

```text
Pairs: US/USDT 👉 Trade Type = SHORT 🔴 👉 Leverage :- 20x⚡️ Entry = [ 0.0123 TO 0.0123 ]❌ StopLoss :- 0.0127✅ Take profit = [ 0.0121, 0.0119, 0.0118, 0.0116, 0.0115, 0.0112 ]
```

It:

- normalizes `US/USDT` to the Bybit symbol `USUSDT`;
- supports LONG/SHORT and BUY/SELL;
- reads the leverage from the signal;
- reads an entry range; equal endpoints become one entry, while a range is split between both endpoints;
- calculates quantity from `RISK_PCT` (default 0.5% of account equity at the stop);
- reads Bybit instrument tick/quantity filters before submitting orders;
- creates separate partial TP orders for every TP target, each with the signal SL;
- uses Bybit's `linear` USDT-perpetual API through `pybit`.

Bybit's V5 order API supports linear orders with partial TP/SL parameters, and leverage is set through the V5 position leverage endpoint. See the official Bybit API documentation before enabling live trading.

## Credentials

Create `pybit-credentials.txt` locally:

```text
api_key=YOUR_BYBIT_API_KEY
api_secret=YOUR_BYBIT_API_SECRET
testnet=false
```

Create `telegram-credentials.txt` locally:

```text
api_id=YOUR_TELEGRAM_API_ID
api_hash=YOUR_TELEGRAM_API_HASH
phone=+4512345678
```

Example files are provided as `pybit-credentials.example.txt` and `telegram-credentials.example.txt`. Real credential files and Telegram session files are ignored by Git.

## Add / find a Telegram channel ID

Use the new fetcher:

```bash
python telegram-channel-id.py @channel_username
```

Or automatically add it to the local channel configuration:

```bash
python telegram-channel-id.py @channel_username --add "New Channel"
```

Private channels require the Telegram account used by Telethon to be a member.

The resulting `telegram-channels.txt` uses:

```text
-1001234567890=New Channel
```

The Bybit copier listens to every channel in this file, so adding another channel does not require editing the Python source.

## Safety

Start with `testnet=true` and a tiny account before using live funds. The copier places real exchange orders when `testnet=false`. Verify the symbol exists on Bybit, the account is in the expected one-way/hedge mode, and the API key has only the permissions required for trading.

The default risk is `RISK_PCT = 0.005` (0.5%). Change it deliberately after testing.

The old `signal-copier-4.py` remains in the repository for the existing MT5 workflow; `bybit-signal-copier.py` is the new Bybit implementation.

## Disclaimer

Trading cryptocurrencies and derivatives involves substantial risk. This software is provided for educational purposes; you are responsible for your own trading decisions and API permissions.
