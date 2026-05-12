"""
Safe Punterplay dry run.

Connects to the first AdsPower profile, opens or reuses Punterplay, finds any
active match, opens the match details page, selects FULL TIME OVER/UNDER, fills
odds + test stake + 30 mins expiry, and stops before Place bet.

Usage:
    python dry_run_punterplay.py

Optional environment variables:
    DRY_RUN_SELECTION=Over|Under   default: Over
    DRY_RUN_LINE=2.5               optional exact OU line to click
    DRY_RUN_LEAGUE=Japan J1 League optional league button to try first
    DRY_RUN_ODDS=2.05              optional odds to fill; defaults to market price
    DRY_RUN_STAKE=1                default test stake
"""

from __future__ import annotations

import os
import time
from decimal import Decimal

from selenium.webdriver.support.ui import WebDriverWait

from login import (
    close_driver_bridge,
    connect_to_browser,
    ensure_punterplay_tab,
    fetch_profile_ids,
    login as login_to_punterplay,
    start_adspower_profile,
)
from punterplay_betting import (
    _fill_odds,
    _fill_stake,
    _open_match_details,
    _select_expiry_if_available,
    _select_full_time_ou_line,
)
from signals import BettingSignal


SELECTION = os.getenv("DRY_RUN_SELECTION", "Over").strip().title()
TARGET_LINE = os.getenv("DRY_RUN_LINE")
TARGET_LEAGUE = os.getenv("DRY_RUN_LEAGUE", "").strip()
TARGET_ODDS = os.getenv("DRY_RUN_ODDS")
TEST_STAKE = Decimal(os.getenv("DRY_RUN_STAKE", "1"))


def line_to_decimal(value: str) -> Decimal:
    value = value.strip().replace(",", ".")
    if "/" not in value:
        return Decimal(value)
    parts = [Decimal(part) for part in value.split("/") if part]
    return sum(parts) / Decimal(len(parts))


def wait_ready(driver) -> None:
    WebDriverWait(driver, 20).until(
        lambda browser: browser.execute_script("return document.readyState") in {"interactive", "complete"}
    )


def click_events_panel(driver) -> None:
    clicked = driver.execute_script(
        """
        const button = Array.from(document.querySelectorAll('button,[role=button]'))
            .find((element) => (element.innerText || '').trim() === 'EVENTS');
        if (!button) return false;
        button.click();
        return true;
        """
    )
    if clicked:
        print("Opened EVENTS panel.")
        time.sleep(1)


def has_ou_rows(driver) -> bool:
    return bool(driver.execute_script(
        """
        return Array.from(document.querySelectorAll('div[class*="overflow-hidden"][class*="bg-white"]'))
            .some((row) => row.querySelectorAll('div[class*="w-1/6"]').length >= 3);
        """
    ))


def ensure_logged_in(driver) -> None:
    if "#/sign-in" not in driver.current_url.lower():
        return
    print("Punterplay sign-in page is open. Logging in first...")
    login_to_punterplay(driver, "DryRun")
    time.sleep(2)


def click_league(driver, league_name: str) -> bool:
    return bool(driver.execute_script(
        """
        const expected = arguments[0].trim().toLowerCase();
        const button = Array.from(document.querySelectorAll('button'))
            .find((element) => {
                const text = (element.innerText || '').trim().toLowerCase();
                const rect = element.getBoundingClientRect();
                return text === expected && rect.width && rect.height;
            });
        if (!button) return false;
        button.scrollIntoView({ block: 'center', inline: 'center' });
        button.click();
        return true;
        """,
        league_name,
    ))


def click_first_useful_league(driver, already_clicked: set[str] | None = None) -> str | None:
    already_clicked = already_clicked or set()
    league = driver.execute_script(
        """
        const alreadyClicked = new Set(arguments[0].map((value) => value.toLowerCase()));
        const skip = new Set([
            'china', 'hong kong', 'india', 'japan', 'mongolia', 'portugal',
            'south korea', 'vietnam', 'soccer', 'baseball', 'basketball',
            'tennis', 'cricket', 'boxing', 'e-sport', 'events', 'info'
        ]);
        const buttons = Array.from(document.querySelectorAll('button'))
            .map((button) => {
                const rect = button.getBoundingClientRect();
                return { button, text: (button.innerText || '').trim(), rect };
            })
            .filter((item) => item.text && item.rect.width && item.rect.height)
            .filter((item) => !skip.has(item.text.toLowerCase()))
            .filter((item) => !alreadyClicked.has(item.text.toLowerCase()))
            .filter((item) => /league|cup|division|serie|liga|premier|j1|j2/i.test(item.text));

        const target = buttons[0];
        if (!target) return null;
        target.button.scrollIntoView({ block: 'center', inline: 'center' });
        target.button.click();
        return target.text;
        """,
        list(already_clicked),
    )
    if league:
        print(f"Clicked league: {league}")
        time.sleep(4)
    return league


