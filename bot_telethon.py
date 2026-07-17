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
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors.common import TypeNotFoundError as TelethonTypeNotFoundError
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError

from login import (
    BlackSelectionMissingError,
    check_black_order_by_id,
    ensure_betfair_session_authorized,
    ensure_black_session_authorized,
    keep_black_session_alive,
    open_betfair_match,
    place_black_bet,
    refresh_black_default_stake,
    run_all_profiles,
    update_betfair_default_stake,
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
SESSION_AUTH_CHECK_SECONDS = 15 * 60


class RuntimeState:
    def __init__(self) -> None:
        self.sessions = []
        self.stakes = {}
        self.ready = asyncio.Event()
        self.bet_lock = asyncio.Lock()
        self.stake_lock = asyncio.Lock()
        self.betfair_lock = asyncio.Lock()


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

        # Refresh Betfair default stake on Profile-2 (if present).
        betfair_session = next((s for s in state.sessions if s.get("betfair")), None)
        if betfair_session is not None:
            async with state.betfair_lock:
                try:
                    bf_result = await loop.run_in_executor(
                        None, lambda: update_betfair_default_stake(betfair_session)
                    )
                    betfair_session["stake"] = bf_result
                    state.stakes[betfair_session["profile_label"]] = bf_result
                    print(
                        f"Default stake refreshed for {betfair_session['profile_label']}: "
                        f"balance EUR {bf_result['balance']}, stake EUR {bf_result['stake']} "
                        f"({bf_result['percent']}%).",
                        flush=True,
                    )
                except Exception as exc:
                    if result:
                        betfair_session["stake"] = dict(result)
                        betfair_session["stake"]["source"] = "Profile-1 fallback"
                        state.stakes[betfair_session["profile_label"]] = betfair_session["stake"]
                        print(
                            f"Betfair default stake refresh failed: {exc!r}; "
                            f"using Profile-1 stake EUR {result['stake']} as fallback.",
                            flush=True,
                        )
                    else:
                        print(
                            f"Betfair default stake refresh failed: {exc!r}",
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


async def keep_sessions_authorized(state: RuntimeState) -> None:
    await state.ready.wait()
    while True:
        await asyncio.sleep(SESSION_AUTH_CHECK_SECONDS)
        if not state.sessions:
            continue

        loop = asyncio.get_running_loop()
        primary_session = next((session for session in state.sessions if session.get("login_enabled")), state.sessions[0])
        async with state.bet_lock:
            try:
                result = await loop.run_in_executor(
                    None, lambda: ensure_black_session_authorized(primary_session)
                )
                print(
                    f"Black auth-check completed: {result.get('status')}.",
                    flush=True,
                )
            except Exception as exc:
                print(f"Black auth-check failed: {exc}", flush=True)

        betfair_session = next((session for session in state.sessions if session.get("betfair")), None)
        if betfair_session is None:
            continue
        async with state.betfair_lock:
            try:
                result = await loop.run_in_executor(
                    None, lambda: ensure_betfair_session_authorized(betfair_session)
                )
                print(
                    f"Betfair auth-check completed: {result.get('status')}.",
                    flush=True,
                )
            except Exception as exc:
                print(f"Betfair auth-check failed: {exc}", flush=True)


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
        f"{signal.selection_label} | odds {signal.odds} | expiry {signal.expiry}"
        f"{f' | matched £{signal.matched_amount}' if signal.matched_amount is not None else ''}",
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
            print(f"Black bet placement failed: {_describe_exception(exc)}", flush=True)
            traceback.print_exc()


def _describe_exception(exc: BaseException) -> str:
    """Return a non-empty diagnostic string for ``exc``.

    Selenium ``WebDriverException`` subclasses often stringify to ``'Message: '``
    with an empty payload, which makes both console logs and Telegram replies
    useless. Fall back to the type name plus a short traceback tail so failures
    are always actionable.
    """

    text = str(exc).strip() if exc is not None else ""
    # Strip Selenium's empty 'Message:' prefix when there's nothing after it.
    if text.lower().startswith("message:"):
        text = text[len("message:"):].strip()
    if text.lower() == "none":
        text = ""
    type_name = type(exc).__name__ if exc is not None else "Exception"
    if text:
        return f"{type_name}: {text}"
    # Fall back to the last traceback frame so we know where it blew up.
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    last_frame = ""
    for line in reversed(tb):
        line = line.strip()
        if line.startswith("File "):
            last_frame = line
            break
    return f"{type_name} (no message){' @ ' + last_frame if last_frame else ''}"


def _format_signal_work_message(signal) -> str:
    return (
        f"Asia: Принял в работу: {signal.teams or 'unknown match'} | "
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


def _telethon_retry_delay(consecutive_failures: int) -> int:
    return min(60, max(5, consecutive_failures * 5))


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
        return f"Asia: Ставка: {teams_label} | сумма {stake_label} | статус {status_label}"

    status = result.get("status", "unknown")
    label = {
        "accepted": "Asia: Ставка принята",
        "pending": "Asia: Ставка отправлена, но итоговый статус не подтвержден",
        "rejected": "Asia: Ставка не принята",
        "cancelled": "Asia: Ставка отменена",
    }.get(status, f"Asia: Статус ставки: {status}")
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
            await event.reply(
                f"Betfair: взял в работу — {signal.teams or 'unknown match'} | "
                f"{signal.selection_label}"
            )

            async def process_signal():
                """Strictly serialized flow for one signal.

                Asia/Black works first (full-screen), then Betfair works
                (full-screen). If either one cannot find the requested line it is
                retried, and when both are still missing the line they are checked
                one after another (never at the same time) for up to 3 rounds.
                Finally, if Black placed a bet, its order status is checked ~5 min
                later.
                """
                await state.ready.wait()
                loop = asyncio.get_running_loop()

                signal_teams = signal.teams or "unknown match"
                signal_label = signal.selection_label

                max_rounds = 3
                round_pause = 90  # seconds between alternating retry rounds
                deferred_check_delay = 5 * 60  # final Black status check delay

                # State machines: "pending" -> placed | missing | not_found | error
                black_state = "pending"
                black_order_id = None
                black_place_result = None
                betfair_state = "pending"

                def _pick_black_session():
                    return next(
                        (s for s in state.sessions if s.get("login_enabled")),
                        state.sessions[0],
                    )

                def _pick_betfair_session():
                    return next((s for s in state.sessions if s.get("betfair")), None)

                for round_index in range(1, max_rounds + 1):
                    is_last_round = round_index == max_rounds

                    # ---- Phase 1: Asia / Black ----
                    if black_state in ("pending", "missing"):
                        async with state.bet_lock:
                            primary_session = _pick_black_session()
                            try:
                                place_result = await loop.run_in_executor(
                                    None, lambda: place_black_bet(primary_session, signal)
                                )
                                black_place_result = place_result
                                black_order_id = place_result.get("order_id")
                                black_state = "placed"
                                print(
                                    f"Black bet placement completed with status: "
                                    f"{place_result.get('status')}, orderId={black_order_id}",
                                    flush=True,
                                )
                            except BlackSelectionMissingError as exc:
                                black_state = "missing"
                                print(
                                    f"Black attempt {round_index}/{max_rounds} "
                                    f"gave up on missing line: {exc}",
                                    flush=True,
                                )
                                if is_last_round:
                                    await event.reply(
                                        "Asia: Ставка проверена 3 раза, нужная линия так и не появилась на сайте."
                                    )
                            except Exception as exc:
                                black_state = "error"
                                detail = _describe_exception(exc)
                                print(f"Black bet placement failed: {detail}", flush=True)
                                traceback.print_exc()
                                await event.reply(f"Asia: Ставка не завершена: {detail}")

                    # ---- Phase 2: Betfair (only after Asia finished this round) ----
                    if betfair_state in ("pending", "missing"):
                        betfair_session = _pick_betfair_session()
                        if betfair_session is None:
                            betfair_state = "error"
                            await event.reply("Betfair: профиль не готов — пропускаю.")
                        else:
                            async with state.betfair_lock:
                                if not betfair_session.get("stake"):
                                    stake_source = next(
                                        (s for s in state.sessions if s.get("login_enabled") and s.get("stake")),
                                        None,
                                    )
                                    if stake_source and stake_source.get("stake"):
                                        betfair_session["stake"] = dict(stake_source["stake"])
                                        betfair_session["stake"]["source"] = "Profile-1 fallback"
                                        state.stakes[betfair_session["profile_label"]] = betfair_session["stake"]
                                        print(
                                            f"Betfair stake missing; using Profile-1 stake EUR "
                                            f"{betfair_session['stake']['stake']} as fallback.",
                                            flush=True,
                                        )
                                try:
                                    result = await loop.run_in_executor(
                                        None,
                                        lambda sig=signal: open_betfair_match(betfair_session, sig),
                                    )
                                    print(
                                        f"Betfair match open completed for {signal_teams} ({signal_label}): "
                                        f"opened={result.get('opened')}, "
                                        f"label={result.get('label')!r}, url={result.get('url')}",
                                        flush=True,
                                    )
                                    if not result.get("opened"):
                                        betfair_state = "not_found"
                                        result_url = (result.get("url") or "").lower()
                                        result_error = (result.get("error") or "").strip()
                                        if "/search" in result_url or "search?" in result_url:
                                            message = "Betfair: матч не найден в результатах поиска."
                                        elif result_url.rstrip("/").endswith("/exchange/plus"):
                                            message = (
                                                "Betfair: поиск не открыл матч и остался на главной биржи."
                                            )
                                        else:
                                            message = "Betfair: матч не открыт после поиска."
                                        if result_error:
                                            message = f"{message} Причина: {result_error}"
                                        await event.reply(message)
                                    elif result.get("selection_error"):
                                        betfair_state = "missing"
                                        print(
                                            f"Betfair attempt {round_index}/{max_rounds} for "
                                            f"{signal_teams} did not find the line: {result.get('selection_error')}",
                                            flush=True,
                                        )
                                        if is_last_round:
                                            await event.reply(
                                                "Betfair: 3 раза проверено, нужная ставка так и не появилась на сайте."
                                            )
                                    else:
                                        betfair_state = "placed"
                                        label = (result.get("label") or "").strip()
                                        await event.reply(
                                            f"Betfair: открыл матч — {label or signal_teams}"
                                        )
                                        bf_sel = result.get("betfair_selection")
                                        if bf_sel:
                                            bf_odds = result.get("betfair_odds")
                                            bf_stake = result.get("betfair_stake")
                                            bf_placed = result.get("betfair_bet_placed", False)
                                            stake_part = f", ставка {bf_stake}" if bf_stake else ""
                                            placed_part = " ✓" if bf_placed else ""
                                            await event.reply(
                                                f"Betfair: выбрал Back {bf_sel} @ {bf_odds}{stake_part}{placed_part}"
                                            )
                                except Exception as exc:
                                    betfair_state = "error"
                                    detail = _describe_exception(exc)
                                    print(f"Betfair match open failed for {signal_teams}: {detail}", flush=True)
                                    traceback.print_exc()
                                    await event.reply(f"Betfair: ошибка — {detail}")

                    # Keep retrying (one after another) only while a side still misses the line.
                    if black_state != "missing" and betfair_state != "missing":
                        break
                    if not is_last_round:
                        await asyncio.sleep(round_pause)

                # ---- Phase 3: Black deferred final status (only if it placed) ----
                if black_state != "placed":
                    return

                if black_order_id in (None, ""):
                    await event.reply(
                        "Asia: Не удалось определить номер заказа после ставки — проверьте вручную."
                    )
                    return

                print(
                    f"Black bet placed, order #{black_order_id}. Sleeping {deferred_check_delay}s "
                    f"before final status check.",
                    flush=True,
                )
                await asyncio.sleep(deferred_check_delay)

                async with state.bet_lock:
                    primary_session = _pick_black_session()
                    try:
                        final_result = await loop.run_in_executor(
                            None,
                            lambda: check_black_order_by_id(primary_session, black_order_id, signal),
                        )
                        print(
                            f"Black deferred check for order #{black_order_id}: "
                            f"status={final_result.get('order_status')}, "
                            f"stake={final_result.get('order_stake')}",
                            flush=True,
                        )
                        final_result.setdefault("teams", (black_place_result or {}).get("teams"))
                        final_result.setdefault("selection", (black_place_result or {}).get("selection"))
                        final_result.setdefault("odds", (black_place_result or {}).get("odds"))
                        await event.reply(_format_signal_result_message(final_result))
                    except Exception as exc:
                        print(f"Black deferred order check failed: {exc}", flush=True)
                        await event.reply(
                            f"Asia: Не удалось прочитать итог по заказу #{black_order_id}: {exc}"
                        )

            asyncio.create_task(process_signal())

        adspower_task = asyncio.create_task(start_adspower_after_listener_ready(state))
        adspower_task.add_done_callback(report_adspower_task_result)
        asyncio.create_task(refresh_stakes_daily(state))
        asyncio.create_task(keep_sessions_authorized(state))

        # Telethon can crash on TypeNotFoundError when Telegram introduces a new TL
        # object the installed Telethon doesn't know yet (the stored difference contains
        # an unknown constructor id). Clear the cached error and resume listening so the
        # bot stays online instead of exiting. If this keeps happening, upgrade Telethon:
        #     .venv\Scripts\python.exe -m pip install -U telethon
        consecutive_reconnect_failures = 0
        while True:
            try:
                if hasattr(client, "_updates_error"):
                    client._updates_error = None
                if not client.is_connected():
                    try:
                        # After transport-level failures (for example WinError 5 / aborted
                        # local socket), Telethon can keep stale sender state around. Always
                        # tear it down before attempting a fresh connect.
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        await client.connect()
                    except AuthKeyDuplicatedError as exc:
                        print(
                            "Telethon auth key is permanently invalidated "
                            "(session file used from two IPs simultaneously): "
                            f"{exc}. Delete betproj_session.session and re-login. "
                            "Stopping listener to avoid infinite reconnect spam.",
                            flush=True,
                        )
                        return
                    except Exception as conn_exc:
                        consecutive_reconnect_failures += 1
                        delay = _telethon_retry_delay(consecutive_reconnect_failures)
                        print(
                            f"Telethon reconnect failed #{consecutive_reconnect_failures}: "
                            f"{conn_exc!r}; retrying in {delay}s.",
                            flush=True,
                        )
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        await asyncio.sleep(delay)
                        continue
                    if not await client.is_user_authorized():
                        consecutive_reconnect_failures += 1
                        delay = _telethon_retry_delay(consecutive_reconnect_failures)
                        print(
                            "Telethon session is not authorized after reconnect; "
                            "delete the .session file and re-login.",
                            flush=True,
                        )
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        await asyncio.sleep(max(delay, 30))
                        continue
                    consecutive_reconnect_failures = 0
                    print("Telethon reconnected; resuming listener.", flush=True)
                await client.run_until_disconnected()
                break
            except TelethonTypeNotFoundError as exc:
                print(
                    f"Telethon TypeNotFoundError, ignoring and continuing to listen: {exc}",
                    flush=True,
                )
                await asyncio.sleep(2)
                continue
            except AuthKeyDuplicatedError as exc:
                print(
                    "Telethon auth key is permanently invalidated "
                    "(session file used from two IPs simultaneously): "
                    f"{exc}. Delete betproj_session.session and re-login. "
                    "Stopping listener to avoid infinite reconnect spam.",
                    flush=True,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return
            except Exception as exc:
                consecutive_reconnect_failures += 1
                delay = _telethon_retry_delay(consecutive_reconnect_failures)
                print(
                    f"Telethon listener crashed: {exc!r}; restarting in {delay}s.",
                    flush=True,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(delay)
                continue


if __name__ == "__main__":
    asyncio.run(main())
