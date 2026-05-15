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
import re
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events

from login import (
    BlackSelectionMissingError,
    keep_black_session_alive,
    place_black_bet,
    refresh_black_default_stake,
    run_all_profiles,
)
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
BLACK_KEEPALIVE_SECONDS = 20 * 60


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
        primary_session = next((session for session in state.sessions if session.get("login_enabled")), state.sessions[0])
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: refresh_black_default_stake(primary_session))
        primary_session["stake"] = result
        state.stakes = {primary_session["profile_label"]: result}
        print(
            f"Default stake refreshed for {primary_session['profile_label']}: "
            f"balance EUR {result['balance']}, stake EUR {result['stake']} ({result['percent']}%).",
            flush=True,
        )


async def refresh_stakes_daily(state: RuntimeState) -> None:
    await state.ready.wait()
    while True:
        await asyncio.sleep(seconds_until_next_stake_refresh())
        try:
            await refresh_stakes(state)
        except Exception as exc:
            print(f"Stake refresh failed: {exc}", flush=True)


async def keep_black_session_active(state: RuntimeState) -> None:
    await state.ready.wait()
    while True:
        await asyncio.sleep(BLACK_KEEPALIVE_SECONDS)
        async with state.bet_lock:
            if not state.sessions:
                continue
            primary_session = next((session for session in state.sessions if session.get("login_enabled")), state.sessions[0])
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(None, lambda: keep_black_session_alive(primary_session))
                print(f"Black keepalive completed: {result.get('status')}.", flush=True)
            except Exception as exc:
                print(f"Black keepalive failed: {exc}", flush=True)


async def start_adspower_after_listener_ready(state: RuntimeState) -> None:
    print("Telegram listener is ready. Starting or connecting AdsPower profiles...", flush=True)
    loop = asyncio.get_running_loop()
    state.sessions = await loop.run_in_executor(
        None,
        lambda: run_all_profiles(expected_profiles=ADSPOWER_PROFILE_COUNT, wait_for_enter=False),
    )
    state.ready.set()
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
        primary_session = next((session for session in state.sessions if session.get("login_enabled")), state.sessions[0])
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: place_black_bet(primary_session, signal))
            print(f"Black bet placement completed with status: {result.get('status')}", flush=True)
        except Exception as exc:
            print(f"Black bet placement failed: {exc}", flush=True)


def _format_signal_work_message(signal) -> str:
    return (
        f"Принял в работу: {signal.teams or 'unknown match'} | "
        f"{signal.selection_label} | odds {signal.odds}"
    )


def _sanitize_telegram_detail(detail: str, limit: int = 220) -> str:
    cleaned = []
    for char in detail or "":
        if char in "\n\r\t" or char.isprintable():
            cleaned.append(char)
    compact = " | ".join(part.strip() for part in "".join(cleaned).splitlines() if part.strip())
    compact = " ".join(compact.split())
    return compact[:limit]


def _format_teams_vs_label(teams: str) -> str:
    normalized = " ".join((teams or "unknown match").split())
    normalized = re.sub(r"\s+[Vv][Ss]\.?\s+", " vs ", normalized)
    return normalized


def _format_signal_result_message(result: dict) -> str:
    order_status = (result.get("order_status") or "").strip()
    order_stake = (result.get("order_stake") or "").strip()
    if order_status or order_stake:
        teams_label = _format_teams_vs_label(result.get("teams") or "unknown match")
        stake_label = order_stake or "?"
        status_label = order_status or result.get("status", "unknown")
        return f"Ставка: {teams_label} | сумма {stake_label} | статус {status_label}"

    status = result.get("status", "unknown")
    label = {
        "accepted": "Ставка принята",
        "pending": "Ставка отправлена, но итоговый статус не подтвержден",
        "rejected": "Ставка не принята",
        "cancelled": "Ставка отменена",
    }.get(status, f"Статус ставки: {status}")
    fills = result.get("fills") or []
    detail = _sanitize_telegram_detail((result.get("detail") or "").strip())
    message = (
        f"{label}: {result.get('teams') or 'unknown match'} | "
        f"{result.get('selection')} | odds {result.get('odds')}"
    )
    if status == "accepted" and fills:
        fill_parts = []
        for item in fills:
            bookie = (item.get("bookie") or "?").strip()
            amount = item.get("amount_text") or item.get("amount") or "0"
            percent = item.get("percent_text") or item.get("percent") or "0"
            fill_parts.append(f"{bookie} {amount} euro ({percent}%)")
        detail = ", ".join(fill_parts)
    if detail:
        message += f"\n{detail}"
    return message


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

            signal = parse_betting_signal(text)
            if signal is None:
                return

            await event.reply(_format_signal_work_message(signal))

            async def process_signal_reply():
                await state.ready.wait()
                async with state.bet_lock:
                    primary_session = next((session for session in state.sessions if session.get("login_enabled")), state.sessions[0])
                    loop = asyncio.get_running_loop()
                    try:
                        result = await loop.run_in_executor(None, lambda: place_black_bet(primary_session, signal))
                        print(f"Black bet placement completed with status: {result.get('status')}", flush=True)
                        await event.reply(_format_signal_result_message(result))
                    except BlackSelectionMissingError as exc:
                        print(f"Black bet placement gave up: {exc}", flush=True)
                        await event.reply(
                            "Ставка проверена 3 раза, нужная линия так и не появилась на сайте."
                        )
                    except Exception as exc:
                        print(f"Black bet placement failed: {exc}", flush=True)
                        await event.reply(f"Ставка не завершена: {exc}")

            asyncio.create_task(process_signal_reply())

        adspower_task = asyncio.create_task(start_adspower_after_listener_ready(state))
        adspower_task.add_done_callback(report_adspower_task_result)
        asyncio.create_task(refresh_stakes_daily(state))
        asyncio.create_task(keep_black_session_active(state))

        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
