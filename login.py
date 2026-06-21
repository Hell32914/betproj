"""
BetInAsia / Black auto-login script via AdsPower profiles.
Starts AdsPower browser profiles, logs into BetInAsia, opens Black, and logs into Black.

Usage:
    pip install -r requirements.txt
    python login.py
"""

import os
import re
import sys
import time
import socket
import subprocess
import unicodedata
import requests
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import NoSuchWindowException, TimeoutException, WebDriverException
from selenium.webdriver.firefox.options import Options as FirefoxOptions

load_dotenv()

LOGIN_URL = "https://betinasia.com"
BETINASIA_URL_PART = "betinasia.com"
PORTAL_URL = "https://portal.betinasia.com/Dashboard/Products"
PORTAL_LOGIN_URL = "https://portal.betinasia.com/Account/Login"
BLACK_URL_PART = "black.betinasia.com"
BLACK_URL = "https://black.betinasia.com"
BLACK_SPORTSBOOK_URL = "https://black.betinasia.com/sportsbook"

ADSPOWER_API_URL = os.getenv("ADSPOWER_API_URL", "http://local.adspower.net:50325")
BETINASIA_EMAIL = os.getenv("BETINASIA_EMAIL")
BETINASIA_PASSWORD = os.getenv("BETINASIA_PASSWORD")
BLACK_USERNAME = os.getenv("BLACK_USERNAME")
BLACK_PASSWORD = os.getenv("BLACK_PASSWORD")
BETFAIR_USERNAME = os.getenv("BETFAIR_USERNAME", "asint2018")
BETFAIR_PASSWORD = os.getenv("BETFAIR_PASSWORD", ";4m/TyS7-u-bY?*")
BETFAIR_LOGIN_URL = "https://www.betfair.com/exchange/plus/"
BETFAIR_URL_PART = "betfair.com"
STAKE_PERCENT = Decimal(os.getenv("STAKE_PERCENT", "5"))
EURO_SYMBOL = "\u20ac"
TEAM_SUFFIXES = {
    "fc", "afc", "cf", "sc", "ac", "fk", "bk", "ik", "if", "sv", "jk",
    "kf", "club", "football", "team",
}
TEAM_SEARCH_ALIASES = {
    "alaves": ["deportivo alaves", "alavés", "deportivo alavés"],
    "sao paulo": ["são paulo", "sao paulo fc", "são paulo fc"],
}


def fetch_profile_ids(expected: int = 2) -> list[str]:
    """Fetch all profiles from AdsPower and return first `expected` IDs."""
    url = f"{ADSPOWER_API_URL}/api/v1/user/list"
    params = {"page": 1, "page_size": 100}
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"AdsPower /user/list error: {data.get('msg')}")
    profiles = data.get("data", {}).get("list", [])
    if len(profiles) < expected:
        raise RuntimeError(
            f"Expected at least {expected} profiles in AdsPower, found {len(profiles)}."
        )
    ids = [p["user_id"] for p in profiles[:expected]]
    print(f"Found profiles: {ids}")
    return ids


def _extract_connection_info(profile_data: dict, profile_id: str) -> dict | None:
    ws = profile_data.get("ws") or {}
    webdriver_path = profile_data.get("webdriver") or ""

    if ws.get("selenium") and webdriver_path:
        return {
            "ws": ws,
            "webdriver": webdriver_path,
            "debug_port": profile_data.get("debug_port"),
            "marionette_port": profile_data.get("marionette_port"),
        }

    print(f"Profile {profile_id} is active, but AdsPower did not return connection info.")
    return None


def start_adspower_profile(profile_id: str) -> tuple[dict, bool]:
    """Return AdsPower connection info and whether the profile was already running."""
    active_url = f"{ADSPOWER_API_URL}/api/v1/browser/active"
    active_resp = requests.get(active_url, params={"user_id": profile_id}, timeout=15)
    active_resp.raise_for_status()
    active_data = active_resp.json()
    if active_data.get("code") == 0 and active_data.get("data", {}).get("status") == "Active":
        print(f"Profile {profile_id} is already running, reusing session.")
        connection_info = _extract_connection_info(active_data["data"], profile_id)
        if connection_info:
            return connection_info, True

    print(f"Profile {profile_id} is not running or needs a fresh connection. Starting it...")
    url = f"{ADSPOWER_API_URL}/api/v1/browser/start"
    params = {"user_id": profile_id, "open_tabs": 1}
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"AdsPower failed to start profile {profile_id}: {data.get('msg')}"
        )
    connection_info = _extract_connection_info(data["data"], profile_id)
    if not connection_info:
        raise RuntimeError(f"AdsPower did not return Selenium connection info for profile {profile_id}.")
    return connection_info, False


def stop_adspower_profile(profile_id: str) -> None:
    """Stop an AdsPower profile."""
    url = f"{ADSPOWER_API_URL}/api/v1/browser/stop"
    params = {"user_id": profile_id}
    try:
        requests.get(url, params=params, timeout=10)
    except Exception:
        pass


def close_driver_bridge(driver: webdriver.Remote | None) -> None:
    if not driver:
        return

    proc = getattr(driver, "_gd_proc", None)
    if not proc or proc.poll() is not None:
        return

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_geckodriver(url: str, timeout: int = 15) -> None:
    """Poll GET /status until geckodriver responds or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{url}/status", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"geckodriver did not respond at {url} within {timeout}s")


def _attach_firefox_via_marionette(
    marionette_port: int, webdriver_path: str, profile_label: str
) -> webdriver.Remote:
    """Start geckodriver with --connect-existing, POST /session to attach
    to the already-running Firefox via Marionette, then reuse that session.
    """
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    gd_port = _free_port()
    gd_url = f"http://127.0.0.1:{gd_port}"

    proc = subprocess.Popen(
        [
            webdriver_path,
            "--port", str(gd_port),
            "--connect-existing",
            f"--marionette-port={marionette_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_for_geckodriver(gd_url)
    except Exception:
        proc.kill()
        raise

    session_id = None
    try:
        # POST /session to tell geckodriver to connect to the existing Firefox.
        resp = requests.post(
            f"{gd_url}/session",
            json={"capabilities": {"alwaysMatch": {"moz:firefoxOptions": {}}}},
            timeout=15,
        )
        resp.raise_for_status()
        session_id = resp.json()["value"]["sessionId"]
        print(f"[{profile_label}] geckodriver session: {session_id}")

        # Attach Selenium to the existing session without POSTing /session again.
        class _ReattachDriver(RemoteWebDriver):
            def start_session(self, capabilities):
                self.caps = {}

        driver = _ReattachDriver(command_executor=gd_url, options=FirefoxOptions())
        driver.session_id = session_id
        driver._gd_proc = proc
        return driver
    except Exception:
        if session_id:
            try:
                requests.delete(f"{gd_url}/session/{session_id}", timeout=5)
            except Exception:
                pass
        if proc.poll() is None:
            proc.kill()
        raise


def connect_to_browser(browser_info: dict, profile_label: str = "") -> webdriver.Remote:
    """Connect Selenium to the running AdsPower browser instance."""
    raw_address = browser_info["ws"]["selenium"]
    clean_address = (
        raw_address
        .replace("ws://", "")
        .replace("wss://", "")
        .split("/")[0]
    )
    webdriver_path = browser_info["webdriver"]
    is_firefox = "gecko" in webdriver_path.lower()

    print(f"[{profile_label}] Browser: {'Firefox' if is_firefox else 'Chrome'} | "
          f"Address: {clean_address} | Driver: {webdriver_path}")

    last_exc = None
    for attempt in range(1, 4):
        try:
            if is_firefox:
                # AdsPower exposes Firefox debugging and Marionette on separate ports.
                # geckodriver --connect-existing must attach to Marionette, not debug_port.
                marionette_port = int(browser_info.get("marionette_port") or clean_address.split(":")[-1])
                return _attach_firefox_via_marionette(
                    marionette_port, webdriver_path, profile_label
                )
            else:
                options = ChromeOptions()
                options.add_experimental_option("debuggerAddress", clean_address)
                service = ChromeService(executable_path=webdriver_path)
                return webdriver.Chrome(service=service, options=options)
        except Exception as exc:
            last_exc = exc
            print(f"[{profile_label}] Connection attempt {attempt}/3 failed: {exc}. Retrying in 3s...")
            time.sleep(3)

    raise RuntimeError(f"Could not connect to browser after 3 attempts: {last_exc}")


def _find_first_visible(driver: webdriver.Remote, selectors: list[tuple[str, str]], timeout: int = 30):
    """Return the first visible element matching one of the supplied locators."""
    last_exc = None

    def find_visible(browser):
        nonlocal last_exc
        for by, selector in selectors:
            try:
                elements = browser.find_elements(by, selector)
                for element in elements:
                    if element.is_displayed():
                        return element
            except NoSuchWindowException as exc:
                last_exc = exc
                print(f"Browser context was discarded while searching for {selector}; retrying current tab...")
                return False
            except WebDriverException as exc:
                last_exc = exc
                message = str(exc).lower()
                if "discarded" in message or "no such window" in message:
                    print(f"Browser context was discarded while searching for {selector}; retrying current tab...")
                return False
        return False

    try:
        return WebDriverWait(driver, timeout).until(find_visible)
    except Exception as exc:
        last_exc = last_exc or exc
        try:
            print(f"Current URL while searching element: {driver.current_url}")
            print(f"Page title while searching element: {driver.title}")
        except Exception:
            pass
        raise RuntimeError(f"Could not find visible element from selectors: {selectors}") from last_exc


def ensure_betinasia_tab(driver: webdriver.Remote, profile_label: str) -> None:
    """Navigate the current tab to BetInAsia without switching browser tabs."""
    print(f"[{profile_label}] Opening BetInAsia in current tab...")
    driver.get(LOGIN_URL)
    WebDriverWait(driver, 20).until(lambda browser: BETINASIA_URL_PART in browser.current_url)


def _find_first_clickable(driver: webdriver.Remote, selectors: list[tuple[str, str]], timeout: int = 30):
    """Return the first clickable element matching one of the supplied locators."""
    last_exc = None

    def find_clickable(browser):
        nonlocal last_exc
        for by, selector in selectors:
            try:
                elements = browser.find_elements(by, selector)
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        return element
            except Exception as exc:
                last_exc = exc
        return False

    try:
        return WebDriverWait(driver, timeout).until(find_clickable)
    except Exception as exc:
        raise RuntimeError(f"Could not find clickable element from selectors: {selectors}") from (last_exc or exc)


def _control_value(driver: webdriver.Remote, element) -> str:
    return driver.execute_script(
        """
        const element = arguments[0];
        const editableSelector = 'input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]';
        const target = element.matches?.(editableSelector)
            ? element
            : (element.querySelector?.(editableSelector) || (element.contains(document.activeElement) ? document.activeElement : element));
        if (!target) return '';
        if ('value' in target) return target.value || target.getAttribute('value') || '';
        return target.innerText || target.textContent || target.getAttribute('aria-valuetext') || '';
        """,
        element,
    ) or ""


def _set_input_value(driver: webdriver.Remote, element, value: str) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        driver.execute_script(
            """
            const element = arguments[0];
            const editableSelector = 'input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]';
            const input = element.matches?.(editableSelector) ? element : (element.querySelector?.(editableSelector) || element);
            input.focus?.({ preventScroll: true });
            input.click?.();
            """,
            element,
        )
    except Exception:
        pass

    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
        element.send_keys(value)
    except Exception:
        driver.execute_script(
            """
            const element = arguments[0];
            const value = arguments[1];
            const editableSelector = 'input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]';
            const input = element.matches?.(editableSelector)
                ? element
                : (element.querySelector?.(editableSelector) || (element.contains(document.activeElement) ? document.activeElement : element));
            input.focus?.({ preventScroll: true });
            if ('value' in input) {
                const setter = Object.getOwnPropertyDescriptor(input.__proto__, 'value')?.set;
                if (setter) setter.call(input, value);
                else input.value = value;
            } else {
                input.textContent = value;
            }
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: value }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            element,
            value,
        )

    current_value = _control_value(driver, element)
    if current_value == value:
        return

    driver.execute_script(
        """
        const element = arguments[0];
        const value = arguments[1];
        const editableSelector = 'input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]';
        const target = element.matches?.(editableSelector)
            ? element
            : (element.querySelector?.(editableSelector) || (element.contains(document.activeElement) ? document.activeElement : element));
        if ('value' in target) {
            const setter = Object.getOwnPropertyDescriptor(target.__proto__, 'value')?.set;
            if (setter) {
                setter.call(target, value);
            } else {
                target.value = value;
            }
        } else {
            target.textContent = value;
        }
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )


def open_login_tab(driver: webdriver.Remote, profile_label: str) -> None:
    """Open BetInAsia in the current browser tab."""
    print(f"[{profile_label}] Current URL before navigation: {driver.current_url}")

    ensure_betinasia_tab(driver, profile_label)
    print(f"[{profile_label}] Navigated to: {driver.current_url}")


def _visible_text_lower(driver: webdriver.Remote) -> str:
    return (_visible_page_text(driver) or "").lower()


def _visible_page_text(driver: webdriver.Remote) -> str:
    return driver.execute_script("return document.body ? document.body.innerText : '';") or ""


def _strip_diacritics(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value or "")
        if not unicodedata.combining(char)
    )


def _normalize_team_text(value: str | None) -> str:
    text = _strip_diacritics(value or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    parts = [part for part in text.split() if part]
    trimmed = [part for part in parts if part not in TEAM_SUFFIXES]
    return " ".join(trimmed or parts)


def _team_search_queries(team_name: str | None) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        value = " ".join((candidate or "").split()).strip()
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append(value)

    def normalized_parts(value: str | None) -> list[str]:
        text = _strip_diacritics(value or "").lower().strip()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return [part for part in text.split() if part]

    # Reserve / youth / women markers that some books strip from their listings
    # (e.g. "Defensor Sporting (Res)" appears as just "Defensor Sporting" on Black).
    reserve_markers = {
        "res", "reserve", "reserves",
        "ii", "iii", "iv",
        "b", "c",
        "u17", "u18", "u19", "u20", "u21", "u23",
        "w", "women", "fem", "femenino", "feminin", "ladies",
        "youth", "jr", "juniors", "academy",
    }
    raw_parts = normalized_parts(team_name)
    raw_normalized = " ".join(raw_parts)
    normalized = _normalize_team_text(team_name)
    normalized_core_parts = normalized.split()
    stripped_parts = [p for p in normalized_core_parts if p not in reserve_markers]
    if stripped_parts and stripped_parts != normalized_core_parts:
        add(" ".join(stripped_parts))
        if len(stripped_parts) >= 2:
            add(" ".join(stripped_parts[:2]))

    # Keep both forms: Black sometimes keeps prefixes like KF/FK/Club/FC in search
    # results, while our core normalizer intentionally strips them.
    add(raw_normalized)

    add(normalized)
    for alias in TEAM_SEARCH_ALIASES.get(normalized, []):
        add(alias)
    add(team_name)

    if team_name:
        without_parenthetical = re.sub(r"\([^)]*\)", " ", _strip_diacritics(team_name).lower())
        without_parenthetical = re.sub(r"[^a-z0-9]+", " ", without_parenthetical).strip()
        add(without_parenthetical)

    # Books often swap reserve-team suffixes between roman numerals, digits and B/C.
    reserve_aliases = {
        "ii": ["b", "2"],
        "iii": ["c", "3"],
        "iv": ["4"],
        "b": ["ii", "2"],
        "c": ["iii", "3"],
        "2": ["ii", "b"],
        "3": ["iii", "c"],
        "4": ["iv"],
    }
    if raw_parts:
        tail = raw_parts[-1]
        for alias in reserve_aliases.get(tail, []):
            add(" ".join(raw_parts[:-1] + [alias]))

    if len(normalized_core_parts) >= 2:
        add(" ".join(normalized_core_parts[:2]))
    if len(raw_parts) >= 2:
        add(" ".join(raw_parts[:2]))
    return queries


def _normalized_team_aliases(team_name: str | None) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        normalized = _normalize_team_text(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            aliases.append(normalized)

        raw = _strip_diacritics(candidate or "").lower().strip()
        raw = re.sub(r"[^a-z0-9]+", " ", raw)
        raw = " ".join(raw.split())
        if raw and raw not in seen:
            seen.add(raw)
            aliases.append(raw)

    for query in _team_search_queries(team_name):
        add(query)
    return aliases


def _signal_market_key(signal) -> str:
    market = (getattr(signal, "market", "") or "").lower()
    raw_text = (getattr(signal, "raw_text", "") or "").lower()
    if "second half" in market or "sh goals" in raw_text or "second half" in raw_text:
        return "second_half_goals"
    if "next goal" in market or "next goal" in raw_text:
        return "next_goal"
    return "full_time_goals"


def _black_market_headers(signal) -> list[str]:
    market_key = _signal_market_key(signal)
    if market_key == "second_half_goals":
        return [
            "2nd half goals",
            "second half goals",
            "2nd half total goals",
            "second half total goals",
            "2nd half asian total goals",
            "second half asian total goals",
            "asian total goals",
            "total goals",
        ]
    return ["asian total goals", "total goals"]


def _betfair_market_texts(signal, line_str: str) -> list[str]:
    market_key = _signal_market_key(signal)
    if market_key == "second_half_goals":
        return [
            "2nd half goals",
            "second half goals",
            "2nd half total goals",
            "second half total goals",
            "2nd half over under goals",
            "second half over under goals",
            f"2nd half over/under {line_str} goals",
            f"second half over/under {line_str} goals",
        ]
    return [
        f"over/under {line_str} goals",
        f"goals over/under {line_str}",
        f"goal line {line_str}",
        "goals over/under",
        "over under goals",
        "total goals",
        "alternative total goals",
        "goal lines",
    ]


def _signal_market_label(signal) -> str:
    market_key = _signal_market_key(signal)
    if market_key == "second_half_goals":
        return "Second Half Goals"
    if market_key == "next_goal":
        return "Next Goal / Full Time Goals"
    return "Full Time Goals"


def _click_text_if_visible(driver: webdriver.Remote, text: str, selector: str = "button,a,[role=button]") -> bool:
    return bool(driver.execute_script(
        """
        const expected = arguments[0].trim().toLowerCase();
        const selector = arguments[1];
        const isVisible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height && rect.x < window.innerWidth && rect.y < window.innerHeight;
        };
        const target = Array.from(document.querySelectorAll(selector))
            .filter(isVisible)
            .find((element) => (element.innerText || element.textContent || '').trim().toLowerCase() === expected);
        if (!target) return false;
        target.scrollIntoView({ block: 'center', inline: 'center' });
        target.click();
        return true;
        """,
        text,
        selector,
    ))


def _click_contains_if_visible(driver: webdriver.Remote, text: str, selector: str = "button,a,[role=button]") -> bool:
    return bool(driver.execute_script(
        """
        const expected = arguments[0].trim().toLowerCase();
        const selector = arguments[1];
        const isVisible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height && rect.x < window.innerWidth && rect.y < window.innerHeight;
        };
        const target = Array.from(document.querySelectorAll(selector))
            .filter(isVisible)
            .find((element) => (element.innerText || element.textContent || '').trim().toLowerCase().includes(expected));
        if (!target) return false;
        target.scrollIntoView({ block: 'center', inline: 'center' });
        target.click();
        return true;
        """,
        text,
        selector,
    ))


def _money_to_decimal(value: str) -> Decimal:
    normalized = value.replace(EURO_SYMBOL, "").replace(" ", "").replace(",", ".")
    normalized = re.sub(r"[^0-9.-]", "", normalized)
    if not normalized:
        raise ValueError(f"Could not parse money value from: {value!r}")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Could not parse money value from: {value!r}") from exc


def _format_stake_amount(amount: Decimal) -> str:
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if amount > 0 and rounded == Decimal("0.00"):
        rounded = Decimal("0.01")
    return format(rounded, "f")


def _calculate_stake_from_balance(balance: Decimal) -> Decimal:
    return balance * STAKE_PERCENT / Decimal("100")


def _open_black_account_menu(driver: webdriver.Remote, profile_label: str) -> None:
    if "settings" in _visible_text_lower(driver):
        return

    opened = False
    for _ in range(8):
        opened = bool(driver.execute_script(
            """
        const pageHasSettings = () => (document.body?.innerText || '').toLowerCase().includes('settings');
        if (pageHasSettings()) return true;

        const dispatchHover = (target, x, y) => {
            if (!target) return false;
            const events = [
                new PointerEvent('pointerover', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true }),
                new PointerEvent('pointerenter', { bubbles: false, cancelable: true, view: window, clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true }),
                new PointerEvent('pointermove', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true }),
                new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }),
                new MouseEvent('mouseenter', { bubbles: false, cancelable: true, view: window, clientX: x, clientY: y }),
                new MouseEvent('mousemove', { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }),
            ];
            for (const event of events) target.dispatchEvent(event);
            if (pageHasSettings()) return true;
            return false;
        };

        const hoverPoint = (x, y) => {
            let target = document.elementFromPoint(x, y);
            for (let depth = 0; target && depth < 6; depth += 1, target = target.parentElement) {
                if (dispatchHover(target, x, y)) return true;
            }
            return false;
        };

        const xValues = [34, 38, 30, 45, 54].map((offset) => window.innerWidth - offset);
        const yValues = [35, 39, 31, 45, 26];
        for (const y of yValues) {
            for (const x of xValues) {
                if (hoverPoint(x, y)) return true;
            }
        }
        return pageHasSettings();
            """
        ))
        if opened:
            break
        time.sleep(0.7)
    if not opened:
        raise RuntimeError("Could not open Black account menu from the profile icon/balance area.")
    WebDriverWait(driver, 10).until(lambda browser: "settings" in _visible_text_lower(browser))
    print(f"[{profile_label}] Opened Black account menu.")


def update_black_default_stake(driver: webdriver.Remote, profile_label: str) -> dict:
    balance = _read_black_balance(driver, profile_label)
    stake_amount = _calculate_stake_from_balance(balance)
    stake = _format_stake_amount(stake_amount)
    _set_black_default_stake(driver, stake, profile_label)
    return {"balance": str(balance), "stake": stake, "percent": str(STAKE_PERCENT)}


def _decimal_variants(value: Decimal) -> list[str]:
    normalized = format(value.normalize(), "f")
    fixed = format(value.quantize(Decimal("0.01")), "f")
    variants = {normalized, fixed, normalized.replace(".", ","), fixed.replace(".", ",")}

    scaled = int((value * 100).copy_abs()) % 100
    if scaled in {25, 75}:
        low = format((value - Decimal("0.25")).normalize(), "f")
        high = format((value + Decimal("0.25")).normalize(), "f")
        split_variants = {
            f"{low}/{high}",
            f"{low.replace('.', ',')}/{high.replace('.', ',')}",
            f"{low}/{high.replace('.', ',')}",
            f"{low.replace('.', ',')}/{high}",
            f"{low},{high}",
            f"{low.replace('.', ',')},{high.replace('.', ',')}",
            f"{low}, {high}",
            f"{low.replace('.', ',')}, {high.replace('.', ',')}",
        }
        variants.update(split_variants)

    return sorted(variants, key=len)


def _read_black_betslip_state(driver: webdriver.Remote) -> dict:
    return driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const looksLikeActiveTicket = (text) => {
            return (text.includes('stake') && text.includes('price') && text.includes('place'))
                || (text.includes('stake at price') && text.includes('ex. returns'))
                || text.includes('betslip');
        };
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.width > 150 && item.rect.height > 90)
            .filter((item) => looksLikeActiveTicket(item.text))
            .sort((a, b) => {
                const aTicket = a.text.includes('stake') && a.text.includes('price') && a.text.includes('place') ? 0 : 1;
                const bTicket = b.text.includes('stake') && b.text.includes('price') && b.text.includes('place') ? 0 : 1;
                return aTicket - bTicket || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height);
            })[0];
        if (!panel) return { ok: false, reason: 'betslip panel not found', text: document.body?.innerText || '' };

        const inputs = Array.from(panel.element.querySelectorAll('input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]'))
            .filter(isVisible)
            .filter((element) => !element.disabled && element.getAttribute('aria-disabled') !== 'true')
            .filter((element) => {
                const tag = element.tagName.toLowerCase();
                const role = (element.getAttribute('role') || '').toLowerCase();
                if ((tag === 'button' || role === 'button')
                    && !element.matches('input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"]')) {
                    return false;
                }
                return true;
            })
            .map((element) => ({
                tag: element.tagName.toLowerCase(),
                role: element.getAttribute('role') || '',
                value: element.value || element.getAttribute('value') || '',
                text: (element.innerText || element.textContent || '').trim(),
                placeholder: (element.getAttribute('placeholder') || '').trim(),
                aria: (element.getAttribute('aria-label') || '').trim(),
            }));
        const placeButton = Array.from(panel.element.querySelectorAll('button,[role="button"]'))
            .filter(isVisible)
            .find((element) => {
                const text = textOf(element);
                return text === 'place' || text.includes('place');
            });
        return {
            ok: true,
            text: panel.element.innerText || '',
            inputs,
            placeEnabled: !!placeButton && !placeButton.disabled && placeButton.getAttribute('aria-disabled') !== 'true',
        };
        """
    ) or {"ok": False, "reason": "betslip panel not found", "text": ""}


