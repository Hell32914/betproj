"""
Telegram group listener + AdsPower bootstrap.
Authorizes in Telegram, starts listening to the configured group, then starts or
connects to AdsPower profiles and logs into BetInAsia / Black.

Usage:
    pip install -r requirements.txt
    python bot_telethon.py   (first run asks for phone number + confirmation code)
"""

import os
import asyncio
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events

from login import run_all_profiles
from signals import parse_betting_signal

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
GROUP_ID = int(os.getenv("TG_GR_ID"))
ADSPOWER_PROFILE_COUNT = int(os.getenv("ADSPOWER_PROFILE_COUNT", "2"))


class RuntimeState:
    def __init__(self) -> None:
        self.sessions = []
        self.stakes = {}
        self.ready = asyncio.Event()
        self.bet_lock = asyncio.Lock()
        self.stake_lock = asyncio.Lock()


def seconds_until_next_stake_refresh() -> float:
    now = datetime.now()
    next_refresh = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if next_refresh <= now:
        next_refresh += timedelta(days=1)
    return (next_refresh - now).total_seconds()


async def refresh_stakes(state: RuntimeState) -> None:
    async with state.stake_lock:
        if not state.sessions:
            return
        state.stakes = {}


async def refresh_stakes_daily(state: RuntimeState) -> None:
    await state.ready.wait()
    while True:
        await asyncio.sleep(seconds_until_next_stake_refresh())
        try:
            await refresh_stakes(state)
        except Exception as exc:
            print(f"Stake refresh failed: {exc}", flush=True)


async def start_adspower_after_listener_ready(state: RuntimeState) -> None:
    print("Telegram listener is ready. Starting or connecting AdsPower profiles...", flush=True)
    loop = asyncio.get_running_loop()
    state.sessions = await loop.run_in_executor(
        None,
        lambda: run_all_profiles(expected_profiles=ADSPOWER_PROFILE_COUNT, wait_for_enter=False),
    )
    state.ready.set()
    await refresh_stakes(state)
    print("AdsPower profiles are ready. Telegram listener is still running.", flush=True)


def report_adspower_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return

    exc = task.exception()
    if exc:
        print(f"AdsPower task failed: {exc}", flush=True)


async def handle_signal(state: RuntimeState, text: str) -> None:
    signal = parse_betting_signal(text)
    if signal is None:
        return

    print(
        f"[SIGNAL] {signal.teams or 'unknown match'} | {signal.market} | "
        f"{signal.selection_label} | odds {signal.odds} | expiry {signal.expiry}",
        flush=True,
    )
    await state.ready.wait()

    async with state.bet_lock:
        print(
            "BetInAsia/Black bet placement is not wired yet. "
            "Signal parsed and browser sessions are ready.",
            flush=True,
        )


async def main():
    state = RuntimeState()

    async with TelegramClient("betproj_session", API_ID, API_HASH) as client:
        print(f"Listening to group {GROUP_ID} ...")

        @client.on(events.NewMessage(chats=GROUP_ID))
        async def on_message(event):
            sender = await event.get_sender()
            if sender:
                name = getattr(sender, "username", None) or getattr(sender, "first_name", None) or str(sender.id)
                name = f"@{name}" if getattr(sender, "username", None) else name
            else:
                name = "unknown"
            text = event.message.text or "<non-text message>"
            print(f"[MSG] {name}: {text}", flush=True)
            asyncio.create_task(handle_signal(state, text))

        adspower_task = asyncio.create_task(start_adspower_after_listener_ready(state))
        adspower_task.add_done_callback(report_adspower_task_result)
        asyncio.create_task(refresh_stakes_daily(state))

        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
