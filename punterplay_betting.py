"""Punterplay balance reading and bet placement helpers."""

from decimal import Decimal, ROUND_DOWN
import os
import re
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from login import (
    close_driver_bridge,
    connect_to_browser,
    ensure_punterplay_tab,
    _find_first_clickable,
    _find_first_visible,
    _set_input_value,
)
from signals import BettingSignal


STAKE_PERCENT = Decimal("0.05")
SUBMIT_BETS = os.getenv("PUNTERPLAY_SUBMIT_BETS", "false").lower() in {"1", "true", "yes", "on"}


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _visible_text(driver: webdriver.Remote) -> str:
    return driver.execute_script("return document.body ? document.body.innerText : '';") or ""


def _find_balance_in_text(text: str) -> Decimal:
    patterns = [
        r"Balance\s*[:\n ]+[^\d-]*(?P<amount>\d+(?:[,.]\d{1,2})?)",
        r"Available\s*[:\n ]+[^\d-]*(?P<amount>\d+(?:[,.]\d{1,2})?)",
        r"Funds\s*[:\n ]+[^\d-]*(?P<amount>\d+(?:[,.]\d{1,2})?)",
        r"£\s*(?P<amount>\d+(?:[,.]\d{1,2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return Decimal(match.group("amount").replace(",", "."))
    raise RuntimeError("Could not find account balance on the Punterplay page.")


def read_balance(driver: webdriver.Remote, profile_label: str) -> Decimal:
    ensure_punterplay_tab(driver, profile_label)
    WebDriverWait(driver, 20).until(
        lambda browser: browser.execute_script("return document.readyState") in {"interactive", "complete"}
    )
    balance = _find_balance_in_text(_visible_text(driver))
    print(f"[{profile_label}] Balance detected: {balance}")
    return balance


def refresh_profile_stake(session: dict) -> Decimal:
    profile_label = session["profile_label"]
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        balance = read_balance(driver, profile_label)
        stake = _money(balance * STAKE_PERCENT)
        print(f"[{profile_label}] Stake refreshed: {stake} (5% of balance)")
        return stake
    finally:
        close_driver_bridge(driver)


def refresh_all_stakes(sessions: list[dict]) -> dict[str, Decimal]:
    stakes = {}
    for session in sessions:
        stakes[session["profile_id"]] = refresh_profile_stake(session)
    return stakes


def _click_text(driver: webdriver.Remote, text: str, timeout: int = 20) -> None:
    lowered = text.lower()

    def find_element(browser):
        elements = browser.find_elements(By.XPATH, "//*[self::button or self::div or self::span or self::a]")
        for element in elements:
            try:
                element_text = (element.text or "").strip().lower()
                if lowered in element_text and element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue
        return False

    element = WebDriverWait(driver, timeout).until(find_element)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    element.click()


def _js_click_exact(driver: webdriver.Remote, text: str, selector: str = "button,[role=button]") -> bool:
    return bool(driver.execute_script(
        """
        const expected = arguments[0].trim().toLowerCase();
        const elements = Array.from(document.querySelectorAll(arguments[1]));
        const target = elements.find((element) => {
            const text = (element.innerText || element.textContent || '').trim().toLowerCase();
            const rect = element.getBoundingClientRect();
            return text === expected && rect.width && rect.height;
        });
        if (!target) return false;
        target.scrollIntoView({ block: 'center', inline: 'center' });
        target.click();
        return true;
        """,
        text,
        selector,
    ))


def _js_click_contains(driver: webdriver.Remote, text: str, selector: str = "button,[role=button]") -> bool:
    return bool(driver.execute_script(
        """
        const expected = arguments[0].trim().toLowerCase();
        const elements = Array.from(document.querySelectorAll(arguments[1]));
        const target = elements.find((element) => {
            const text = (element.innerText || element.textContent || '').trim().toLowerCase();
            const rect = element.getBoundingClientRect();
            return text.includes(expected) && rect.width && rect.height;
        });
        if (!target) return false;
        target.scrollIntoView({ block: 'center', inline: 'center' });
        target.click();
        return true;
        """,
        text,
        selector,
    ))


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _team_tokens(value: str) -> set[str]:
    return {token for token in _normalize_text(value).split() if len(token) > 2}


def _line_to_decimal(value: str) -> Decimal | None:
    value = value.strip().replace(",", ".")
    if "/" in value:
        parts = [Decimal(part) for part in value.split("/") if part]
        if not parts:
            return None
        return sum(parts) / Decimal(len(parts))
    try:
        return Decimal(value)
    except Exception:
        return None


def _line_matches(display_line: str, expected_line: Decimal) -> bool:
    parsed = _line_to_decimal(display_line)
    return parsed is not None and abs(parsed - expected_line) <= Decimal("0.01")


def _open_events_panel(driver: webdriver.Remote) -> None:
    if _js_click_exact(driver, "EVENTS"):
        time.sleep(0.5)


def _set_events_filter(driver: webdriver.Remote, value: str) -> None:
    if not value:
        return

    filter_input = driver.execute_script(
        """
        const panel = Array.from(document.querySelectorAll('div'))
            .find((element) => (element.innerText || '').includes('Highlights'));
        const input = panel ? panel.querySelector('input[type="text"], input:not([type])') : null;
        if (!input) return null;
        input.focus();
        input.value = arguments[0];
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
        """,
        value,
    )
    if filter_input:
        time.sleep(0.8)


def _select_league(driver: webdriver.Remote, signal: BettingSignal) -> None:
    _open_events_panel(driver)

    candidates = [
        signal.league_line,
        signal.league,
        signal.country,
    ]
    for candidate in [item for item in candidates if item]:
        if _js_click_exact(driver, candidate):
            time.sleep(2)
            if _page_contains_match(driver, signal):
                return

    for filter_text in [item for item in candidates if item]:
        _set_events_filter(driver, filter_text)
        if signal.country and _js_click_exact(driver, signal.country):
            time.sleep(0.5)
        if signal.league and (_js_click_exact(driver, signal.league) or _js_click_contains(driver, signal.league)):
            time.sleep(3)
            if _page_contains_match(driver, signal):
                return
        if filter_text and (_js_click_exact(driver, filter_text) or _js_click_contains(driver, filter_text)):
            time.sleep(3)
            if _page_contains_match(driver, signal):
                return

    raise RuntimeError(
        f"Could not select Punterplay league for signal: "
        f"country={signal.country}, league={signal.league}, league_line={signal.league_line}"
    )


def _page_contains_match(driver: webdriver.Remote, signal: BettingSignal) -> bool:
    if not signal.teams:
        return False

    home, away = _split_teams(signal.teams)
    body = _normalize_text(_visible_text(driver))
    return bool(_team_tokens(home) & set(body.split())) and bool(_team_tokens(away) & set(body.split()))


def _split_teams(teams: str) -> tuple[str, str]:
    parts = re.split(r"\s+vs\s+", teams, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return teams, ""
    return parts[0].strip(), parts[1].strip()


def _select_market(driver: webdriver.Remote, signal: BettingSignal) -> None:
    _select_league(driver, signal)
    _open_match_details(driver, signal)
    _select_full_time_ou_line(driver, signal)


def _click_ou_price(driver: webdriver.Remote, signal: BettingSignal) -> None:
    if not signal.teams:
        raise RuntimeError("Signal does not contain teams, cannot find event row.")

    home, away = _split_teams(signal.teams)
    home_tokens = list(_team_tokens(home))
    away_tokens = list(_team_tokens(away))
    selection_index = 0 if signal.selection.lower() == "over" else 1

    result = driver.execute_script(
        """
        const homeTokens = arguments[0];
        const awayTokens = arguments[1];
        const expectedLine = arguments[2];
        const selectionIndex = arguments[3];

        const normalize = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
        const hasAny = (text, tokens) => tokens.some((token) => text.includes(token));
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

        const linePattern = new RegExp('^\\\\d+(?:[.,]\\\\d+)?(?:/\\\\d+(?:[.,]\\\\d+)?)?$');

        let candidates = Array.from(document.querySelectorAll('div[class*="overflow-hidden"][class*="bg-white"]'))
            .map((element) => ({
                element,
                text: element.innerText || '',
                rect: element.getBoundingClientRect(),
                cols: Array.from(element.querySelectorAll('div[class*="w-1/6"]')),
            }))
            .filter((item) => {
                const normalized = normalize(item.text);
                return item.cols.length >= 3 && hasAny(normalized, homeTokens) && hasAny(normalized, awayTokens);
            })
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));

        if (!candidates.length) {
            candidates = Array.from(document.querySelectorAll('div'))
                .map((element) => ({
                    element,
                    text: element.innerText || '',
                    rect: element.getBoundingClientRect(),
                    cols: Array.from(element.querySelectorAll('div[class*="w-1/6"]')),
                }))
                .filter((item) => {
                    const normalized = normalize(item.text);
                    return item.cols.length >= 3 && hasAny(normalized, homeTokens) && hasAny(normalized, awayTokens);
                })
                .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
        }

        const summaries = [];
        for (const candidate of candidates) {
            const ouColumn = candidate.cols[2];
            const groups = Array.from(ouColumn.querySelectorAll('div[class*="border-b"]'));
            const fallbackGroups = groups.length ? groups : [ouColumn];
            for (const group of fallbackGroups) {
                const lines = (group.innerText || '').split('\\n').map((line) => line.trim()).filter(Boolean);
                const displayLine = lines.find((line) => linePattern.test(line));
                const prices = Array.from(group.querySelectorAll('button.price'));
                summaries.push({ displayLine, lines, prices: prices.map((price) => price.innerText.trim()) });
                const parsedLine = parseLine(displayLine);
                if (parsedLine === null || Math.abs(parsedLine - expectedLine) > 0.01) continue;
                const price = prices[selectionIndex];
                if (!price) continue;
                price.scrollIntoView({ block: 'center', inline: 'center' });
                price.click();
                return { ok: true, price: price.innerText.trim(), displayLine };
            }
        }
        return { ok: false, reason: 'No matching OU price found', summaries };
        """,
        home_tokens,
        away_tokens,
        float(signal.line),
        selection_index,
    )

    if not result or not result.get("ok"):
        raise RuntimeError(f"Could not find OU price for {signal.teams} {signal.selection_label}: {result}")

    print(f"Clicked OU price: line={result.get('displayLine')} price={result.get('price')}")
    time.sleep(1)


def _open_match_details(driver: webdriver.Remote, signal: BettingSignal) -> None:
    if not signal.teams:
        raise RuntimeError("Signal does not contain teams, cannot open match details.")

    home, away = _split_teams(signal.teams)
    home_tokens = list(_team_tokens(home))
    away_tokens = list(_team_tokens(away))

    if "/trade/single/" in driver.current_url and _page_contains_match(driver, signal):
        print(f"Match detail page is already open: {driver.current_url}")
        return

    result = driver.execute_script(
        """
        const homeTokens = arguments[0];
        const awayTokens = arguments[1];
        const normalize = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
        const hasAny = (text, tokens) => tokens.some((token) => text.includes(token));

        const collectRows = (selector) => Array.from(document.querySelectorAll(selector))
            .map((element) => ({ element, text: element.innerText || '', rect: element.getBoundingClientRect() }))
            .filter((item) => {
                const text = normalize(item.text);
                return item.rect.width && item.rect.height && hasAny(text, homeTokens) && hasAny(text, awayTokens);
            })
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));

        let rows = collectRows('div[class*="overflow-hidden"][class*="bg-white"]');
        if (!rows.length) rows = collectRows('div');

        for (const row of rows) {
            const detailButton = row.element.querySelector('[aria-label="View event details"]');
            if (detailButton) {
                detailButton.scrollIntoView({ block: 'center', inline: 'center' });
                detailButton.click();
                return { ok: true, clicked: 'View event details' };
            }

            const clickable = Array.from(row.element.querySelectorAll('[role="button"], button, span, div'))
                .find((element) => {
                    const text = normalize(element.innerText || element.textContent || '');
                    const rect = element.getBoundingClientRect();
                    return rect.width && rect.height && hasAny(text, homeTokens);
                });
            if (!clickable) {
                row.element.scrollIntoView({ block: 'center', inline: 'center' });
                row.element.click();
                return { ok: true, clicked: 'event row' };
            }
            clickable.scrollIntoView({ block: 'center', inline: 'center' });
            clickable.click();
            return { ok: true, clicked: clickable.innerText || clickable.textContent || '' };
        }
        return { ok: false, reason: 'Match row was not found' };
        """,
        home_tokens,
        away_tokens,
    )

    if not result or not result.get("ok"):
        raise RuntimeError(f"Could not open match details for {signal.teams}: {result}")

    print(f"Opened match details by clicking: {result.get('clicked')}")
    WebDriverWait(driver, 20).until(
        lambda browser: "/trade/single/" in browser.current_url
        or "FULL TIME" in _visible_text(browser).upper()
    )
    time.sleep(1)


def _select_full_time_ou_line(driver: webdriver.Remote, signal: BettingSignal) -> None:
    selection = signal.selection.lower()
    if selection not in {"over", "under"}:
        raise RuntimeError(f"Unsupported signal selection: {signal.selection}")

    result = driver.execute_script(
        """
        const selection = arguments[0];
        const expectedLine = arguments[1];

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

        const normalize = (value) => (value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
        const containers = Array.from(document.querySelectorAll('div, section'))
            .map((element) => ({ element, text: element.innerText || '', rect: element.getBoundingClientRect() }))
            .filter((item) => {
                const text = normalize(item.text);
                return item.rect.width && item.rect.height
                    && text.includes('full time')
                    && text.includes('over/under')
                    && text.includes('over')
                    && text.includes('under');
            })
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));

        const summaries = [];
        for (const container of containers) {
            const blocks = Array.from(container.element.querySelectorAll('button, [role="button"], div, span'))
                .map((element) => ({ element, text: (element.innerText || element.textContent || '').trim(), rect: element.getBoundingClientRect() }))
                .filter((item) => item.text && item.rect.width && item.rect.height);

            const buttons = Array.from(container.element.querySelectorAll('button'))
                .map((element) => ({ element, text: (element.innerText || element.textContent || '').trim(), rect: element.getBoundingClientRect() }))
                .filter((item) => item.text && item.rect.width && item.rect.height)
                .map((item) => ({
                    ...item,
                    lines: item.text.split('\\n').map((line) => line.trim()).filter(Boolean),
                }))
                .filter((item) => item.lines.length >= 2);

            const exactLineButtons = buttons.filter((item) => {
                const parsed = parseLine(item.lines[0]);
                return parsed !== null && Math.abs(parsed - expectedLine) <= 0.01;
            });

            for (const item of exactLineButtons) {
                summaries.push({ text: item.text, x: item.rect.x, y: item.rect.y });
            }

            if (!exactLineButtons.length) continue;
            const headerOver = blocks.find((item) => normalize(item.text) === 'over');
            const headerUnder = blocks.find((item) => normalize(item.text) === 'under');
            const leftLimit = container.rect.x + (container.rect.width / 2);
            const wantLeft = selection === 'over';

            const byHeader = exactLineButtons.filter((item) => {
                if (selection === 'over' && headerOver) return item.rect.x <= headerOver.rect.x + headerOver.rect.width + 80;
                if (selection === 'under' && headerUnder) return item.rect.x >= headerUnder.rect.x - 80;
                return wantLeft ? item.rect.x < leftLimit : item.rect.x >= leftLimit;
            });

            const target = (byHeader.length ? byHeader : exactLineButtons)
                .sort((a, b) => a.rect.y - b.rect.y)[0];
            target.element.scrollIntoView({ block: 'center', inline: 'center' });
            target.element.click();
            return { ok: true, clicked: target.text, summaries };
        }
        return { ok: false, reason: 'FULL TIME OVER/UNDER line not found', summaries };
        """,
        selection,
        float(signal.line),
    )

    if not result or not result.get("ok"):
        raise RuntimeError(f"Could not select FULL TIME OVER/UNDER {signal.selection_label}: {result}")

    print(f"Selected FULL TIME OVER/UNDER line: {result.get('clicked')}")
    time.sleep(1)


def _select_expiry_if_available(driver: webdriver.Remote, expiry: str) -> None:
    try:
        expiry_candidates = [expiry, expiry.replace(" min", " mins"), expiry.replace("mins", "min")]
        for candidate in expiry_candidates:
            if _js_click_contains(driver, f"Expiry {candidate}") or _js_click_exact(driver, candidate):
                time.sleep(0.5)
                return

        if _open_visible_expiry_field(driver):
            time.sleep(0.5)
            for candidate in expiry_candidates:
                if _js_click_exact(driver, candidate, "button,[role=button],div,span"):
                    time.sleep(0.5)
                    return
                if _js_click_contains(driver, candidate, "button,[role=button],div,span"):
                    time.sleep(0.5)
                    return

        if _open_expiry_dropdown(driver):
            for candidate in expiry_candidates:
                if _js_click_exact(driver, candidate) or _js_click_contains(driver, candidate):
                    time.sleep(0.5)
                    return
            time.sleep(0.5)
            return

        _click_text(driver, expiry_candidates[0], timeout=3)
    except Exception:
        print(f"Expiry selector '{expiry}' was not visible after price click; continuing.")


def _open_visible_expiry_field(driver: webdriver.Remote) -> bool:
    return bool(driver.execute_script(
        """
        const isVisible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height
                && rect.x >= 0 && rect.y >= 0
                && rect.x < window.innerWidth && rect.y < window.innerHeight;
        };
        const textOf = (element) => (element.innerText || element.value || element.textContent || '').trim();
        const panels = Array.from(document.querySelectorAll('div, section'))
            .filter((element) => {
                if (!isVisible(element)) return false;
                const text = textOf(element).toLowerCase();
                return text.includes('expiry') && text.includes('risk stake')
                    && text.includes('odds') && text.includes('place bet');
            })
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) - (br.width * br.height);
            });
        const panel = panels[0] || document.body;
        const expiryLabel = Array.from(panel.querySelectorAll('div, span, label'))
            .find((element) => isVisible(element) && textOf(element).toLowerCase() === 'expiry');
        if (!expiryLabel) return false;

        const labelRect = expiryLabel.getBoundingClientRect();
        const candidates = Array.from(panel.querySelectorAll('button,[role="button"],div,span,input'))
            .filter((element) => {
                if (element === expiryLabel || !isVisible(element)) return false;
                const rect = element.getBoundingClientRect();
                return rect.x >= labelRect.x - 12
                    && rect.x <= labelRect.x + 170
                    && rect.y > labelRect.y
                    && rect.y <= labelRect.y + 70;
            })
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aScore = Math.abs(ar.x - labelRect.x) + Math.abs(ar.y - (labelRect.y + 32));
                const bScore = Math.abs(br.x - labelRect.x) + Math.abs(br.y - (labelRect.y + 32));
                return aScore - bScore;
            });
        const target = candidates.find((element) => {
            const text = textOf(element).toLowerCase();
            return text === 'instant' || text.includes('min') || element.tagName === 'INPUT'
                || element.getAttribute('role') === 'button';
        }) || candidates[0];
        if (!target) return false;
        target.click();
        return true;
        """
    ))


def _open_expiry_dropdown(driver: webdriver.Remote) -> bool:
    return bool(driver.execute_script(
        """
        const labels = Array.from(document.querySelectorAll('div, span, label'));
        const label = labels.find((element) => (element.innerText || '').trim().toLowerCase() === 'expiry');
        if (!label) return false;
        const root = label.parentElement || document.body;
        const candidates = Array.from(root.querySelectorAll('button, input, div, span'))
            .filter((element) => {
                const text = (element.innerText || element.value || element.textContent || '').trim().toLowerCase();
                const rect = element.getBoundingClientRect();
                return rect.width && rect.height && (text === 'instant' || text.includes('min'));
            });
        const target = candidates[0];
        if (!target) return false;
        target.click();
        return true;
        """
    ))


def _visible_bet_slip_inputs(driver: webdriver.Remote) -> list:
    inputs = driver.execute_script(
        """
        const isVisible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height
                && rect.x >= 0 && rect.y >= 0
                && rect.x < window.innerWidth && rect.y < window.innerHeight;
        };
        const contextText = (element) => {
            let text = '';
            let parent = element.parentElement;
            for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
                text += '\\n' + (parent.innerText || '');
            }
            return text.toLowerCase();
        };
        return Array.from(document.querySelectorAll('input'))
            .filter((input) => isVisible(input) && !input.disabled && input.type !== 'checkbox')
            .map((input) => ({ input, rect: input.getBoundingClientRect(), context: contextText(input) }))
            .filter((item) => item.context.includes('risk stake') && item.context.includes('odds'))
            .sort((a, b) => a.rect.x - b.rect.x)
            .map((item) => item.input);
        """
    )
    return inputs or []


def _fill_odds(driver: webdriver.Remote, odds: Decimal) -> None:
    value = f"{odds:.3f}"
    inputs = _visible_bet_slip_inputs(driver)
    if len(inputs) >= 2:
        _set_input_value(driver, inputs[-1], value)
        print(f"Filled odds: {value}")
        return

    if not inputs:
        raise RuntimeError("Could not find Odds input in bet slip.")

    _set_input_value(driver, inputs[0], value)
    print(f"Filled odds: {value}")


def _fill_stake(driver: webdriver.Remote, stake: Decimal) -> None:
    value = f"{stake:.2f}"
    inputs = _visible_bet_slip_inputs(driver)
    if inputs:
        _set_input_value(driver, inputs[0], value)
        print(f"Filled stake: {stake:.2f}")
        return

    stake_input = _find_first_visible(driver, [
        (By.XPATH, "//input[contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'stake')]"),
        (By.XPATH, "//input[contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'amount')]"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'stake')]"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'amount')]"),
        (By.CSS_SELECTOR, "input[type='number']"),
        (By.CSS_SELECTOR, "input[inputmode='decimal']"),
        (By.CSS_SELECTOR, "input"),
    ], timeout=20)
    _set_input_value(driver, stake_input, f"{stake:.2f}")