def _fill_betslip_input(driver: webdriver.Remote, element, value: str) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
    try:
        ActionChains(driver).move_to_element(element).click().perform()
    except Exception:
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].focus({preventScroll: true}); arguments[0].click?.();", element)

    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.DELETE)
        element.send_keys(value)
    except Exception:
        try:
            ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.DELETE).send_keys(value).perform()
        except Exception:
            _set_input_value(driver, element, value)
    else:
        current_value = _control_value(driver, element).strip()
        if current_value != value:
            try:
                ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.DELETE).send_keys(value).perform()
            except Exception:
                pass
            current_value = _control_value(driver, element).strip()
            if current_value != value:
                _set_input_value(driver, element, value)

    try:
        element.send_keys(Keys.TAB)
    except Exception:
        driver.execute_script("arguments[0].blur?.(); document.activeElement?.blur?.();", element)

    try:
        driver.execute_script(
            """
            const element = arguments[0];
            const value = arguments[1];
            const editableSelector = 'input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]';
            const target = element.matches?.(editableSelector)
                ? element
                : (element.querySelector?.(editableSelector) || (element.contains(document.activeElement) ? document.activeElement : element));
            if (!target) return;
            target.focus?.({ preventScroll: true });
            if ('value' in target && target.value !== value) {
                const setter = Object.getOwnPropertyDescriptor(target.__proto__, 'value')?.set;
                if (setter) setter.call(target, value);
                else target.value = value;
            } else if (!('value' in target) && (target.textContent || '').trim() !== value) {
                target.textContent = value;
            }
            target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: value }));
            target.dispatchEvent(new Event('change', { bubbles: true }));
            target.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            element,
            value,
        )
    except Exception:
        pass

    time.sleep(0.25)


def _open_black_search(driver: webdriver.Remote, profile_label: str) -> None:
    def search_dialog_open(browser: webdriver.Remote) -> bool:
        return _black_search_dialog_open(browser)

    current_url = (driver.current_url or "").lower()
    visible_text = _visible_text_lower(driver)
    login_gate = _has_visible_password_input(driver) or (
        "log in" in visible_text and "username" in visible_text and "password" in visible_text
    )
    if login_gate:
        print(f"[{profile_label}] Black search detected login gate; restoring Black session.")
        _login_black(driver, profile_label)
        driver.get(BLACK_SPORTSBOOK_URL)
        _wait_document_ready(driver)
        time.sleep(1.5)
        current_url = (driver.current_url or "").lower()

    if "/orders" in current_url:
        driver.get(BLACK_SPORTSBOOK_URL)
        _wait_document_ready(driver)
        time.sleep(2)
        print(f"[{profile_label}] Returned Black to sportsbook before opening match search.")

    _dismiss_black_update_banner(driver, profile_label)

    def open_search_with_shortcut(browser: webdriver.Remote) -> bool:
        shortcut_attempts = [
            lambda: ActionChains(browser).key_down(Keys.CONTROL).send_keys("f").key_up(Keys.CONTROL).perform(),
            lambda: browser.find_element(By.TAG_NAME, "body").send_keys(Keys.CONTROL, "f"),
            lambda: browser.find_element(By.TAG_NAME, "html").send_keys(Keys.CONTROL, "f"),
        ]
        for attempt in shortcut_attempts:
            try:
                attempt()
                if WebDriverWait(browser, 2).until(lambda current: search_dialog_open(current)):
                    print(f"[{profile_label}] Opened Black search via Ctrl+F fallback.")
                    return True
            except Exception:
                continue
        return False

    def find_orders_search_candidate(browser: webdriver.Remote):
        return browser.execute_script(
            """
            const textOf = (element) => (element.innerText || element.textContent || '').trim();
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };

            const navTexts = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element).toLowerCase() }))
                .filter((item) => item.rect.y < 95 && item.text === 'orders')
                .sort((a, b) => {
                    const aArea = a.rect.width * a.rect.height;
                    const bArea = b.rect.width * b.rect.height;
                    return aArea - bArea;
                });

            for (const item of navTexts) {
                const ordersNode = item.element.closest('li,button,a,[role="button"]') || item.element;
                const listSibling = ordersNode.nextElementSibling;
                if (listSibling && isVisible(listSibling)) {
                    return listSibling;
                }

                const centerY = item.rect.y + item.rect.height / 2;
                const containers = [];
                let parent = item.element.parentElement;
                for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
                    const rect = parent.getBoundingClientRect();
                    if (rect.y < 110 && rect.height < 120 && rect.width < window.innerWidth * 0.9) {
                        containers.push(parent);
                    }
                }

                const candidates = containers.flatMap((container) => Array.from(container.children))
                    .filter((element) => element !== item.element && isVisible(element))
                    .map((element) => {
                        const rect = element.getBoundingClientRect();
                        const marker = [
                            element.getAttribute('aria-label') || '',
                            element.getAttribute('data-testid') || '',
                            element.className?.toString() || '',
                            textOf(element),
                            element.querySelector('svg,path') ? 'has-svg' : '',
                        ].join(' ').toLowerCase();
                        return { element, rect, marker };
                    })
                    .filter((candidate) => candidate.rect.x > item.rect.right + 5)
                    .filter((candidate) => candidate.rect.x < item.rect.right + 140)
                    .filter((candidate) => Math.abs((candidate.rect.y + candidate.rect.height / 2) - centerY) < 24)
                    .filter((candidate) => candidate.rect.width * candidate.rect.height > 50)
                    .sort((a, b) => {
                        const aSearch = /search|magnif|lens|has-svg/.test(a.marker) ? 0 : 1;
                        const bSearch = /search|magnif|lens|has-svg/.test(b.marker) ? 0 : 1;
                        const aDistance = Math.abs(a.rect.x - (item.rect.right + 52));
                        const bDistance = Math.abs(b.rect.x - (item.rect.right + 52));
                        return aSearch - bSearch || aDistance - bDistance;
                    });
                const first = candidates[0]?.element;
                if (first) {
                    return first;
                }

                const directSibling = item.element.parentElement?.nextElementSibling;
                if (directSibling && isVisible(directSibling)) {
                    return directSibling;
                }
            }

            return null;
            """
        )

    if search_dialog_open(driver):
        print(f"[{profile_label}] Black search is already open.")
        return

    result = driver.execute_script(
        """
        const dialogAlreadyOpen = () => {
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ');
            return Array.from(document.querySelectorAll('div,section,aside'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                .filter((item) => item.rect.y < 420 && item.rect.width > 240 && item.rect.height > 120)
                .some((item) => {
                    const hasVisibleInput = Array.from(item.element.querySelectorAll('input'))
                        .some((input) => isVisible(input) && !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()));
                    if (!hasVisibleInput) return false;
                    return item.text.includes('live events')
                        || item.text.includes('use ctrl-f')
                        || item.text.includes('use ctrl f');
                });
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim();
        if (dialogAlreadyOpen()) return true;

        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0
                && rect.x < window.innerWidth
                && rect.y < window.innerHeight;
        };

        const fireClick = (element, x, y) => {
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                element.dispatchEvent(new MouseEvent(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
            }
            try { element.click?.(); } catch (error) {}
            return dialogAlreadyOpen();
        };

        const clickElement = (element) => {
            const clickable = element.closest('button,a,[role="button"],li') || element;
            clickable.scrollIntoView?.({ block: 'center', inline: 'center' });

            const rect = clickable.getBoundingClientRect();
            const x = rect.x + rect.width / 2;
            const y = rect.y + rect.height / 2;

            const targets = [];
            const pushTarget = (target) => {
                if (target && !targets.includes(target)) targets.push(target);
            };

            pushTarget(clickable);
            pushTarget(document.elementFromPoint(x, y));
            pushTarget(clickable.querySelector?.('svg'));
            pushTarget(clickable.querySelector?.('path'));
            pushTarget(clickable.firstElementChild);

            let parent = document.elementFromPoint(x, y);
            for (let depth = 0; parent && depth < 5; depth += 1, parent = parent.parentElement) {
                pushTarget(parent);
            }

            for (const target of targets) {
                if (fireClick(target, x, y)) return true;
            }

            return dialogAlreadyOpen();
        };

        const summarize = (element) => {
            const rect = element.getBoundingClientRect();
            return {
                text: textOf(element).slice(0, 80),
                tag: element.tagName.toLowerCase(),
                className: (element.className || '').toString().slice(0, 120),
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
            };
        };

        const navTexts = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element).toLowerCase() }))
            .filter((item) => item.rect.y < 95 && item.text === 'orders')
            .sort((a, b) => {
                const aArea = a.rect.width * a.rect.height;
                const bArea = b.rect.width * b.rect.height;
                return aArea - bArea;
            });

        for (const item of navTexts) {
            const centerY = item.rect.y + item.rect.height / 2;
            const ordersNode = item.element.closest('li,button,a,[role="button"]') || item.element;
            const listSibling = ordersNode.nextElementSibling;
            if (listSibling && isVisible(listSibling) && clickElement(listSibling)) {
                return true;
            }
            const containers = [];
            let parent = item.element.parentElement;
            for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
                const rect = parent.getBoundingClientRect();
                if (rect.y < 110 && rect.height < 120 && rect.width < window.innerWidth * 0.9) {
                    containers.push(parent);
                }
            }
            const candidates = containers.flatMap((container) => Array.from(container.children))
                .filter((element) => element !== item.element && isVisible(element))
                .map((element) => {
                    const rect = element.getBoundingClientRect();
                    const marker = [
                        element.getAttribute('aria-label') || '',
                        element.getAttribute('data-testid') || '',
                        element.className?.toString() || '',
                        textOf(element),
                        element.querySelector('svg,path') ? 'has-svg' : '',
                    ].join(' ').toLowerCase();
                    return { element, rect, marker };
                })
                .filter((candidate) => candidate.rect.x > item.rect.right + 5)
                .filter((candidate) => candidate.rect.x < item.rect.right + 140)
                .filter((candidate) => Math.abs((candidate.rect.y + candidate.rect.height / 2) - centerY) < 24)
                .filter((candidate) => candidate.rect.width * candidate.rect.height > 50)
                .sort((a, b) => {
                    const aSearch = /search|magnif|lens|has-svg/.test(a.marker) ? 0 : 1;
                    const bSearch = /search|magnif|lens|has-svg/.test(b.marker) ? 0 : 1;
                    const aDistance = Math.abs(a.rect.x - (item.rect.right + 52));
                    const bDistance = Math.abs(b.rect.x - (item.rect.right + 52));
                    return aSearch - bSearch || aDistance - bDistance;
                });
            for (const candidate of candidates.slice(0, 12)) {
                if (clickElement(candidate.element)) return true;
            }

            const directSibling = item.element.parentElement?.nextElementSibling;
            if (directSibling && isVisible(directSibling) && clickElement(directSibling)) {
                return true;
            }

            return {
                ok: false,
                reason: 'orders-neighbour-click-failed',
                orders: summarize(item.element),
                ordersNode: summarize(ordersNode),
                candidates: candidates.slice(0, 8).map((candidate) => ({ ...summarize(candidate.element), marker: candidate.marker.slice(0, 120) })),
                listSibling: listSibling ? summarize(listSibling) : null,
                directSibling: directSibling ? summarize(directSibling) : null,
            };
        }

        return {
            ok: false,
            reason: 'orders-not-found',
            topNav: Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
                .filter(isVisible)
                .map((element) => summarize(element))
                .filter((item) => item.y < 95)
                .slice(0, 20),
        };
        """
    )
    if result is not True and isinstance(result, dict) and result.get("reason") == "orders-neighbour-click-failed":
        candidate = find_orders_search_candidate(driver)
        if candidate is not None:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", candidate)
            except WebDriverException:
                pass

            fallback_attempts = [
                ("native", lambda: candidate.click()),
                ("actions", lambda: ActionChains(driver).move_to_element(candidate).pause(0.1).click(candidate).perform()),
                (
                    "center-point",
                    lambda: driver.execute_script(
                        """
                        const element = arguments[0];
                        const rect = element.getBoundingClientRect();
                        const x = rect.x + rect.width / 2;
                        const y = rect.y + rect.height / 2;
                        const target = document.elementFromPoint(x, y) || element;
                        for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                            const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                            target.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerType: 'mouse' }));
                        }
                        target.click?.();
                        """,
                        candidate,
                    ),
                ),
            ]

            for attempt_name, attempt in fallback_attempts:
                try:
                    attempt()
                    if WebDriverWait(driver, 2).until(lambda browser: search_dialog_open(browser)):
                        print(f"[{profile_label}] Opened Black search via {attempt_name} fallback.")
                        return
                except Exception:
                    continue

        if open_search_with_shortcut(driver):
            return

    if result is not True and open_search_with_shortcut(driver):
        return

    if result is not True:
        raise RuntimeError(f"Could not open Black search. Details: {result!r}")
    WebDriverWait(driver, 10).until(search_dialog_open)
    print(f"[{profile_label}] Opened Black search.")


def _fill_black_search(driver: webdriver.Remote, query: str, profile_label: str) -> None:
    normalized_query = query.strip()
    query_words = [word for word in _normalize_team_text(normalized_query).split() if len(word) >= 3]

    def read_search_state(browser: webdriver.Remote):
        return browser.execute_script(
            """
            const requested = arguments[0];
            const queryWords = arguments[1] || [];
            const normalize = (value) => (value || '')
                .toLowerCase()
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/[^a-z0-9]+/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const normalizedRequest = normalize(requested);
            const bodyText = document.body?.innerText || '';
            const body = normalize(bodyText);
            const noResults = body.includes('no results found') || body.includes('no events found');

            const hasVisibleSearchInput = (root) => Array.from(root.querySelectorAll('input'))
                .some((input) => isVisible(input) && !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()));

            const roots = Array.from(document.querySelectorAll('aside,section,main,div'))
                .filter(isVisible)
                .map((element) => ({
                    element,
                    rect: element.getBoundingClientRect(),
                    text: element.innerText || element.textContent || '',
                    hasInput: hasVisibleSearchInput(element),
                }))
                .filter((item) => item.rect.y < 460)
                .filter((item) => item.rect.width > 240 && item.rect.height > 70)
                .filter((item) => {
                    const text = normalize(item.text);
                    const modalMarker = text.includes('live events') || text.includes('use ctrl f');
                    const searchRoot = item.hasInput && (modalMarker || text.includes('all sports'));
                    return searchRoot;
                })
                .sort((a, b) => {
                    const aModal = normalize(a.text).includes('live events') || normalize(a.text).includes('use ctrl f') ? 0 : 1;
                    const bModal = normalize(b.text).includes('live events') || normalize(b.text).includes('use ctrl f') ? 0 : 1;
                    return aModal - bModal || (a.hasInput === b.hasInput ? 0 : (a.hasInput ? -1 : 1)) || (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height);
                });
            const root = roots[0]?.element || document.body;
            const resultRows = Array.from(root.querySelectorAll('li,a,button,[role="button"],[role="option"],[role="listitem"],article,div'))
                .filter(isVisible)
                .map((element) => {
                    const row = element.closest('a,button,li,[role="button"],[role="option"],[role="listitem"],article') || element;
                    const rect = row.getBoundingClientRect();
                    const text = row.innerText || row.textContent || '';
                    return { row, rect, text, normalized: normalize(text) };
                })
                .filter((item) => item.text && item.text.length < 500)
                .filter((item) => item.rect.width > 45 && item.rect.height > 12)
                .filter((item) => {
                    if (normalizedRequest && item.normalized.includes(normalizedRequest)) return true;
                    return queryWords.some((word) => item.normalized.includes(normalize(word)));
                })
                .filter((item, index, array) => array.findIndex((other) => other.row === item.row) === index)
                .slice(0, 6);
            if (resultRows.length) {
                return {
                    ok: true,
                    reason: 'result-row',
                    rows: resultRows.map((item) => item.text.split('\\n').map((line) => line.trim()).filter(Boolean).slice(0, 4).join(' | ').slice(0, 180)),
                };
            }
            if (noResults) {
                return {
                    ok: false,
                    reason: 'no-results',
                    text: bodyText.split('\\n').map((line) => line.trim()).filter(Boolean).slice(0, 10).join(' | ').slice(0, 500),
                };
            }
            return false;
            """,
            normalized_query,
            query_words,
        )

    search_input = WebDriverWait(driver, 10).until(
        lambda browser: browser.execute_script(
            """
            const dialogOpen = () => {
                const isVisible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0
                        && rect.bottom > 0
                        && rect.right > 0;
                };
                const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ');
                return Array.from(document.querySelectorAll('div,section,aside'))
                    .filter(isVisible)
                    .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                    .filter((item) => item.rect.y < 420 && item.rect.width > 240 && item.rect.height > 120)
                    .some((item) => {
                        const hasVisibleInput = Array.from(item.element.querySelectorAll('input'))
                            .some((input) => isVisible(input) && !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()));
                        if (!hasVisibleInput) return false;
                        return item.text.includes('live events')
                            || item.text.includes('use ctrl-f')
                            || item.text.includes('use ctrl f');
                    });
            };
            if (!dialogOpen()) return null;
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();

            const modalRoots = Array.from(document.querySelectorAll('div,section,aside'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                .filter((item) => item.rect.y < 380)
                .filter((item) => item.rect.width > 240)
                .filter((item) => item.text.includes('all sports') || item.text.includes('live events') || item.text.includes('use ctrl-f as hotkey'))
                .sort((a, b) => (a.rect.y - b.rect.y) || (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));

            for (const root of modalRoots) {
                const localInputs = Array.from(root.element.querySelectorAll('input'))
                    .filter(isVisible)
                    .filter((input) => !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()))
                    .map((input) => {
                        const marker = [
                            input.getAttribute('placeholder') || '',
                            input.getAttribute('aria-label') || '',
                            input.name || '',
                            String(input.className || ''),
                            textOf(input.parentElement || input),
                        ].join(' ').toLowerCase();
                        return { input, marker, rect: input.getBoundingClientRect() };
                    })
                    .sort((a, b) => {
                        const aSearch = /search|league|game|team|event/.test(a.marker) ? 0 : 1;
                        const bSearch = /search|league|game|team|event/.test(b.marker) ? 0 : 1;
                        return aSearch - bSearch || a.rect.y - b.rect.y || a.rect.x - b.rect.x;
                    });
                if (localInputs[0]) return localInputs[0].input;
            }

            const inputs = Array.from(document.querySelectorAll('input'))
                .filter(isVisible)
                .filter((input) => !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()))
                .map((input) => {
                    const rect = input.getBoundingClientRect();
                    const marker = [
                        input.getAttribute('placeholder') || '',
                        input.getAttribute('aria-label') || '',
                        input.name || '',
                        String(input.className || ''),
                        textOf(input.parentElement || input),
                    ].join(' ').toLowerCase();
                    return { input, rect, marker };
                })
                .filter((item) => item.rect.y < 320)
                .sort((a, b) => {
                    const aSearch = /search|league|game/.test(a.marker) ? 0 : 1;
                    const bSearch = /search|league|game/.test(b.marker) ? 0 : 1;
                    return aSearch - bSearch || a.rect.y - b.rect.y;
                });
            return inputs.length ? inputs[0].input : null;
            """
        )
    )
    try:
        driver.execute_script(
            """
            const input = arguments[0];
            input.focus({ preventScroll: true });
            if (typeof input.click === 'function') input.click();
            """,
            search_input,
        )
    except Exception:
        pass

    for _ in range(2):
        search_input.send_keys(Keys.CONTROL, "a")
        search_input.send_keys(Keys.DELETE)
        _set_input_value(driver, search_input, normalized_query)
        try:
            WebDriverWait(driver, 3).until(
                lambda browser: (search_input.get_attribute("value") or "").strip().lower() == normalized_query.lower()
            )
            break
        except Exception:
            continue
    final_value = (search_input.get_attribute("value") or "").strip().lower()
    if final_value != normalized_query.lower():
        raise RuntimeError(
            f"Black search input did not keep the requested query. Expected {normalized_query!r}, got {final_value!r}."
        )

    try:
        driver.execute_script(
            """
            const input = arguments[0];
            const value = arguments[1];
            input.focus({ preventScroll: true });
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: value }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            search_input,
            normalized_query,
        )
    except Exception:
        pass

    try:
        search_state = WebDriverWait(driver, 4).until(read_search_state)
    except TimeoutException as first_timeout:
        if _black_search_dialog_open(driver):
            try:
                search_input.send_keys(Keys.ENTER)
            except Exception:
                try:
                    driver.execute_script(
                        """
                        const input = arguments[0];
                        input.focus({ preventScroll: true });
                        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
                        input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
                        """,
                        search_input,
                    )
                except Exception:
                    pass
        try:
            search_state = WebDriverWait(driver, 11).until(read_search_state)
        except TimeoutException as exc:
            search_state = driver.execute_script(
                """
                const requested = arguments[0];
                const normalize = (value) => (value || '')
                    .toLowerCase()
                    .normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/[^a-z0-9]+/g, ' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const isVisible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0
                        && rect.bottom > 0
                        && rect.right > 0
                        && rect.x < window.innerWidth
                        && rect.y < window.innerHeight;
                };
                const visibleInputs = Array.from(document.querySelectorAll('input'))
                    .filter(isVisible)
                    .map((input) => ({
                        value: input.value || input.getAttribute('value') || '',
                        placeholder: input.getAttribute('placeholder') || '',
                        aria: input.getAttribute('aria-label') || '',
                    }))
                    .slice(0, 8);
                const textLines = (document.body?.innerText || '')
                    .split('\\n')
                    .map((line) => line.trim())
                    .filter(Boolean)
                    .slice(0, 24);
                const rowSamples = Array.from(document.querySelectorAll('li,a,button,[role="button"],[role="option"],[role="listitem"],article,div'))
                    .filter(isVisible)
                    .map((element) => (element.innerText || element.textContent || '').trim())
                    .filter((text) => text && text.length < 300)
                    .filter((text, index, array) => array.indexOf(text) === index)
                    .slice(0, 12);
                const bodyLower = (document.body?.innerText || '').toLowerCase();
                return {
                    ok: false,
                    reason: 'search-timeout',
                    requested,
                    normalizedRequested: normalize(requested),
                    dialogOpen: Array.from(document.querySelectorAll('div,section,aside'))
                        .filter(isVisible)
                        .map((element) => ({ element, rect: element.getBoundingClientRect(), text: (element.innerText || element.textContent || '').toLowerCase().replace(/\\s+/g, ' ') }))
                        .filter((item) => item.rect.y < 420 && item.rect.width > 240 && item.rect.height > 120)
                        .some((item) => {
                            const hasVisibleInput = Array.from(item.element.querySelectorAll('input'))
                                .some((input) => isVisible(input) && !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()));
                            if (!hasVisibleInput) return false;
                            return item.text.includes('live events')
                                || item.text.includes('use ctrl-f')
                                || item.text.includes('use ctrl f');
                        }),
                    inputs: visibleInputs,
                    text: textLines.join(' | ').slice(0, 900),
                    rows: rowSamples,
                };
                """,
                normalized_query,
            )
            if isinstance(search_state, dict):
                inputs = search_state.get("inputs") or []
                query_set = any((str(item.get("value", "")).strip().lower() == normalized_query.lower()) for item in inputs)
                if search_state.get("dialogOpen") and query_set:
                    print(
                        f"[{profile_label}] Black search rows not visible yet for {normalized_query!r}; continuing with typed query.",
                        flush=True,
                    )
                    search_state = {"ok": True, "reason": "search-timeout-soft", "requested": normalized_query}
            if not search_state or not search_state.get("ok"):
                raise RuntimeError(
                    f"Black search timed out waiting for visible match rows for {normalized_query!r}. State: {search_state!r}"
                ) from (exc or first_timeout)

    if not search_state or not search_state.get("ok"):
        raise RuntimeError(f"Black search returned no visible match rows for {normalized_query!r}. State: {search_state!r}")
    time.sleep(1.2)
    print(f"[{profile_label}] Searched Black live events for first team: {normalized_query}")


def _search_black_live_events(
    driver: webdriver.Remote,
    team_name: str,
    profile_label: str,
    alternate_team_names: list[str | None] | None = None,
) -> str:
    last_error = None
    queries: list[str] = []
    seen_queries: set[str] = set()

    def add_queries(name: str | None) -> None:
        for query in _team_search_queries(name):
            key = query.strip().lower()
            if not key or key in seen_queries:
                continue
            seen_queries.add(key)
            queries.append(query)

    add_queries(team_name)
    for alternate_name in alternate_team_names or []:
        add_queries(alternate_name)

    for query in queries:
        try:
            _fill_black_search(driver, query, profile_label)
            time.sleep(5)
            return query
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"No usable Black search query for team: {team_name}")


def _confirm_black_place_order(driver: webdriver.Remote, profile_label: str) -> None:
    try:
        modal_state = WebDriverWait(driver, 5).until(lambda browser: browser.execute_script(
            """
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
            const dialogs = Array.from(document.querySelectorAll('div,section,aside'))
                .filter(isVisible)
                .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
                .filter((item) => item.rect.width > 220 && item.rect.height > 120)
                .filter((item) => item.text.includes('place order') || item.text.includes('are you sure you want to place this order'))
                .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
            const dialog = dialogs[0];
            if (!dialog) return false;
            const buttons = Array.from(dialog.element.querySelectorAll('button,[role="button"]'))
                .filter(isVisible)
                .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }));
            const placeOrderButton = buttons
                .filter((item) => item.text === 'place order' || item.text.includes('place order'))
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
            if (!placeOrderButton) {
                return { ok: false, reason: 'place order button not found', text: dialog.text.slice(0, 400) };
            }
            return { ok: true, button: placeOrderButton.element, text: dialog.text.slice(0, 400) };
            """
        ))
    except TimeoutException:
        return

    if not modal_state or modal_state is False:
        return
    if not modal_state.get("ok"):
        raise RuntimeError(f"Black order confirmation dialog appeared but could not be confirmed: {modal_state!r}")

    button = modal_state.get("button")
    if not button:
        raise RuntimeError(f"Black order confirmation dialog appeared but no Place Order button was returned: {modal_state!r}")

    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", button)
    try:
        button.click()
    except Exception:
        try:
            ActionChains(driver).move_to_element(button).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", button)
    print(f"[{profile_label}] Confirmed Black Place Order dialog.")


