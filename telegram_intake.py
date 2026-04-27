#!/usr/bin/env python3
"""
Telegram bot for NODE / kestrel-memory integration.

Text input → kestrel_persona.handle() (intent classify + respond in Kestrel voice).
File/document attachments → ~/incoming/telegram/ for Manual Ingest.

Token is read from ~/.telegram_token (KEY=VALUE format) — never committed.
"""
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent))
import kestrel_persona as persona

_CFG_FILE = Path.home() / ".telegram_token"

def _load_config() -> dict:
    if not _CFG_FILE.exists():
        raise RuntimeError(f"Missing config: {_CFG_FILE}  (needs TOKEN= and CHAT_ID=)")
    cfg = {}
    for line in _CFG_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg

_cfg    = _load_config()
TOKEN   = _cfg["TOKEN"]
CHAT_ID = _cfg["CHAT_ID"]
API     = f"https://api.telegram.org/bot{TOKEN}"

TELEGRAM_DIR = Path.home() / "incoming/telegram"
OFFSET_FILE  = Path("/tmp/tg_bot_offset")


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send(text: str) -> None:
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text[:4000]},
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Attachment download
# ---------------------------------------------------------------------------

def _safe_name(raw: str) -> str:
    name = Path(raw).name
    name = re.sub(r"[^\w.\-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name[:200] or "file"


def _download(file_id: str, filename: str) -> Optional[Path]:
    try:
        r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=15)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        content = requests.get(url, timeout=120).content

        TELEGRAM_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = _safe_name(filename)
        dest = TELEGRAM_DIR / f"{ts}_{safe}"
        if dest.exists():
            dest = TELEGRAM_DIR / f"{ts}_{os.getpid()}_{safe}"
        dest.write_bytes(content)
        return dest
    except Exception as exc:
        send(persona.build_error_soft(str(exc)))
        return None


def handle_attachment(msg: dict) -> None:
    for key in ("document", "video", "audio", "voice"):
        f = msg.get(key)
        if f:
            fname = f.get("file_name") or f.get("title") or key
            dest = _download(f["file_id"], fname)
            if dest:
                send(
                    f"Saved: {dest.name}\n"
                    f"Manual Ingest UI → Telegram section → select → choose type/mode → Queue."
                )
            return

    photos = msg.get("photo")
    if photos:
        p = max(photos, key=lambda x: x.get("file_size", 0))
        dest = _download(p["file_id"], "photo.jpg")
        if dest:
            send(
                f"Saved: {dest.name}\n"
                f"Manual Ingest UI → Telegram section → select → choose type/mode → Queue."
            )


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def main() -> None:
    offset = int(OFFSET_FILE.read_text().strip()) if OFFSET_FILE.exists() else 0

    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            updates = r.json().get("result", [])
        except Exception:
            time.sleep(5)
            continue

        for update in updates:
            uid = update["update_id"]
            msg = update.get("message", {})

            if any(k in msg for k in ("document", "photo", "video", "audio", "voice")):
                handle_attachment(msg)
            elif "text" in msg:
                send(persona.handle(msg["text"]))

            offset = uid + 1
            OFFSET_FILE.write_text(str(offset))


if __name__ == "__main__":
    main()