def ensure_any_active_match(driver) -> None:
    if has_ou_rows(driver):
        print("Active OU rows are already visible.")
        return

    click_events_panel(driver)

    if TARGET_LEAGUE and click_league(driver, TARGET_LEAGUE):
        print(f"Clicked configured league: {TARGET_LEAGUE}")
        time.sleep(4)
        if has_ou_rows(driver):
            return

    clicked_leagues = set()
    if TARGET_LEAGUE:
        clicked_leagues.add(TARGET_LEAGUE)

    for _ in range(12):
        clicked = click_first_useful_league(driver, clicked_leagues)
        if not clicked:
            break
        clicked_leagues.add(clicked)
        if has_ou_rows(driver):
            return

    raise RuntimeError("Could not find any active match with an OU market.")


def find_demo_signal(driver) -> BettingSignal:
    selection_index = 0 if SELECTION == "Over" else 1
    target_line = float(Decimal(TARGET_LINE)) if TARGET_LINE else None

    result = driver.execute_script(
        """
        const selectionIndex = arguments[0];
        const targetLine = arguments[1];

        const parseLine = (value) => {
            value = (value || '').trim().replace(',', '.');
            if (!value) return null;
            if (value.includes('/')) {
                const parts = value.split('/').map(Number).filter((part) => !Number.isNaN(part));
                if (!parts.length) return null;
                return parts.reduce((sum, part) => sum + part, 0) / parts.length;
            }
            const parsed = Number(value);
            return Number.isNaN(parsed) ? null : parsed;
        };

        const scorePattern = new RegExp('^\\\\d+$');
        const periodPattern = new RegExp('^\\\\d+h', 'i');

        const rows = Array.from(document.querySelectorAll('div[class*="overflow-hidden"][class*="bg-white"]'));
        let fallback = null;
        for (const row of rows) {
            const cols = Array.from(row.querySelectorAll('div[class*="w-1/6"]'));
            if (cols.length < 3) continue;

            const teamBlock = row.querySelector('div[class*="border-l"]');
            const eventText = (teamBlock || row).innerText || '';
            const ouColumn = cols[2];
            const groups = Array.from(ouColumn.querySelectorAll('div[class*="border-b"]'));
            const lineGroups = groups.length ? groups : [ouColumn];

            for (const group of lineGroups) {
                const lines = (group.innerText || '').split('\\n').map((line) => line.trim()).filter(Boolean);
                const displayLine = lines.find((line) => parseLine(line) !== null);
                const parsedLine = parseLine(displayLine);
                if (parsedLine === null) continue;

                const prices = Array.from(group.querySelectorAll('button.price'));
                const price = prices[selectionIndex];
                if (!price) continue;

                const teamLines = eventText.split('\\n').map((line) => line.trim()).filter(Boolean);
                const home = teamLines[0] || '';
                    const away = teamLines.find((line, index) => index > 0 && !scorePattern.test(line) && !periodPattern.test(line)) || '';
                const signal = {
                    ok: true,
                    home,
                    away,
                    line: displayLine,
                    selection: selectionIndex === 0 ? 'Over' : 'Under',
                    price: price.innerText.trim(),
                };
                if (targetLine !== null && Math.abs(parsedLine - targetLine) > 0.01) {
                    fallback = fallback || signal;
                    continue;
                }
                return signal;
            }
        }

        return fallback || { ok: false };
        """,
        selection_index,
        target_line,
    )

    if not result or not result.get("ok"):
        raise RuntimeError("Could not find an OU line on the current page.")

    odds = Decimal(TARGET_ODDS or result["price"])
    return BettingSignal(
        raw_text="dry run",
        odds=odds,
        selection=result["selection"],
        line=line_to_decimal(str(result["line"])),
        market="FULL TIME OVER/UNDER",
        expiry="30 mins",
        teams=f"{result['home']} vs {result['away']}",
    )


def main() -> None:
    profile_id = fetch_profile_ids(expected=1)[0]
    browser_info, _ = start_adspower_profile(profile_id)

    driver = None
    try:
        driver = connect_to_browser(browser_info, "DryRun")
        ensure_punterplay_tab(driver, "DryRun")
        wait_ready(driver)
        ensure_logged_in(driver)
        time.sleep(2)

        ensure_any_active_match(driver)
        signal = find_demo_signal(driver)
        print(f"Demo signal: {signal.teams} | {signal.selection_label} | odds {signal.odds}")

        _open_match_details(driver, signal)
        _select_full_time_ou_line(driver, signal)
        _fill_odds(driver, signal.odds)
        _fill_stake(driver, TEST_STAKE)
        _select_expiry_if_available(driver, "30 mins")

        print("Dry run prepared bet slip successfully:")
        print(f"  Event: {signal.teams}")
        print(f"  Market: FULL TIME OVER/UNDER")
        print(f"  Selection: {signal.selection_label}")
        print(f"  Odds filled: {signal.odds:.3f}")
        print(f"  Stake filled: {TEST_STAKE:.2f}")
        print("  Expiry: 30 mins")
        print("Place bet was NOT clicked.")
        print("Leaving the browser as-is for 30 seconds so you can see the result...")
        time.sleep(30)
    finally:
        close_driver_bridge(driver)


if __name__ == "__main__":
    main()