def _open_black_top_orders(driver: webdriver.Remote, profile_label: str) -> bool:
    target = driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0
                && rect.x < window.innerWidth
                && rect.y < window.innerHeight;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const orders = Array.from(document.querySelectorAll('button,a,[role="button"],div,span,li'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.y < 110)
            .filter((item) => item.text === 'orders')
            .sort((a, b) => a.rect.x - b.rect.x || a.rect.y - b.rect.y)[0];
        return orders ? (orders.element.closest('button,a,[role="button"],li') || orders.element) : null;
        """
    )
    if not target:
        print(f"[{profile_label}] Could not find top Black Orders tab.")
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", target)
    except Exception:
        pass

    for attempt in (
        lambda: target.click(),
        lambda: ActionChains(driver).move_to_element(target).pause(0.1).click(target).perform(),
        lambda: driver.execute_script("arguments[0].click();", target),
    ):
        try:
            attempt()
            if WebDriverWait(driver, 6).until(lambda browser: browser.execute_script(
                """
                const isVisible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0
                        && rect.bottom > 0
                        && rect.right > 0;
                };
                const text = (document.body?.innerText || '').toLowerCase();
                if ((window.location.pathname || '').toLowerCase().includes('/orders')) return true;
                return text.includes('selection') && text.includes('status') && text.includes('stake') && text.includes('profit/loss');
                """
            )):
                print(f"[{profile_label}] Opened Black top Orders view.")
                return True
        except Exception:
            continue

    print(f"[{profile_label}] Could not open Black top Orders view.")
    return False


def _normalize_black_order_status(raw_status: str) -> str:
    normalized = (raw_status or '').strip().lower()
    if normalized in {'reconciled', 'accepted', 'matched', 'confirmed', 'done', 'success'}:
        return 'accepted'
    if normalized in {'failed', 'rejected', 'declined', 'unplaced'}:
        return 'rejected'
    if normalized in {'cancelled', 'canceled', 'void'}:
        return 'cancelled'
    if normalized in {'open', 'pending', 'processing'}:
        return 'pending'
    return 'unknown'


class BlackSelectionMissingError(RuntimeError):
    """Raised when the requested Asian Total Goals selection never shows up on the match page after retries."""


def _read_black_orders_max_id(driver: webdriver.Remote, profile_label: str) -> int | None:
    try:
        result = driver.execute_script(
            """
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0;
            };
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const textOf = (element) => normalize(element.innerText || element.textContent || '');
            const hasOrdersHeaders = (text) => {
                const lower = (text || '').toLowerCase();
                return lower.includes('selection')
                    && lower.includes('status')
                    && (lower.includes('stake') || lower.includes('profit/loss') || lower.includes('price'));
            };
            const statusRegex = /\b(Open|Failed|Reconciled|Cancelled|Canceled|Rejected|Pending|Accepted|Matched|Confirmed|Done|Success|Unplaced)\b/i;

            const containers = Array.from(document.querySelectorAll('table,div,section,main,article'))
                .filter(isVisible)
                .map((element) => ({
                    element,
                    rect: element.getBoundingClientRect(),
                    text: textOf(element),
                }))
                .filter((item) => item.rect.width > 260 && item.rect.height > 120)
                .filter((item) => hasOrdersHeaders(item.text))
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));

            const collectCandidateTexts = (root) => {
                const candidates = Array.from(root.querySelectorAll('tr,[role="row"],li,div,section,article,td,span'))
                    .filter(isVisible)
                    .map((element) => ({
                        text: textOf(element),
                        rect: element.getBoundingClientRect(),
                    }))
                    .filter((item) => item.text && /\\b\\d{6,14}\\b/.test(item.text))
                    .filter((item) => item.text.length <= 320)
                    .filter((item) => !hasOrdersHeaders(item.text))
                    .filter((item) => /\u20ac/.test(item.text) || statusRegex.test(item.text) || item.text.toLowerCase().includes('order id'))
                    .sort((a, b) => a.text.length - b.text.length || a.rect.y - b.rect.y || a.rect.x - b.rect.x);
                return candidates.map((item) => item.text);
            };

            let sources = [];
            for (const container of containers) {
                sources = collectCandidateTexts(container.element);
                if (sources.length) break;
            }
            if (!sources.length) {
                sources = containers.map((item) => item.text).filter(Boolean);
            }
            if (!sources.length) {
                sources = [textOf(document.body)].filter(Boolean);
            }

            let max = 0;
            for (const source of sources) {
                const matches = source.match(/\\b\\d{6,14}\\b/g) || [];
                for (const value of matches) {
                    const num = parseInt(value, 10);
                    if (Number.isFinite(num) && num > max) max = num;
                }
            }
            return max || null;
            """
        )
    except Exception as exc:
        print(f"[{profile_label}] Could not read Black orders max id: {exc}", flush=True)
        return None
    if isinstance(result, (int, float)) and result:
        return int(result)
    return None


def _snapshot_black_max_order_id(driver: webdriver.Remote, profile_label: str) -> int | None:
    """Open the Black top Orders view and return the largest order id currently shown.

    Used as a watermark: the row of a freshly placed bet must have an order id strictly
    greater than this snapshot, so we never mistake a leftover top row (same team and
    even same stake) for the new bet while the new row is still rendering.
    """
    if not _open_black_top_orders(driver, profile_label):
        return None
    time.sleep(1.5)
    snapshot = _read_black_orders_max_id(driver, profile_label)
    if snapshot:
        print(f"[{profile_label}] Black pre-bet max order id snapshot: {snapshot}", flush=True)
        return snapshot
    return None


def _capture_new_black_order_id(
    driver: webdriver.Remote,
    profile_label: str,
    min_order_id: int | None,
    timeout: int = 45,
) -> int | None:
    """After Place, poll the Orders table for the new max order id strictly greater
    than `min_order_id`. Order ids are monotonically increasing and only one bet is
    placed at a time under the bet lock, so the new max IS our bet. Returns None if
    no new id appears within `timeout`."""
    if not _open_black_top_orders(driver, profile_label):
        return None
    deadline = time.monotonic() + timeout
    last_seen = None
    while time.monotonic() < deadline:
        current_max = _read_black_orders_max_id(driver, profile_label)
        if current_max:
            last_seen = current_max
            if not min_order_id or current_max > int(min_order_id):
                print(
                    f"[{profile_label}] Captured new Black order id: {current_max} "
                    f"(pre-bet snapshot {min_order_id}).",
                    flush=True,
                )
                return current_max
        time.sleep(1)
    print(
        f"[{profile_label}] No new Black order id appeared after Place "
        f"(snapshot {min_order_id}, last seen max {last_seen}).",
        flush=True,
    )
    return None


def _read_black_top_order_row(
    driver: webdriver.Remote,
    profile_label: str,
    timeout: int = 60,
    home_team: str | None = None,
    expected_stake: Decimal | None = None,
    min_order_id: int | None = None,
) -> dict:
    if not _open_black_top_orders(driver, profile_label):
        return {"status": "pending", "accepted": False, "detail": "Top Orders view not opened.", "order_status": "Unknown", "order_stake": "?"}

    time.sleep(6)

    home_team_lower = (home_team or "").strip().lower()
    if expected_stake is not None:
        try:
            expected_stake_variants = _decimal_variants(expected_stake)
        except Exception:
            expected_stake_variants = []
    else:
        expected_stake_variants = []

    min_id = int(min_order_id) if isinstance(min_order_id, (int, float)) and min_order_id else 0

    def read_row(browser: webdriver.Remote):
        return browser.execute_script(
            """
            const homeTeam = (arguments[0] || '').toLowerCase();
            const stakeVariants = (arguments[1] || []).map((value) => String(value).toLowerCase());
            const minOrderId = Number(arguments[2] || 0);
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim();
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();

            const tables = Array.from(document.querySelectorAll('table,div,section,main'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: normalize(textOf(element)).toLowerCase() }))
                .filter((item) => item.text.includes('selection') && item.text.includes('status') && item.text.includes('stake'))
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
            const table = tables[0];
            if (!table) return null;

            const tableRows = Array.from(table.element.querySelectorAll('tr,[role="row"]'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: normalize(textOf(element)) }))
                .filter((item) => item.rect.height > 24)
                .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);

            const headerRow = tableRows.find((item) => {
                const lower = item.text.toLowerCase();
                return lower.includes('selection') && lower.includes('status') && lower.includes('stake');
            });

            let dataRows = [];
            if (tableRows.length >= 2 && headerRow) {
                dataRows = tableRows.filter((item) => item.rect.y > headerRow.rect.bottom + 2 && item.text);
            }

            if (!dataRows.length) {
                dataRows = Array.from(table.element.querySelectorAll('div,section,article'))
                    .filter(isVisible)
                    .map((element) => ({ element, rect: element.getBoundingClientRect(), text: normalize(textOf(element)) }))
                    .filter((item) => item.rect.height > 26 && item.rect.width > table.rect.width * 0.6)
                    .filter((item) => item.rect.y > (headerRow ? headerRow.rect.bottom : table.rect.y + 30))
                    .filter((item) => item.text && !item.text.toLowerCase().includes('selection status price stake'))
                    .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
            }

            const stakeMatchesExpected = (stakeText) => {
                if (!stakeText) return false;
                if (!stakeVariants.length) return true;
                const lowered = stakeText.toLowerCase();
                return stakeVariants.some((variant) => variant && lowered.includes(variant));
            };

            const pickStake = (rowEl, rowText) => {
                const euroMatches = Array.from(rowText.matchAll(/\u20ac\\s*\\d+(?:[.,]\\d+)?/g)).map((match) => match[0]);
                let stake = euroMatches[0] || '';
                const directCells = Array.from(rowEl.children || [])
                    .filter(isVisible)
                    .map((element) => normalize(textOf(element)))
                    .filter(Boolean);
                if (directCells.length >= 5) {
                    const cellStake = directCells.find((value, index) => index >= 3 && /\u20ac\\s*\\d+(?:[.,]\\d+)?/.test(value));
                    if (cellStake) stake = cellStake.match(/\u20ac\\s*\\d+(?:[.,]\\d+)?/)[0];
                }
                return stake;
            };

            const evaluateRow = (rowItem) => {
                const rowText = rowItem.text;
                const statusMatch = rowText.match(/\\b(Open|Failed|Reconciled|Cancelled|Canceled|Rejected|Pending|Accepted|Matched|Confirmed|Done)\\b/i);
                const stake = pickStake(rowItem.element, rowText);
                const idMatches = rowText.match(/\\b\\d{8,14}\\b/g) || [];
                let orderId = 0;
                for (const value of idMatches) {
                    const num = parseInt(value, 10);
                    if (Number.isFinite(num) && num > orderId) orderId = num;
                }
                return {
                    status: statusMatch ? statusMatch[1] : 'Unknown',
                    stake: stake || '?',
                    orderId: orderId || null,
                    rowText: rowText.slice(0, 500),
                };
            };

            // Prefer the topmost (most recently placed) row that matches the team. The site
            // can silently reduce the stake (e.g. €0.97 -> €0.95), so DO NOT gate the team
            // match on the expected stake — otherwise we fall through and pick an older row
            // that happens to keep the original stake. When a min order id watermark is
            // supplied, also require the row's order id to be strictly greater so we don't
            // pick a leftover older row for the same team that is still on top while the
            // freshly placed row is rendering.
            if (homeTeam) {
                const teamCandidates = dataRows.filter((item) => item.text.toLowerCase().includes(homeTeam));
                for (const rowItem of teamCandidates) {
                    const evaluated = evaluateRow(rowItem);
                    if (!minOrderId || (evaluated.orderId && evaluated.orderId > minOrderId)) {
                        return { ok: true, matchedBy: 'team', ...evaluated };
                    }
                }
            }
            if (stakeVariants.length) {
                for (const rowItem of dataRows) {
                    const evaluated = evaluateRow(rowItem);
                    if (!stakeMatchesExpected(evaluated.stake)) continue;
                    if (minOrderId && (!evaluated.orderId || evaluated.orderId <= minOrderId)) continue;
                    return { ok: true, matchedBy: 'stake', ...evaluated };
                }
            }
            const firstRow = dataRows[0];
            if (!firstRow) return { ok: false, reason: 'orders-row-not-found', pageText: table.text.slice(0, 600) };
            const topEvaluated = evaluateRow(firstRow);
            if (minOrderId && (!topEvaluated.orderId || topEvaluated.orderId <= minOrderId)) {
                return { ok: false, reason: 'new-order-not-arrived-yet', pageText: table.text.slice(0, 600) };
            }
            return { ok: true, matchedBy: 'top', ...topEvaluated };
            """,
            home_team_lower,
            expected_stake_variants,
            min_id,
        )

    row_data = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row_data = read_row(driver)
        ok_basic = row_data and row_data.get("ok") and row_data.get("status") and row_data.get("stake")
        # Keep polling until the newly placed row actually shows up. The orders table can
        # take several seconds to add the new row, so accepting a "top" or stake-only
        # match while waiting would silently report the wrong (older) bet.
        if ok_basic:
            matched_by = row_data.get("matchedBy")
            status_ok = row_data.get("status") != "Unknown"
            if home_team_lower:
                if matched_by == "team" and status_ok:
                    break
            elif expected_stake_variants:
                if matched_by in {"team", "stake"} and status_ok:
                    break
            else:
                if status_ok:
                    break
        time.sleep(1)

    if not row_data or not row_data.get("ok"):
        detail = ((row_data or {}).get("pageText") or "Top order row not found.")[:250]
        print(f"[{profile_label}] Could not read Black top order row. {detail}")
        return {
            "status": "pending",
            "accepted": False,
            "detail": detail,
            "order_status": "Unknown",
            "order_stake": "?",
            "order_id": None,
        }

    raw_status = row_data.get("status") or "Unknown"
    stake = row_data.get("stake") or "?"
    detail = row_data.get("rowText") or ""
    order_id = row_data.get("orderId")
    if isinstance(order_id, (int, float)) and order_id:
        order_id = int(order_id)
    else:
        order_id = None
    normalized_status = _normalize_black_order_status(raw_status)
    print(
        f"[{profile_label}] Black top order row: status={raw_status}, stake={stake}, "
        f"orderId={order_id} (matched by {row_data.get('matchedBy', 'top')})."
    )
    return {
        "status": normalized_status,
        "accepted": normalized_status == "accepted",
        "detail": detail,
        "order_status": raw_status,
        "order_stake": stake,
        "order_id": order_id,
    }


def _read_black_order_by_id(
    driver: webdriver.Remote,
    profile_label: str,
    order_id: int,
    timeout: int = 30,
) -> dict:
    """Open the Black Orders view and return status/stake for the row with the exact
    order id given. Used for the deferred (~5 min) check after a bet is placed so we
    look up the bet we actually placed, never a sibling row for the same fixture."""
    if not _open_black_top_orders(driver, profile_label):
        return {
            "status": "pending",
            "accepted": False,
            "detail": "Top Orders view not opened.",
            "order_status": "Unknown",
            "order_stake": "?",
            "order_id": order_id,
        }
    time.sleep(2)

    def read_row(browser: webdriver.Remote):
        return browser.execute_script(
            """
            const targetId = String(arguments[0] || '');
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim();
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const tables = Array.from(document.querySelectorAll('table,div,section,main'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: normalize(textOf(element)).toLowerCase() }))
                .filter((item) => item.text.includes('selection') && item.text.includes('status') && item.text.includes('stake'))
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
            const statusRegex = /\b(Open|Failed|Reconciled|Cancelled|Canceled|Rejected|Pending|Accepted|Matched|Confirmed|Done|Success|Unplaced)\b/i;
            const roots = tables.length
                ? tables.slice(0, 3).map((item) => item.element)
                : [document.querySelector('main') || document.body].filter(Boolean);
            const seen = new Set();
            let candidates = [];
            for (const root of roots) {
                for (const element of Array.from(root.querySelectorAll('tr,[role="row"],li,div,section,article,td,span'))) {
                    if (!isVisible(element) || seen.has(element)) continue;
                    seen.add(element);
                    const text = normalize(textOf(element));
                    if (text.includes(targetId)) candidates.push({ element, text });
                }
            }
            if (!candidates.length && document.body) {
                const bodyText = normalize(textOf(document.body));
                if (bodyText.includes(targetId)) candidates = [{ element: document.body, text: bodyText }];
            }
            if (!candidates.length) return { ok: false, reason: 'order-id-not-found' };
            // We want the SMALLEST container that includes the order id, a € amount, AND a
            // status keyword — that is the full row for this order. Fallbacks: smallest
            // with € only, then smallest overall.
            const filterAnd = (predicate) => {
                const subset = candidates.filter(predicate);
                if (!subset.length) return null;
                subset.sort((a, b) => a.text.length - b.text.length);
                return subset[0];
            };
            const rowItem = filterAnd((item) => /\u20ac/.test(item.text) && statusRegex.test(item.text))
                || filterAnd((item) => /\u20ac/.test(item.text))
                || filterAnd(() => true);
            const rowText = rowItem.text;
            const statusMatch = rowText.match(statusRegex);
            const euroMatches = Array.from(rowText.matchAll(/\u20ac\\s*\\d+(?:[.,]\\d+)?/g)).map((m) => m[0]);
            return {
                ok: true,
                status: statusMatch ? statusMatch[1] : 'Unknown',
                stake: euroMatches[0] || '?',
                rowText: rowText.slice(0, 500),
            };
            """,
            str(order_id),
        )

    row_data = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row_data = read_row(driver)
        if row_data and row_data.get("ok") and row_data.get("status") and row_data.get("status") != "Unknown":
            break
        time.sleep(1)

    if not row_data or not row_data.get("ok"):
        print(f"[{profile_label}] Could not find Black order id {order_id} in Orders.")
        return {
            "status": "pending",
            "accepted": False,
            "detail": "Order id not found in Orders table.",
            "order_status": "Unknown",
            "order_stake": "?",
            "order_id": order_id,
        }

    raw_status = row_data.get("status") or "Unknown"
    stake = row_data.get("stake") or "?"
    normalized_status = _normalize_black_order_status(raw_status)
    print(
        f"[{profile_label}] Black deferred check for orderId={order_id}: "
        f"status={raw_status}, stake={stake}."
    )
    return {
        "status": normalized_status,
        "accepted": normalized_status == "accepted",
        "detail": row_data.get("rowText", ""),
        "order_status": raw_status,
        "order_stake": stake,
        "order_id": order_id,
    }


def check_black_order_by_id(session: dict, order_id: int, signal=None) -> dict:
    """Public helper used by the Telegram layer to look up a previously placed order
    by id after a delay. Reuses the active browser session."""
    profile_label = session.get("profile_label", "Profile-1")
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        result = _read_black_order_by_id(driver, profile_label, int(order_id))
    finally:
        close_driver_bridge(driver)
    if signal is not None:
        result["teams"] = getattr(signal, "teams", None) or getattr(signal, "home_team", None)
        result["selection"] = getattr(signal, "selection_label", None) or f"{signal.selection} {signal.line}"
        result["odds"] = str(getattr(signal, "odds", ""))
    return result


def _open_black_orders_view(driver: webdriver.Remote, profile_label: str) -> bool:
    def locate_orders_target() -> object:
        return driver.execute_script(
            """
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
            const panel = Array.from(document.querySelectorAll('aside,section,div'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                .filter((item) => item.rect.x > window.innerWidth * 0.68)
                .filter((item) => item.rect.width > 180 && item.rect.height > 120)
                .filter((item) => item.text.includes('betslip') || item.text.includes('recent orders') || item.text.includes('live orders'))
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
            if (!panel) return null;

            const recentOrders = Array.from(panel.element.querySelectorAll('button,[role="tab"],[role="button"],div,span'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                .filter((item) => item.text === 'recent orders' || item.text.includes('recent orders'))
                .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x)[0];
            if (!recentOrders) return null;

            return recentOrders.element.closest('button,[role="tab"],[role="button"]') || recentOrders.element;
            """
        )

    target = locate_orders_target()
    if not target:
        print(f"[{profile_label}] Could not find Black Recent Orders tab after placing order.")
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", target)
    except Exception:
        pass

    click_attempts = (
        lambda: target.click(),
        lambda: ActionChains(driver).move_to_element(target).pause(0.1).click(target).perform(),
        lambda: driver.execute_script("arguments[0].click();", target),
        lambda: driver.execute_script(
            """
            const target = arguments[0];
            const rect = target.getBoundingClientRect();
            const x = rect.x + rect.width / 2;
            const y = rect.y + rect.height / 2;
            const liveTarget = document.elementFromPoint(x, y) || target;
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                liveTarget.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerType: 'mouse' }));
            }
            liveTarget.click?.();
            return true;
            """,
            target,
        ),
    )
    for attempt in click_attempts:
        try:
            attempt()
            if WebDriverWait(driver, 4).until(lambda browser: browser.execute_script(
                """
                const isVisible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0
                        && rect.bottom > 0
                        && rect.right > 0;
                };
                const panel = Array.from(document.querySelectorAll('aside,section,div'))
                    .filter(isVisible)
                    .map((element) => ({ element, rect: element.getBoundingClientRect(), text: (element.innerText || element.textContent || '').trim().toLowerCase() }))
                    .filter((item) => item.rect.x > window.innerWidth * 0.68)
                    .filter((item) => item.rect.width > 180 && item.rect.height > 120)
                    .filter((item) => item.text.includes('betslip') || item.text.includes('recent orders') || item.text.includes('live orders'))
                    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
                if (!panel) return false;

                const tabs = Array.from(panel.element.querySelectorAll('button,[role="tab"],[role="button"],div,span'))
                    .filter(isVisible)
                    .map((element) => ({
                        text: (element.innerText || element.textContent || '').trim().toLowerCase(),
                        selected: (element.getAttribute('aria-selected') || '').toLowerCase() === 'true'
                            || (element.getAttribute('class') || '').toLowerCase().includes('active')
                            || (element.getAttribute('class') || '').toLowerCase().includes('selected'),
                    }))
                    .filter((item) => item.text === 'recent orders' || item.text.includes('recent orders'));
                return tabs.some((item) => item.selected) || panel.text.includes('recent orders');
                """
            )):
                print(f"[{profile_label}] Switched Black right panel to Recent Orders.")
                return True
        except Exception:
            continue

    print(f"[{profile_label}] Could not switch Black right panel to Recent Orders.")
    return False


def _black_search_dialog_open(driver: webdriver.Remote) -> bool:
    try:
        return bool(driver.execute_script(
            """
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase().replace(/\\s+/g, ' ');
            return Array.from(document.querySelectorAll('div,section,aside'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                .filter((item) => item.rect.y < 420 && item.rect.width > 240 && item.rect.height > 120)
                .some((item) => {
                    const hasVisibleInput = Array.from(item.element.querySelectorAll('input'))
                        .some((input) => isVisible(input) && !['checkbox', 'radio', 'hidden'].includes((input.type || '').toLowerCase()));
                    if (!hasVisibleInput) return false;
                    return item.text.includes('live events')
                        || item.text.includes('use ctrl-f')
                        || item.text.includes('use ctrl f');
                });
            """
        ))
    except Exception:
        text = _visible_text_lower(driver)
        return "live events" in text


def _dismiss_black_search_dialog(driver: webdriver.Remote, profile_label: str) -> bool:
    if not _black_search_dialog_open(driver):
        return True

    try:
        driver.execute_script(
            """
            const events = [
                new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }),
                new KeyboardEvent('keyup', { key: 'Escape', code: 'Escape', bubbles: true }),
            ];
            for (const event of events) document.dispatchEvent(event);
            for (const event of events) document.body?.dispatchEvent(event);
            """
        )
    except Exception:
        pass

    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass

    time.sleep(0.6)
    dismissed = not _black_search_dialog_open(driver)
    if dismissed:
        print(f"[{profile_label}] Dismissed Black search dialog.")
    return dismissed


def _black_current_match_page_open(driver: webdriver.Remote) -> bool:
    current_url = (driver.current_url or "").lower()
    return "/sportsbook/football" in current_url and not _black_search_dialog_open(driver)


def _black_match_context_matches(
    driver: webdriver.Remote,
    home_team: str,
    away_team: str | None,
) -> bool:
    if _black_search_dialog_open(driver):
        return False

    home_variants = _team_search_queries(home_team)
    away_variants = _team_search_queries(away_team)
    return bool(driver.execute_script(
        """
        const homeVariants = arguments[0] || [];
        const awayVariants = arguments[1] || [];
        const normalize = (value) => (value || '')
            .toLowerCase()
            .normalize('NFD')
            .replace(/[\\u0300-\\u036f]/g, '')
            .replace(/[^a-z0-9]+/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
        const wordsFor = (variants) => Array.from(new Set(
            variants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))
        ));
        const wordMatch = (text, word) => {
            if (!word) return false;
            if (text.includes(word)) return true;
            for (const token of text.split(' ')) {
                const prefix = Math.min(token.length, word.length, 4);
                if (prefix >= 4 && token.slice(0, prefix) === word.slice(0, prefix)) return true;
            }
            return false;
        };
        const teamPresent = (text, variants, words, allowOneWordFallback) => {
            if (!variants.length && !words.length) return true;
            if (variants.some((value) => normalize(value) && text.includes(normalize(value)))) return true;
            const hits = words.filter((word) => wordMatch(text, word)).length;
            if (!words.length) return false;
            const required = allowOneWordFallback ? 1 : Math.max(1, words.length - 1);
            return hits >= required;
        };
        const homeWords = wordsFor(homeVariants);
        const awayWords = wordsFor(awayVariants);
        const bodyText = (document.body?.innerText || '').toLowerCase();
        // 'live events' is unique to the search modal; 'all sports' also appears in the page's
        // left navigation rail when not in the modal, so only reject on the modal-only marker.
        if (bodyText.includes('live events')) return false;

        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();

        const sections = Array.from(document.querySelectorAll('main,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x < window.innerWidth * 0.82)
            .filter((item) => item.rect.y < window.innerHeight * 0.45)
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));

        for (const section of sections.slice(0, 12)) {
            const normalizedText = normalize(section.text);
            if (!teamPresent(normalizedText, homeVariants, homeWords, false)) continue;
            if (!teamPresent(normalizedText, awayVariants, awayWords, true)) continue;
            return true;
        }
        return false;
        """,
        home_variants,
        away_variants,
    ))


def _black_loose_match_context_matches(
    driver: webdriver.Remote,
    home_team: str,
    away_team: str | None,
) -> bool:
    """Fallback guard: accept page context when both teams are present in visible text."""
    home_variants = _team_search_queries(home_team)
    away_variants = _team_search_queries(away_team)
    try:
        return bool(driver.execute_script(
            """
            const homeVariants = arguments[0] || [];
            const awayVariants = arguments[1] || [];
            const normalize = (value) => (value || '')
                .toLowerCase()
                .normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '')
                .replace(/[^a-z0-9]+/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const bodyText = normalize(document.body?.innerText || '');
            const wordsFor = (variants) => Array.from(new Set(
                variants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))
            ));
            const wordMatch = (text, word) => {
                if (!word) return false;
                if (text.includes(word)) return true;
                for (const token of text.split(' ')) {
                    const prefix = Math.min(token.length, word.length, 4);
                    if (prefix >= 4 && token.slice(0, prefix) === word.slice(0, prefix)) return true;
                }
                return false;
            };

            const hasTeam = (variants, words, allowOneWordFallback) => {
                if (!variants.length && !words.length) return true;
                if (variants.some((value) => {
                    const n = normalize(value);
                    return n && bodyText.includes(n);
                })) return true;
                const hits = words.filter((word) => wordMatch(bodyText, word)).length;
                if (!words.length) return false;
                const required = allowOneWordFallback ? 1 : Math.max(1, words.length - 1);
                return hits >= required;
            };

            const homeWords = wordsFor(homeVariants);
            const awayWords = wordsFor(awayVariants);
            const homeOk = hasTeam(homeVariants, homeWords, false);
            if (!homeOk) return false;
            if (!awayVariants.length) return true;
            return hasTeam(awayVariants, awayWords, true);
            """,
            home_variants,
            away_variants,
        ))
    except Exception:
        return False


def _ensure_black_betslip_safe_to_use(driver: webdriver.Remote, profile_label: str) -> None:
    try:
        _activate_black_order_tab(driver, "Betslip", profile_label)
        time.sleep(0.3)
    except Exception:
        pass

    state = driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const looksLikeOccupiedBetslip = (text) => {
            return text.includes('stake')
                || text.includes('price')
                || text.includes('place')
                || text.includes('timeout')
                || text.includes('less than min order');
        };
        const looksLikeEmptyBetslip = (text) => {
            return text.includes('betslip is empty')
                || text.includes('to add a bet to your betslip')
                || text === 'betslip'
                || text === 'betslip classic exchange arb calc'
                || text === 'betslip recent orders live orders';
        };
        const candidates = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.rect.width > 180 && item.rect.height > 80)
            .filter((item) => item.text.includes('betslip') || looksLikeOccupiedBetslip(item.text) || looksLikeEmptyBetslip(item.text))
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
        const panel = candidates[0]?.element;
        if (!panel) return { ok: true, reason: 'panel-missing' };

        const panelText = textOf(panel);
        if (looksLikeEmptyBetslip(panelText) && !looksLikeOccupiedBetslip(panelText)) {
            return { ok: true, reason: 'empty' };
        }

        const activeInputs = Array.from(panel.querySelectorAll('input'))
            .filter(isVisible)
            .filter((element) => !element.disabled && !element.readOnly)
            .filter((element) => !['checkbox', 'radio', 'hidden'].includes((element.type || '').toLowerCase()));
        const placeButtons = Array.from(panel.querySelectorAll('button,[role="button"]'))
            .filter(isVisible)
            .filter((element) => {
                const text = textOf(element);
                return text === 'place' || text.includes('place');
            })
            .filter((element) => !element.disabled && element.getAttribute('aria-disabled') !== 'true');
        const looksLikeSettledOrderPanel = /(reconciled|success|accepted|matched|unplaced|order id|placed at)/.test(panelText)
            || panelText.includes('profit/loss');
        if (!activeInputs.length && !placeButtons.length && looksLikeSettledOrderPanel) {
            return { ok: true, reason: 'settled-order-panel' };
        }
        if (!activeInputs.length && !placeButtons.length && !panelText.includes('less than min order')) {
            return { ok: true, reason: 'no-active-betslip-controls' };
        }

        const closeButtons = Array.from(panel.querySelectorAll('button,[role="button"],div,span'))
            .filter(isVisible)
            .map((element) => ({
                element,
                rect: element.getBoundingClientRect(),
                text: textOf(element),
                marker: [
                    element.getAttribute('aria-label') || '',
                    element.getAttribute('title') || '',
                    element.className?.toString() || '',
                    textOf(element),
                ].join(' ').toLowerCase(),
            }))
            .filter((item) => item.rect.x > window.innerWidth * 0.86)
            .filter((item) => item.rect.y < window.innerHeight * 0.5)
            .sort((a, b) => {
                const aClose = /(close|remove|delete|clear|cancel|×|x)/.test(a.marker) ? 0 : 1;
                const bClose = /(close|remove|delete|clear|cancel|×|x)/.test(b.marker) ? 0 : 1;
                return aClose - bClose || a.rect.y - b.rect.y;
            });

        for (const candidate of closeButtons.slice(0, 5)) {
            const target = candidate.element.closest('button,[role="button"]') || candidate.element;
            target.scrollIntoView({ block: 'center', inline: 'center' });
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                target.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' }));
            }
            try { target.click?.(); } catch (error) {}
            const refreshed = textOf(panel);
            if (looksLikeEmptyBetslip(refreshed) && !looksLikeOccupiedBetslip(refreshed)) return { ok: true, reason: 'cleared' };
        }

        return { ok: false, reason: 'betslip-not-empty', text: panelText.slice(0, 300) };
        """
    )
    if not state or not state.get("ok"):
        raise RuntimeError(f"[{profile_label}] Refusing to continue with non-empty Black betslip: {state!r}")


def _find_black_live_match_candidate(
    driver: webdriver.Remote,
    team_variants: list[str],
    opponent_variants: list[str],
    team_name: str,
    opponent_name: str | None,
):
    last_state: dict = {}

    def read_candidate(browser: webdriver.Remote):
        nonlocal last_state
        state = browser.execute_script(
            """
            const teamVariants = arguments[0] || [];
            const opponentVariants = arguments[1] || [];
            const normalize = (value) => (value || '')
                .toLowerCase()
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/[^a-z0-9]+/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
            const wordsFor = (variants) => Array.from(new Set(
                variants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))
            ));
            const teamWords = wordsFor(teamVariants);
            const opponentWords = wordsFor(opponentVariants);
            const isVisible = (element) => {
                if (!element) return false;
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || '1') > 0
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim();
            const fuzzyHas = (text, word) => {
                if (!word) return false;
                if (text.includes(word)) return true;
                for (const token of text.split(' ')) {
                    const prefix = Math.min(token.length, word.length, 4);
                    if (prefix >= 4 && token.slice(0, prefix) === word.slice(0, prefix)) return true;
                }
                return false;
            };
            const hasExactVariant = (text, variants) => variants.some((value) => {
                const normalized = normalize(value);
                return normalized && text.includes(normalized);
            });
            const scoreFor = (text, rect) => {
                const normalizedText = normalize(text);
                const teamHits = teamWords.filter((word) => fuzzyHas(normalizedText, word)).length;
                const opponentHits = opponentWords.filter((word) => fuzzyHas(normalizedText, word)).length;
                const exactTeam = hasExactVariant(normalizedText, teamVariants);
                const exactOpponent = hasExactVariant(normalizedText, opponentVariants);
                const hasVs = /\\b(v|vs)\\b/.test(normalizedText);
                let score = 0;
                if (exactTeam) score += 140;
                if (exactOpponent) score += 90;
                score += teamHits * 45;
                score += opponentHits * 30;
                if (hasVs) score += 35;
                if (/\\b\\d+\\s*[:-]\\s*\\d+\\b/.test(normalizedText)) score += 12;
                if (normalizedText.includes('live') || normalizedText.includes('in play')) score += 10;
                if (normalizedText.includes('no results') || normalizedText.includes('no events')) score -= 500;
                if (normalizedText.includes('use ctrl f') || normalizedText.includes('all sports live events')) score -= 220;
                if (normalizedText.includes('order id') || normalizedText.includes('selection status price stake')) score -= 260;
                if (normalizedText.includes('asian total goals') || normalizedText.includes('over') || normalizedText.includes('under')) score -= 60;
                if (rect.x < 8 || rect.x > window.innerWidth - 90) score -= 90;
                if (rect.y < 45) score -= 80;
                return { score, teamHits, opponentHits, exactTeam, exactOpponent, hasVs, normalizedText };
            };
            const rowMatchesTeams = (metrics) => {
                const hasTeam = metrics.exactTeam
                    || metrics.teamHits >= Math.max(1, Math.min(teamWords.length || 1, 2));
                if (!hasTeam) return false;
                if (!opponentWords.length && !opponentVariants.length) return true;
                const hasOpponent = metrics.exactOpponent || metrics.opponentHits >= 1;
                const strongTeamOnly = metrics.teamHits >= Math.max(1, Math.min(teamWords.length || 1, 2));
                return hasOpponent || (strongTeamOnly && metrics.hasVs) || metrics.exactTeam;
            };

            const rootCandidates = Array.from(document.querySelectorAll('aside,section,div,main'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element).toLowerCase() }))
                .filter((item) => item.text.includes('live events') || item.text.includes('use ctrl-f') || item.text.includes('use ctrl f'))
                .filter((item) => item.rect.width > 240 && item.rect.height > 80)
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
            const root = rootCandidates[0]?.element || document.body;
            const rootText = textOf(root);
            const normalizedRootText = normalize(rootText);
            const noResults = normalizedRootText.includes('no results found')
                || normalizedRootText.includes('no events found');

            const seen = new Set();
            const candidates = [];
            const elements = Array.from(root.querySelectorAll('li,a,button,[role="button"],[role="option"],[role="listitem"],article,div'));
            for (const element of elements) {
                if (!isVisible(element)) continue;
                const row = element.closest('a,button,li,[role="button"],[role="option"],[role="listitem"],article') || element;
                if (!isVisible(row) || seen.has(row)) continue;
                seen.add(row);
                const rect = row.getBoundingClientRect();
                const text = textOf(row);
                if (!text || text.length < 2 || text.length > 500) continue;
                if (rect.width < 45 || rect.height < 12 || rect.height > Math.min(340, window.innerHeight * 0.7)) continue;
                const metrics = scoreFor(text, rect);
                if (!rowMatchesTeams(metrics)) continue;
                if (metrics.score < 45) continue;
                candidates.push({ row, rect, text, metrics, area: rect.width * rect.height });
            }
            candidates.sort((a, b) => b.metrics.score - a.metrics.score || a.area - b.area || a.rect.y - b.rect.y);
            const pick = candidates[0] || null;
            return {
                ok: !!pick,
                candidate: pick ? pick.row : null,
                reason: pick ? 'candidate-found' : (noResults ? 'no-results' : 'no-candidate'),
                noResults,
                rootText: rootText.split('\\n').map((line) => line.trim()).filter(Boolean).slice(0, 12).join(' | ').slice(0, 700),
                candidates: candidates.slice(0, 8).map((item) => ({
                    text: item.text.slice(0, 220),
                    score: item.metrics.score,
                    teamHits: item.metrics.teamHits,
                    opponentHits: item.metrics.opponentHits,
                    exactTeam: item.metrics.exactTeam,
                    exactOpponent: item.metrics.exactOpponent,
                    x: Math.round(item.rect.x),
                    y: Math.round(item.rect.y),
                    w: Math.round(item.rect.width),
                    h: Math.round(item.rect.height),
                })),
            };
            """,
            team_variants,
            opponent_variants,
        ) or {}
        last_state = dict(state)
        candidate = last_state.pop("candidate", None)
        return candidate or False

    try:
        return WebDriverWait(driver, 18).until(read_candidate)
    except TimeoutException as exc:
        raise RuntimeError(
            f"Could not find Black live match row for {team_name} vs {opponent_name or '?'}. "
            f"Search state: {last_state!r}"
        ) from exc


def _open_black_live_match(
    driver: webdriver.Remote,
    team_name: str,
    opponent_name: str | None,
    profile_label: str,
) -> None:
    team_variants = _team_search_queries(team_name)
    opponent_variants = _team_search_queries(opponent_name)

    def match_opened(browser: webdriver.Remote) -> bool:
        return _black_match_context_matches(browser, team_name, opponent_name)

    click_attempts = [
        ("native", lambda candidate: candidate.click()),
        ("actions", lambda candidate: ActionChains(driver).move_to_element(candidate).pause(0.1).click(candidate).perform()),
        (
            "js-center",
            lambda candidate: driver.execute_script(
                """
                const element = arguments[0];
                const target = element.closest('button,a,[role="button"],li') || element;
                target.scrollIntoView({ block: 'center', inline: 'center' });
                const rect = target.getBoundingClientRect();
                const x = rect.x + rect.width / 2;
                const y = rect.y + rect.height / 2;
                const node = document.elementFromPoint(x, y) || target;
                for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                    node.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerType: 'mouse' }));
                }
                try { target.click?.(); } catch (error) {}
                """,
                candidate,
            ),
        ),
        ("enter", lambda candidate: candidate.send_keys(Keys.ENTER)),
    ]

    for attempt_name, attempt in click_attempts:
        try:
            candidate = _find_black_live_match_candidate(
                driver, team_variants, opponent_variants, team_name, opponent_name
            )
            before_url = driver.current_url or ""
            attempt(candidate)
            open_reason = WebDriverWait(driver, 6).until(lambda browser: (
                "context" if match_opened(browser)
                else "url" if (browser.current_url or "") != before_url and _black_current_match_page_open(browser)
                else False
            ))
            if open_reason:
                print(f"[{profile_label}] Opened Black live match for: {team_name} via {attempt_name} ({open_reason}).")
                return
        except Exception:
            continue

    # Some Black live-result rows are visible but not reliably clickable.
    # In this case, resolve the best matching result href and navigate directly.
    direct_href = driver.execute_script(
        """
        const teamVariants = arguments[0] || [];
        const opponentVariants = arguments[1] || [];
        const normalize = (value) => (value || '')
            .toLowerCase()
            .normalize('NFD')
            .replace(/[\\u0300-\\u036f]/g, '')
            .replace(/[^a-z0-9]+/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
        const wordsFor = (variants) => Array.from(new Set(
            variants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))
        ));
        const teamWords = wordsFor(teamVariants);
        const opponentWords = wordsFor(opponentVariants);
        const isVisible = (element) => {
            if (!element) return false;
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0
                && rect.x < window.innerWidth
                && rect.y < window.innerHeight;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim();
        const fuzzyHas = (text, word) => {
            if (!word) return false;
            if (text.includes(word)) return true;
            for (const token of text.split(' ')) {
                const prefix = Math.min(token.length, word.length, 4);
                if (prefix >= 4 && token.slice(0, prefix) === word.slice(0, prefix)) return true;
            }
            return false;
        };
        const hasVariant = (text, variants) => variants.some((value) => {
            const n = normalize(value);
            return n && text.includes(n);
        });
        const scoreText = (text) => {
            const n = normalize(text);
            const teamHits = teamWords.filter((word) => fuzzyHas(n, word)).length;
            const oppHits = opponentWords.filter((word) => fuzzyHas(n, word)).length;
            let score = 0;
            if (hasVariant(n, teamVariants)) score += 100;
            if (hasVariant(n, opponentVariants)) score += 70;
            score += teamHits * 30;
            score += oppHits * 22;
            if (/\b(v|vs)\b/.test(n)) score += 15;
            return score;
        };
        const links = Array.from(document.querySelectorAll('a[href]'))
            .filter(isVisible)
            .map((el) => ({
                el,
                href: el.href || '',
                text: textOf(el),
                score: scoreText(textOf(el)),
                rect: el.getBoundingClientRect(),
            }))
            .filter((item) => item.href && /\\/sportsbook\\/football\\//i.test(item.href))
            .filter((item) => item.score >= 45)
            .sort((a, b) => b.score - a.score || a.rect.y - b.rect.y || a.rect.x - b.rect.x);
        return links[0] ? links[0].href : null;
        """,
        team_variants,
        opponent_variants,
    )
    if direct_href:
        try:
            driver.get(direct_href)
            _wait_document_ready(driver)
            if _black_match_context_matches(driver, team_name, opponent_name) or _black_current_match_page_open(driver):
                print(f"[{profile_label}] Opened Black live match for: {team_name} via direct-url fallback.")
                return
        except Exception:
            pass

    details = driver.execute_script(
        """
        const teamVariants = arguments[0];
        const opponentVariants = arguments[1];
        const normalize = (value) => (value || '')
            .toLowerCase()
            .normalize('NFD')
            .replace(/[\\u0300-\\u036f]/g, '')
            .replace(/[^a-z0-9]+/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
        const teamWords = Array.from(new Set(teamVariants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))));
        const opponentWords = Array.from(new Set(opponentVariants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))));
        const fuzzyHas = (text, word) => {
            if (!word) return false;
            if (text.includes(word)) return true;
            for (const token of text.split(' ')) {
                const prefix = Math.min(token.length, word.length, 4);
                if (prefix >= 4 && token.slice(0, prefix) === word.slice(0, prefix)) return true;
            }
            return false;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        return Array.from(document.querySelectorAll('li,button,a,[role="button"],div'))
            .filter(isVisible)
            .map((element) => ({ text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.rect.y > 90 && item.rect.x > window.innerWidth * 0.14 && item.rect.x < window.innerWidth * 0.82)
            .filter((item) => {
                const text = normalize(item.text);
                return teamVariants.some((value) => normalize(value) && text.includes(normalize(value)))
                    || teamWords.some((word) => fuzzyHas(text, word));
            })
            .filter((item) => {
                if (!opponentVariants.length && !opponentWords.length) return true;
                const text = normalize(item.text);
                return opponentVariants.some((value) => normalize(value) && text.includes(normalize(value)))
                    || opponentWords.some((word) => fuzzyHas(text, word));
            })
            .slice(0, 8)
            .map((item) => item.text.slice(0, 220));
        """,
        team_variants,
        opponent_variants,
    )
    raise RuntimeError(f"Could not open Black live match for team: {team_name}. Candidates: {details!r}")


