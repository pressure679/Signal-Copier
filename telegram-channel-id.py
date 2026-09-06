#!/usr/bin/env python3
"""Resolve a Telegram channel/group to its numeric ID.

Usage:
  python telegram-channel-id.py @channel_username
  python telegram-channel-id.py https://t.me/channel_username
  python telegram-channel-id.py -1001234567890

Use --add NAME to append the resolved channel to telegram-channels.txt:
  python telegram-channel-id.py @new_channel --add "New Channel"

The account used by Telethon must be a member of private channels.
"""

import argparse
import asyncio
import re
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS = BASE_DIR / "telegram-credentials.txt"
CHANNELS_FILE = BASE_DIR / "telegram-channels.txt"
SESSION_NAME = "signal_copier_channel_lookup"


def load_kv_file(path: Path):
    values = {}
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
        elif ":" in line:
            k, v = line.split(":", 1)
        else:
            continue
        values[k.strip().lower()] = v.strip().strip('"').strip("'")
    return values


def normalize_target(target: str):
    target = target.strip()
    m = re.match(r"https?://t\.me/(?:c/)?([^/?#]+)", target, re.I)
    if m:
        target = m.group(1)
    return target


def append_channel(channel_id: int, name: str):
    existing = set()
    if CHANNELS_FILE.exists():
        for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                existing.add(line.split("=", 1)[0].strip())
    # Keep the canonical Telegram -100... ID in the config file.
    if str(channel_id) in existing:
        return
    with CHANNELS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{channel_id}={name}\n")


async def run(target: str, add_name: Optional[str]):
    cfg = load_kv_file(CREDENTIALS)
    api_id = cfg.get("api_id")
    api_hash = cfg.get("api_hash")
    phone = cfg.get("phone")
    if not api_id or not api_hash:
        raise RuntimeError(f"{CREDENTIALS} needs api_id=... and api_hash=...")

    target = normalize_target(target)
    try:
        entity_ref = int(target)
    except ValueError:
        entity_ref = target if target.startswith("@") else f"@{target}"

    client = TelegramClient(SESSION_NAME, int(api_id), api_hash)
    await client.start(phone=phone)
    try:
        entity = await client.get_entity(entity_ref)
        channel_id = int(entity.id)
        # Telethon returns the bare channel ID; Telegram event filters use -100... IDs.
        if getattr(entity, "broadcast", False) or hasattr(entity, "megagroup"):
            channel_id = -1000000000000 + channel_id
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity.id)
        username = getattr(entity, "username", None)
        print(f"Name:     {title}")
        print(f"Username: @{username}" if username else "Username: (none/private)")
        print(f"ID:       {channel_id}")
        print(f"Config:   {channel_id}={title}")
        if add_name:
            append_channel(channel_id, add_name)
            print(f"Added to {CHANNELS_FILE}: {channel_id}={add_name}")
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="@username, t.me link, or numeric Telegram ID")
    parser.add_argument("--add", metavar="NAME", help="append the channel to telegram-channels.txt")
    args = parser.parse_args()
    asyncio.run(run(args.target, args.add))


if __name__ == "__main__":
    main()