def place_bet(session: dict, signal: BettingSignal, stake: Decimal) -> None:
    profile_label = session["profile_label"]
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        ensure_punterplay_tab(driver, profile_label)
        WebDriverWait(driver, 20).until(
            lambda browser: browser.execute_script("return document.readyState") in {"interactive", "complete"}
        )
        print(
            f"[{profile_label}] Placing bet: {signal.teams or 'unknown match'} | "
            f"{signal.market} | {signal.selection_label} | Expiry {signal.expiry} | stake {stake}"
        )
        _select_market(driver, signal)
        _fill_odds(driver, signal.odds)
        _fill_stake(driver, stake)
        _select_expiry_if_available(driver, signal.expiry)
        if not SUBMIT_BETS:
            print(f"[{profile_label}] Dry run: stake filled, final bet submit skipped.")
            return
        place_button = _find_first_clickable(driver, [
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'place bet')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm')]"),
        ], timeout=20)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", place_button)
        place_button.click()
        print(f"[{profile_label}] Bet submitted.")
    finally:
        close_driver_bridge(driver)


def place_bet_for_all_profiles(
    sessions: list[dict], signal: BettingSignal, stakes: dict[str, Decimal]
) -> None:
    for session in sessions:
        stake = stakes.get(session["profile_id"])
        if stake is None:
            stake = refresh_profile_stake(session)
            stakes[session["profile_id"]] = stake
        place_bet(session, signal, stake)