def _verify_black_betslip_target(
    driver: webdriver.Remote,
    selection: str,
    line: Decimal,
    home_team: str | None,
    away_team: str | None,
    profile_label: str,
) -> None:
    line_variants = _decimal_variants(line)
    selection_lower = selection.strip().lower()
    home_variants = _team_search_queries(home_team)
    away_variants = _team_search_queries(away_team)
    try:
        verified = WebDriverWait(driver, 8).until(lambda browser: browser.execute_script(
        """
        const selection = arguments[0].trim().toLowerCase();
        const lineVariants = arguments[1].map((value) => value.toLowerCase());
        const homeVariants = arguments[2] || [];
        const awayVariants = arguments[3] || [];
        const normalize = (value) => (value || '')
            .toLowerCase()
            .normalize('NFD')
            .replace(/[\\u0300-\\u036f]/g, '')
            .replace(/[^a-z0-9]+/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
        const wordsFor = (variants) => Array.from(new Set(
            variants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))
        ));
        const wordMatch = (text, word) => {
            if (!word) return false;
            if (text.includes(word)) return true;
            return text.split(' ').some((token) => {
                const prefix = Math.min(token.length, word.length, 4);
                return prefix >= 4 && token.slice(0, prefix) === word.slice(0, prefix);
            });
        };
        const teamPresent = (text, variants, words, allowOneWordFallback) => {
            if (!variants.length && !words.length) return true;
            if (variants.some((value) => normalize(value) && text.includes(normalize(value)))) return true;
            const hits = words.filter((word) => wordMatch(text, word)).length;
            if (!words.length) return false;
            return hits >= (allowOneWordFallback ? 1 : Math.max(1, words.length - 1));
        };
        const homeWords = wordsFor(homeVariants);
        const awayWords = wordsFor(awayVariants);
        const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\$&');
        const hasExactLineToken = (text) => lineVariants.some((line) => {
            const pattern = new RegExp(`(^|[^\\d/])${escapeRegExp(line)}([^\\d/]|$)`);
            return pattern.test(text);
        });
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();

        const panels = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.rect.width > 150 && item.rect.height > 90)
            .filter((item) => {
                const text = item.text;
                return (text.includes('stake') && text.includes('price') && text.includes('place'))
                    || text.includes('betslip');
            })
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));

        for (const panel of panels) {
            const text = panel.text;
            const normalizedText = normalize(text);
            const hasLine = hasExactLineToken(text);
            const hasSelection = text.includes(selection);
            const hasHome = teamPresent(normalizedText, homeVariants, homeWords, false);
            const hasAway = teamPresent(normalizedText, awayVariants, awayWords, true);
            if (hasLine && hasSelection) {
                return {
                    ok: true,
                    teamsInTicket: hasHome && hasAway,
                    text: text.slice(0, 300),
                };
            }
        }
        return null;
        """,
        selection_lower,
        line_variants,
        home_variants,
        away_variants,
        ))
    except TimeoutException as exc:
        page_text = _visible_page_text(driver)
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
        raise RuntimeError(
            f"[{profile_label}] Black betslip verification failed for {selection} {line}. Page: {short_text}"
        ) from exc
    if not verified:
        page_text = _visible_page_text(driver)
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
        raise RuntimeError(
            f"[{profile_label}] Black betslip verification failed for {selection} {line}. Page: {short_text}"
        )
    if verified.get("teamsInTicket"):
        return
    if _black_match_context_matches(driver, home_team or "", away_team) or _black_current_match_page_open(driver):
        print(
            f"[{profile_label}] Black betslip verified by selection/line; team names are on the match page, not inside the ticket."
        )
        return
    raise RuntimeError(
        f"[{profile_label}] Black betslip has {selection} {line}, but match context is not confirmed. "
        f"Ticket: {verified.get('text', '')}"
    )


def _select_black_asian_total_goals(
    driver: webdriver.Remote,
    selection: str,
    line: Decimal,
    profile_label: str,
    market_headers: list[str] | None = None,
    prefer_left: bool = False,
) -> None:
    line_variants = _decimal_variants(line)
    target_headers = market_headers or ["asian total goals"]
    result = driver.execute_script(
        """
        const selection = arguments[0].trim().toLowerCase();
        const lineVariants = arguments[1].map((value) => value.toLowerCase());
        const normalizeHeader = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
        const marketHeaders = (arguments[2] || []).map(normalizeHeader);
        const normalizeNumber = (value) => (value || '').toLowerCase().replace(/\\s+/g, '').replace(',', '.');
        const normalizedLineVariants = lineVariants.map(normalizeNumber);
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const normalizedTextOf = (element) => textOf(element).replace(/\\s+/g, ' ').trim();
        const oddsPattern = /^\\d+(?:[.,]\\d+)+$/;
        const linePattern = /^\\d+(?:[.,]\\d+)?(?:\\s*(?:\\/|,)\\s*\\d+(?:[.,]\\d+)?)?$/;
        const descendants = (root, selector) => Array.from(root.querySelectorAll(selector)).filter(isVisible);
        const clickElement = (element) => {
            const clickable = element.closest('button,[role="button"]') || element;
            clickable.scrollIntoView({ block: 'center', inline: 'center' });
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                clickable.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' }));
            }
            try {
                if (typeof clickable.click === 'function') clickable.click();
            } catch (error) {}
        };
        const findExactLineCell = (rowElement) => {
            const candidates = descendants(rowElement, 'button,div,[role="button"],span,p,li')
                .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
                .filter((item) => item.text && item.text.length <= 16)
                .filter((item) => linePattern.test(item.text));
            return candidates
                .filter((item) => normalizedLineVariants.includes(normalizeNumber(item.text)))
                .sort((a, b) => a.rect.x - b.rect.x || a.rect.y - b.rect.y)[0] || null;
        };
        const findSelectionLabel = (rowElement, target) => descendants(rowElement, 'button,div,[role="button"],span,p,li')
            .map((element) => ({ element, text: normalizedTextOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.text === target)
            .sort((a, b) => a.rect.x - b.rect.x || a.rect.y - b.rect.y)[0] || null;
        const findOddsButtons = (rowElement) => descendants(rowElement, 'button,div,[role="button"],span')
            .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => oddsPattern.test(item.text))
            .sort((a, b) => a.rect.x - b.rect.x || a.rect.y - b.rect.y);
        const pickOddsForSelection = (rowElement) => {
            const overLabel = findSelectionLabel(rowElement, 'over');
            const underLabel = findSelectionLabel(rowElement, 'under');
            const oddsButtons = findOddsButtons(rowElement);
            if (!overLabel || !underLabel || oddsButtons.length < 2) {
                return null;
            }
            if (selection === 'over') {
                return oddsButtons
                    .filter((item) => item.rect.left >= overLabel.rect.right - 8)
                    .filter((item) => item.rect.right <= underLabel.rect.left + 14)
                    .sort((a, b) => Math.abs(a.rect.left - overLabel.rect.right) - Math.abs(b.rect.left - overLabel.rect.right))[0]
                    || oddsButtons
                        .filter((item) => item.rect.left >= overLabel.rect.right - 8 && item.rect.left < underLabel.rect.left + 40)
                        .sort((a, b) => Math.abs(a.rect.left - overLabel.rect.right) - Math.abs(b.rect.left - overLabel.rect.right))[0]
                    || null;
            }
            if (selection === 'under') {
                return oddsButtons
                    .filter((item) => item.rect.left >= underLabel.rect.right - 8)
                    .sort((a, b) => Math.abs(a.rect.left - underLabel.rect.right) - Math.abs(b.rect.left - underLabel.rect.right))[0]
                    || null;
            }
            return null;
        };
        const buildRowCandidate = (element) => {
            const rect = element.getBoundingClientRect();
            const text = normalizedTextOf(element);
            if (rect.width <= 280 || rect.height < 24 || rect.height > 120) {
                return null;
            }
            if (!text.includes('over') || !text.includes('under')) {
                return null;
            }
            const lineCell = findExactLineCell(element);
            if (!lineCell) {
                return null;
            }
            const overLabel = findSelectionLabel(element, 'over');
            const underLabel = findSelectionLabel(element, 'under');
            if (!overLabel || !underLabel) {
                return null;
            }
            const targetOdds = pickOddsForSelection(element);
            if (!targetOdds) {
                return null;
            }
            const verticalCenters = [lineCell.rect, overLabel.rect, underLabel.rect, targetOdds.rect]
                .map((candidateRect) => candidateRect.top + candidateRect.height / 2);
            const minCenter = Math.min.apply(null, verticalCenters);
            const maxCenter = Math.max.apply(null, verticalCenters);
            if (maxCenter - minCenter > 22) {
                return null;
            }
            return {
                element,
                rect,
                text,
                lineText: normalizeNumber(lineCell.text),
                targetOdds,
                area: rect.width * rect.height,
            };
        };
        const collectSectionCandidates = () => {
            const headers = Array.from(document.querySelectorAll('div,section,header,span,h2,h3,h4'))
                .filter(isVisible)
                .map((element) => ({ element, text: normalizeHeader(normalizedTextOf(element)), rawText: normalizedTextOf(element), rect: element.getBoundingClientRect() }))
                .filter((item) => marketHeaders.includes(item.text))
                .sort((a, b) => a.rect.y - b.rect.y);
            const containers = [];
            for (let index = 0; index < headers.length; index += 1) {
                const header = headers[index];
                const nextHeader = headers[index + 1] || null;
                let current = header.element;
                for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
                    if (!current || !isVisible(current)) continue;
                    const rect = current.getBoundingClientRect();
                    if (rect.width <= 350 || rect.height <= 100) continue;
                    const rows = descendants(current, 'div,section,li,button,[role="button"]')
                        .map(buildRowCandidate)
                        .filter(Boolean)
                        .filter((item) => item.rect.y >= header.rect.bottom - 8);
                        
                    const boundedRows = nextHeader
                        ? rows.filter((item) => item.rect.y < nextHeader.rect.top - 8)
                        : rows;
                    if (!boundedRows.length) continue;
                    containers.push({
                        element: current,
                        rect,
                        headerText: header.text,
                        rows: boundedRows,
                    });
                    break;
                }
            }
            return containers
                .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height))
                .filter((item, index, array) => array.findIndex((candidate) => candidate.element === item.element) === index);
        };
        const marketSections = collectSectionCandidates();
        for (const section of marketSections) {
            const rows = section.rows
                .filter((item) => normalizedLineVariants.includes(item.lineText))
                .sort((a, b) => a.area - b.area || a.rect.y - b.rect.y || a.rect.height - b.rect.height);
            if (!rows.length) {
                continue;
            }
            const row = rows[0];
            clickElement(row.targetOdds.element);
            return {
                ok: true,
                rowText: row.text.slice(0, 220),
                sectionText: section.headerText,
                lineText: row.lineText,
                selection,
            };
        }
        const sectionTexts = Array.from(document.querySelectorAll('div,section,header,span,h2,h3,h4'))
            .filter(isVisible)
            .map((element) => normalizedTextOf(element))
            .filter((text) => text.includes('goal'))
            .slice(0, 12);
        const rowSamples = Array.from(document.querySelectorAll('div,section,li,button,[role="button"]'))
            .filter(isVisible)
            .map((element) => normalizedTextOf(element))
            .filter((text) => text.includes('over') && text.includes('under'))
            .slice(0, 8);
        return {
            ok: false,
            reason: 'market-row-not-found',
            selection,
            lineVariants,
            marketHeaders,
            sections: sectionTexts,
            rowSamples,
        };
        """,
        selection,
        line_variants,
        target_headers,
        prefer_left,
    )
    if not result or not result.get("ok"):
        raise RuntimeError(f"Could not select {target_headers} {selection} {line}. Details: {result!r}")
    time.sleep(0.8)
    print(f"[{profile_label}] Selected Black market {result.get('sectionText')}: {selection} {line}.")


def _set_black_betslip_price_and_place(
    driver: webdriver.Remote,
    price: Decimal,
    profile_label: str,
    stake: str | None = None,
) -> None:
    price_text = format(price.normalize(), "f")
    stake_text = (stake or "").strip()
    locate_script = """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const looksLikeActiveTicket = (text) => {
            return (text.includes('stake') && text.includes('price') && text.includes('place'))
                || (text.includes('stake at price') && text.includes('ex. returns'))
                || text.includes('betslip');
        };
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.width > 150 && item.rect.height > 90)
            .filter((item) => looksLikeActiveTicket(item.text))
            .sort((a, b) => {
                const aTicket = a.text.includes('stake') && a.text.includes('price') && a.text.includes('place') ? 0 : 1;
                const bTicket = b.text.includes('stake') && b.text.includes('price') && b.text.includes('place') ? 0 : 1;
                return aTicket - bTicket || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height);
            })[0];
        if (!panel) {
            return { ok: false, reason: 'betslip panel not found', text: document.body?.innerText || '' };
        }

        const panelRect = panel.rect;
        const placeButtonItem = Array.from(panel.element.querySelectorAll('button,[role="button"]'))
            .filter(isVisible)
            .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.text === 'place' || item.text.includes('place'))
            .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x)[0] || null;
        const topControlsBottom = placeButtonItem ? placeButtonItem.rect.bottom + 14 : panelRect.top + 180;
        const editableSelector = 'input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex]';
        const controlValue = (element) => {
            if ('value' in element) return element.value || element.getAttribute('value') || '';
            return element.innerText || element.textContent || element.getAttribute('aria-valuetext') || '';
        };
        const controls = Array.from(panel.element.querySelectorAll(editableSelector))
            .filter(isVisible)
            .filter((element) => !element.disabled && element.getAttribute('aria-disabled') !== 'true')
            .filter((element) => {
                const tag = element.tagName.toLowerCase();
                const role = (element.getAttribute('role') || '').toLowerCase();
                const tabIndex = element.getAttribute('tabindex');
                if (tabIndex !== null && Number(tabIndex) < 0) return false;
                if ((tag === 'button' || role === 'button')
                    && !element.matches('input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"]')) {
                    return false;
                }
                return true;
            })
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element.parentElement || element) }))
            .filter((item) => item.rect.y < topControlsBottom)
            .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);

        const compactRect = (rect) => ({
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            w: Math.round(rect.width),
            h: Math.round(rect.height),
        });
        const labels = Array.from(panel.element.querySelectorAll('label,div,span,p'))
            .filter(isVisible)
            .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.rect.y < topControlsBottom)
            .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
        const rowInputs = controls
            .filter((item) => item.rect.y > panelRect.top + 35 && item.rect.y < topControlsBottom)
            .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
        const pickFallbackInput = (usedElements) => rowInputs.find((item) => !usedElements.has(item.element))
            || controls.find((item) => !usedElements.has(item.element))
            || null;
        const pickInputByLabel = (labelText, usedElements) => {
            const matchingLabels = labels.filter((item) => item.text === labelText);
            for (const label of matchingLabels) {
                const labelCenterX = label.rect.left + label.rect.width / 2;
                const candidate = controls
                    .filter((item) => !usedElements.has(item.element))
                    .filter((item) => item.rect.y >= label.rect.y - 16 && item.rect.y <= label.rect.bottom + 55)
                    .map((item) => {
                        const inputCenterX = item.rect.left + item.rect.width / 2;
                        const leftOfLabelPenalty = item.rect.right < label.rect.left - 15 ? 500 : 0;
                        const farBelowPenalty = item.rect.y > label.rect.bottom + 45 ? 120 : 0;
                        const abovePenalty = item.rect.bottom < label.rect.top - 4 ? 300 : 0;
                        const score = Math.abs(inputCenterX - labelCenterX)
                            + Math.abs(item.rect.y - label.rect.bottom) * 0.25
                            + leftOfLabelPenalty
                            + farBelowPenalty
                            + abovePenalty;
                        return { ...item, score };
                    })
                    .sort((a, b) => a.score - b.score || a.rect.x - b.rect.x)[0];
                if (candidate) return candidate;
            }
            return pickFallbackInput(usedElements);
        };
        const pickFieldBoxByLabel = (labelText, usedElements) => {
            const matchingLabels = labels.filter((item) => item.text === labelText);
            for (const label of matchingLabels) {
                const x = label.rect.left + label.rect.width / 2;
                const yValues = [
                    label.rect.bottom + 8,
                    label.rect.bottom + 16,
                    label.rect.bottom + 24,
                    label.rect.top + label.rect.height / 2,
                ];
                for (const y of yValues) {
                    const rawTarget = document.elementFromPoint(x, y);
                    const target = rawTarget?.closest?.('input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"],[tabindex],div,span') || rawTarget;
                    if (!target || !panel.element.contains(target) || !isVisible(target) || usedElements.has(target)) continue;
                    const rect = target.getBoundingClientRect();
                    if (rect.width < 18 || rect.height < 10 || rect.y >= topControlsBottom) continue;
                    const tag = target.tagName.toLowerCase();
                    const role = (target.getAttribute('role') || '').toLowerCase();
                    if (tag === 'button' || role === 'button') continue;
                    return { element: target, rect, text: textOf(target.parentElement || target), fallback: 'point-under-label' };
                }
            }
            return null;
        };

        const usedInputs = new Set();
        const stakeItem = pickInputByLabel('stake', usedInputs) || pickFieldBoxByLabel('stake', usedInputs);
        if (stakeItem) usedInputs.add(stakeItem.element);
        const priceItem = pickInputByLabel('price', usedInputs) || pickFieldBoxByLabel('price', usedInputs);
        const stakeInput = stakeItem?.element || null;
        const priceInput = priceItem?.element || null;
        const placeButton = placeButtonItem?.element || null;
        const placeDisabled = !placeButton
            || !!placeButton.disabled
            || placeButton.getAttribute('disabled') !== null
            || placeButton.getAttribute('aria-disabled') === 'true'
            || /disabled/.test(String(placeButton.className || '').toLowerCase());
        return {
            ok: true,
            panel: panel.element,
            stakeInput,
            priceInput,
            placeButton,
            placeDisabled,
            panelText: panel.element.innerText || document.body?.innerText || '',
            controlDebug: {
                labels: labels
                    .filter((item) => ['timeout', 'stake', 'price', 'place'].includes(item.text))
                    .map((item) => ({ text: item.text, rect: compactRect(item.rect) })),
                inputs: controls.map((item) => ({
                    tag: item.element.tagName.toLowerCase(),
                    role: item.element.getAttribute('role') || '',
                    value: controlValue(item.element),
                    placeholder: item.element.getAttribute('placeholder') || '',
                    aria: item.element.getAttribute('aria-label') || '',
                    readOnly: !!item.element.readOnly || item.element.getAttribute('readonly') !== null,
                    rect: compactRect(item.rect),
                    parentText: item.text.slice(0, 80),
                })),
                selected: {
                    stake: stakeItem ? { rect: compactRect(stakeItem.rect), fallback: stakeItem.fallback || '' } : null,
                    price: priceItem ? { rect: compactRect(priceItem.rect), fallback: priceItem.fallback || '' } : null,
                },
            },
        };
    """
    def normalized_decimal_text(raw_value: str) -> str:
        return format(_money_to_decimal(raw_value).normalize(), "f")

    def locate_controls(timeout: float = 0.0) -> dict:
        deadline = time.monotonic() + max(timeout, 0.0)
        last_controls = None
        while True:
            controls = driver.execute_script(locate_script)
            if controls and controls.get("ok"):
                return controls
            last_controls = controls
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        controls = last_controls
        page_text = (controls or {}).get("text", "")
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
        raise RuntimeError(f"Could not prepare Black betslip. Reason: {(controls or {}).get('reason')}. Page: {short_text}")

    result = locate_controls(timeout=8.0)
    if not result or not result.get("ok"):
        page_text = (result or {}).get("text", "")
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
        raise RuntimeError(f"Could not prepare Black betslip. Reason: {(result or {}).get('reason')}. Page: {short_text}")

    price_input = result.get("priceInput")
    if not price_input:
        short_text = " | ".join(line.strip() for line in (result.get("panelText", "") or "").splitlines() if line.strip())[:700]
        raise RuntimeError(
            f"Could not prepare Black betslip. Reason: price input not found. "
            f"Controls: {result.get('controlDebug')!r}. Page: {short_text}"
        )

    stake_input = result.get("stakeInput")
    place_button = result.get("placeButton")
    if not place_button:
        short_text = " | ".join(line.strip() for line in (result.get("panelText", "") or "").splitlines() if line.strip())[:700]
        raise RuntimeError(
            f"Could not click Black Place. Reason: place button not found. "
            f"Controls: {result.get('controlDebug')!r}. Page: {short_text}"
        )
    if stake_input and getattr(stake_input, "id", None) == getattr(price_input, "id", None):
        short_text = " | ".join(line.strip() for line in (result.get("panelText", "") or "").splitlines() if line.strip())[:700]
        raise RuntimeError(
            f"Could not prepare Black betslip. Reason: stake and price resolved to the same input. "
            f"Controls: {result.get('controlDebug')!r}. Page: {short_text}"
        )

    if stake_input and stake_text:
        _fill_betslip_input(driver, stake_input, stake_text)
        time.sleep(0.3)
        result = locate_controls(timeout=4.0)
        price_input = result.get("priceInput")
        place_button = result.get("placeButton")
        if not price_input:
            short_text = " | ".join(line.strip() for line in (result.get("panelText", "") or "").splitlines() if line.strip())[:700]
            raise RuntimeError(
                f"Could not prepare Black betslip after filling stake. Reason: price input not found. "
                f"Controls: {result.get('controlDebug')!r}. Page: {short_text}"
            )

    _fill_betslip_input(driver, price_input, price_text)

    normalized_price = format(Decimal(price_text).normalize(), "f")
    normalized_stake = None
    if stake_text:
        normalized_stake = format(Decimal(stake_text).normalize(), "f")

    def betslip_ready(browser: webdriver.Remote):
        state = browser.execute_script(locate_script)
        if not state or not state.get("ok"):
            return False
        current_price_input = state.get("priceInput")
        current_place_button = state.get("placeButton")
        if not current_price_input or not current_place_button:
            return False
        try:
            current_price = normalized_decimal_text(_control_value(browser, current_price_input))
        except Exception:
            return False
        if current_price != normalized_price:
            return False
        if normalized_stake is not None:
            current_stake_input = state.get("stakeInput")
            stake_ok = False
            if current_stake_input:
                try:
                    current_stake = normalized_decimal_text(_control_value(browser, current_stake_input))
                    stake_ok = current_stake == normalized_stake
                except Exception:
                    stake_ok = False
            if not stake_ok:
                debug_inputs = ((state.get("controlDebug") or {}).get("inputs") or [])
                for item in debug_inputs:
                    try:
                        value = normalized_decimal_text(str(item.get("value", "")))
                    except Exception:
                        continue
                    if value == normalized_stake:
                        stake_ok = True
                        break
            if not stake_ok:
                return False
        if state.get("placeDisabled"):
            return False
        return state

    try:
        ready_state = WebDriverWait(driver, 8).until(betslip_ready)
    except TimeoutException as exc:
        debug_state = None
        try:
            debug_state = driver.execute_script(locate_script)
        except Exception:
            debug_state = None
        snapshot = _read_black_betslip_state(driver)
        panel_text = " | ".join(line.strip() for line in (snapshot.get("text", "") or "").splitlines() if line.strip())[:700]
        inputs = snapshot.get("inputs") or []
        inputs_text = "; ".join(
            f"tag={item.get('tag', '')!r}, role={item.get('role', '')!r}, value={item.get('value', '')!r}, "
            f"text={item.get('text', '')!r}, placeholder={item.get('placeholder', '')!r}, aria={item.get('aria', '')!r}"
            for item in inputs[:4]
        )
        raise RuntimeError(
            f"Black betslip did not become ready after filling stake/price. "
            f"Target stake={stake_text or 'existing'}, price={price_text}. "
            f"Inputs: {inputs_text or 'none'}. Controls: {(debug_state or {}).get('controlDebug')!r}. "
            f"Panel: {panel_text}"
        ) from exc
    ready_place_button = ready_state.get("placeButton")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", ready_place_button)
    try:
        ready_place_button.click()
    except Exception:
        try:
            ActionChains(driver).move_to_element(ready_place_button).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", ready_place_button)
    _confirm_black_place_order(driver, profile_label)
    print(f"[{profile_label}] Entered stake {stake_text or 'existing'}, price {price_text} and clicked Place.")


def _read_black_order_panel_text(driver: webdriver.Remote, tab_name: str | None = None) -> str:
    if tab_name:
        clicked = driver.execute_script(
            """
            const expected = arguments[0].trim().toLowerCase();
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0;
            };
            const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
            const panel = Array.from(document.querySelectorAll('aside,section,div'))
                .filter(isVisible)
                .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
                .filter((item) => item.rect.x > window.innerWidth * 0.68)
                .filter((item) => item.rect.width > 180 && item.rect.height > 120)
                .filter((item) => item.text.includes('betslip') || item.text.includes('recent orders') || item.text.includes('live orders'))
                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
            if (!panel) return false;

            const candidates = Array.from(panel.element.querySelectorAll('button,[role="tab"],[role="button"],div,span'))
                .filter(isVisible)
                .filter((element) => textOf(element) === expected || textOf(element).includes(expected));
            const firstCandidate = candidates.length ? candidates[0] : null;
            const target = firstCandidate ? (firstCandidate.closest('button,[role="tab"],[role="button"]') || firstCandidate) : null;
            if (!target) return false;
            target.scrollIntoView({ block: 'center', inline: 'center' });
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                target.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' }));
            }
            try {
                if (typeof target.click === 'function') target.click();
            } catch (error) {}
            return true;
            """,
            tab_name,
        )
        if clicked:
            try:
                WebDriverWait(driver, 4).until(lambda browser: browser.execute_script(
                    """
                    const expected = arguments[0].trim().toLowerCase();
                    const isVisible = (element) => {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && rect.width > 0
                            && rect.height > 0
                            && rect.bottom > 0
                            && rect.right > 0;
                    };
                    const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
                    const tabs = Array.from(document.querySelectorAll('button,[role="tab"],[role="button"],div,span'))
                        .filter(isVisible)
                        .map((element) => ({
                            text: textOf(element),
                            selected: (element.getAttribute('aria-selected') || '').toLowerCase() === 'true'
                                || (element.getAttribute('class') || '').toLowerCase().includes('active')
                                || (element.getAttribute('class') || '').toLowerCase().includes('selected'),
                        }))
                        .filter((item) => item.text === expected || item.text.includes(expected));
                    return tabs.some((item) => item.selected);
                    """,
                    tab_name,
                ))
            except Exception:
                pass
        time.sleep(0.4)

    return driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.rect.width > 180 && item.rect.height > 120)
            .filter((item) => item.text.includes('betslip') || item.text.includes('recent orders') || item.text.includes('live orders'))
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.element;
        return panel ? textOf(panel) : '';
        """
    ) or ""


def _activate_black_order_tab(driver: webdriver.Remote, tab_name: str, profile_label: str) -> bool:
    script_result = driver.execute_script(
        """
        const expected = arguments[0].trim().toLowerCase();
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.rect.width > 180 && item.rect.height > 120)
            .filter((item) => item.text.includes('betslip') || item.text.includes('recent orders') || item.text.includes('live orders'))
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
        if (!panel) return null;

        const candidates = Array.from(panel.element.querySelectorAll('button,[role="tab"],[role="button"],div,span'))
            .filter(isVisible)
            .map((element) => ({ element, text: textOf(element) }))
            .filter((item) => item.text === expected || item.text.includes(expected));
        const firstCandidate = candidates.length ? candidates[0].element : null;
        const target = firstCandidate ? (firstCandidate.closest('button,[role="tab"],[role="button"]') || firstCandidate) : null;
        return target || null;
        """,
        tab_name,
    )
    if not script_result:
        return False

    target = script_result
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", target)
    activation_probe = """
        const element = arguments[0];
        if (!element || !element.isConnected) return false;
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    """
    for click_attempt in (
        lambda: target.click(),
        lambda: ActionChains(driver).move_to_element(target).click().perform(),
        lambda: driver.execute_script("arguments[0].click();", target),
        lambda: driver.execute_script(
            """
            const target = arguments[0];
            target.scrollIntoView({ block: 'center', inline: 'center' });
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                target.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' }));
            }
            return true;
            """,
            target,
        ),
    ):
        try:
            click_attempt()
            if WebDriverWait(driver, 2).until(lambda browser: browser.execute_script(activation_probe, target)):
                return True
        except Exception:
            continue

    print(f"[{profile_label}] Could not activate Black order tab: {tab_name}")
    return False


def _format_black_fill_breakdown(items: list[dict]) -> str:
    parts = []
    for item in items:
        bookie = (item.get("bookie") or "?").strip()
        amount = item.get("amount_text") or item.get("amount") or "0"
        percent = item.get("percent_text") or item.get("percent") or "0"
        parts.append(f"{bookie} {amount} euro ({percent}%)")
    return ", ".join(parts)


def _parse_black_fill_breakdown(tooltip_text: str) -> list[dict]:
    rows = []
    for line in (tooltip_text or "").splitlines():
        normalized = " ".join(line.split()).strip()
        match = re.match(r"^(?P<bookie>[A-Za-z0-9_+.-]+)\s+€?(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<percent>\d+(?:[.,]\d+)?)%$", normalized)
        if not match:
            continue
        rows.append({
            "bookie": match.group("bookie"),
            "amount": match.group("amount").replace(",", "."),
            "amount_text": match.group("amount"),
            "percent": match.group("percent").replace(",", "."),
            "percent_text": match.group("percent"),
        })
    return rows


def _black_recent_orders_loaded(panel_text: str) -> bool:
    normalized = " ".join((panel_text or "").lower().split())
    if not normalized:
        return False
    if normalized in {"betslip recent orders live orders", "betslip recent orders 1 live orders"}:
        return False
    meaningful_tokens = (
        "full-time",
        "orders",
        "stake",
        "position",
        "done",
        "reconciled",
        "accepted",
        "matched",
        "confirmed",
    )
    return any(token in normalized for token in meaningful_tokens)


def _read_black_recent_order_fill(driver: webdriver.Remote, signal, profile_label: str) -> dict | None:
    _open_black_orders_view(driver, profile_label)
    if not _activate_black_order_tab(driver, "Recent Orders", profile_label):
        return None
    try:
        WebDriverWait(driver, 4).until(
            lambda browser: _black_recent_orders_loaded(_read_black_order_panel_text(browser, None))
        )
    except Exception:
        time.sleep(0.5)
    details = driver.execute_script(
        """
        const selection = (arguments[0] || '').trim().toLowerCase();
        const lineVariants = arguments[1].map((value) => value.toLowerCase());
        const homeTeam = (arguments[2] || '').trim().toLowerCase();
        const awayTeam = (arguments[3] || '').trim().toLowerCase();
        const normalizeNumber = (value) => (value || '').toLowerCase().replace(/\\s+/g, '').replace(',', '.');
        const normalizedLineVariants = lineVariants.map(normalizeNumber);
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.rect.width > 180 && item.rect.height > 180)
            .filter((item) => item.text.includes('recent orders'))
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
        if (!panel) return null;

        const cardCandidates = Array.from(panel.element.querySelectorAll('div,section,article'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.width > 180 && item.rect.height > 70)
            .filter((item) => item.rect.y > panel.rect.y + 18)
            .map((item) => {
                let score = 0;
                if (homeTeam && item.text.includes(homeTeam)) score += 4;
                if (awayTeam && item.text.includes(awayTeam)) score += 4;
                if (selection && item.text.includes(selection)) score += 3;
                if (normalizedLineVariants.some((line) => normalizeNumber(item.text).includes(line))) score += 3;
                if (item.text.includes('asian')) score += 2;
                if (item.text.includes('over') || item.text.includes('under')) score += 1;
                if (item.text.includes('done') || item.text.includes('reconciled') || item.text.includes('accepted') || item.text.includes('matched') || item.text.includes('confirmed')) score += 2;
                if (item.text.includes('stake') || item.text.includes('position') || /\\b\\d+\\s+orders\\b/.test(item.text)) score += 1;
                return { ...item, score };
            })
            .filter((item) => item.score >= 4 || item.text.includes(homeTeam) || item.text.includes(awayTeam))
            .sort((a, b) => b.score - a.score || a.rect.y - b.rect.y || a.rect.x - b.rect.x);
        const card = cardCandidates[0];
        if (!card) {
            return { ok: false, reason: 'recent-order-card-not-found', panelText: panel.text.slice(0, 500) };
        }

        const hoverCandidates = Array.from(card.element.querySelectorAll('div,span,button'))
            .filter(isVisible)
            .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.text.includes('done') || item.text.includes('success') || item.text.includes('reconciled'))
            .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
        return {
            ok: true,
            card: card.element,
            hoverTarget: hoverCandidates.length ? hoverCandidates[0].element : card.element,
            cardText: card.text.slice(0, 500),
        };
        """,
        getattr(signal, "selection", ""),
        _decimal_variants(getattr(signal, "line")),
        getattr(signal, "home_team", None) or "",
        getattr(signal, "away_team", None) or "",
    )
    if not details or not details.get("ok"):
        reason = (details or {}).get("reason") or "unknown"
        panel_text = (details or {}).get("panelText") or ""
        if reason != "recent-order-card-not-found" or panel_text:
            print(f"[{profile_label}] Recent Orders read failed: {reason}. {panel_text[:250]}")
        return None

    card_element = details.get("card")
    hover_target = details.get("hoverTarget") or card_element
    if not card_element or not hover_target:
        return None

    try:
        ActionChains(driver).move_to_element(card_element).pause(0.2).move_to_element(hover_target).perform()
    except Exception:
        try:
            ActionChains(driver).move_to_element(hover_target).perform()
        except Exception:
            return {"status": "accepted", "detail": details.get("cardText", ""), "fills": []}

    time.sleep(0.4)
    tooltip_text = driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim();
        const tooltips = Array.from(document.querySelectorAll('div,section,aside'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.width > 120 && item.rect.height > 70)
            .filter((item) => /bookie/i.test(item.text) && /amount/i.test(item.text) && /%\\s*of\\s*want/i.test(item.text))
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
        return tooltips.length ? tooltips[0].text : '';
        """
    ) or ""
    fills = _parse_black_fill_breakdown(tooltip_text)
    if fills:
        detail = _format_black_fill_breakdown(fills)
        print(f"[{profile_label}] Black recent order fills: {detail}")
        return {"status": "accepted", "detail": detail, "fills": fills}

    card_text = details.get("cardText", "")
    if any(word in card_text.lower() for word in ("done", "reconciled", "success", "accepted", "matched", "confirmed")):
        return {"status": "accepted_no_fill_breakdown", "detail": card_text, "fills": []}
    return None


def _black_snapshot_matches_signal(snapshot_text: str, signal) -> bool:
    text = (snapshot_text or "").lower()
    if not text:
        return False

    team_name = (getattr(signal, "home_team", None) or "").lower()
    opponent_name = (getattr(signal, "away_team", None) or "").lower()
    selection = (getattr(signal, "selection", "") or "").lower()
    line_variants = [value.lower() for value in _decimal_variants(getattr(signal, "line"))]

    score = 0
    if team_name and team_name in text:
        score += 3
    if opponent_name and opponent_name in text:
        score += 3
    if selection and selection in text:
        score += 2
    if any(line in text for line in line_variants):
        score += 2
    if "asian total goals" in text:
        score += 2
    return score >= 5


def _classify_black_order_snapshot(snapshots: dict[str, str], signal) -> tuple[str, str]:
    failure_words = ("cancel", "cancelled", "canceled", "reject", "rejected", "declined", "void", "failed", "not accepted")
    pending_words = ("pending", "processing", "placing", "submitting")
    success_words = ("accepted", "matched", "confirmed", "done", "reconciled", "success")

    for tab_name in ("live orders", "recent orders", "betslip"):
        text = snapshots.get(tab_name, "")
        if not _black_snapshot_matches_signal(text, signal):
            continue
        if any(word in text for word in failure_words):
            status = "cancelled" if "cancel" in text or "void" in text else "rejected"
            return status, text[:500]
        if tab_name == "betslip" and any(word in text for word in success_words):
            return "accepted", text[:500]
        if tab_name in {"live orders", "recent orders"}:
            return "pending", text[:500]
        if any(word in text for word in pending_words):
            return "pending", text[:500]
        if "stake" in text or "price" in text or "place" in text:
            return "pending", text[:500]

    betslip_text = snapshots.get("betslip", "")
    if "less than min order" in betslip_text:
        return "rejected", betslip_text[:500]
    if "betslip is empty" in betslip_text and any(snapshots.get(name, "") for name in ("live orders", "recent orders")):
        combined = " | ".join(filter(None, [snapshots.get("live orders", "")[:250], snapshots.get("recent orders", "")[:250]]))
        if _black_snapshot_matches_signal(combined, signal):
            return "pending", combined[:500]
    return "unknown", betslip_text[:500]


def _monitor_black_bet_status(driver: webdriver.Remote, signal, profile_label: str, timeout: int = 40) -> dict:
    deadline = time.monotonic() + timeout
    accepted_at = None
    last_detail = ""
    accepted_fills = []
    accepted_without_fill_at = None

    while time.monotonic() < deadline:
        snapshots = {
            "betslip": _read_black_order_panel_text(driver, None),
            "recent orders": _read_black_order_panel_text(driver, "Recent Orders"),
        }
        status, detail = _classify_black_order_snapshot(snapshots, signal)
        last_detail = detail or last_detail

        recent_fill = _read_black_recent_order_fill(driver, signal, profile_label)
        if recent_fill:
            if recent_fill.get("status") == "accepted":
                status = "accepted"
                detail = recent_fill.get("detail") or detail
                last_detail = detail or last_detail
                accepted_fills = recent_fill.get("fills") or accepted_fills
                accepted_without_fill_at = None
            elif recent_fill.get("status") == "accepted_no_fill_breakdown":
                if accepted_without_fill_at is None:
                    accepted_without_fill_at = time.monotonic()
                last_detail = recent_fill.get("detail") or last_detail

        if status in {"cancelled", "rejected"}:
            print(f"[{profile_label}] Black order status: {status}. {detail[:250]}")
            return {"status": status, "detail": detail, "accepted": False, "fills": accepted_fills}

        if status == "accepted":
            if accepted_at is None:
                accepted_at = time.monotonic()
            elif time.monotonic() - accepted_at >= 3:
                print(f"[{profile_label}] Black order status: accepted. {detail[:250]}")
                return {"status": "accepted", "detail": detail, "accepted": True, "fills": accepted_fills}
        else:
            if accepted_at is None and status == "pending":
                print(f"[{profile_label}] Black order status: pending.")

        if accepted_at is None and accepted_without_fill_at is not None and time.monotonic() - accepted_without_fill_at >= 6:
            print(f"[{profile_label}] Black order status: accepted without fill breakdown. {last_detail[:250]}")
            return {"status": "accepted", "detail": last_detail, "accepted": True, "fills": accepted_fills}

        time.sleep(1)

    final_status = "accepted" if accepted_at is not None else "pending"
    print(f"[{profile_label}] Black order final monitor status: {final_status}. {last_detail[:250]}")
    return {"status": final_status, "detail": last_detail, "accepted": final_status == "accepted", "fills": accepted_fills}


def place_black_bet(session: dict, signal) -> dict:
    profile_label = session.get("profile_label", "Profile-1")
    team_name = getattr(signal, "home_team", None)
    opponent_name = getattr(signal, "away_team", None)
    stake_info = session.get("stake") if isinstance(session.get("stake"), dict) else None
    stake_value = stake_info.get("stake") if stake_info else None
    if not team_name:
        raise RuntimeError("Signal does not contain a first team name for Black search.")

    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        # Snapshot the current highest order id before we place anything. The new bet's
        # row must have a strictly greater id, which prevents picking a leftover top row
        # for the same team while the freshly placed row is still rendering.
        pre_bet_max_order_id = _snapshot_black_max_order_id(driver, profile_label)
        current_url = (driver.current_url or "").lower()
        if "/sportsbook" not in current_url:
            driver.get(BLACK_SPORTSBOOK_URL)
            _wait_document_ready(driver)
            time.sleep(2)
        if not stake_value:
            try:
                balance = _read_black_balance(driver, profile_label)
                stake_value = _format_stake_amount(_calculate_stake_from_balance(balance))
                session["stake"] = {"balance": str(balance), "stake": stake_value, "percent": str(STAKE_PERCENT)}
                print(
                    f"[{profile_label}] Using calculated Black stake EUR {stake_value} "
                    f"from balance EUR {balance} because no default stake was cached."
                )
            except Exception as exc:
                print(f"[{profile_label}] Could not calculate fallback Black stake: {exc}", flush=True)
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        has_exchange_match = bool(getattr(signal, "has_exchange_match", False))
        if has_exchange_match:
            matched_amount = getattr(signal, "matched_amount", None)
            print(
                f"[{profile_label}] Signal has exchange matched volume"
                f"{f' £{matched_amount}' if matched_amount is not None else ''}; using extended match search.",
                flush=True,
            )

        search_attempt_teams = [team_name]
        if has_exchange_match and opponent_name:
            search_attempt_teams.append(opponent_name)

        open_match_error = None
        for attempt_index, search_team in enumerate(search_attempt_teams, start=1):
            if attempt_index > 1:
                driver.get(BLACK_SPORTSBOOK_URL)
                _wait_document_ready(driver)
                time.sleep(2)
                try:
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                except Exception:
                    pass
            print(f"[{profile_label}] Searching Black by normalized team: {search_team}")
            _open_black_search(driver, profile_label)
            alternate_names = [name for name in (team_name, opponent_name) if name and name != search_team]
            used_query = _search_black_live_events(driver, search_team, profile_label, alternate_names)
            print(f"[{profile_label}] Using Black search query: {used_query}")
            try:
                _open_black_live_match(driver, team_name, opponent_name, profile_label)
                open_match_error = None
                break
            except Exception as exc:
                open_match_error = exc
                if not has_exchange_match or attempt_index >= len(search_attempt_teams):
                    raise
                print(
                    f"[{profile_label}] Black match search retry {attempt_index}/{len(search_attempt_teams)} failed: {exc}",
                    flush=True,
                )
                _dismiss_black_search_dialog(driver, profile_label)
        if open_match_error is not None:
            raise open_match_error
        if _black_search_dialog_open(driver):
            _dismiss_black_search_dialog(driver, profile_label)
        if _black_search_dialog_open(driver):
            raise RuntimeError(f"[{profile_label}] Black search dialog is still open after match click; aborting before bet selection.")
        context_ok = _black_match_context_matches(driver, team_name, opponent_name)
        if not context_ok:
            context_ok = _black_current_match_page_open(driver)
        if not context_ok:
            context_ok = _black_loose_match_context_matches(driver, team_name, opponent_name)
        if not context_ok:
            try:
                print(f"[{profile_label}] Black context mismatch, retrying match open before abort.")
                _open_black_live_match(driver, team_name, opponent_name, profile_label)
            except Exception as exc:
                print(f"[{profile_label}] Black context recovery open failed: {exc}", flush=True)
            context_ok = _black_match_context_matches(driver, team_name, opponent_name)
            if not context_ok:
                context_ok = _black_current_match_page_open(driver)
            if not context_ok:
                context_ok = _black_loose_match_context_matches(driver, team_name, opponent_name)
        if not context_ok:
            raise RuntimeError(
                f"[{profile_label}] Required Black match context not reached for {team_name} vs {opponent_name or '?'}; aborting before bet selection."
            )
        _wait_document_ready(driver)
        time.sleep(2)
        prefer_left = "loss" in getattr(signal, "raw_text", "").lower()
        # Single attempt at picking the Asian Total Goals line. If the market or the
        # requested line isn't on the page yet, raise BlackSelectionMissingError so the
        # caller (bot_telethon) can release the bet lock, let queued signals run, and
        # retry this signal later. Doing the retries here would block other bets for ~5
        # minutes.
        market_headers = _black_market_headers(signal)
        market_label = _signal_market_label(signal)
        try:
            WebDriverWait(driver, 20).until(
                lambda browser: any(header in _visible_text_lower(browser) for header in market_headers)
            )
            _ensure_black_betslip_safe_to_use(driver, profile_label)
            _select_black_asian_total_goals(
                driver,
                signal.selection,
                signal.line,
                profile_label,
                market_headers=market_headers,
                prefer_left=prefer_left,
            )
        except Exception as exc:
            raise BlackSelectionMissingError(
                f"{market_label} {signal.selection} {signal.line} not available for "
                f"{team_name} vs {opponent_name or '?'}: {exc}"
            ) from exc
        _verify_black_betslip_target(driver, signal.selection, signal.line, team_name, opponent_name, profile_label)
        _set_black_betslip_price_and_place(driver, signal.odds, profile_label, stake=stake_value)
        # Just remember the new order id. Order ids are monotonic and only one bet is
        # placed at a time under the bet lock, so the new max id is unambiguously ours.
        # The actual status/price are intentionally NOT read here — the Telegram layer
        # waits ~5 minutes and then looks up this exact order id.
        new_order_id = _capture_new_black_order_id(driver, profile_label, pre_bet_max_order_id)
        return {
            "profile_label": profile_label,
            "status": "placed" if new_order_id else "pending",
            "accepted": False,
            "detail": "",
            "fills": [],
            "order_status": "Unknown",
            "order_stake": "?",
            "order_id": new_order_id,
            "teams": getattr(signal, "teams", None) or team_name,
            "selection": getattr(signal, "selection_label", f"{signal.selection} {signal.line}"),
            "odds": str(signal.odds),
        }
    finally:
        close_driver_bridge(driver)


def _read_black_balance(driver: webdriver.Remote, profile_label: str) -> Decimal:
    balance_text = ""
    for _ in range(15):
        balance_text = driver.execute_script(
            """
        const euro = String.fromCharCode(8364);
        const moneyPattern = new RegExp(euro + '\\s*[0-9]+(?:[.,][0-9]+)?');
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0
                && rect.x < window.innerWidth
                && rect.y < window.innerHeight;
        };
        const candidates = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: (element.innerText || element.textContent || '').trim() }))
            .filter((item) => item.rect.y < 120 && item.rect.x > window.innerWidth * 0.55 && moneyPattern.test(item.text))
            .sort((a, b) => b.rect.x - a.rect.x || a.rect.width * a.rect.height - b.rect.width * b.rect.height);
        for (const item of candidates) {
            const match = item.text.match(moneyPattern);
            if (match) return match[0];
        }
        return '';
            """
        ) or ""
        if balance_text:
            break
        time.sleep(1)
    if not balance_text:
        text = _visible_page_text(driver)
        money_values = re.findall(r"\u20ac\s*[0-9]+(?:[.,][0-9]+)?", text)
        if not money_values:
            raise RuntimeError("Could not read Black balance from the top profile balance.")
        balance_text = money_values[0]
    balance = _money_to_decimal(balance_text)
    print(f"[{profile_label}] Black balance: EUR {balance}")
    return balance


def _set_black_default_stake(driver: webdriver.Remote, stake: str, profile_label: str) -> None:
    if "settings" not in _visible_text_lower(driver):
        _open_black_account_menu(driver, profile_label)
    if not _click_text_if_visible(driver, "Settings", selector="button,a,[role=button],div,li"):
        if not _click_contains_if_visible(driver, "Settings", selector="button,a,[role=button],div,li"):
            raise RuntimeError("Could not click Black Settings in the account menu.")
    WebDriverWait(driver, 15).until(lambda browser: "input defaults" in _visible_text_lower(browser))

    if not _click_text_if_visible(driver, "Input Defaults", selector="button,a,[role=button],[role=tab],div,li"):
        if not _click_contains_if_visible(driver, "Input Defaults", selector="button,a,[role=button],[role=tab],div,li"):
            raise RuntimeError("Could not click Input Defaults in Black settings.")
    WebDriverWait(driver, 15).until(lambda browser: "default stake" in _visible_text_lower(browser))

    result = driver.execute_script(
        """
        const stake = arguments[0];
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0
                && rect.x < window.innerWidth
                && rect.y < window.innerHeight;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();

        const seen = new Set();
        const labels = [];
        const addLabel = (element) => {
            if (!element || seen.has(element) || !isVisible(element)) return;
            const rect = element.getBoundingClientRect();
            const text = textOf(element);
            if (text === 'default stake' || text.includes('default stake')) {
                seen.add(element);
                labels.push(element);
            }
        };

        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const node = walker.currentNode;
            if ((node.nodeValue || '').trim().toLowerCase() === 'default stake') {
                addLabel(node.parentElement);
            }
        }
        Array.from(document.querySelectorAll('label,p,span,div')).forEach(addLabel);

        labels.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        const editableInputs = () => Array.from(document.querySelectorAll('input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"]'))
            .filter((element) => isVisible(element) && !element.disabled && !element.readOnly)
            .filter((element) => !['checkbox', 'radio', 'hidden'].includes((element.type || '').toLowerCase()));
        for (const label of labels) {
            const labelRect = label.getBoundingClientRect();
            const scopedRoot = [label, label.parentElement, label.closest('label,section,div,form,li')]
                .find((element) => element && element.querySelectorAll);
            const inputs = Array.from((scopedRoot || document).querySelectorAll('input,textarea,[contenteditable="true"],[role="textbox"],[role="spinbutton"]'))
                .filter((element) => isVisible(element) && !element.disabled && !element.readOnly)
                .filter((element) => !['checkbox', 'radio', 'hidden'].includes((element.type || '').toLowerCase()))
                .map((element) => ({ element, rect: element.getBoundingClientRect() }))
                .filter((item) => item.rect.y >= labelRect.bottom - 6 && item.rect.y < labelRect.bottom + 90)
                .filter((item) => item.rect.x >= labelRect.x - 40 && item.rect.x < labelRect.x + 320)
                .sort((a, b) => {
                    const aDistance = Math.abs(a.rect.y - labelRect.bottom) + Math.abs(a.rect.x - labelRect.x) / 10;
                    const bDistance = Math.abs(b.rect.y - labelRect.bottom) + Math.abs(b.rect.x - labelRect.x) / 10;
                    return aDistance - bDistance;
                });
            const input = inputs[0]?.element;
            if (!input) continue;
            input.scrollIntoView({ block: 'center', inline: 'center' });
            input.focus({ preventScroll: true });
            const setter = Object.getOwnPropertyDescriptor(input.__proto__, 'value')?.set;
            if (setter) {
                setter.call(input, stake);
            } else {
                input.value = stake;
            }
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: stake }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
            input.blur();
            return { ok: input.value === stake, value: input.value, label: textOf(label) };
        }
        const fallbackInput = editableInputs()
            .map((element) => {
                let score = 1000;
                let node = element;
                for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                    const text = textOf(node);
                    if (text.includes('default stake')) {
                        score = depth;
                        break;
                    }
                }
                return { element, score };
            })
            .filter((item) => item.score < 1000)
            .sort((a, b) => a.score - b.score)[0]?.element;
        if (fallbackInput) {
            fallbackInput.scrollIntoView({ block: 'center', inline: 'center' });
            fallbackInput.focus({ preventScroll: true });
            const setter = Object.getOwnPropertyDescriptor(fallbackInput.__proto__, 'value')?.set;
            if (setter) {
                setter.call(fallbackInput, stake);
            } else {
                fallbackInput.value = stake;
            }
            fallbackInput.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: stake }));
            fallbackInput.dispatchEvent(new Event('change', { bubbles: true }));
            fallbackInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
            fallbackInput.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
            fallbackInput.blur();
            return { ok: fallbackInput.value === stake, value: fallbackInput.value, label: 'fallback default stake container' };
        }
        return { ok: false, value: '', label: '', labels: labels.map((label) => textOf(label)).slice(0, 5) };
        """,
        stake,
    )
    current_value = str((result or {}).get("value", ""))
    try:
        parsed_value = _money_to_decimal(current_value)
    except ValueError:
        parsed_value = None
    if not result or parsed_value != Decimal(stake):
        raise RuntimeError(
            f"Could not update Black Default Stake input. Result: {result!r}, expected {stake!r}."
        )

    driver.execute_script(
        """
        const events = [
            new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }),
            new KeyboardEvent('keyup', { key: 'Escape', code: 'Escape', bubbles: true }),
        ];
        for (const event of events) document.dispatchEvent(event);
        for (const event of events) document.body?.dispatchEvent(event);
        """
    )
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass
    time.sleep(1)
    print(f"[{profile_label}] Black Default Stake set to EUR {stake}.")


def refresh_black_default_stake(session: dict) -> dict:
    profile_label = session.get("profile_label", "Profile-1")
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        driver.get(BLACK_URL)
        _wait_document_ready(driver)
        time.sleep(2)
        if not _black_appears_logged_in(driver) and _has_visible_password_input(driver):
            _fill_login_form(driver, BLACK_USERNAME, BLACK_PASSWORD, profile_label, "Black")
            WebDriverWait(driver, 30).until(lambda browser: not _has_visible_password_input(browser))
        return update_black_default_stake(driver, profile_label)
    finally:
        close_driver_bridge(driver)


def _has_visible_password_input(driver: webdriver.Remote) -> bool:
    return bool(driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && Number(style.opacity || '1') > 0
                && !element.disabled
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0
                && rect.x < window.innerWidth
                && rect.y < window.innerHeight;
        };
        return Array.from(document.querySelectorAll('input[type="password"]')).some(isVisible);
        """
    ))


def _black_appears_logged_in(driver: webdriver.Remote) -> bool:
    current_url = driver.current_url.lower()
    if BLACK_URL_PART not in current_url:
        return False
    if any(path in current_url for path in ("/sportsbook", "/trade", "/orders")):
        return True
    page_text = _visible_text_lower(driver)
    return "sportsbook" in page_text and ("orders" in page_text or "balance" in page_text or "trade" in page_text)


def _click_betinasia_sign_in(driver: webdriver.Remote, profile_label: str) -> bool:
    selectors = [
        (By.XPATH, "//a[normalize-space()='Sign In']"),
        (By.XPATH, "//button[normalize-space()='Sign In']"),
        (By.XPATH, "//*[@role='button' and normalize-space()='Sign In']"),
        (By.XPATH, "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
        (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
    ]
    before_url = driver.current_url

    for by, selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
            for element in elements:
                if not element.is_displayed() or not element.is_enabled():
                    continue
                href = element.get_attribute("href")
                print(f"[{profile_label}] Clicking BetInAsia Sign In...")
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                try:
                    element.click()
                except Exception:
                    driver.execute_script(
                        "arguments[0].dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));",
                        element,
                    )
                try:
                    WebDriverWait(driver, 8).until(
                        lambda browser: _has_visible_password_input(browser)
                        or browser.current_url != before_url
                    )
                except Exception:
                    if href:
                        print(f"[{profile_label}] Sign In click did not open form, navigating to href: {href}")
                        driver.get(href)
                    else:
                        print(f"[{profile_label}] Sign In click did not open form, navigating to portal login directly.")
                        driver.get(PORTAL_LOGIN_URL)
                return True
        except Exception:
            continue

    clicked = _click_text_if_visible(driver, "Sign In") or _click_contains_if_visible(driver, "Sign In")
    if clicked:
        print(f"[{profile_label}] Clicked BetInAsia Sign In via JavaScript fallback.")
    return clicked


def _wait_document_ready(driver: webdriver.Remote, timeout: int = 30) -> None:
    WebDriverWait(driver, timeout).until(
        lambda browser: browser.execute_script("return document.readyState") in {"interactive", "complete"}
    )


def _click_current_login_submit(driver: webdriver.Remote, password_input, profile_label: str, login_name: str) -> bool:
    click_result = driver.execute_script(
        """
        const passwordInput = arguments[0];
        const isVisible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height && rect.x < window.innerWidth && rect.y < window.innerHeight;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        let root = passwordInput.closest('form');
        if (!root) {
            const containers = [];
            let parent = passwordInput.parentElement;
            for (let depth = 0; parent && depth < 8; depth += 1, parent = parent.parentElement) {
                containers.push(parent);
            }
            root = containers.find((element) => {
                const text = textOf(element);
                return isVisible(element)
                    && element.querySelectorAll('input').length >= 2
                    && (text.includes('sign in') || text.includes('log in') || text.includes('login'));
            }) || containers[containers.length - 1] || document.body;
        }

        const candidates = Array.from(root.querySelectorAll('button,input[type="submit"],[role="button"],a'))
            .filter(isVisible)
            .filter((element) => {
                const text = textOf(element);
                const value = (element.value || '').trim().toLowerCase();
                return element.type === 'submit'
                    || text === 'sign in'
                    || text.includes('sign in')
                    || text.includes('log in')
                    || text.includes('login')
                    || value.includes('sign in')
                    || value.includes('login');
            })
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return Math.abs(ar.y - passwordInput.getBoundingClientRect().y)
                    - Math.abs(br.y - passwordInput.getBoundingClientRect().y);
            });
        const button = candidates[0];
        if (!button) return false;
        button.scrollIntoView({ block: 'center', inline: 'center' });
        button.focus();
        button.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
        button.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
        button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        return true;
        """,
        password_input,
    )
    clicked = bool(click_result)
    if clicked:
        print(f"[{profile_label}] Clicked {login_name} submit button in current form.")
    return clicked


def _fill_login_form(driver: webdriver.Remote, username: str, password: str, profile_label: str, login_name: str) -> None:
    username_input = _find_first_visible(driver, [
        (By.CSS_SELECTOR, "input[name='email']"),
        (By.CSS_SELECTOR, "input[name='username']"),
        (By.CSS_SELECTOR, "input[name='login']"),
        (By.CSS_SELECTOR, "input[autocomplete='username']"),
        (By.CSS_SELECTOR, "input[autocomplete='email']"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input[type='text']"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'email')]"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'username')]"),
    ], timeout=20)
    password_input = _find_first_visible(driver, [
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.CSS_SELECTOR, "input[name='password']"),
        (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
    ], timeout=20)

    print(f"[{profile_label}] Filling {login_name} credentials...")
    _set_input_value(driver, username_input, username)
    time.sleep(0.3)
    _set_input_value(driver, password_input, password)
    time.sleep(0.3)

    before_submit_url = driver.current_url
    if not _click_current_login_submit(driver, password_input, profile_label, login_name):
        login_btn = _find_first_clickable(driver, [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'log in')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
        ], timeout=20)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", login_btn)
        WebDriverWait(driver, 10).until(lambda browser: login_btn.is_enabled())
        print(f"[{profile_label}] Clicking {login_name} login button...")
        login_btn.click()

    try:
        WebDriverWait(driver, 12).until(
            lambda browser: browser.current_url != before_submit_url
            or not _has_visible_password_input(browser)
        )
    except Exception:
        print(f"[{profile_label}] Submit click did not change page; pressing Enter in password field...")
        password_input.send_keys(Keys.ENTER)


def _login_betinasia_portal(driver: webdriver.Remote, profile_label: str) -> None:
    current_url = driver.current_url.lower()
    if "portal.betinasia.com/dashboard/products" in current_url:
        print(f"[{profile_label}] BetInAsia products page already open: {driver.current_url}")
        return

    if BETINASIA_URL_PART not in driver.current_url:
        driver.get(LOGIN_URL)
    _wait_document_ready(driver)
    time.sleep(2)

    if not _has_visible_password_input(driver):
        if not _click_betinasia_sign_in(driver, profile_label):
            driver.get(PORTAL_LOGIN_URL)
        time.sleep(2)

    _wait_document_ready(driver)
    if "portal.betinasia.com" in driver.current_url.lower():
        if not _has_visible_password_input(driver):
            driver.get(PORTAL_URL)
            _wait_document_ready(driver)
            time.sleep(2)
            if "account/login" not in driver.current_url.lower():
                print(f"[{profile_label}] BetInAsia products page open: {driver.current_url}")
                return

    if _has_visible_password_input(driver):
        _fill_login_form(driver, BETINASIA_EMAIL, BETINASIA_PASSWORD, profile_label, "BetInAsia")
        time.sleep(2)
        driver.get(PORTAL_URL)
        _wait_document_ready(driver)
        WebDriverWait(driver, 30).until(
            lambda browser: "portal.betinasia.com/dashboard/products" in browser.current_url.lower()
            or "products" in _visible_text_lower(browser)
        )
        if "account/login" in driver.current_url.lower() and _has_visible_password_input(driver):
            raise RuntimeError("BetInAsia login did not complete; still on login page after submitting credentials.")
        print(f"[{profile_label}] BetInAsia portal login complete: {driver.current_url}")
        return

    raise RuntimeError("BetInAsia Sign In was clicked, but the login form did not open.")


def _open_black_product(driver: webdriver.Remote, profile_label: str) -> None:
    if BLACK_URL_PART in driver.current_url.lower():
        print(f"[{profile_label}] Black is already open: {driver.current_url}")
        return

    print(f"[{profile_label}] Opening Black directly in current tab...")
    driver.get(BLACK_URL)
    _wait_document_ready(driver)
    time.sleep(2)
    print(f"[{profile_label}] Black product opened: {driver.current_url}")


def _login_black(driver: webdriver.Remote, profile_label: str) -> None:
    if BLACK_URL_PART not in driver.current_url.lower():
        driver.get(BLACK_URL)
    _wait_document_ready(driver)
    time.sleep(2)

    if _black_appears_logged_in(driver) or not _has_visible_password_input(driver):
        print(f"[{profile_label}] Black already appears to be logged in: {driver.current_url}")
        return

    _fill_login_form(driver, BLACK_USERNAME, BLACK_PASSWORD, profile_label, "Black")
    WebDriverWait(driver, 30).until(
        lambda browser: not _has_visible_password_input(browser)
        or "dashboard" in _visible_text_lower(browser)
        or "sports" in _visible_text_lower(browser)
    )
    print(f"[{profile_label}] Black login complete: {driver.current_url}")


def _dismiss_black_update_banner(driver: webdriver.Remote, profile_label: str) -> dict:
    """Best-effort click on transient bottom Update/Refresh banner in Black UI."""
    try:
        result = driver.execute_script(
            """
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || '1') > 0
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > 0
                    && rect.right > 0
                    && rect.x < window.innerWidth
                    && rect.y < window.innerHeight;
            };
            const norm = (value) => (value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
            const isUpdateText = (text) => {
                const t = norm(text);
                return t === 'update' || t.startsWith('update ')
                    || t === 'refresh' || t.startsWith('refresh ')
                    || t === 'reload' || t.startsWith('reload ');
            };
            const candidates = Array.from(document.querySelectorAll("button, a, [role='button'], div, span"))
                .filter(isVisible)
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                    const marker = norm([
                        el.className && el.className.toString ? el.className.toString() : '',
                        el.getAttribute('data-testid') || '',
                        el.getAttribute('data-test') || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                    ].join(' '));
                    return { el, rect, text, marker };
                })
                .filter((item) => isUpdateText(item.text) || /update|refresh|reload/.test(item.marker))
                .filter((item) => item.rect.y > window.innerHeight * 0.55)
                .sort((a, b) => {
                    const aTxt = isUpdateText(a.text) ? 0 : 1;
                    const bTxt = isUpdateText(b.text) ? 0 : 1;
                    return aTxt - bTxt || b.rect.y - a.rect.y;
                });

            const pick = candidates[0];
            if (!pick) return { found: false };
            const target = pick.el.closest("button, a, [role='button']") || pick.el;
            target.scrollIntoView({ block: 'center', inline: 'center' });
            for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                const EventCtor = name.startsWith('pointer') ? (window.PointerEvent || MouseEvent) : MouseEvent;
                try {
                    target.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' }));
                } catch (error) {
                    target.dispatchEvent(new MouseEvent(name.replace('pointer', 'mouse'), { bubbles: true, cancelable: true, view: window }));
                }
            }
            try { target.click?.(); } catch (error) {}
            return {
                found: true,
                clicked: pick.text || pick.marker || 'update',
                x: Math.round(pick.rect.x),
                y: Math.round(pick.rect.y),
            };
            """
        ) or {"found": False}
    except Exception:
        return {"found": False}

    if result.get("found"):
        print(f"[{profile_label}] Dismissed Black update banner: {result.get('clicked')}")
        time.sleep(0.25)
    return result


def _ensure_black_session_ready(driver: webdriver.Remote, profile_label: str) -> bool:
    _dismiss_black_update_banner(driver, profile_label)
    current_url = (driver.current_url or "").lower()
    if BLACK_URL_PART in current_url:
        _wait_document_ready(driver)
        time.sleep(1)
        if _black_appears_logged_in(driver):
            print(f"[{profile_label}] Reusing active Black session: {driver.current_url}")
            return True

    if BLACK_URL_PART not in current_url:
        driver.get(BLACK_SPORTSBOOK_URL)
        _wait_document_ready(driver)
        time.sleep(2)
        if _black_appears_logged_in(driver):
            print(f"[{profile_label}] Active Black session recovered after sportsbook open: {driver.current_url}")
            return True

    return False


def _ping_black_session(driver: webdriver.Remote, profile_label: str) -> dict:
    _dismiss_black_update_banner(driver, profile_label)
    result = driver.execute_script(
        """
        const isVisible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && rect.bottom > 0
                && rect.right > 0;
        };
        const dispatchMove = (target, x, y) => {
            if (!target) return false;
            const events = ['pointerover', 'mouseover', 'pointermove', 'mousemove'];
            for (const name of events) {
                const EventCtor = name.startsWith('pointer') ? PointerEvent : MouseEvent;
                target.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerType: 'mouse' }));
            }
            return true;
        };
        const candidates = Array.from(document.querySelectorAll('main,section,div,button,a,[role="button"]'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: (element.innerText || element.textContent || '').trim().toLowerCase() }))
            .filter((item) => item.rect.y > 70 && item.rect.x < window.innerWidth * 0.85)
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
        const target = candidates[0]?.element || document.body;
        const rect = (target.getBoundingClientRect && target.getBoundingClientRect()) || { x: 100, y: 100, width: 50, height: 50 };
        const x = rect.x + Math.max(10, Math.min(rect.width / 2, 80));
        const y = rect.y + Math.max(10, Math.min(rect.height / 2, 80));
        target.scrollIntoView?.({ block: 'center', inline: 'center' });
        dispatchMove(target, x, y);
        window.dispatchEvent(new Event('focus'));
        document.dispatchEvent(new Event('visibilitychange'));
        return { ok: true, url: window.location.href, text: (target.innerText || target.textContent || '').trim().slice(0, 120) };
        """
    )
    print(f"[{profile_label}] Black keepalive ping sent.")
    return {"status": "alive", "detail": (result or {}).get("url", driver.current_url)}


def keep_black_session_alive(session: dict) -> dict:
    profile_label = session.get("profile_label", "Profile-1")
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        current_url = (driver.current_url or "").lower()
        if BLACK_URL_PART not in current_url:
            driver.get(BLACK_SPORTSBOOK_URL)
            _wait_document_ready(driver)
            time.sleep(2)

        if not _ensure_black_session_ready(driver, profile_label):
            raise RuntimeError("Black session is no longer active.")

        return _ping_black_session(driver, profile_label)
    finally:
        close_driver_bridge(driver)


def ensure_black_session_authorized(session: dict) -> dict:
    profile_label = session.get("profile_label", "Profile-1")
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        current_url = (driver.current_url or "").lower()
        if BLACK_URL_PART not in current_url:
            driver.get(BLACK_SPORTSBOOK_URL)
            _wait_document_ready(driver)
            time.sleep(2)

        if _ensure_black_session_ready(driver, profile_label):
            return _ping_black_session(driver, profile_label)

        print(f"[{profile_label}] Black session is not authorized during health check; re-logging.")
        login(driver, profile_label)
        if not _ensure_black_session_ready(driver, profile_label):
            raise RuntimeError("Black session could not be restored during health check.")
        return {"status": "relogged", "detail": driver.current_url}
    finally:
        close_driver_bridge(driver)


def login(driver: webdriver.Remote, profile_label: str) -> None:
    """Perform BetInAsia portal login, open Black, then perform Black login."""
    time.sleep(2)
    if BETINASIA_URL_PART not in driver.current_url.lower():
        driver.get(LOGIN_URL)

    _login_betinasia_portal(driver, profile_label)
    _open_black_product(driver, profile_label)
    _login_black(driver, profile_label)


def _is_betfair_logged_in(driver: webdriver.Remote) -> bool:
    """Detect whether the Betfair top bar shows a logged-in state.

    Primary signal: there is no visible password input on the page. The login
    form sits in the top header and disappears once the user is authenticated.
    """
    try:
        return bool(driver.execute_script(
            r"""
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            const pwds = Array.from(document.querySelectorAll("input[type='password']"))
                .filter(visible);
            // If a visible password field exists, we are NOT logged in.
            if (pwds.length > 0) return false;
            const text = (document.body?.innerText || '').toLowerCase();
            if (text.includes('log out') || text.includes('logout') || text.includes('my account')) return true;
            const searchInputs = Array.from(document.querySelectorAll("input[type='search'], input[type='text'], input:not([type])"))
                .filter(visible)
                .filter((el) => {
                    const hint = [
                        el.getAttribute('placeholder') || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('name') || '',
                        el.id || '',
                    ].join(' ').toLowerCase();
                    return /search|find|team|competition|event|sport|market|команд|соревн|событ|поиск|найти/.test(hint);
                });
            return searchInputs.length > 0 && !text.includes('присоединиться сейчас') && !text.includes('join now');
            """
        ))
    except Exception:
        return False


def _dismiss_betfair_blocking_overlays(driver: webdriver.Remote, profile_label: str) -> dict:
    """Best-effort dismissal for Betfair dialogs that block search or betslip input.

    In practice the most damaging one is the visible "Session Expired" modal,
    which leaves the page looking alive while intercepting all actions.
    """
    try:
        result = driver.execute_script(
            r"""
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            function normalized(text) {
                return (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
            }
            const keywords = [
                'session expired', 'you have been logged out', 'logged out',
                'session has expired', 'your session has expired'
            ];
            const buttonKeywords = ['ok', 'okay', 'close', 'dismiss', 'log in', 'login'];
            const roots = Array.from(document.querySelectorAll("dialog, [role='dialog'], [aria-modal='true'], div, section, aside"))
                .filter(visible)
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = normalized(el.innerText || el.textContent || '');
                    const style = window.getComputedStyle(el);
                    const z = Number(style.zIndex || 0) || 0;
                    const fixedLike = style.position === 'fixed' || style.position === 'sticky';
                    return { el, rect, text, z, fixedLike };
                })
                .filter((item) => item.text)
                .filter((item) => keywords.some((kw) => item.text.includes(kw)))
                .filter((item) => item.fixedLike || item.z >= 100 || (item.rect.width >= 240 && item.rect.height >= 80));
            if (!roots.length) return { found: false };
            roots.sort((a, b) => b.z - a.z || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
            const root = roots[0];
            const buttons = Array.from(root.el.querySelectorAll("button, [role='button'], input[type='submit'], input[type='button']"))
                .filter(visible)
                .map((el) => ({
                    el,
                    text: normalized(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || ''),
                }));
            const pick = buttons.find((item) => buttonKeywords.some((kw) => item.text === kw || item.text.startsWith(kw))) || buttons[0] || null;
            if (pick && pick.el) {
                pick.el.scrollIntoView({ block: 'center', inline: 'center' });
                try { pick.el.click(); } catch (e) {}
            }
            return {
                found: true,
                text: root.text.slice(0, 220),
                clicked: pick ? pick.text : null,
                requiresLogin: root.text.includes('expired') || root.text.includes('logged out'),
            };
            """
        ) or {"found": False}
    except Exception:
        return {"found": False}

    if result.get("found"):
        print(
            f"[{profile_label}] Dismissed Betfair blocking dialog: "
            f"{result.get('clicked') or 'no-button'} | {result.get('text')}",
            flush=True,
        )
        time.sleep(0.5)
    return result


def login_betfair(driver: webdriver.Remote, profile_label: str) -> None:
    """Log into betfair.com/exchange/plus/ using the top-bar form.

    Idempotent: returns immediately when the account is already authenticated.
    """
    if not BETFAIR_USERNAME or not BETFAIR_PASSWORD:
        raise RuntimeError(
            "Missing BETFAIR_USERNAME / BETFAIR_PASSWORD; cannot log into Betfair."
        )

    def wait_ready(timeout: int = 20) -> None:
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

    def locate_login_fields(timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                elems = driver.execute_script(
                    r"""
                    function visible(el) {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return false;
                        const cs = window.getComputedStyle(el);
                        return cs.visibility !== 'hidden' && cs.display !== 'none';
                    }
                    const pwds = Array.from(document.querySelectorAll("input[type='password']"))
                        .filter(visible);
                    if (pwds.length === 0) return null;
                    const pwd = pwds[0];
                    const form = pwd.closest('form') || pwd.parentElement;
                    let candidates = [];
                    if (form) {
                        candidates = Array.from(form.querySelectorAll(
                            "input[type='text'], input[type='email'], input:not([type])"
                        )).filter(visible);
                    }
                    if (candidates.length === 0) {
                        candidates = Array.from(document.querySelectorAll(
                            "input[type='text'], input[type='email']"
                        )).filter(visible);
                    }
                    const user = candidates[0] || null;
                    let btn = null;
                    if (form) {
                        btn = form.querySelector(
                            "button[type='submit'], input[type='submit'], button#login_now_button, input#login_now_button"
                        );
                        if (!btn) {
                            const btns = Array.from(form.querySelectorAll('button')).filter(visible);
                            btn = btns[btns.length - 1] || null;
                        }
                    }
                    return [user, pwd, btn];
                    """
                )
            except Exception:
                elems = None
            if elems and elems[0] and elems[1]:
                return elems[0], elems[1], elems[2]
            time.sleep(0.5)
        return None, None, None

    if BETFAIR_URL_PART not in (driver.current_url or "").lower():
        print(f"[{profile_label}] Navigating to Betfair: {BETFAIR_LOGIN_URL}")
        driver.get(BETFAIR_LOGIN_URL)
    wait_ready()
    _dismiss_betfair_blocking_overlays(driver, profile_label)

    if _is_betfair_logged_in(driver):
        print(f"[{profile_label}] Betfair already logged in; skipping form fill.")
        return

    user_el = pwd_el = btn_el = None
    for attempt_name, reopen_home, timeout in (
        ("current page", False, 8),
        ("exchange home", True, 25),
    ):
        if reopen_home:
            print(f"[{profile_label}] Betfair login form not visible on current page; reopening Exchange home.")
            driver.get(BETFAIR_LOGIN_URL)
            wait_ready()
            time.sleep(1.0)
            _dismiss_betfair_blocking_overlays(driver, profile_label)
            if _is_betfair_logged_in(driver):
                print(f"[{profile_label}] Betfair already logged in after reopening Exchange home.")
                return
        user_el, pwd_el, btn_el = locate_login_fields(timeout)
        if user_el and pwd_el:
            break

    if not user_el or not pwd_el:
        page_text = _visible_page_text(driver)
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:500]
        raise RuntimeError(
            f"[{profile_label}] Betfair login form not found "
            f"(username={bool(user_el)}, password={bool(pwd_el)}, url={driver.current_url}). "
            f"Page: {short_text}"
        )

    try:
        user_el.click()
    except Exception:
        pass
    try:
        user_el.clear()
    except Exception:
        pass
    user_el.send_keys(BETFAIR_USERNAME)

    try:
        pwd_el.click()
    except Exception:
        pass
    try:
        pwd_el.clear()
    except Exception:
        pass
    pwd_el.send_keys(BETFAIR_PASSWORD)

    if btn_el is not None:
        try:
            btn_el.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", btn_el)
            except Exception:
                pwd_el.send_keys(Keys.ENTER)
    else:
        pwd_el.send_keys(Keys.ENTER)

    # Wait for the login to complete (login button disappears or 'Log Out' shows).
    end = time.time() + 30
    while time.time() < end:
        if _is_betfair_logged_in(driver):
            print(f"[{profile_label}] Betfair login successful.")
            return
        time.sleep(1)

    print(
        f"[{profile_label}] WARNING: Could not confirm Betfair login state within 30s. "
        "Check the browser manually."
    )


def _find_betfair_search_input(driver: webdriver.Remote):
    """Return the top-bar Betfair search input element, or None."""
    return driver.execute_script(
        r"""
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = window.getComputedStyle(el);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
        }
        // Prefer explicit search inputs (type=search), then text inputs whose
        // placeholder/aria mentions team/competition/event/sport in any language.
        const all = Array.from(document.querySelectorAll(
            "input[type='search'], input[type='text'], input:not([type])"
        )).filter(visible);
        // Exclude obvious login fields by ignoring inputs whose closest <form>
        // also contains a password field.
        const candidates = all.filter((el) => {
            const f = el.closest('form');
            if (f && f.querySelector("input[type='password']")) return false;
            return true;
        });
        const hint = (el) => ((el.getAttribute('placeholder') || '') + ' '
            + (el.getAttribute('aria-label') || '') + ' '
            + (el.getAttribute('name') || '') + ' '
            + (el.id || '')).toLowerCase();
        const keywords = [
            'search', 'find', 'team', 'teams', 'competition', 'event', 'sport', 'market',
            'команд', 'соревн', 'событ', 'поиск', 'найти'
        ];
        const scored = candidates
            .filter((el) => keywords.some((kw) => hint(el).indexOf(kw) !== -1));
        return scored[0] || candidates[0] || null;
        """
    )


def _ensure_betfair_search_input(driver: webdriver.Remote, profile_label: str):
    deadline = time.time() + 20
    while time.time() < deadline:
        _dismiss_betfair_blocking_overlays(driver, profile_label)
        search_el = _find_betfair_search_input(driver)
        if search_el:
            return search_el
        time.sleep(0.5)

    print(f"[{profile_label}] Betfair search input not found; reopening exchange home and retrying login/search.", flush=True)
    driver.get(BETFAIR_LOGIN_URL)
    _wait_document_ready(driver)
    time.sleep(1.5)
    if not _is_betfair_logged_in(driver):
        login_betfair(driver, profile_label)

    deadline = time.time() + 15
    while time.time() < deadline:
        search_el = _find_betfair_search_input(driver)
        if search_el:
            return search_el
        time.sleep(0.5)
    return None


def _is_betfair_event_url(url: str | None) -> bool:
    lower = (url or "").lower()
    if BETFAIR_URL_PART not in lower:
        return False
    if "/aboutus/" in lower or "/privacy.policy" in lower:
        return False
    if "/betting/" in lower:
        return False
    if "/exchange/plus/search" in lower:
        return False
    if "/exchange/plus/" not in lower:
        return False
    if "/football/" not in lower:
        return False
    return bool(re.search(r"-betting-\d{6,}", lower) or re.search(r"[?&]eventid=\d+", lower))


def _find_betfair_event_href_in_dom(
    driver: webdriver.Remote,
    team_norms: list[str],
    opponent_norms: list[str],
) -> dict | None:
    try:
        result = driver.execute_script(
            _BETFAIR_FUZZY_HELPERS + r"""
            const teamNorms = arguments[0] || [];
            const oppNorms = arguments[1] || [];
            const isGoodHref = (href) => {
                const h = (href || '').toLowerCase();
                if (!h) return false;
                if (h.indexOf('betfair.com') === -1 && !h.startsWith('/')) return false;
                if (h.indexOf('/exchange/plus/') === -1) return false;
                if (h.indexOf('/football/') === -1) return false;
                if (h.indexOf('/aboutus/') !== -1 || h.indexOf('/privacy.policy') !== -1) return false;
                if (h.indexOf('/exchange/plus/search') !== -1) return false;
                if (h.indexOf('/betting/') !== -1) return false;
                return /-betting-\d{6,}/i.test(h) || /[?&]eventid=\d+/i.test(h);
            };
            const links = Array.from(document.querySelectorAll('a[href]'))
                .filter(visible)
                .map((a) => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.innerText || a.textContent || '').trim();
                    const candidateText = norm(text + ' ' + href);
                    return {
                        href,
                        text,
                        score: fuzzyAliasScore(candidateText, teamNorms, oppNorms),
                    };
                })
                .filter((item) => isGoodHref(item.href))
                .filter((item) => item.score > 0)
                .sort((a, b) => b.score - a.score || a.text.length - b.text.length);
            if (!links.length) return null;
            const pick = links[0];
            return { href: pick.href, label: (pick.text || pick.href).slice(0, 220), score: pick.score };
            """,
            team_norms,
            opponent_norms,
        )
    except Exception:
        return None
    if isinstance(result, dict) and result.get("href"):
        return result
    return None


def _betfair_page_matches_target_event(
    driver: webdriver.Remote,
    team_norms: list[str],
    opponent_norms: list[str],
) -> bool:
    current_url = driver.current_url or ""
    if not _is_betfair_event_url(current_url):
        return False
    try:
        match_score = driver.execute_script(
            _BETFAIR_FUZZY_HELPERS + r"""
            const teamNorms = arguments[0] || [];
            const oppNorms = arguments[1] || [];
            const text = norm(document.body?.innerText || '');
            return fuzzyAliasScore(text, teamNorms, oppNorms);
            """,
            team_norms,
            opponent_norms,
        )
        return bool(match_score and int(match_score) > 0)
    except Exception:
        return True


def open_betfair_match(session: dict, signal) -> dict:
    """Open the match on Betfair for the signal, then click the Back cell of
    the matching Over/Under <line> Goals market.

    The driver is closed before returning. Returns a dict with summary info.
    """
    profile_label = session.get("profile_label", "Profile-2")
    team_name = getattr(signal, "home_team", None)
    if not team_name:
        raise RuntimeError("Signal does not contain a first team name for Betfair search.")

    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        open_result = _betfair_search_and_open(driver, signal, profile_label)
        if not open_result.get("opened"):
            return open_result
        try:
            stake_info = session.get("stake") if isinstance(session.get("stake"), dict) else None
            fallback_stake = (stake_info or {}).get("stake")
            sel_result = _select_betfair_overunder_back(driver, signal, profile_label, fallback_stake=fallback_stake)
            open_result.update(sel_result)
        except Exception as exc:
            print(
                f"[{profile_label}] Betfair Over/Under selection failed: {exc}",
                flush=True,
            )
            open_result["selection_error"] = str(exc)
        return open_result
    finally:
        close_driver_bridge(driver)


_BETFAIR_FUZZY_HELPERS = r"""
function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = window.getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
}
function norm(s) {
    return (s || '').toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z0-9]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}
function tokens(s) {
    return norm(s).split(' ').filter((w) => w.length >= 3);
}
function wordMatch(text, word) {
    // Word matches if it appears as a substring, OR shares a >=4-char prefix
    // with any whitespace-separated token (covers source typos like "whalen" vs "whale").
    if (!word) return false;
    if (text.indexOf(word) !== -1) return true;
    const toks = text.split(' ');
    for (const t of toks) {
        if (!t) continue;
        const k = Math.min(t.length, word.length, 4);
        if (k >= 4 && t.slice(0, k) === word.slice(0, k)) return true;
    }
    return false;
}
function fuzzyTeamScore(candidateNorm, teamNorm, oppNorm) {
    const t = candidateNorm || '';
    const teamWords = tokens(teamNorm);
    const oppWords = tokens(oppNorm);
    let score = 0;
    let teamHits = 0, oppHits = 0;
    for (const w of teamWords) if (wordMatch(t, w)) teamHits++;
    for (const w of oppWords) if (wordMatch(t, w)) oppHits++;
    if (teamWords.length === 0 && oppWords.length === 0) return 0;
    // Need at least one hit and at most one missing team word.
    if (teamWords.length && teamHits < Math.max(1, teamWords.length - 1)) return 0;
    // If opponent is known, require at least one opponent hit to avoid
    // opening unrelated matches that only share the home-team token.
    if (oppWords.length && oppHits < 1) return 0;
    score += teamHits * 30;
    score += oppHits * 35;
    // Heavy bonus for exact substring of full normalized team/opp.
    if (teamNorm && t.indexOf(teamNorm) !== -1) score += 60;
    if (oppNorm && t.indexOf(oppNorm) !== -1) score += 80;
    return score;
}
function fuzzyAliasScore(candidateNorm, teamNorms, oppNorms) {
    const teams = Array.isArray(teamNorms) && teamNorms.length ? teamNorms : [''];
    const opponents = Array.isArray(oppNorms) && oppNorms.length ? oppNorms : [''];
    let best = 0;
    for (const teamNorm of teams) {
        for (const oppNorm of opponents) {
            const score = fuzzyTeamScore(candidateNorm, teamNorm, oppNorm);
            if (score > best) best = score;
        }
    }
    return best;
}
"""


def _betfair_search_and_open(driver: webdriver.Remote, signal, profile_label: str) -> dict:
    """Search Betfair for the signal's first team and open the best match.

    Returns a dict with at least {opened: bool, url: str, label: str|None}.
    """
    team_name = getattr(signal, "home_team", None)
    opponent_name = getattr(signal, "away_team", None)
    if not team_name:
        raise RuntimeError("Signal does not contain a first team name for Betfair search.")

    if BETFAIR_URL_PART not in (driver.current_url or "").lower():
        print(f"[{profile_label}] Navigating to Betfair: {BETFAIR_LOGIN_URL}")
        driver.get(BETFAIR_LOGIN_URL)
        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

    _dismiss_betfair_blocking_overlays(driver, profile_label)

    if not _is_betfair_logged_in(driver):
        print(f"[{profile_label}] Betfair session is not logged in before search; logging in again.")
        login_betfair(driver, profile_label)
        if not _is_betfair_logged_in(driver):
            return {
                "profile_label": profile_label,
                "team": team_name,
                "opponent": opponent_name,
                "opened": False,
                "url": driver.current_url,
                "error": "Betfair login could not be confirmed before search.",
            }

    search_el = _ensure_betfair_search_input(driver, profile_label)
    if not search_el:
        return {
            "profile_label": profile_label,
            "team": team_name,
            "opponent": opponent_name,
            "opened": False,
            "url": driver.current_url,
            "error": "Betfair search input not found after login/reopen.",
        }

    team_norms = _normalized_team_aliases(team_name)
    opponent_norms = _normalized_team_aliases(opponent_name) if opponent_name else []

    search_queries: list[str] = []
    seen_queries: set[str] = set()

    def add_search_query(candidate: str | None) -> None:
        value = " ".join((candidate or "").split()).strip()
        if not value:
            return
        key = value.lower()
        if key in seen_queries:
            return
        seen_queries.add(key)
        search_queries.append(value)

    if opponent_name:
        opponent_queries = _team_search_queries(opponent_name)
        team_queries = _team_search_queries(team_name)
        if team_queries and opponent_queries:
            add_search_query(f"{team_queries[0]} {opponent_queries[0]}")
            add_search_query(f"{team_queries[0]} vs {opponent_queries[0]}")
    for query in _team_search_queries(team_name):
        add_search_query(query)

    clicked_label = None
    for search_query in search_queries or [team_name]:
        search_el = _find_betfair_search_input(driver) or _ensure_betfair_search_input(driver, profile_label)
        if not search_el:
            break

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search_el)
        except Exception:
            pass
        try:
            search_el.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].focus();", search_el)
            except Exception:
                pass
        try:
            search_el.send_keys(Keys.CONTROL, "a")
            search_el.send_keys(Keys.DELETE)
        except Exception:
            pass
        try:
            search_el.clear()
        except Exception:
            pass

        print(f"[{profile_label}] Betfair search: typing query '{search_query}'")
        search_el.send_keys(search_query)

        end = time.time() + 8
        clicked_label = None
        while time.time() < end:
            clicked_label = driver.execute_script(
                _BETFAIR_FUZZY_HELPERS + r"""
                const teamNorms = arguments[0] || [];
                const oppNorms = arguments[1] || [];
                const items = Array.from(document.querySelectorAll(
                    "a, li, [role='option'], [role='listitem'], [role='menuitem'], [role='link'], [role='button']"
                )).filter(visible).map((el) => {
                    const text = (el.innerText || el.textContent || '').trim();
                    const score = fuzzyAliasScore(norm(text), teamNorms, oppNorms);
                    return { el, text, n: norm(text), score };
                }).filter((it) => it.text && it.text.length < 250 && it.score > 0);
                if (items.length === 0) return null;
                items.sort((a, b) => b.score - a.score || a.text.length - b.text.length);
                const pick = items[0];
                pick.el.scrollIntoView({block: 'center'});
                pick.el.click();
                return pick.text.slice(0, 200);
                """,
                team_norms,
                opponent_norms,
            )
            if clicked_label:
                break
            time.sleep(0.5)

        if not clicked_label:
            print(f"[{profile_label}] No Betfair autocomplete result for '{search_query}'; submitting search via Enter.")
            try:
                search_el.send_keys(Keys.ENTER)
            except Exception:
                pass
            time.sleep(1.0)
            current_url = (driver.current_url or "").lower()
            if "/search" not in current_url and "search?" not in current_url:
                driver.execute_script(
                    "window.location.href = '/exchange/plus/search?query=' + encodeURIComponent(arguments[0]);",
                    search_query,
                )
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass

        overlay_state = _dismiss_betfair_blocking_overlays(driver, profile_label)
        if overlay_state.get("requiresLogin"):
            print(f"[{profile_label}] Betfair search for '{search_query}' hit an expired session dialog; logging in again.", flush=True)
            login_betfair(driver, profile_label)
            continue

        if not _is_betfair_logged_in(driver):
            print(
                f"[{profile_label}] Betfair search for '{search_query}' landed on a logged-out page; logging in again.",
                flush=True,
            )
            login_betfair(driver, profile_label)
            continue

        followed = _follow_betfair_search_results(driver, team_norms, opponent_norms, profile_label)
        if followed:
            clicked_label = followed

        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

        event_ok = _betfair_page_matches_target_event(driver, team_norms, opponent_norms)
        if not event_ok:
            candidate = _find_betfair_event_href_in_dom(driver, team_norms, opponent_norms)
            if candidate and candidate.get("href"):
                href = candidate.get("href")
                try:
                    absolute = driver.execute_script(
                        "return new URL(arguments[0], window.location.origin).href;",
                        href,
                    )
                except Exception:
                    absolute = href
                try:
                    driver.get(absolute)
                    WebDriverWait(driver, 12).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                    event_ok = _betfair_page_matches_target_event(driver, team_norms, opponent_norms)
                    if event_ok and not clicked_label:
                        clicked_label = candidate.get("label")
                except Exception:
                    pass

        if clicked_label and event_ok:
            print(f"[{profile_label}] Betfair opened result: {clicked_label!r}")
            return {
                "profile_label": profile_label,
                "team": team_name,
                "opponent": opponent_name,
                "opened": True,
                "label": clicked_label,
                "url": driver.current_url,
            }

        if clicked_label and not event_ok:
            print(
                f"[{profile_label}] Betfair ignored non-event result: {clicked_label!r} | url={driver.current_url}",
                flush=True,
            )

        current_url = (driver.current_url or "").lower()
        if "/search" not in current_url and "search?" not in current_url and _betfair_page_matches_target_event(driver, team_norms, opponent_norms):
            inferred_label = f"{team_name} v {opponent_name}" if opponent_name else team_name
            print(f"[{profile_label}] Betfair left search after query '{search_query}', using current match page.")
            return {
                "profile_label": profile_label,
                "team": team_name,
                "opponent": opponent_name,
                "opened": True,
                "label": inferred_label,
                "url": driver.current_url,
            }
    return {
        "profile_label": profile_label,
        "team": team_name,
        "opponent": opponent_name,
        "opened": False,
        "url": driver.current_url,
    }


def _follow_betfair_search_results(
    driver: webdriver.Remote, team_norms: list[str], opponent_norms: list[str], profile_label: str
):
    """If we are on a Betfair Search Results listing, click the matching match link.

    Returns the clicked link text on success, otherwise None.
    """
    end = time.time() + 15
    last_debug = None
    while time.time() < end:
        url = (driver.current_url or "").lower()
        on_results = "/search" in url or "search?" in url
        if not on_results:
            # Also check for the visible "Search Results" header (covers cases
            # where the URL changed via SPA routing without the path word).
            on_results = bool(driver.execute_script(
                r"""
                const nodes = document.querySelectorAll("h1, h2, h3, header");
                for (const n of nodes) {
                    const t = (n.innerText || '').trim().toLowerCase();
                    if (t === 'search results' || t.startsWith('search results')) return true;
                }
                return false;
                """
            ))
        if not on_results:
            return None
        result = driver.execute_script(
            _BETFAIR_FUZZY_HELPERS + r"""
            const teamNorms = arguments[0] || [];
            const oppNorms = arguments[1] || [];
            const isBadHref = (href) => {
                const h = (href || '').toLowerCase();
                if (!h) return false;
                if (h.indexOf('/aboutus/') !== -1) return true;
                if (h.indexOf('/privacy.policy') !== -1) return true;
                if (h.indexOf('gamblingcommission.gov.uk') !== -1) return true;
                if (h.indexOf('/exchange/plus/search') !== -1) return true;
                if (h.indexOf('/betting/football/s-') !== -1) return true;
                return false;
            };
            const hasInterestingText = (text) => {
                const lowered = (text || '').toLowerCase();
                return /\bv\b|\bvs\b/.test(lowered) || /football|soccer|goals|match/.test(lowered);
            };
            const pickHref = (element) => {
                if (!element) return '';
                const direct = element.matches && element.matches('a[href]') ? element : null;
                const nested = element.querySelector ? element.querySelector('a[href]') : null;
                const parent = element.closest ? element.closest('a[href]') : null;
                const target = direct || nested || parent;
                return target ? (target.getAttribute('href') || '') : '';
            };
            const pickClickTarget = (element) => {
                if (!element) return null;
                return element.closest?.("a, button, [role='link'], [role='button']")
                    || element.querySelector?.("a, button, [role='link'], [role='button']")
                    || element;
            };
            const labelFrom = (element) => {
                if (!element) return '';
                const direct = (element.matches?.('a[href], button, [role="link"], [role="button"]') ? element : null);
                const nested = element.querySelector?.('a[href], button, [role="link"], [role="button"]') || null;
                const target = direct || nested || null;
                const texts = [
                    target ? (target.innerText || target.textContent || '') : '',
                    element.getAttribute?.('aria-label') || '',
                    element.getAttribute?.('title') || '',
                    element.innerText || element.textContent || '',
                ].map((value) => (value || '').trim()).filter(Boolean);
                const preferred = texts.find((value) => /\bv\b|\bvs\b/i.test(value) && value.length <= 220)
                    || texts.find((value) => value.length <= 140)
                    || texts[0]
                    || '';
                return preferred.replace(/\s+/g, ' ').trim();
            };
            const all = Array.from(document.querySelectorAll(
                "a, button, [role='link'], [role='button'], [role='option'], li, article, div"
            )).filter(visible);
            const scored = all.map((element) => {
                const text = (element.innerText || element.textContent || '').trim();
                const label = labelFrom(element);
                const href = pickHref(element);
                const n = norm(text);
                const labelNorm = norm(label);
                const score = Math.max(
                    fuzzyAliasScore(labelNorm, teamNorms, oppNorms),
                    fuzzyAliasScore(n, teamNorms, oppNorms)
                );
                const rect = element.getBoundingClientRect();
                // Match-page links on Betfair Exchange look like
                // /exchange/plus/.../<slug>-betting-<id> — event IDs are 6+ digits.
                // Competition/league pages use short IDs (1-4 digits), so we exclude them.
                const hrefLooksLikeMatch = /-betting-\d{6,}/i.test(href)
                    || /[?&]eventId=\d+/i.test(href);
                // Text contains a ' v ' separator (e.g. 'Team A v Team B').
                const hasVs = /\bv\b/i.test(label || text) || /\bvs\b/i.test(label || text);
                return {
                    el: element,
                    clickEl: pickClickTarget(element),
                    text,
                    label,
                    n,
                    href,
                    score,
                    badHref: isBadHref(href),
                    hrefLooksLikeMatch,
                    hasVs,
                    interestingText: hasInterestingText(text),
                    interestingLabel: hasInterestingText(label),
                    area: rect.width * rect.height,
                    central: rect.x > 80 && rect.right < window.innerWidth * 0.92 && rect.y > 70,
                };
            });
            // Primary: match-shaped href AND positive fuzzy score.
            let cands = scored.filter((it) => (it.label || it.text))
                .filter((it) => (it.label || '').length < 220 || it.hrefLooksLikeMatch)
                .filter((it) => it.area > 0 && it.area < window.innerWidth * window.innerHeight * 0.92)
                .filter((it) => it.central || it.hrefLooksLikeMatch)
                .filter((it) => !it.badHref)
                .filter((it) => it.hrefLooksLikeMatch || it.hasVs || it.interestingLabel || it.interestingText)
                .filter((it) => it.score > 0);
            // Fallback: positive score AND text contains ' v ' separator.
            if (cands.length === 0) {
                cands = scored.filter((it) => (it.label || it.text))
                    .filter((it) => (it.label || '').length < 260 || it.hrefLooksLikeMatch)
                    .filter((it) => it.central || it.hrefLooksLikeMatch)
                    .filter((it) => !it.badHref)
                    .filter((it) => it.score > 0 && (it.hasVs || it.hrefLooksLikeMatch));
            }
            if (cands.length === 0) {
                // Return debug snapshot for the caller to log.
                const debugPool = scored
                    .filter((it) => it.central || it.hrefLooksLikeMatch)
                    .sort((a, b) => b.score - a.score || a.area - b.area)
                    .slice(0, 12);
                return {
                    error: 'no candidate',
                    hrefSample: debugPool.map((s) => s.href).filter((h) => h),
                    textSample: debugPool.map((s) => (s.label || s.text).slice(0, 100)).filter((t) => t),
                };
            }
            cands.sort((a, b) => b.score - a.score || (a.hrefLooksLikeMatch === b.hrefLooksLikeMatch ? 0 : (a.hrefLooksLikeMatch ? -1 : 1)) || (a.label || a.text).length - (b.label || b.text).length || a.area - b.area);
            const pick = cands[0];
            if (pick.badHref || (pick.href && !pick.hrefLooksLikeMatch)) {
                return {
                    error: 'non-event candidate',
                    hrefSample: [pick.href].filter(Boolean),
                    textSample: [(pick.label || pick.text || '').slice(0, 120)],
                };
            }
            try { pick.clickEl.scrollIntoView({ block: 'center' }); } catch (e) {}
            let clicked = false;
            try {
                pick.clickEl.click();
                clicked = true;
            } catch (e) {}
            if (!clicked) {
                try {
                    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                    pick.clickEl.dispatchEvent(evt);
                    clicked = true;
                } catch (e) {}
            }
            // Hard fallback: navigate to the href directly so the SPA loads the match.
            if (pick.href) {
                try {
                    const absolute = new URL(pick.href, window.location.origin).href;
                    if (window.location.href !== absolute) {
                        // Only navigate if the SPA click didn't trigger a route change.
                        setTimeout(() => {
                            if (window.location.href.indexOf('/search') !== -1) {
                                window.location.href = absolute;
                            }
                        }, 800);
                    }
                } catch (e) {}
            }
            return { ok: true, text: (pick.label || pick.text || pick.href).slice(0, 200), href: pick.href };
            """,
            team_norms,
            opponent_norms,
        )
        if isinstance(result, dict) and result.get("ok"):
            clicked = result.get("text") or result.get("href")
            print(f"[{profile_label}] Betfair search-results: followed link {clicked!r}")
            try:
                WebDriverWait(driver, 12).until(
                    lambda d: "/search" not in (d.current_url or "").lower()
                )
            except Exception:
                pass
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            return clicked
        if isinstance(result, dict) and result.get("error"):
            last_debug = result
        time.sleep(0.5)
    if last_debug is not None:
        print(
            f"[{profile_label}] Betfair search-results: no match clicked. "
            f"hrefSample={last_debug.get('hrefSample')!r} "
            f"textSample={last_debug.get('textSample')!r}",
            flush=True,
        )
    return None


def _select_betfair_overunder_back(
    driver: webdriver.Remote,
    signal,
    profile_label: str,
    fallback_stake: str | None = None,
) -> dict:
    """Click the Back (blue) odds cell for the Over/Under <line> Goals selection.

    Picks blue vs pink by comparing computed background-color (Back is blue, Lay
    is pink), with fallbacks on class/aria hints. If the requested market is not
    on screen, click the matching left-sidebar link to open it.
    """
    selection = (getattr(signal, "selection", "") or "").strip().lower()
    if selection not in ("over", "under"):
        raise RuntimeError(f"Unsupported Betfair selection: {selection!r}")
    line_value = getattr(signal, "line", None)
    if line_value is None:
        raise RuntimeError("Signal has no line value for Betfair Over/Under.")
    line_str = format(Decimal(str(line_value)).normalize(), "f")
    target_odds = format(Decimal(str(getattr(signal, "odds"))).normalize(), "f")
    label_text = f"{selection} {line_str} goals"
    market_texts = _betfair_market_texts(signal, line_str)
    market_label = _signal_market_label(signal)
    market_key = _signal_market_key(signal)
    allow_label_fallback = True
    preferred_tabs = ["half time", "2nd half"] if market_key == "second_half_goals" else []

    if preferred_tabs:
        try:
            driver.execute_script(
                r"""
                const preferredTabs = arguments[0].map((value) => (value || '').toLowerCase().trim());
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                const candidates = Array.from(document.querySelectorAll("a, button, [role='button'], [role='tab'], li, div, span"))
                    .filter(visible)
                    .map((el) => ({
                        el,
                        text: (el.innerText || el.textContent || '').trim().toLowerCase().replace(/\s+/g, ' '),
                        rect: el.getBoundingClientRect(),
                    }))
                    .filter((item) => item.text && item.text.length <= 40)
                    .filter((item) => preferredTabs.some((tab) => item.text === tab || item.text.startsWith(tab)));
                if (candidates.length > 0) {
                    candidates.sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
                    const pick = candidates[0].el.closest("a, button, [role='button'], [role='tab']") || candidates[0].el;
                    pick.scrollIntoView({ block: 'center', inline: 'center' });
                    pick.click();
                }
                """,
                preferred_tabs,
            )
            time.sleep(0.7)
        except Exception:
            pass

    # 1) Try to bring the market into the DOM: click the left-sidebar entry if
    # the market section is not already visible.
    try:
        driver.execute_script(
            r"""
            const marketTexts = arguments[0];
            const normalizeMarket = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
            const marketTextNorms = marketTexts.map(normalizeMarket);
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            const links = Array.from(document.querySelectorAll("a, button, [role='button'], li"))
                .filter(visible)
                .filter((el) => {
                    const t = normalizeMarket(el.innerText || '');
                    return marketTextNorms.some((marketText) => t === marketText || t.includes(marketText));
                });
            if (links.length > 0) {
                links[0].scrollIntoView({ block: 'center' });
                links[0].click();
            }
            """,
            market_texts,
        )
    except Exception:
        pass

    # 2) Poll for a row whose label text equals 'Over <line> Goals' / 'Under <line> Goals'
    end = time.time() + 30
    info = None

    def _activate_betfair_betslip() -> dict:
        """Best-effort click on betslip controls (Place Bets / Edit) in classic UI."""
        try:
            result = driver.execute_script(
                r"""
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                function norm(text) {
                    return (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
                }
                const labels = ['place bets', 'place bet', 'betslip', 'bet slip', 'edit'];
                const clickTarget = (item) => {
                    const target = item.el.closest("button, a, [role='button'], [role='tab']") || item.el;
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    try { target.click(); } catch (e) {}
                    return { clicked: true, label: item.text || item.marker, x: Math.round(item.rect.x), y: Math.round(item.rect.y) };
                };
                const controls = Array.from(document.querySelectorAll(
                    "button, a, [role='button'], [role='tab'], [data-test], [data-testid], li, div, span"
                ))
                    .filter(visible)
                    .map((el) => {
                        const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                        const marker = norm([
                            el.getAttribute('data-test') || '',
                            el.getAttribute('data-testid') || '',
                            el.className && el.className.toString ? el.className.toString() : '',
                            el.id || '',
                            el.getAttribute('role') || '',
                        ].join(' '));
                        const rect = el.getBoundingClientRect();
                        return { el, text, marker, rect };
                    })
                    .filter((item) => item.text || item.marker)
                    .filter((item) => labels.some((label) => item.text === label || item.text.startsWith(label) || item.marker.includes(label)));
                if (!controls.length) return { clicked: false };

                const placeBetsTop = controls
                    .filter((item) => item.text === 'place bets' || item.text.startsWith('place bets'))
                    .filter((item) => item.rect.x > window.innerWidth * 0.45)
                    .sort((a, b) => a.rect.y - b.rect.y || b.rect.x - a.rect.x)[0];
                if (placeBetsTop) return clickTarget(placeBetsTop);

                controls.sort((a, b) => {
                    const scoreFor = (item) => {
                        let score = 0;
                        if (item.text === 'place bets') score += 8;
                        if (item.text.startsWith('place bet')) score += 6;
                        if (item.text.includes('betslip') || item.text.includes('bet slip')) score += 4;
                        if (item.text.startsWith('edit')) score += 2;
                        if (item.rect.x > window.innerWidth * 0.45) score += 3;
                        if (item.rect.y < window.innerHeight * 0.45) score += 2;
                        if (item.rect.y > window.innerHeight * 0.55) score += 1;
                        return score;
                    };
                    const aScore = scoreFor(a);
                    const bScore = scoreFor(b);
                    return bScore - aScore || b.rect.width * b.rect.height - a.rect.width * a.rect.height;
                });
                const pick = controls[0];
                return clickTarget(pick);
                """
            )
            return result if isinstance(result, dict) else {"clicked": False}
        except Exception:
            return {"clicked": False}

    def _click_betfair_action_button(confirm_only: bool = False) -> dict:
        """Click Place/Confirm inside the active betslip coupon, not a global nav tab."""
        try:
            result = driver.execute_script(
                r"""
                const confirmOnly = !!arguments[0];
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                function norm(text) {
                    return (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
                }
                function txt(el) {
                    return norm(el.innerText || el.textContent || el.value || '');
                }
                function isActionText(t) {
                    if (!t) return false;
                    if (confirmOnly) return t === 'confirm bet' || t === 'confirm bets' || t.startsWith('confirm bet');
                    return t === 'confirm bet' || t === 'confirm bets'
                        || t === 'place bet' || t === 'place bets'
                        || t.startsWith('confirm bet') || t.startsWith('place bet');
                }
                const panels = Array.from(document.querySelectorAll('aside, section, div, form'))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = txt(el);
                        const inputs = Array.from(el.querySelectorAll("input[type='text'], input[type='number'], input[type='tel'], input:not([type]), textarea")).filter(visible);
                        const buttons = Array.from(el.querySelectorAll("button, [role='button'], input[type='submit'], input[type='button']")).filter(visible)
                            .map((b) => ({ el: b, text: txt(b), rect: b.getBoundingClientRect() }));
                        const hasCancel = buttons.some((b) => b.text === 'cancel' || b.text.startsWith('cancel'));
                        const actionButtons = buttons.filter((b) => isActionText(b.text));
                        return {
                            el,
                            rect,
                            text,
                            inputs,
                            hasCancel,
                            actionButtons,
                            score: (rect.x > window.innerWidth * 0.55 ? 3 : 0)
                                + (rect.y > window.innerHeight * 0.35 ? 2 : 0)
                                + (inputs.length > 0 ? 2 : 0)
                                + (hasCancel ? 2 : 0)
                                + (actionButtons.length > 0 ? 6 : 0),
                        };
                    })
                    .filter((p) => p.rect.width > 150 && p.rect.height > 90)
                    .filter((p) => p.actionButtons.length > 0)
                    .sort((a, b) => b.score - a.score || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
                if (!panels.length) {
                    return { error: confirmOnly ? 'confirm bet button not found' : 'confirm/place button not found' };
                }
                const panel = panels[0];
                panel.actionButtons.sort((a, b) => {
                    const aPlace = a.text.includes('place') ? 0 : 1;
                    const bPlace = b.text.includes('place') ? 0 : 1;
                    return aPlace - bPlace || b.rect.y - a.rect.y || b.rect.x - a.rect.x;
                });
                const btn = panel.actionButtons[0].el;
                btn.scrollIntoView({ block: 'center', inline: 'center' });
                const r = btn.getBoundingClientRect();
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                const liveTarget = document.elementFromPoint(x, y) || btn;
                for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    const EventCtor = name.startsWith('pointer') ? (window.PointerEvent || MouseEvent) : MouseEvent;
                    try {
                        liveTarget.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerType: 'mouse' }));
                    } catch (e) {
                        liveTarget.dispatchEvent(new MouseEvent(name.replace('pointer', 'mouse'), { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
                    }
                }
                try { btn.click(); } catch (e) {}
                return {
                    ok: true,
                    label: (btn.innerText || btn.value || '').trim(),
                    panelText: panel.text.slice(0, 180),
                    hasCancel: panel.hasCancel,
                    inputCount: panel.inputs.length,
                };
                """,
                confirm_only,
            )
            return result if isinstance(result, dict) else {"error": "action button click script returned invalid result"}
        except Exception as exc:
            return {"error": f"action button click failed: {exc}"}

    def read_betfair_betslip_state(timeout: float = 0.0) -> dict:
        deadline = time.monotonic() + max(timeout, 0.0)
        last_state = None
        while True:
            last_state = driver.execute_script(
                r"""
                const selection = (arguments[0] || '').toLowerCase().trim();
                const lineStr = (arguments[1] || '').toLowerCase().replace(',', '.').trim();
                const labelText = (arguments[2] || '').toLowerCase().trim();
                const marketTexts = (arguments[3] || []).map((value) => (value || '').toLowerCase().trim());
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                function txt(el) {
                    return ((el.innerText || el.textContent || '')).trim();
                }
                function normalize(value) {
                    return (value || '').toLowerCase().replace(/\s+/g, ' ').replace(/,/g, '.').trim();
                }
                const selector = "input[type='text'], input[type='number'], input[type='tel'], input:not([type]), textarea";
                const rightPanels = Array.from(document.querySelectorAll('aside, section, div, form'))
                    .filter(visible)
                    .map((el) => ({
                        el,
                        rect: el.getBoundingClientRect(),
                        text: txt(el),
                    }))
                    .filter((item) => item.rect.x > window.innerWidth * 0.58)
                    .filter((item) => item.rect.width > 160 && item.rect.height > 110)
                    .sort((a, b) => a.rect.x - b.rect.x || b.rect.height - a.rect.height);

                const panelStates = rightPanels.map((item) => {
                    const normalizedText = normalize(item.text);
                    const inputs = Array.from(item.el.querySelectorAll(selector)).filter(visible);
                    const buttons = Array.from(item.el.querySelectorAll("button, [role='button'], input[type='submit']"))
                        .filter(visible)
                        .map((el) => normalize(txt(el) || el.value || ''));
                    const empty = normalizedText.includes('you have no bets on this market')
                        || normalizedText.includes('click on the odds to add selections to the betslip')
                        || normalizedText.includes('no bets on this market');
                    const hasActionButton = buttons.some((text) => text === 'confirm bet' || text === 'confirm bets'
                        || text === 'place bet' || text === 'place bets'
                        || text.startsWith('confirm bet') || text.startsWith('place bet'));
                    const hasSelectionText = (!!labelText && normalizedText.includes(labelText))
                        || (!!selection && !!lineStr && normalizedText.includes(selection) && normalizedText.includes(lineStr))
                        || marketTexts.some((marketText) => marketText && normalizedText.includes(normalize(marketText)));
                    const betslipLike = normalizedText.includes('place bets')
                        || normalizedText.includes('open bets')
                        || normalizedText.includes('betslip')
                        || hasActionButton
                        || inputs.length > 0;
                    return {
                        ready: !empty && (inputs.length > 0 || (hasActionButton && hasSelectionText)),
                        empty,
                        betslipLike,
                        hasSelectionText,
                        hasActionButton,
                        inputCount: inputs.length,
                        buttonTexts: buttons.slice(0, 6),
                        panelText: item.text.replace(/\s+/g, ' ').slice(0, 280),
                        x: Math.round(item.rect.x),
                        y: Math.round(item.rect.y),
                        w: Math.round(item.rect.width),
                        h: Math.round(item.rect.height),
                    };
                });

                panelStates.sort((a, b) => {
                    const aReady = a.ready ? 0 : 1;
                    const bReady = b.ready ? 0 : 1;
                    return aReady - bReady
                        || (b.inputCount - a.inputCount)
                        || ((b.hasActionButton ? 1 : 0) - (a.hasActionButton ? 1 : 0))
                        || ((b.hasSelectionText ? 1 : 0) - (a.hasSelectionText ? 1 : 0))
                        || ((b.betslipLike ? 1 : 0) - (a.betslipLike ? 1 : 0));
                });

                if (panelStates.length > 0) return panelStates[0];
                return {
                    ready: false,
                    empty: false,
                    betslipLike: false,
                    hasSelectionText: false,
                    hasActionButton: false,
                    inputCount: 0,
                    buttonTexts: [],
                    panelText: '',
                };
                """,
                selection,
                line_str,
                label_text,
                market_texts,
            ) or {
                "ready": False,
                "empty": False,
                "betslipLike": False,
                "hasSelectionText": False,
                "hasActionButton": False,
                "inputCount": 0,
                "buttonTexts": [],
                "panelText": "",
            }
            if last_state.get("ready"):
                return last_state
            if time.monotonic() >= deadline:
                return last_state
            time.sleep(0.25)

    while time.time() < end:
        info = driver.execute_script(
            r"""
            const want = arguments[0];           // 'over' or 'under'
            const lineStr = arguments[1];        // '1.5'
            const labelText = arguments[2];      // 'over 1.5 goals'
            const marketTexts = arguments[3];    // e.g. ['over/under 1.5 goals'] or ['2nd half goals']
            const allowLabelFallback = arguments[4];
            const marketKey = arguments[5];
            const normalizeMarket = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
            const marketTextNorms = marketTexts.map(normalizeMarket);
            const normalizedLine = (lineStr || '').toLowerCase().replace(',', '.').trim();
            const rowMatchesSelection = (text) => {
                const normalized = (text || '').toLowerCase().replace(/\s+/g, ' ').replace(',', '.').trim();
                if (!normalized || normalized.indexOf(want) === -1) return false;
                if (normalized.indexOf(labelText) !== -1) return true;
                return normalized.indexOf(normalizedLine) !== -1;
            };
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            function txt(el) { return ((el.innerText || el.textContent || '')).trim(); }
            function normalizedTxt(el) { return txt(el).toLowerCase().replace(/\s+/g, ' ').trim(); }
            function parseRgb(s) {
                const m = (s || '').match(/rgba?\(([^)]+)\)/);
                if (!m) return null;
                const parts = m[1].split(',').map((x) => parseFloat(x.trim()));
                return { r: parts[0]||0, g: parts[1]||0, b: parts[2]||0 };
            }
            function numericCells(root) {
                let cells = Array.from(root.querySelectorAll("button, [role='button'], a, td, div, span"))
                    .filter(visible)
                    .filter((el) => {
                        const t = txt(el);
                        if (!t || t.length > 50) return false;
                        return /^\d+(\.\d+)?\b/.test(t);
                    });
                return cells.filter((cell) => {
                    return !cells.some((other) => other !== cell && cell.contains(other));
                });
            }
            function scoreBackCell(cell) {
                const cs = window.getComputedStyle(cell);
                const bg = parseRgb(cs.backgroundColor) || { r: 255, g: 255, b: 255 };
                const cls = (cell.className && cell.className.toString
                    ? cell.className.toString().toLowerCase() : '');
                const aria = ((cell.getAttribute('aria-label') || '') + ' '
                    + (cell.getAttribute('title') || '')).toLowerCase();
                let score = 0;
                if (cls.indexOf('back') !== -1) score += 60;
                if (cls.indexOf('lay') !== -1) score -= 60;
                if (aria.indexOf('back') !== -1) score += 30;
                if (aria.indexOf('lay') !== -1) score -= 30;
                let p = cell.parentElement;
                for (let j = 0; j < 5 && p; j++) {
                    const pc = (p.className && p.className.toString
                        ? p.className.toString().toLowerCase() : '');
                    if (pc.indexOf('back') !== -1) { score += 25; break; }
                    if (pc.indexOf('lay') !== -1) { score -= 25; break; }
                    p = p.parentElement;
                }
                if (bg.b > bg.r + 5) score += 30;
                if (bg.r > bg.b + 5) score -= 30;
                return { cell, score, rect: cell.getBoundingClientRect(), priceText: txt(cell), bg, cls };
            }
            function clickCell(cell) {
                const targets = [];
                const pushUnique = (element) => {
                    if (!element || targets.includes(element) || !visible(element)) return;
                    targets.push(element);
                };
                pushUnique(cell);
                pushUnique(cell.closest?.("td, button, [role='button'], a, [role='link']"));
                let parent = cell.parentElement;
                for (let depth = 0; depth < 6 && parent; depth += 1, parent = parent.parentElement) {
                    const marker = [
                        parent.className && parent.className.toString ? parent.className.toString() : '',
                        parent.getAttribute('role') || '',
                        parent.getAttribute('data-test') || '',
                        parent.getAttribute('data-testid') || '',
                        parent.getAttribute('aria-label') || '',
                        parent.getAttribute('title') || '',
                    ].join(' ').toLowerCase();
                    const tag = (parent.tagName || '').toLowerCase();
                    if (tag === 'td' || tag === 'button' || tag === 'a' || /back|lay|price|runner|bet|selection|odds/.test(marker)) {
                        pushUnique(parent);
                    }
                }
                const clickSummaries = [];
                for (const target of targets.slice(0, 8)) {
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    const rect = target.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    const liveTarget = document.elementFromPoint(x, y) || target;
                    for (const name of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        const EventCtor = name.startsWith('pointer') ? (window.PointerEvent || MouseEvent) : MouseEvent;
                        try {
                            liveTarget.dispatchEvent(new EventCtor(name, { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, pointerType: 'mouse' }));
                        } catch (e) {
                            liveTarget.dispatchEvent(new MouseEvent(name.replace('pointer', 'mouse'), { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
                        }
                    }
                    try { target.click?.(); } catch (e) {}
                    clickSummaries.push({
                        tag: (target.tagName || '').toLowerCase(),
                        text: txt(target).slice(0, 40),
                        cls: (target.className && target.className.toString ? target.className.toString() : '').slice(0, 80),
                    });
                }
                return clickSummaries;
            }
            function pickBackCell(cells) {
                const scored = cells.map(scoreBackCell);
                const backs = scored.filter((s) => s.score > 0);
                backs.sort((a, b) => b.rect.right - a.rect.right);
                return backs[0] || null;
            }
            function clickLegacyExchangeMarket() {
                const headers = Array.from(document.querySelectorAll('*'))
                    .filter(visible)
                    .map((el) => ({ el, text: normalizedTxt(el), rect: el.getBoundingClientRect() }))
                    .filter((item) => item.rect.x > 120)
                    .filter((item) => {
                        const text = normalizeMarket(item.text);
                        return marketTextNorms.some((marketText) => text === marketText || text.indexOf(marketText) !== -1);
                    })
                    .sort((a, b) => a.text.length - b.text.length || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
                const debug = [];
                for (const header of headers.slice(0, 12)) {
                    let container = header.el;
                    for (let depth = 0; depth < 8 && container; depth++, container = container.parentElement) {
                        if (!visible(container)) continue;
                        const containerText = normalizedTxt(container);
                        const containerMarketText = normalizeMarket(containerText);
                        if (!marketTextNorms.some((marketText) => containerMarketText.indexOf(marketText) !== -1)) continue;
                        if (!rowMatchesSelection(containerText)) continue;
                        const containerRect = container.getBoundingClientRect();
                        if (containerRect.width < 220 || containerRect.height < 55) continue;
                        const rows = Array.from(container.querySelectorAll('tr, li, div'))
                            .filter(visible)
                            .map((row) => ({ row, text: normalizedTxt(row), rect: row.getBoundingClientRect() }))
                            .filter((item) => rowMatchesSelection(item.text))
                            .filter((item) => item.rect.y >= header.rect.y - 8)
                            .filter((item) => item.rect.height <= 95 && item.rect.width >= 130)
                            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height) || a.rect.y - b.rect.y);
                        for (const row of rows) {
                            const cells = numericCells(row.row);
                            if (cells.length < 2) {
                                debug.push({ row: row.text.slice(0, 120), cells: cells.length });
                                continue;
                            }
                            const pick = pickBackCell(cells);
                            if (!pick) {
                                debug.push({ row: row.text.slice(0, 120), cells: cells.map((cell) => txt(cell)).slice(0, 6) });
                                continue;
                            }
                            const clickTargets = clickCell(pick.cell);
                            return { ok: true, priceText: pick.priceText, score: pick.score, bg: pick.bg, cls: pick.cls, method: 'legacy-market-card', clickTargets };
                        }
                    }
                }
                return { error: 'legacy market row not found', marketTexts, labelText, headers: headers.slice(0, 5).map((h) => h.text.slice(0, 120)), debug: debug.slice(0, 5) };
            }
            const legacyResult = clickLegacyExchangeMarket();
            if (legacyResult && legacyResult.ok) return legacyResult;
            if (!allowLabelFallback) return legacyResult;
            // Find the SMALLEST element whose visible text equals (or starts with) the label.
            // Exclude 'first half' / 'half time' / 'second half' rows so we hit the
            // full-match Over/Under market, matching how Black always treats the
            // signal as full-time.
            const labels = Array.from(document.querySelectorAll("*"))
                .filter(visible)
                .filter((el) => {
                    const t = txt(el).toLowerCase();
                    if (!t || t.length > 200) return false;
                    if (!rowMatchesSelection(t)) return false;
                    if (marketKey !== 'second_half_goals' && t.indexOf('half') !== -1) return false;
                    return true;
                });
            if (labels.length === 0) {
                return { error: 'label not found', labelText, marketKey };
            }
            // Prefer leaf-most label (shortest text containing it).
            labels.sort((a, b) => txt(a).length - txt(b).length);

            // For each candidate, walk up to a row container that has 2+ clickable
            // price buttons but does NOT contain a second 'X.5 Goals' row label.
            function findRowAndCells(label) {
                let p = label;
                for (let i = 0; i < 10 && p; i++) {
                    p = p.parentElement;
                    if (!p) break;
                    const rowText = (p.innerText || '').toLowerCase();
                    const goalCounts = (rowText.match(/\d+(\.\d+)?\s*goals/g) || []).length;
                    // Find clickable cells with numeric content inside this container.
                    let cells = numericCells(p);
                    if (cells.length >= 2) {
                        // Make sure container is just this row, not the whole card.
                        if (goalCounts > 1) continue;
                        return { row: p, cells };
                    }
                }
                return null;
            }
            let chosen = null;
            for (const lab of labels) {
                chosen = findRowAndCells(lab);
                if (chosen) break;
            }
            if (!chosen) {
                return { error: 'row with price cells not found', labelText };
            }
            // Score each clickable cell — pick the Back (blue) one furthest to the right
            // of the 'Back' group (which is leftmost). Back cells: cls includes 'back',
            // background blue dominates red. Lay cells: cls includes 'lay', red>blue.
            const scored = chosen.cells.map(scoreBackCell);
            // Among Back-scoring cells, prefer the rightmost (best Back price column).
            const pick = pickBackCell(chosen.cells);
            if (!pick) {
                return { error: 'no Back cell could be identified', scored: scored.map((s) => ({ score: s.score, price: s.priceText, cls: s.cls })) };
            }
            const clickTargets = clickCell(pick.cell);
            return { ok: true, priceText: pick.priceText, score: pick.score, bg: pick.bg, cls: pick.cls, clickTargets };
            """,
            selection,
            line_str,
            label_text,
            market_texts,
            allow_label_fallback,
            market_key,
        )
        if info and info.get("ok"):
            betslip_state = read_betfair_betslip_state(timeout=2.5)
            if betslip_state.get("ready"):
                info["betslipState"] = betslip_state
                break
            activation = _activate_betfair_betslip()
            if activation.get("clicked"):
                time.sleep(0.35)
                betslip_state = read_betfair_betslip_state(timeout=1.2)
                if betslip_state.get("ready"):
                    info["betslipState"] = betslip_state
                    break
            info = {
                **info,
                "error": "betslip did not open after odds click",
                "betslipState": betslip_state,
                "betslipActivation": activation,
            }
        time.sleep(0.6)

    if not info or not info.get("ok"):
        raise RuntimeError(
            f"[{profile_label}] Could not select Betfair Back {selection.title()} "
            f"{line_str} Goals in {market_label}: {info!r}"
        )
    odds_text = info.get("priceText", "")
    print(
        f"[{profile_label}] Betfair Back selected: {market_label} {selection.title()} {line_str} "
        f"Goals at available odds {odds_text}; target odds {target_odds}"
    )

    # 3) Fill in stake and place the bet.
    stake_str = None
    bet_placed = False
    accepted_odds_text = target_odds
    last_betfair_error = None
    try:
        balance = _read_betfair_balance(driver, profile_label)
        stake_amount = _calculate_stake_from_balance(balance)
        stake_str = _format_stake_amount(stake_amount)
        print(f"[{profile_label}] Betfair stake: {stake_str} (balance {balance})")
    except Exception as exc:
        if fallback_stake:
            stake_str = str(fallback_stake)
            print(f"[{profile_label}] Betfair: using cached stake {stake_str}; balance read failed: {exc}", flush=True)
        else:
            print(f"[{profile_label}] Betfair: could not compute stake: {exc}", flush=True)
            last_betfair_error = str(exc)

    if stake_str:
        def _normalize_numeric_text(value: str | None) -> str:
            return (value or "").strip().replace(" ", "").replace(",", ".")

        def _same_numeric_text(actual: str | None, expected: str | None) -> bool:
            a = _normalize_numeric_text(actual)
            e = _normalize_numeric_text(expected)
            if a == e:
                return True
            try:
                return abs(float(a) - float(e)) < 1e-6
            except Exception:
                return False

        def _fallback_fill_betfair_inputs_via_send_keys() -> dict:
            controls = driver.execute_script(
                r"""
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                function norm(text) {
                    return (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
                }
                function val(el) {
                    if (!el) return '';
                    if (typeof el.value === 'string') return el.value;
                    return (el.textContent || el.innerText || el.getAttribute('aria-valuetext') || '').trim();
                }
                const clickPlaceBets = () => {
                    const items = Array.from(document.querySelectorAll("button, a, [role='button'], [role='tab'], li, div, span"))
                        .filter(visible)
                        .map((el) => ({
                            el,
                            text: norm(el.innerText || el.textContent || el.getAttribute('aria-label') || ''),
                            rect: el.getBoundingClientRect(),
                        }))
                        .filter((item) => item.text === 'place bets' || item.text.startsWith('place bets'))
                        .filter((item) => item.rect.x > window.innerWidth * 0.45)
                        .sort((a, b) => a.rect.y - b.rect.y || b.rect.x - a.rect.x);
                    const pick = items[0];
                    if (!pick) return false;
                    const target = pick.el.closest("button, a, [role='button'], [role='tab']") || pick.el;
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    try { target.click(); } catch (e) {}
                    return true;
                };
                clickPlaceBets();
                const selector = "input[type='text'], input[type='number'], input[type='tel'], input:not([type]), textarea, [contenteditable='true'], [role='textbox'], [role='spinbutton']";
                const panels = Array.from(document.querySelectorAll('aside, section, div, form'))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = norm(el.innerText || el.textContent || '');
                        const inputs = Array.from(el.querySelectorAll(selector)).filter(visible);
                        const hasCancel = Array.from(el.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']"))
                            .filter(visible)
                            .some((b) => norm(b.innerText || b.value || '').includes('cancel'));
                        const hasAction = Array.from(el.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']"))
                            .filter(visible)
                            .some((b) => {
                                const t = norm(b.innerText || b.value || '');
                                return t.includes('place') || t.includes('confirm') || t.includes('edit');
                            });
                        const betslipLike = text.includes('betslip') || text.includes('bet slip') || text.includes('place bet') || text.includes('confirm bet') || text.includes('open bets');
                        return { el, rect, text, inputs, hasCancel, hasAction, betslipLike };
                    })
                    .filter((p) => p.rect.width > 160 && p.rect.height > 110)
                    .filter((p) => p.inputs.length > 0 || p.hasAction || p.betslipLike)
                    .sort((a, b) => {
                        const aScore = (a.hasCancel ? 4 : 0) + (a.hasAction ? 5 : 0) + (a.betslipLike ? 4 : 0) + (a.rect.x > window.innerWidth * 0.5 ? 2 : 0) + a.inputs.length;
                        const bScore = (b.hasCancel ? 4 : 0) + (b.hasAction ? 5 : 0) + (b.betslipLike ? 4 : 0) + (b.rect.x > window.innerWidth * 0.5 ? 2 : 0) + b.inputs.length;
                        return bScore - aScore || b.rect.y - a.rect.y;
                    });
                if (!panels.length) return { ok: false, reason: 'betslip controls panel not found' };
                const panel = panels[0];
                const inputs = panel.inputs;
                if (!inputs.length) {
                    return {
                        ok: false,
                        reason: 'betslip panel found but inputs not ready',
                        panelText: panel.text.slice(0, 260),
                    };
                }
                let stakeInput = inputs.find((el) => {
                    const hint = norm([
                        el.getAttribute('placeholder') || '',
                        el.getAttribute('name') || '',
                        el.getAttribute('id') || '',
                        el.getAttribute('aria-label') || '',
                        el.className && el.className.toString ? el.className.toString() : '',
                        el.parentElement ? (el.parentElement.innerText || el.parentElement.textContent || '') : '',
                    ].join(' '));
                    return hint.includes('stake');
                }) || null;
                let oddsInput = inputs.find((el) => {
                    if (el === stakeInput) return false;
                    const hint = norm([
                        el.getAttribute('placeholder') || '',
                        el.getAttribute('name') || '',
                        el.getAttribute('id') || '',
                        el.getAttribute('aria-label') || '',
                        el.className && el.className.toString ? el.className.toString() : '',
                        el.parentElement ? (el.parentElement.innerText || el.parentElement.textContent || '') : '',
                    ].join(' '));
                    return hint.includes('odds') || hint.includes('price');
                }) || null;
                if (!stakeInput || !oddsInput) {
                    const numeric = inputs.filter((el) => /^\d+(?:[\.,]\d+)?$/.test((val(el) || '').trim()));
                    if (!oddsInput) oddsInput = numeric[0] || inputs[0] || null;
                    if (!stakeInput) stakeInput = numeric.find((el) => el !== oddsInput) || inputs.find((el) => el !== oddsInput) || null;
                }
                if (!stakeInput || !oddsInput) {
                    return {
                        ok: false,
                        reason: 'stake/odds controls unresolved',
                        inputCount: inputs.length,
                        values: inputs.map((el) => val(el)).slice(0, 8),
                    };
                }
                return {
                    ok: true,
                    stakeInput,
                    oddsInput,
                    stakeBefore: val(stakeInput),
                    oddsBefore: val(oddsInput),
                };
                """
            )
            if not isinstance(controls, dict) or not controls.get("ok"):
                return {"ok": False, "error": f"controls not found: {controls!r}"}

            stake_input = controls.get("stakeInput")
            odds_input = controls.get("oddsInput")
            if not stake_input or not odds_input:
                return {"ok": False, "error": f"controls unresolved: {controls!r}"}

            def send_value(element, target_value: str, allow_leading_dot: bool = False) -> str:
                attempts = [target_value]
                if "." in target_value:
                    attempts.append(target_value.replace(".", ","))
                if allow_leading_dot and target_value.startswith("0."):
                    attempts.append(target_value[1:])
                    attempts.append(target_value.replace("0.", "0,"))
                seen: set[str] = set()
                for candidate in attempts:
                    if not candidate or candidate in seen:
                        continue
                    seen.add(candidate)
                    try:
                        element.click()
                    except Exception:
                        pass
                    try:
                        element.send_keys(Keys.CONTROL, "a")
                        element.send_keys(Keys.DELETE)
                    except Exception:
                        pass
                    try:
                        element.send_keys(candidate)
                    except Exception:
                        _set_input_value(driver, element, candidate)
                    try:
                        element.send_keys(Keys.TAB)
                    except Exception:
                        pass
                    time.sleep(0.15)
                    current = _control_value(driver, element)
                    if _same_numeric_text(current, target_value):
                        return current
                return _control_value(driver, element)

            odds_after = send_value(odds_input, target_odds, allow_leading_dot=False)
            stake_after = controls.get("stakeBefore") or ""
            if not _same_numeric_text(stake_after, stake_str):
                stake_after = send_value(stake_input, stake_str, allow_leading_dot=True)

            if not _same_numeric_text(odds_after, target_odds):
                return {
                    "ok": False,
                    "error": "fallback odds mismatch",
                    "oddsAfter": odds_after,
                    "targetOdds": target_odds,
                }
            if not _same_numeric_text(stake_after, stake_str):
                return {
                    "ok": False,
                    "error": "fallback stake mismatch",
                    "stakeAfter": stake_after,
                    "targetStake": stake_str,
                }
            return {
                "ok": True,
                "oddsValue": odds_after,
                "stakeValue": stake_after,
            }

        # Give the betslip panel time to render after clicking the Back cell.
        time.sleep(1.5)
        _dismiss_betfair_blocking_overlays(driver, profile_label)
        placed = None
        fill_deadline = time.monotonic() + 14
        while True:
            placed = driver.execute_script(
                r"""
                const stakeVal = arguments[0];
                const oddsVal = arguments[1];
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                function setInputValue(inp, value) {
                    inp.scrollIntoView({ block: 'center' });
                    inp.focus();
                    const tag = (inp.tagName || '').toLowerCase();
                    if (tag === 'input' || tag === 'textarea') {
                        const proto = tag === 'textarea' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (nativeInputValueSetter && nativeInputValueSetter.set) {
                            nativeInputValueSetter.set.call(inp, value);
                        } else {
                            inp.value = value;
                        }
                    } else {
                        inp.textContent = value;
                    }
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                    inp.dispatchEvent(new Event('blur', { bubbles: true }));
                }
                function clearControl(inp) {
                    inp.focus();
                    if (typeof inp.select === 'function') {
                        try { inp.select(); } catch (e) {}
                    }
                    if (typeof inp.setSelectionRange === 'function') {
                        try { inp.setSelectionRange(0, String(controlValue(inp) || '').length); } catch (e) {}
                    }
                    setInputValue(inp, '');
                }
                function controlValue(el) {
                    if (!el) return '';
                    if (typeof el.value === 'string') return el.value;
                    if (el.getAttribute) {
                        const ariaText = el.getAttribute('aria-valuetext') || el.getAttribute('aria-valuenow') || '';
                        if (ariaText) return ariaText;
                    }
                    return (el.textContent || el.innerText || '').trim();
                }
                function normalizeNumeric(value) {
                    return (value || '').toString().trim().replace(/\s+/g, '').replace(',', '.');
                }
                function sameNumericValue(actual, expected) {
                    const a = normalizeNumeric(actual);
                    const e = normalizeNumeric(expected);
                    if (a === e) return true;
                    const an = Number(a);
                    const en = Number(e);
                    return Number.isFinite(an) && Number.isFinite(en) && Math.abs(an - en) < 0.000001;
                }
                function setNumericControl(inp, value, options) {
                    const attempts = [];
                    const raw = (value || '').toString();
                    attempts.push(raw);
                    if (raw.includes('.')) attempts.push(raw.replace('.', ','));
                    if (options && options.allowLeadingZero && /^[0][\.,]/.test(raw)) attempts.push(raw.replace(/^0([\.,])/, '$1'));
                    const seen = new Set();
                    for (const candidate of attempts) {
                        if (!candidate || seen.has(candidate)) continue;
                        seen.add(candidate);
                        clearControl(inp);
                        setInputValue(inp, candidate);
                        const current = controlValue(inp);
                        if (sameNumericValue(current, raw)) {
                            return { ok: true, value: current, used: candidate };
                        }
                    }
                    return { ok: false, value: controlValue(inp), tried: Array.from(seen) };
                }
                // Find the stake input. Betfair betslip inputs may not carry 'stake' in
                // their attributes — try several strategies in order of confidence.
                let inp = null;
                let oddsInp = null;
                const selector = "input[type='text'], input[type='number'], input[type='tel'], input:not([type]), textarea, [contenteditable='true'], [role='textbox'], [role='spinbutton']";
                let allInputs = Array.from(document.querySelectorAll(selector)).filter(visible);
                const panelInputs = allInputs.filter((el) => {
                    const panel = el.closest('aside, section, div, form');
                    if (!panel || !visible(panel)) return false;
                    const rect = panel.getBoundingClientRect();
                    const pText = (panel.innerText || panel.textContent || '').toLowerCase();
                    const hasAction = /place\s*bet|confirm\s*bet|cancel|betslip|bet\s*slip|edit/.test(pText);
                    return rect.width > 150 && rect.height > 90 && (rect.x > window.innerWidth * 0.52 || hasAction);
                });
                if (panelInputs.length > 0) allInputs = panelInputs;
                inp = allInputs.find((el) => {
                    const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                    const nm = (el.getAttribute('name') || '').toLowerCase();
                    const id = (el.getAttribute('id') || '').toLowerCase();
                    const cls = (el.className && el.className.toString ? el.className.toString().toLowerCase() : '');
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    const parent = ((el.parentElement && (el.parentElement.innerText || el.parentElement.textContent)) || '').toLowerCase();
                    return ph.indexOf('stake') !== -1 || nm.indexOf('stake') !== -1
                        || id.indexOf('stake') !== -1 || cls.indexOf('stake') !== -1
                        || aria.indexOf('stake') !== -1 || parent.indexOf('stake') !== -1;
                }) || null;
                if (!inp) {
                    const containers = Array.from(document.querySelectorAll(
                        "[class*='betslip' i], [class*='bet-slip' i], [class*='betEntry' i],"
                        + "[class*='bet-entry' i], [aria-label*='betslip' i], [data-test*='betslip' i]"
                    )).filter(visible);
                    for (const c of containers) {
                        const ins = Array.from(c.querySelectorAll(selector)).filter(visible);
                        if (ins.length >= 2) { oddsInp = ins[0]; inp = ins[1]; break; }
                        if (ins.length === 1) { inp = ins[0]; break; }
                    }
                }
                if (!oddsInp) {
                    oddsInp = allInputs.find((el) => {
                        if (el === inp) return false;
                        const v = controlValue(el).trim().replace(',', '.');
                        return /^\d+(?:\.\d+)?$/.test(v) && v !== stakeVal;
                    }) || null;
                }
                if (!inp && allInputs.length > 0) {
                    inp = allInputs.find((el) => {
                        const v = controlValue(el).trim();
                        return v === '' || v === '0';
                    }) || allInputs[allInputs.length - 1];
                }
                if (!inp) return {
                    error: 'stake input not found',
                    inputCount: allInputs.length,
                    inputValues: allInputs.map((e) => controlValue(e)).slice(0, 5),
                    inputTypes: allInputs.map((e) => (e.getAttribute('type') || e.tagName || '').toLowerCase()).slice(0, 5),
                };
                if (!oddsInp) return {
                    error: 'odds input not found',
                    inputCount: allInputs.length,
                    inputValues: allInputs.map((e) => controlValue(e)).slice(0, 8),
                    inputTypes: allInputs.map((e) => (e.getAttribute('type') || e.tagName || '').toLowerCase()).slice(0, 8),
                };
                const oddsResult = setNumericControl(oddsInp, oddsVal, { allowLeadingZero: false });
                const stakeBefore = controlValue(inp);
                let stakeResult = { ok: true, value: stakeBefore, used: 'existing' };
                if (!sameNumericValue(stakeBefore, stakeVal)) {
                    stakeResult = setNumericControl(inp, stakeVal, { allowLeadingZero: true });
                }
                const normalized = (value) => (value || '').trim().replace(',', '.');
                const actualOdds = Number(normalized(controlValue(oddsInp)));
                const targetOdds = Number(normalized(oddsVal));
                if (!Number.isFinite(actualOdds) || actualOdds + 1e-9 < targetOdds) {
                    return {
                        error: 'odds input below target value',
                        oddsValue: controlValue(oddsInp),
                        targetOdds: oddsVal,
                        oddsAttempt: oddsResult,
                    };
                }
                if (!sameNumericValue(controlValue(inp), stakeVal)) {
                    return {
                        error: 'stake input did not keep target value',
                        stakeBefore,
                        stakeValue: controlValue(inp),
                        targetStake: stakeVal,
                        stakeAttempt: stakeResult,
                    };
                }
                return {
                    stakeSet: true,
                    oddsSet: true,
                    oddsValue: controlValue(oddsInp),
                    stakeValue: controlValue(inp),
                    stakeBefore,
                };
                """,
                stake_str,
                target_odds,
            )
            waiting_for_inputs = (
                isinstance(placed, dict)
                and placed.get("error") in {"stake input not found", "odds input not found"}
                and not int(placed.get("inputCount") or 0)
                and time.monotonic() < fill_deadline
            )
            if not waiting_for_inputs:
                break
            _activate_betfair_betslip()
            time.sleep(0.6)
        needs_fallback_fill = (
            not (isinstance(placed, dict) and placed.get("stakeSet") and placed.get("oddsSet"))
            and isinstance(placed, dict)
            and placed.get("error") in {
                "stake input did not keep target value",
                "stake input not found",
                "odds input not found",
            }
        )
        if needs_fallback_fill:
            fallback_fill = _fallback_fill_betfair_inputs_via_send_keys()
            if not fallback_fill.get("ok") and "controls not found" in str(fallback_fill.get("error", "")):
                activation = _activate_betfair_betslip()
                if activation.get("clicked"):
                    time.sleep(0.8)
                fallback_fill = _fallback_fill_betfair_inputs_via_send_keys()
            if fallback_fill.get("ok"):
                placed = {
                    "stakeSet": True,
                    "oddsSet": True,
                    "oddsValue": fallback_fill.get("oddsValue"),
                    "stakeValue": fallback_fill.get("stakeValue"),
                    "via": "selenium-send-keys-fallback",
                }
            else:
                placed = {
                    "error": "fallback fill failed",
                    "jsResult": placed,
                    "fallback": fallback_fill,
                }
        if not (isinstance(placed, dict) and placed.get("stakeSet") and placed.get("oddsSet")):
            print(f"[{profile_label}] Betfair: stake fill result: {placed!r}", flush=True)
            last_betfair_error = f"stake/odds fill failed: {placed!r}"
        else:
            accepted_odds_text = str(placed.get("oddsValue") or target_odds)
            # Wait for Betfair UI to update button label to "Confirm bet".
            time.sleep(1.2)
            confirmed = _click_betfair_action_button(confirm_only=False)
            if isinstance(confirmed, dict) and confirmed.get("ok"):
                first_label = confirmed.get("label", "")
                print(
                    f"[{profile_label}] Betfair: clicked '{first_label}' — "
                    f"{market_label} {selection.title()} {line_str} @ {accepted_odds_text} "
                    f"(target {target_odds}, available {odds_text}), stake {stake_str}"
                )
                # If the first button was "Place bet/bets", Betfair shows a
                # second "Confirm bet" button. Wait and click it.
                if "place" in first_label.lower():
                    time.sleep(1.2)
                    confirmed2 = _click_betfair_action_button(confirm_only=True)
                    if isinstance(confirmed2, dict) and confirmed2.get("ok"):
                        print(
                            f"[{profile_label}] Betfair: confirmed — "
                            f"'{confirmed2.get('label')}'"
                        )
                        bet_placed = True
                    else:
                        last_betfair_error = f"confirm bet button not found/clicked: {confirmed2!r}"
                        print(
                            f"[{profile_label}] Betfair: confirm-2 result: {confirmed2!r}",
                            flush=True,
                        )
                else:
                    bet_placed = True
            else:
                last_betfair_error = f"place/confirm button not found/clicked: {confirmed!r}"
                print(f"[{profile_label}] Betfair: confirm click result: {confirmed!r}", flush=True)
    else:
        last_betfair_error = last_betfair_error or "stake value is unavailable"

    if not bet_placed:
        raise RuntimeError(
            f"[{profile_label}] Betfair bet was not placed after selecting {market_label} "
            f"{selection.title()} {line_str}: {last_betfair_error or 'unknown error'}"
        )

    return {
        "betfair_selection": f"{market_label} {selection.title()} {line_str}",
        "betfair_market": market_label,
        "betfair_odds": accepted_odds_text,
        "betfair_target_odds": target_odds,
        "betfair_available_odds": odds_text,
        "betfair_stake": stake_str,
        "betfair_bet_placed": bet_placed,
    }


def _read_betfair_balance(driver: webdriver.Remote, profile_label: str) -> Decimal:
    """Read the main balance shown in the top bar after Betfair login."""
    text = driver.execute_script(
        r"""
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = window.getComputedStyle(el);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
        }
        // The balance area contains the text 'Main' near a currency amount.
        const nodes = Array.from(document.querySelectorAll("*")).filter(visible);
        for (const n of nodes) {
            const t = (n.innerText || '').trim();
            if (t.length > 0 && t.length < 200 && /\bmain\b/i.test(t)
                && /[€£$]\s*\d/.test(t)) {
                return t;
            }
        }
        const topNodes = nodes
            .map((n) => ({ text: (n.innerText || n.textContent || '').trim(), rect: n.getBoundingClientRect() }))
            .filter((item) => item.text && item.rect.top >= 0 && item.rect.top < 120)
            .filter((item) => item.text.length < 500)
            .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
        for (const item of topNodes) {
            const collapsed = item.text.replace(/\s+/g, ' ');
            const mainMatch = collapsed.match(/\bmain\b\s*[€£$]\s*\d[\d.,]*/i);
            if (mainMatch) return mainMatch[0];
        }
        for (const item of topNodes) {
            const collapsed = item.text.replace(/\s+/g, ' ');
            if (/bonus/i.test(collapsed) && !/\bmain\b/i.test(collapsed)) continue;
            const moneyMatch = collapsed.match(/[€£$]\s*\d[\d.,]*/);
            if (moneyMatch) return moneyMatch[0];
        }
        return null;
        """
    )
    if not text:
        raise RuntimeError(f"[{profile_label}] Could not read Betfair balance from top bar.")
    m = re.search(r"[€£$]\s*([\d.,]+)", text)
    if not m:
        raise RuntimeError(f"[{profile_label}] Betfair balance text has no amount: {text!r}")
    return _money_to_decimal(m.group(1))


def _open_betfair_settings_panel(driver: webdriver.Remote, profile_label: str) -> None:
    opened = driver.execute_script(
        r"""
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = window.getComputedStyle(el);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
        }
        // Find a clickable element labeled 'Settings'.
        const candidates = Array.from(document.querySelectorAll(
            "a, button, [role='button'], div, span"
        )).filter(visible).filter((el) => {
            const t = (el.innerText || '').trim().toLowerCase();
            return (t === 'settings' || t.startsWith('settings'))
                && t.length < 30;
        });
        if (candidates.length === 0) return false;
        // Prefer one near the top-right.
        candidates.sort((a, b) => {
            const ra = a.getBoundingClientRect();
            const rb = b.getBoundingClientRect();
            return (ra.top + (window.innerWidth - ra.right)) - (rb.top + (window.innerWidth - rb.right));
        });
        candidates[0].scrollIntoView({ block: 'center' });
        candidates[0].click();
        return true;
        """
    )
    if not opened:
        raise RuntimeError(f"[{profile_label}] Could not click Betfair Settings link.")
    time.sleep(0.6)


def _set_betfair_default_stake(driver: webdriver.Remote, stake: str, profile_label: str) -> None:
    _open_betfair_settings_panel(driver, profile_label)

    # Click the "Betting" tab.
    clicked_betting = bool(driver.execute_script(
        r"""
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = window.getComputedStyle(el);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
        }
        const panelText = Array.from(document.querySelectorAll('div,section,aside,form'))
            .filter(visible)
            .map((el) => (el.innerText || '').trim().toLowerCase())
            .find((text) => text.includes('default stake') && (text.includes('edit') || text.includes('set')));
        return !!panelText;
        """
    ))
    end = time.time() + 8
    while time.time() < end and not clicked_betting:
        clicked_betting = bool(driver.execute_script(
            r"""
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            const cands = Array.from(document.querySelectorAll(
                "a, button, [role='button'], [role='tab'], li, div, span"
            )).filter(visible).filter((el) => {
                const t = (el.innerText || '').trim().toLowerCase();
                return (t === 'betting' || t.startsWith('betting') || t.includes('betting settings'))
                    && t.length < 40;
            });
            if (cands.length === 0) return false;
            const pick = cands[0].closest("a, button, [role='button'], [role='tab']") || cands[0];
            pick.scrollIntoView({ block: 'center' });
            pick.click();
            return true;
            """
        ))
        if not clicked_betting:
            time.sleep(0.4)
    if not clicked_betting:
        raise RuntimeError(f"[{profile_label}] Could not click Betting tab in Settings.")

    # Ensure 'Default stake' checkbox is checked (some users have it off).
    try:
        driver.execute_script(
            r"""
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            const labels = Array.from(document.querySelectorAll("label, div, span"))
                .filter(visible).filter((el) => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t === 'default stake';
                });
            for (const lab of labels) {
                let scope = lab.closest('label') || lab.parentElement;
                if (!scope) continue;
                const cb = scope.querySelector("input[type='checkbox']");
                if (cb && !cb.checked) cb.click();
            }
            """
        )
    except Exception:
        pass
    time.sleep(0.3)

    # Click 'Edit' button inside the Betting panel.
    clicked_edit = bool(driver.execute_script(
        r"""
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = window.getComputedStyle(el);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
        }
        const cands = Array.from(document.querySelectorAll(
            "a, button, [role='button'], span, div"
        )).filter(visible).filter((el) => {
            const t = (el.innerText || '').trim().toLowerCase();
            return t === 'edit';
        });
        if (cands.length === 0) return false;
        // Prefer one whose nearest container also mentions 'Default stake'.
        let pick = cands.find((el) => {
            let p = el;
            for (let i = 0; i < 6 && p; i++) {
                p = p.parentElement;
                if (!p) break;
                if (((p.innerText || '').toLowerCase()).indexOf('default stake') !== -1) return true;
            }
            return false;
        });
        if (!pick) pick = cands[0];
        pick.scrollIntoView({ block: 'center' });
        pick.click();
        return true;
        """
    ))
    if not clicked_edit:
        raise RuntimeError(f"[{profile_label}] Could not click Edit for Default stake.")
    time.sleep(0.4)

    # Fill the 3 stake inputs and click 'Set'.
    result = driver.execute_script(
        r"""
        const stake = arguments[0];
        function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const cs = window.getComputedStyle(el);
            return cs.visibility !== 'hidden' && cs.display !== 'none';
        }
        // Find the 'Default stake' label, walk up to a container holding inputs.
        const labels = Array.from(document.querySelectorAll("label, div, span"))
            .filter(visible).filter((el) => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'default stake';
            });
        let container = null;
        for (const lab of labels) {
            let p = lab;
            for (let i = 0; i < 8 && p; i++) {
                p = p.parentElement;
                if (!p) break;
                const inputs = Array.from(p.querySelectorAll("input"))
                    .filter(visible)
                    .filter((inp) => {
                        const t = (inp.type || '').toLowerCase();
                        return t === '' || t === 'text' || t === 'number' || t === 'tel';
                    });
                if (inputs.length >= 3) { container = p; break; }
            }
            if (container) break;
        }
        if (!container) {
            // Fall back: any container that has 3+ numeric inputs near a 'Set' button.
            const setBtns = Array.from(document.querySelectorAll("a, button, [role='button']"))
                .filter(visible).filter((el) => (el.innerText || '').trim().toLowerCase() === 'set');
            for (const b of setBtns) {
                let p = b;
                for (let i = 0; i < 8 && p; i++) {
                    p = p.parentElement;
                    if (!p) break;
                    const inputs = Array.from(p.querySelectorAll("input")).filter(visible)
                        .filter((inp) => {
                            const t = (inp.type || '').toLowerCase();
                            return t === '' || t === 'text' || t === 'number' || t === 'tel';
                        });
                    if (inputs.length >= 3) { container = p; break; }
                }
                if (container) break;
            }
        }
        if (!container) return { error: 'stake inputs container not found' };
        const inputs = Array.from(container.querySelectorAll("input")).filter(visible)
            .filter((inp) => {
                const t = (inp.type || '').toLowerCase();
                return t === '' || t === 'text' || t === 'number' || t === 'tel';
            }).slice(0, 3);
        const nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        for (const inp of inputs) {
            try { inp.focus(); } catch (e) {}
            try { inp.select(); } catch (e) {}
            nativeSetter.call(inp, '');
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            nativeSetter.call(inp, stake);
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            inp.dispatchEvent(new Event('blur', { bubbles: true }));
        }
        // Click 'Set' inside the same container if present, else globally.
        let setBtn = Array.from(container.querySelectorAll("a, button, [role='button']"))
            .filter(visible).find((el) => (el.innerText || '').trim().toLowerCase() === 'set');
        if (!setBtn) {
            setBtn = Array.from(document.querySelectorAll("a, button, [role='button']"))
                .filter(visible).find((el) => (el.innerText || '').trim().toLowerCase() === 'set');
        }
        if (setBtn) {
            setBtn.scrollIntoView({ block: 'center' });
            setBtn.click();
        }
        return { ok: true, filled: inputs.length, set: !!setBtn };
        """,
        stake,
    )
    if not result or not result.get("ok"):
        raise RuntimeError(f"[{profile_label}] Could not set Betfair default stake: {result!r}")
    print(
        f"[{profile_label}] Betfair Default Stake set to EUR {stake} "
        f"(inputs filled: {result.get('filled')}, set clicked: {result.get('set')})."
    )
    time.sleep(0.5)
    # Try to dismiss the Settings panel by sending Escape (best-effort).
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass


def update_betfair_default_stake(session: dict) -> dict:
    """Read Betfair balance, compute stake and write it as default stake (×3 inputs).

    Returns {balance, stake, percent} like update_black_default_stake.
    """
    profile_label = session.get("profile_label", "Profile-2")
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        # Make sure we're on a Betfair page where the balance and Settings live.
        if BETFAIR_URL_PART not in (driver.current_url or "").lower():
            driver.get(BETFAIR_LOGIN_URL)
            try:
                WebDriverWait(driver, 20).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
        balance = _read_betfair_balance(driver, profile_label)
        print(f"[{profile_label}] Betfair balance: EUR {balance}")
        stake_amount = _calculate_stake_from_balance(balance)
        stake = _format_stake_amount(stake_amount)
        _set_betfair_default_stake(driver, stake, profile_label)
        return {"balance": str(balance), "stake": stake, "percent": str(STAKE_PERCENT)}
    finally:
        close_driver_bridge(driver)


def ensure_betfair_session_authorized(session: dict) -> dict:
    profile_label = session.get("profile_label", "Profile-2")
    driver = None
    try:
        driver = connect_to_browser(session["browser_info"], profile_label)
        if BETFAIR_URL_PART not in (driver.current_url or "").lower():
            driver.get(BETFAIR_LOGIN_URL)
            _wait_document_ready(driver)
            time.sleep(1.5)

        if _is_betfair_logged_in(driver):
            try:
                balance = _read_betfair_balance(driver, profile_label)
                return {"status": "alive", "detail": f"{driver.current_url} | balance EUR {balance}"}
            except Exception:
                return {"status": "alive", "detail": driver.current_url}

        print(f"[{profile_label}] Betfair session is not authorized during health check; re-logging.")
        login_betfair(driver, profile_label)
        if not _is_betfair_logged_in(driver):
            raise RuntimeError("Betfair session could not be restored during health check.")
        try:
            balance = _read_betfair_balance(driver, profile_label)
            return {"status": "relogged", "detail": f"{driver.current_url} | balance EUR {balance}"}
        except Exception:
            return {"status": "relogged", "detail": driver.current_url}
    finally:
        close_driver_bridge(driver)


def run_profile(profile_id: str, profile_label: str, login_enabled: bool = True) -> dict:
    """Full lifecycle: start profile, log into BetInAsia/Black, keep open."""
    driver = None
    was_already_running = False
    try:
        print(f"[{profile_label}] Preparing AdsPower profile: {profile_id}")
        browser_info, was_already_running = start_adspower_profile(profile_id)

        if not login_enabled:
            # Profile-2 is dedicated to Betfair, not BetInAsia/Black.
            print(
                f"[{profile_label}] Skipping BetInAsia/Black login; this profile is "
                f"reserved for Betfair."
            )
            time.sleep(4)
            betfair_driver = None
            try:
                betfair_driver = connect_to_browser(browser_info, profile_label)
                login_betfair(betfair_driver, profile_label)
            except Exception as bf_exc:
                print(
                    f"[{profile_label}] Betfair login failed (continuing anyway): {bf_exc}",
                    file=sys.stderr,
                )
            finally:
                close_driver_bridge(betfair_driver)
            betfair_stake_result = None
            try:
                betfair_stake_result = update_betfair_default_stake({
                    "browser_info": browser_info,
                    "profile_label": profile_label,
                })
                print(
                    f"[{profile_label}] Betfair default stake refreshed on startup: "
                    f"balance EUR {betfair_stake_result['balance']}, "
                    f"stake EUR {betfair_stake_result['stake']} "
                    f"({betfair_stake_result['percent']}%)."
                )
            except Exception as st_exc:
                print(
                    f"[{profile_label}] Startup Betfair default stake refresh failed, "
                    f"continuing: {st_exc}",
                    file=sys.stderr,
                )
            return {
                "profile_id": profile_id,
                "profile_label": profile_label,
                "browser_info": browser_info,
                "was_already_running": was_already_running,
                "login_enabled": False,
                "betfair": True,
                "stake": betfair_stake_result,
            }

        # Wait for the browser to fully initialize before connecting
        time.sleep(4)

        driver = connect_to_browser(browser_info, profile_label)
        if not _ensure_black_session_ready(driver, profile_label):
            open_login_tab(driver, profile_label)
            close_driver_bridge(driver)
            driver = None

            time.sleep(2)
            print(f"[{profile_label}] Reconnecting after opening tab...")
            driver = connect_to_browser(browser_info, profile_label)
            login(driver, profile_label)
        stake_result = None
        try:
            stake_result = update_black_default_stake(driver, profile_label)
            print(
                f"[{profile_label}] Default stake refreshed on startup: "
                f"balance EUR {stake_result['balance']}, stake EUR {stake_result['stake']} ({stake_result['percent']}%)."
            )
        except Exception as exc:
            print(f"[{profile_label}] Startup default stake refresh failed, continuing listener: {exc}")
        close_driver_bridge(driver)

        print(f"[{profile_label}] Done. Browser will remain open. Press Ctrl+C to exit.")
        return {
            "profile_id": profile_id,
            "profile_label": profile_label,
            "browser_info": browser_info,
            "was_already_running": was_already_running,
            "login_enabled": True,
            "stake": stake_result,
        }

    except Exception as exc:
        print(f"[{profile_label}] ERROR: {exc}", file=sys.stderr)
        close_driver_bridge(driver)
        raise


def validate_required_env() -> None:
    """Validate environment required for BetInAsia and Black login."""
    missing = []
    if not BETINASIA_EMAIL:
        missing.append("BETINASIA_EMAIL")
    if not BETINASIA_PASSWORD:
        missing.append("BETINASIA_PASSWORD")
    if not BLACK_USERNAME:
        missing.append("BLACK_USERNAME")
    if not BLACK_PASSWORD:
        missing.append("BLACK_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def run_all_profiles(expected_profiles: int = 2, wait_for_enter: bool = False) -> list[dict]:
    """Start or reuse AdsPower profiles, then set the first profile's Black default stake."""
    validate_required_env()

    profile_ids = fetch_profile_ids(expected=expected_profiles)

    sessions = []
    for idx, profile_id in enumerate(profile_ids, start=1):
        label = f"Profile-{idx}"
        sessions.append(run_profile(profile_id, label, login_enabled=(idx == 1)))

    primary_session = next((session for session in sessions if session.get("login_enabled")), None)
    betfair_session = next((session for session in sessions if session.get("betfair")), None)
    primary_stake = primary_session.get("stake") if primary_session else None
    if betfair_session and not betfair_session.get("stake") and primary_stake:
        betfair_session["stake"] = dict(primary_stake)
        betfair_session["stake"]["source"] = "Profile-1 fallback"
        print(
            f"[{betfair_session['profile_label']}] Using Profile-1 cached stake "
            f"EUR {betfair_session['stake']['stake']} because Betfair balance/default stake refresh failed."
        )

    print(
        "\nAdsPower profiles are ready. Profile-1 -> BetInAsia/Black, "
        "Profile-2 -> Betfair."
    )

    if wait_for_enter:
        print("Press Enter to exit (browsers will stay running)...")
        input()

    return sessions


def main() -> None:
    try:
        run_all_profiles(expected_profiles=2, wait_for_enter=True)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
