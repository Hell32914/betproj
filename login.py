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
    "club", "football", "team",
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


def _set_input_value(driver: webdriver.Remote, element, value: str) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    try:
        driver.execute_script(
            """
            const input = arguments[0];
            input.focus({ preventScroll: true });
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
            const input = arguments[0];
            const value = arguments[1];
            input.focus({ preventScroll: true });
            const setter = Object.getOwnPropertyDescriptor(input.__proto__, 'value')?.set;
            if (setter) setter.call(input, value);
            else input.value = value;
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: value }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            element,
            value,
        )

    current_value = element.get_attribute("value") or ""
    if current_value == value:
        return

    driver.execute_script(
        """
        const element = arguments[0];
        const value = arguments[1];
        const setter = Object.getOwnPropertyDescriptor(element.__proto__, 'value')?.set;
        if (setter) {
            setter.call(element, value);
        } else {
            element.value = value;
        }
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
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


def _normalize_team_text(value: str | None) -> str:
    text = (value or "").lower().strip()
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

    normalized = _normalize_team_text(team_name)
    add(normalized)
    add(team_name)

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
    normalized_parts = normalized.split()
    stripped_parts = [p for p in normalized_parts if p not in reserve_markers]
    if stripped_parts and stripped_parts != normalized_parts:
        add(" ".join(stripped_parts))
        if len(stripped_parts) >= 2:
            add(" ".join(stripped_parts[:2]))

    if len(normalized_parts) >= 2:
        add(" ".join(normalized_parts[:2]))
    return queries


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
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.rect.width > 180 && item.rect.height > 160)
            .filter((item) => item.text.includes('betslip'))
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height))[0];
        if (!panel) return { ok: false, reason: 'betslip panel not found', text: document.body?.innerText || '' };

        const inputs = Array.from(panel.element.querySelectorAll('input'))
            .filter(isVisible)
            .filter((element) => !element.disabled && !element.readOnly)
            .map((element) => ({
                value: element.value || element.getAttribute('value') || '',
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
        _set_input_value(driver, element, value)
    else:
        current_value = (element.get_attribute("value") or "").strip()
        if current_value != value:
            _set_input_value(driver, element, value)

    try:
        element.send_keys(Keys.TAB)
    except Exception:
        driver.execute_script("arguments[0].blur();", element)

    time.sleep(0.25)


def _open_black_search(driver: webdriver.Remote, profile_label: str) -> None:
    def search_dialog_open(browser: webdriver.Remote) -> bool:
        text = _visible_text_lower(browser)
        return "live events" in text or "all sports" in text

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
                    return listSibling.querySelector('svg,path') || listSibling;
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
                    return first.querySelector('svg,path') || first;
                }

                const directSibling = item.element.parentElement?.nextElementSibling;
                if (directSibling && isVisible(directSibling)) {
                    return directSibling.querySelector('svg,path') || directSibling;
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
            const text = (document.body?.innerText || '').toLowerCase();
            return text.includes('live events') || text.includes('all sports');
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
    search_input = WebDriverWait(driver, 10).until(lambda browser: browser.execute_script(
        """
        const dialogOpen = () => {
            const text = (document.body?.innerText || '').toLowerCase();
            return text.includes('live events') || text.includes('all sports');
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
    ))
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
    search_input.send_keys(Keys.ENTER)

    WebDriverWait(driver, 15).until(
        lambda browser: normalized_query.lower() in _visible_text_lower(browser)
        or "no results found" in _visible_text_lower(browser)
    )
    # Give the React result list a brief moment to render fully before consumers
    # start scanning the DOM for the match-card row.
    time.sleep(0.8)
    print(f"[{profile_label}] Searched Black live events for first team: {normalized_query}")


def _search_black_live_events(driver: webdriver.Remote, team_name: str, profile_label: str) -> str:
    last_error = None
    for query in _team_search_queries(team_name):
        try:
            _fill_black_search(driver, query, profile_label)
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
    if normalized in {'failed', 'rejected', 'declined'}:
        return 'rejected'
    if normalized in {'cancelled', 'canceled', 'void'}:
        return 'cancelled'
    if normalized in {'open', 'pending', 'processing'}:
        return 'pending'
    return 'unknown'


class BlackSelectionMissingError(RuntimeError):
    """Raised when the requested Asian Total Goals selection never shows up on the match page after retries."""


def _snapshot_black_max_order_id(driver: webdriver.Remote, profile_label: str) -> int | None:
    """Open the Black top Orders view and return the largest order id currently shown.

    Used as a watermark: the row of a freshly placed bet must have an order id strictly
    greater than this snapshot, so we never mistake a leftover top row (same team and
    even same stake) for the new bet while the new row is still rendering.
    """
    if not _open_black_top_orders(driver, profile_label):
        return None
    time.sleep(1.5)
    try:
        result = driver.execute_script(
            """
            const isVisible = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const text = Array.from(document.querySelectorAll('table,div,section,main'))
                .filter(isVisible)
                .map((element) => (element.innerText || element.textContent || ''))
                .filter((value) => /selection/i.test(value) && /status/i.test(value) && /stake/i.test(value))
                .sort((a, b) => b.length - a.length)[0] || '';
            const matches = text.match(/\\b\\d{8,14}\\b/g) || [];
            if (!matches.length) return null;
            let max = 0;
            for (const value of matches) {
                const num = parseInt(value, 10);
                if (Number.isFinite(num) && num > max) max = num;
            }
            return max || null;
            """
        )
    except Exception as exc:
        print(f"[{profile_label}] Could not snapshot Black max order id: {exc}", flush=True)
        return None
    if isinstance(result, (int, float)) and result:
        snapshot = int(result)
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
        try:
            result = driver.execute_script(
                """
                const isVisible = (element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                };
                const text = Array.from(document.querySelectorAll('table,div,section,main'))
                    .filter(isVisible)
                    .map((element) => (element.innerText || element.textContent || ''))
                    .filter((value) => /selection/i.test(value) && /status/i.test(value) && /stake/i.test(value))
                    .sort((a, b) => b.length - a.length)[0] || '';
                const matches = text.match(/\\b\\d{8,14}\\b/g) || [];
                let max = 0;
                for (const value of matches) {
                    const num = parseInt(value, 10);
                    if (Number.isFinite(num) && num > max) max = num;
                }
                return max || null;
                """
            )
        except Exception as exc:
            print(f"[{profile_label}] Could not read Black orders max id: {exc}", flush=True)
            result = None
        if isinstance(result, (int, float)) and result:
            current_max = int(result)
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
            const table = tables[0];
            if (!table) return null;
            const statusRegex = /\\b(Open|Failed|Reconciled|Cancelled|Canceled|Rejected|Pending|Accepted|Matched|Confirmed|Done)\\b/i;
            const candidates = Array.from(table.element.querySelectorAll('tr,[role="row"],div,section,article'))
                .filter(isVisible)
                .map((element) => ({ element, text: normalize(textOf(element)) }))
                .filter((item) => item.text.includes(targetId));
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
    text = _visible_text_lower(driver)
    return "live events" in text or "all sports" in text


def _black_match_context_matches(
    driver: webdriver.Remote,
    home_team: str,
    away_team: str | None,
) -> bool:
    return bool(driver.execute_script(
        """
        const homeTeam = arguments[0].trim().toLowerCase();
        const awayTeam = (arguments[1] || '').trim().toLowerCase();
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
            if (!section.text.includes(homeTeam)) continue;
            if (awayTeam && !section.text.includes(awayTeam)) continue;
            return true;
        }
        return false;
        """,
        home_team,
        away_team or "",
    ))


def _ensure_black_betslip_safe_to_use(driver: webdriver.Remote, profile_label: str) -> None:
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

    candidate = WebDriverWait(driver, 15).until(lambda browser: browser.execute_script(
        """
        const teamVariants = arguments[0];
        const opponentVariants = arguments[1];
        const normalize = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
        const teamWords = Array.from(new Set(teamVariants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))));
        const opponentWords = Array.from(new Set(opponentVariants.flatMap((value) => normalize(value).split(' ').filter((word) => word.length >= 3))));
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
        const scoreFor = (text, rect) => {
            const normalizedText = normalize(text);
            let score = 0;
            if (teamVariants.some((value) => normalize(value) && normalizedText.includes(normalize(value)))) score += 120;
            if (opponentVariants.some((value) => normalize(value) && normalizedText.includes(normalize(value)))) score += 150;
            const matchedWords = teamWords.filter((word) => normalizedText.includes(word)).length;
            const matchedOpponentWords = opponentWords.filter((word) => normalizedText.includes(word)).length;
            score += matchedWords * 20;
            score += matchedOpponentWords * 25;
            if (normalizedText.includes('live') || normalizedText.includes('in play')) score += 35;
            if (/\\b\\d+\\s*[:-]\\s*\\d+\\b/.test(normalizedText)) score += 20;
            if (rect.x < window.innerWidth * 0.16) score -= 140;
            if (rect.x > window.innerWidth * 0.82) score -= 170;
            if (normalizedText.includes('order id') || normalizedText.includes('placed at') || normalizedText.includes('profit loss') || normalizedText.includes('selection status price stake')) score -= 260;
            if (normalizedText.includes('all sports') || normalizedText.includes('football tennis') || normalizedText.includes('specials')) score -= 220;
            if (normalizedText.includes('results in football') || normalizedText.includes('results in')) score -= 200;
            if (normalizedText.includes('result')) score -= 30;
            if (normalizedText.includes('over') || normalizedText.includes('under')) score -= 30;
            // Penalise rows that obviously enumerate several distinct matches (multiple kickoff
            // times / opponents) so a wrapping container never beats the single match card.
            const kickoffMatches = normalizedText.match(/\\b[0-2]?\\d\\s+[0-5]\\d\\b/g) || [];
            if (kickoffMatches.length >= 2) score -= 120;
            const vsCount = (normalizedText.match(/\\bvs\\b/g) || []).length;
            if (vsCount >= 2) score -= 180;
            return score;
        };

        const rowMatchesTeams = (text) => {
            const t = normalize(text);
            const fuzzyHas = (word) => {
                if (!word) return false;
                if (t.includes(word)) return true;
                // Allow a >=4-char shared prefix with any token (typo tolerance).
                const toks = t.split(' ');
                for (const tok of toks) {
                    const k = Math.min(tok.length, word.length, 4);
                    if (k >= 4 && tok.slice(0, k) === word.slice(0, k)) return true;
                }
                return false;
            };
            const teamHits = teamWords.filter(fuzzyHas).length;
            const hasTeam = !teamWords.length
                || teamHits >= Math.max(1, teamWords.length - 1)
                || teamVariants.some((value) => normalize(value) && t.includes(normalize(value)));
            const hasOpponent = !opponentWords.length
                || opponentWords.some(fuzzyHas)
                || opponentVariants.some((value) => normalize(value) && t.includes(normalize(value)));
            return hasTeam && hasOpponent;
        };

        const roots = Array.from(document.querySelectorAll('li,button,a,[role="button"],article,div'))
            .filter(isVisible)
            .map((element) => {
                const row = element.closest('li,button,a,[role="button"]') || element;
                const rect = row.getBoundingClientRect();
                const text = textOf(row);
                return { row, rect, text, score: scoreFor(text, rect) };
            })
            .filter((item) => item.rect.y > 90)
            .filter((item) => item.rect.width > 150 && item.rect.height >= 24)
            // Single match-card rows are typically well under ~220px tall; anything taller is
            // almost certainly a wrapper enumerating multiple matches.
            .filter((item) => item.rect.height <= 260)
            .filter((item) => item.rect.x > window.innerWidth * 0.14)
            .filter((item) => item.rect.x < window.innerWidth * 0.82)
            .filter((item) => rowMatchesTeams(item.text))
            .filter((item) => item.score >= 80)
            // Highest score wins; on ties prefer the SMALLER element (single match card over
            // any wrapping container that happens to also contain both team names).
            .sort((a, b) => b.score - a.score || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));

        const uniqueRows = [];
        for (const item of roots) {
            if (!uniqueRows.some((current) => current.row === item.row)) {
                uniqueRows.push(item);
            }
        }

        return uniqueRows[0]?.row || null;
        """,
        team_variants,
        opponent_variants,
    ))

    click_attempts = [
        ("native", lambda: candidate.click()),
        ("actions", lambda: ActionChains(driver).move_to_element(candidate).pause(0.1).click(candidate).perform()),
        (
            "js-center",
            lambda: driver.execute_script(
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
        ("enter", lambda: candidate.send_keys(Keys.ENTER)),
    ]

    for attempt_name, attempt in click_attempts:
        try:
            attempt()
            if WebDriverWait(driver, 5).until(lambda browser: match_opened(browser)):
                print(f"[{profile_label}] Opened Black live match for: {team_name} via {attempt_name}.")
                return
        except Exception:
            continue

    details = driver.execute_script(
        """
        const teamVariants = arguments[0];
        const opponentVariants = arguments[1];
        const normalize = (value) => (value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
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
            .filter((item) => teamVariants.some((value) => normalize(value) && normalize(item.text).includes(normalize(value))))
            .filter((item) => !opponentVariants.length || opponentVariants.some((value) => normalize(value) && normalize(item.text).includes(normalize(value))))
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
    verified = WebDriverWait(driver, 8).until(lambda browser: browser.execute_script(
        """
        const selection = arguments[0].trim().toLowerCase();
        const lineVariants = arguments[1].map((value) => value.toLowerCase());
        const homeTeam = (arguments[2] || '').trim().toLowerCase();
        const awayTeam = (arguments[3] || '').trim().toLowerCase();
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
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.text.includes('betslip'))
            .sort((a, b) => (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));

        for (const panel of panels) {
            const text = panel.text;
            const hasLine = hasExactLineToken(text);
            const hasSelection = text.includes(selection);
            const hasHome = !homeTeam || text.includes(homeTeam);
            const hasAway = !awayTeam || text.includes(awayTeam);
            if (hasLine && hasSelection && hasHome && hasAway) {
                return { ok: true, text: text.slice(0, 300) };
            }
        }
        return null;
        """,
        selection_lower,
        line_variants,
        home_team or "",
        away_team or "",
    ))
    if not verified:
        page_text = _visible_page_text(driver)
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
        raise RuntimeError(
            f"[{profile_label}] Black betslip verification failed for {selection} {line}. Page: {short_text}"
        )


def _select_black_asian_total_goals(driver: webdriver.Remote, selection: str, line: Decimal, profile_label: str, prefer_left: bool = False) -> None:
    line_variants = _decimal_variants(line)
    result = driver.execute_script(
        """
        const selection = arguments[0].trim().toLowerCase();
        const lineVariants = arguments[1].map((value) => value.toLowerCase());
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
                .map((element) => ({ element, text: normalizedTextOf(element), rect: element.getBoundingClientRect() }))
                .filter((item) => item.text === 'asian total goals')
                .sort((a, b) => a.rect.y - b.rect.y);
            const containers = [];
            for (const header of headers) {
                let current = header.element;
                for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
                    if (!current || !isVisible(current)) continue;
                    const rect = current.getBoundingClientRect();
                    if (rect.width <= 350 || rect.height <= 100) continue;
                    const rows = descendants(current, 'div,section,li,button,[role="button"]')
                        .map(buildRowCandidate)
                        .filter(Boolean)
                        .filter((item) => item.rect.y >= header.rect.bottom - 8);
                    if (!rows.length) continue;
                    containers.push({
                        element: current,
                        rect,
                        headerText: header.text,
                        rows,
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
            .filter((text) => text === 'asian total goals')
            .slice(0, 5);
        return {
            ok: false,
            reason: 'asian-total-goals-row-not-found',
            selection,
            lineVariants,
            sections: sectionTexts,
        };
        """,
        selection,
        line_variants,
        prefer_left,
    )
    if not result or not result.get("ok"):
        raise RuntimeError(f"Could not select Asian Total Goals {selection} {line}. Details: {result!r}")
    WebDriverWait(driver, 10).until(lambda browser: "betslip" in _visible_text_lower(browser) and "price" in _visible_text_lower(browser))
    print(f"[{profile_label}] Selected Asian Total Goals {selection} {line}.")


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
        const panel = Array.from(document.querySelectorAll('aside,section,div'))
            .filter(isVisible)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element) }))
            .filter((item) => item.rect.x > window.innerWidth * 0.68)
            .filter((item) => item.rect.width > 180 && item.rect.height > 160)
            .filter((item) => item.text.includes('betslip'))
            .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
        if (!panel) {
            return { ok: false, reason: 'betslip panel not found', text: document.body?.innerText || '' };
        }

        const panelRect = panel.rect;
        const inputs = Array.from(panel.element.querySelectorAll('input'))
            .filter(isVisible)
            .filter((element) => !element.disabled && !element.readOnly)
            .map((element) => ({ element, rect: element.getBoundingClientRect(), text: textOf(element.parentElement || element) }))
            .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);

        const findInputByLabel = (labelText, fallbackIndex) => {
            const labels = Array.from(panel.element.querySelectorAll('label,div,span,p'))
                .filter(isVisible)
                .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
                .filter((item) => item.text === labelText)
                .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
            for (const label of labels) {
                const candidate = inputs
                    .filter((item) => item.rect.y >= label.rect.y - 18 && item.rect.y <= label.rect.bottom + 45)
                    .filter((item) => item.rect.x >= label.rect.x - 40)
                    .sort((a, b) => Math.abs(a.rect.y - label.rect.y) - Math.abs(b.rect.y - label.rect.y) || Math.abs(a.rect.x - label.rect.x) - Math.abs(b.rect.x - label.rect.x))[0];
                if (candidate) return candidate.element;
            }
            const rowInputs = inputs.filter((item) => item.rect.y > panelRect.top + 40 && item.rect.y < panelRect.top + 140);
            if (rowInputs[fallbackIndex]) return rowInputs[fallbackIndex].element;
            return inputs[fallbackIndex]?.element || null;
        };

        const stakeInput = findInputByLabel('stake', 0);
        const priceInput = findInputByLabel('price', 1);
        const placeButton = Array.from(panel.element.querySelectorAll('button,[role="button"]'))
            .filter(isVisible)
            .map((element) => ({ element, text: textOf(element), rect: element.getBoundingClientRect() }))
            .filter((item) => item.text === 'place' || item.text.includes('place'))
            .sort((a, b) => b.rect.y - a.rect.y || b.rect.x - a.rect.x)[0]?.element;
        return {
            ok: true,
            panel: panel.element,
            stakeInput,
            priceInput,
            placeButton,
            panelText: panel.element.innerText || document.body?.innerText || '',
        };
    """
    def normalized_decimal_text(raw_value: str) -> str:
        return format(_money_to_decimal(raw_value).normalize(), "f")

    def locate_controls() -> dict:
        controls = driver.execute_script(locate_script)
        if not controls or not controls.get("ok"):
            page_text = (controls or {}).get("text", "")
            short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
            raise RuntimeError(f"Could not prepare Black betslip. Reason: {(controls or {}).get('reason')}. Page: {short_text}")
        return controls

    result = locate_controls()
    if not result or not result.get("ok"):
        page_text = (result or {}).get("text", "")
        short_text = " | ".join(line.strip() for line in page_text.splitlines() if line.strip())[:700]
        raise RuntimeError(f"Could not prepare Black betslip. Reason: {(result or {}).get('reason')}. Page: {short_text}")

    price_input = result.get("priceInput")
    if not price_input:
        short_text = " | ".join(line.strip() for line in (result.get("panelText", "") or "").splitlines() if line.strip())[:700]
        raise RuntimeError(f"Could not prepare Black betslip. Reason: price input not found. Page: {short_text}")

    stake_input = result.get("stakeInput")
    place_button = result.get("placeButton")
    if not place_button:
        short_text = " | ".join(line.strip() for line in (result.get("panelText", "") or "").splitlines() if line.strip())[:700]
        raise RuntimeError(f"Could not click Black Place. Reason: place button not found. Page: {short_text}")

    if stake_input and stake_text:
        _fill_betslip_input(driver, stake_input, stake_text)

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
            current_price = normalized_decimal_text(current_price_input.get_attribute("value") or "")
        except Exception:
            return False
        if current_price != normalized_price:
            return False
        if normalized_stake is not None:
            current_stake_input = state.get("stakeInput")
            if not current_stake_input:
                return False
            try:
                current_stake = normalized_decimal_text(current_stake_input.get_attribute("value") or "")
            except Exception:
                return False
            if current_stake != normalized_stake:
                return False
        if not current_place_button.is_enabled() or current_place_button.get_attribute("aria-disabled") == "true":
            return False
        return state

    try:
        ready_state = WebDriverWait(driver, 8).until(betslip_ready)
    except TimeoutException as exc:
        snapshot = _read_black_betslip_state(driver)
        panel_text = " | ".join(line.strip() for line in (snapshot.get("text", "") or "").splitlines() if line.strip())[:700]
        inputs = snapshot.get("inputs") or []
        inputs_text = "; ".join(
            f"value={item.get('value', '')!r}, placeholder={item.get('placeholder', '')!r}, aria={item.get('aria', '')!r}"
            for item in inputs[:4]
        )
        raise RuntimeError(
            f"Black betslip did not become ready after filling stake/price. "
            f"Target stake={stake_text or 'existing'}, price={price_text}. "
            f"Inputs: {inputs_text or 'none'}. Panel: {panel_text}"
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
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        print(f"[{profile_label}] Searching Black by normalized first team: {team_name}")
        _open_black_search(driver, profile_label)
        used_query = _search_black_live_events(driver, team_name, profile_label)
        print(f"[{profile_label}] Using Black search query: {used_query}")
        _open_black_live_match(driver, team_name, opponent_name, profile_label)
        if _black_search_dialog_open(driver):
            raise RuntimeError(f"[{profile_label}] Black search dialog is still open after match click; aborting before bet selection.")
        if not _black_match_context_matches(driver, team_name, opponent_name):
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
        try:
            WebDriverWait(driver, 20).until(
                lambda browser: "asian total goals" in _visible_text_lower(browser)
            )
            _ensure_black_betslip_safe_to_use(driver, profile_label)
            _select_black_asian_total_goals(
                driver, signal.selection, signal.line, profile_label, prefer_left=prefer_left
            )
        except Exception as exc:
            raise BlackSelectionMissingError(
                f"Asian Total Goals {signal.selection} {signal.line} not available for "
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
            if (rect.y < 140 || rect.height > 90 || rect.width > 520) return;
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
        for (const label of labels) {
            const labelRect = label.getBoundingClientRect();
            const inputs = Array.from(document.querySelectorAll('input'))
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


def _ensure_black_session_ready(driver: webdriver.Remote, profile_label: str) -> bool:
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
            return pwds.length === 0;
            """
        ))
    except Exception:
        return False


def login_betfair(driver: webdriver.Remote, profile_label: str) -> None:
    """Log into betfair.com/exchange/plus/ using the top-bar form.

    Idempotent: returns immediately when the account is already authenticated.
    """
    if BETFAIR_URL_PART not in (driver.current_url or "").lower():
        print(f"[{profile_label}] Navigating to Betfair: {BETFAIR_LOGIN_URL}")
        driver.get(BETFAIR_LOGIN_URL)

    # Allow the page to settle.
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass

    if _is_betfair_logged_in(driver):
        print(f"[{profile_label}] Betfair already logged in; skipping form fill.")
        return

    if not BETFAIR_USERNAME or not BETFAIR_PASSWORD:
        raise RuntimeError(
            "Missing BETFAIR_USERNAME / BETFAIR_PASSWORD; cannot log into Betfair."
        )

    # Locate the login form robustly: pick the <form> that contains a password
    # field (top-bar login form). Works regardless of UI language.
    deadline = time.time() + 30
    user_el = pwd_el = btn_el = None
    while time.time() < deadline:
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
                // Find a visible text/email input within the same form, preferring
                // ones that come BEFORE the password input.
                let candidates = [];
                if (form) {
                    candidates = Array.from(form.querySelectorAll(
                        "input[type='text'], input[type='email'], input:not([type])"
                    )).filter(visible);
                }
                // Fall back to nearest text input in the document if needed.
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
            user_el, pwd_el, btn_el = elems[0], elems[1], elems[2]
            break
        time.sleep(0.5)

    if not user_el or not pwd_el:
        raise RuntimeError(
            f"[{profile_label}] Betfair login form not found "
            f"(username={bool(user_el)}, password={bool(pwd_el)})."
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
            sel_result = _select_betfair_overunder_back(driver, signal, profile_label)
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
    score += teamHits * 30;
    score += oppHits * 35;
    // Heavy bonus for exact substring of full normalized team/opp.
    if (teamNorm && t.indexOf(teamNorm) !== -1) score += 60;
    if (oppNorm && t.indexOf(oppNorm) !== -1) score += 80;
    return score;
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

    deadline = time.time() + 20
    search_el = None
    while time.time() < deadline:
        search_el = _find_betfair_search_input(driver)
        if search_el:
            break
        time.sleep(0.5)
    if not search_el:
        raise RuntimeError(f"[{profile_label}] Betfair search input not found.")

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

    print(f"[{profile_label}] Betfair search: typing team '{team_name}'")
    search_el.send_keys(team_name)

    opponent_norm = _normalize_team_text(opponent_name) if opponent_name else ""
    team_norm = _normalize_team_text(team_name)
    end = time.time() + 12
    clicked_label = None
    while time.time() < end:
        clicked_label = driver.execute_script(
            _BETFAIR_FUZZY_HELPERS + r"""
            const teamNorm = arguments[0];
            const oppNorm = arguments[1];
            const items = Array.from(document.querySelectorAll(
                "a, li, [role='option'], [role='listitem'], [role='menuitem']"
            )).filter(visible).map((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                return { el, text, n: norm(text), score: fuzzyTeamScore(norm(text), teamNorm, oppNorm) };
            }).filter((it) => it.text && it.text.length < 250 && it.score > 0);
            if (items.length === 0) return null;
            items.sort((a, b) => b.score - a.score);
            const pick = items[0];
            pick.el.scrollIntoView({block: 'center'});
            pick.el.click();
            return pick.text.slice(0, 200);
            """,
            team_norm,
            opponent_norm,
        )
        if clicked_label:
            break
        time.sleep(0.5)

    if not clicked_label:
        print(f"[{profile_label}] No Betfair autocomplete result; submitting search via Enter.")
        try:
            search_el.send_keys(Keys.ENTER)
        except Exception:
            pass
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

    # If autocomplete navigated us — or the Enter fallback landed us — on a
    # Betfair Search Results page, click the result row that matches our team.
    followed = _follow_betfair_search_results(driver, team_norm, opponent_norm, profile_label)
    if followed:
        clicked_label = followed

    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass

    if clicked_label:
        print(f"[{profile_label}] Betfair opened result: {clicked_label!r}")
        return {
            "profile_label": profile_label,
            "team": team_name,
            "opponent": opponent_name,
            "opened": True,
            "label": clicked_label,
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
    driver: webdriver.Remote, team_norm: str, opponent_norm: str, profile_label: str
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
            const teamNorm = arguments[0];
            const oppNorm = arguments[1];
            // Result links: every anchor on the page. We DON'T require visible()
            // because Betfair sometimes renders anchors as inline within a flex
            // row whose bounding rect collapses; we filter via href shape and
            // text content instead.
            const all = Array.from(document.querySelectorAll("a"));
            const scored = all.map((a) => {
                const text = (a.innerText || a.textContent || '').trim();
                const href = a.getAttribute('href') || '';
                const n = norm(text);
                const score = fuzzyTeamScore(n, teamNorm, oppNorm);
                // Match-page links on Betfair Exchange look like
                // /exchange/plus/.../<slug>-betting-<id> — event IDs are 6+ digits.
                // Competition/league pages use short IDs (1-4 digits), so we exclude them.
                const hrefLooksLikeMatch = /-betting-\d{6,}/i.test(href)
                    || /[?&]eventId=\d+/i.test(href);
                // Text contains a ' v ' separator (e.g. 'Team A v Team B').
                const hasVs = /\bv\b/i.test(text);
                return { el: a, text, n, href, score, hrefLooksLikeMatch, hasVs };
            });
            // Primary: match-shaped href AND positive fuzzy score.
            let cands = scored.filter((it) => it.hrefLooksLikeMatch && it.score > 0);
            // Fallback A: match-shaped href AND text contains ' v ' separator.
            if (cands.length === 0) {
                cands = scored.filter((it) => it.hrefLooksLikeMatch && it.hasVs);
            }
            // Fallback B: positive score AND text contains ' v ' separator.
            if (cands.length === 0) {
                cands = scored.filter((it) => it.score > 0 && it.hasVs);
            }
            if (cands.length === 0) {
                // Return debug snapshot for the caller to log.
                return {
                    error: 'no candidate',
                    hrefSample: scored.slice(0, 10).map((s) => s.href).filter((h) => h),
                    textSample: scored.slice(0, 10).map((s) => s.text.slice(0, 60)).filter((t) => t),
                };
            }
            cands.sort((a, b) => b.score - a.score);
            const pick = cands[0];
            try { pick.el.scrollIntoView({ block: 'center' }); } catch (e) {}
            let clicked = false;
            try {
                pick.el.click();
                clicked = true;
            } catch (e) {}
            if (!clicked) {
                try {
                    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                    pick.el.dispatchEvent(evt);
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
            return { ok: true, text: (pick.text || pick.href).slice(0, 200), href: pick.href };
            """,
            team_norm,
            opponent_norm,
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


def _select_betfair_overunder_back(driver: webdriver.Remote, signal, profile_label: str) -> dict:
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
    label_text = f"{selection} {line_str} goals"
    market_text = f"over/under {line_str} goals"

    # 1) Try to bring the market into the DOM: click the left-sidebar entry if
    # the market section is not already visible.
    try:
        driver.execute_script(
            r"""
            const marketText = arguments[0];
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
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t === marketText;
                });
            if (links.length > 0) {
                links[0].scrollIntoView({ block: 'center' });
                links[0].click();
            }
            """,
            market_text,
        )
    except Exception:
        pass

    # 2) Poll for a row whose label text equals 'Over <line> Goals' / 'Under <line> Goals'
    end = time.time() + 30
    info = None
    while time.time() < end:
        info = driver.execute_script(
            r"""
            const want = arguments[0];           // 'over' or 'under'
            const lineStr = arguments[1];        // '1.5'
            const labelText = arguments[2];      // 'over 1.5 goals'
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            function txt(el) { return ((el.innerText || el.textContent || '')).trim(); }
            function parseRgb(s) {
                const m = (s || '').match(/rgba?\(([^)]+)\)/);
                if (!m) return null;
                const parts = m[1].split(',').map((x) => parseFloat(x.trim()));
                return { r: parts[0]||0, g: parts[1]||0, b: parts[2]||0 };
            }
            // Find the SMALLEST element whose visible text equals (or starts with) the label.
            // Exclude 'first half' / 'half time' / 'second half' rows so we hit the
            // full-match Over/Under market, matching how Black always treats the
            // signal as full-time.
            const labels = Array.from(document.querySelectorAll("*"))
                .filter(visible)
                .filter((el) => {
                    const t = txt(el).toLowerCase();
                    if (!t || t.length > 200) return false;
                    if (t.indexOf(labelText) === -1) return false;
                    if (t.indexOf('half') !== -1) return false;
                    return true;
                });
            if (labels.length === 0) {
                return { error: 'label not found', labelText };
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
                    let cells = Array.from(p.querySelectorAll(
                        "button, [role='button'], a, td, div"
                    )).filter(visible).filter((el) => {
                        const t = txt(el);
                        if (!t || t.length > 40) return false;
                        return /^\d+(\.\d+)?\b/.test(t);
                    });
                    // Deduplicate ancestor containers — keep only the smallest unique cell.
                    cells = cells.filter((cell) => {
                        return !cells.some((other) => other !== cell && cell.contains(other));
                    });
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
            const scored = chosen.cells.map((cell) => {
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
                // Walk up a few ancestors to inherit Back/Lay class if missing on cell.
                let p = cell.parentElement;
                for (let j = 0; j < 4 && p; j++) {
                    const pc = (p.className && p.className.toString
                        ? p.className.toString().toLowerCase() : '');
                    if (pc.indexOf('back') !== -1) { score += 25; break; }
                    if (pc.indexOf('lay') !== -1) { score -= 25; break; }
                    p = p.parentElement;
                }
                // Color comparison.
                if (bg.b > bg.r + 5) score += 30;
                if (bg.r > bg.b + 5) score -= 30;
                const rect = cell.getBoundingClientRect();
                return { cell, score, rect, priceText: txt(cell), bg, cls };
            });
            // Among Back-scoring cells, prefer the rightmost (best Back price column).
            const backs = scored.filter((s) => s.score > 0);
            backs.sort((a, b) => b.rect.right - a.rect.right);
            const pick = backs[0];
            if (!pick) {
                return { error: 'no Back cell could be identified', scored: scored.map((s) => ({ score: s.score, price: s.priceText, cls: s.cls })) };
            }
            pick.cell.scrollIntoView({ block: 'center', inline: 'center' });
            try { pick.cell.click(); }
            catch (e) {
                const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                pick.cell.dispatchEvent(evt);
            }
            return { ok: true, priceText: pick.priceText, score: pick.score, bg: pick.bg, cls: pick.cls };
            """,
            selection,
            line_str,
            label_text,
        )
        if info and info.get("ok"):
            break
        time.sleep(0.6)

    if not info or not info.get("ok"):
        raise RuntimeError(
            f"[{profile_label}] Could not select Betfair Back {selection.title()} "
            f"{line_str} Goals: {info!r}"
        )
    odds_text = info.get("priceText", "")
    print(
        f"[{profile_label}] Betfair Back selected: {selection.title()} {line_str} "
        f"Goals at odds {odds_text}"
    )

    # 3) Fill in stake and place the bet.
    stake_str = None
    bet_placed = False
    try:
        balance = _read_betfair_balance(driver, profile_label)
        stake_amount = _calculate_stake_from_balance(balance)
        stake_str = _format_stake_amount(stake_amount)
        print(f"[{profile_label}] Betfair stake: {stake_str} (balance {balance})")
    except Exception as exc:
        print(f"[{profile_label}] Betfair: could not compute stake: {exc}", flush=True)

    if stake_str:
        # Give the betslip panel time to render after clicking the Back cell.
        time.sleep(1.5)
        placed = driver.execute_script(
            r"""
            const stakeVal = arguments[0];
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const cs = window.getComputedStyle(el);
                return cs.visibility !== 'hidden' && cs.display !== 'none';
            }
            // Find the stake input. Betfair betslip inputs may not carry 'stake' in
            // their attributes — try several strategies in order of confidence.
            let inp = null;
            // Strategy 1: attribute mentions 'stake'.
            const allInputs = Array.from(document.querySelectorAll(
                "input[type='text'], input[type='number'], input:not([type])"
            )).filter(visible);
            inp = allInputs.find((el) => {
                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                const nm = (el.getAttribute('name') || '').toLowerCase();
                const id = (el.getAttribute('id') || '').toLowerCase();
                const cls = (el.className && el.className.toString ? el.className.toString().toLowerCase() : '');
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                return ph.indexOf('stake') !== -1 || nm.indexOf('stake') !== -1
                    || id.indexOf('stake') !== -1 || cls.indexOf('stake') !== -1
                    || aria.indexOf('stake') !== -1;
            }) || null;
            // Strategy 2: find a betslip/betEntry container and take the second
            // visible input inside it (first = odds, second = stake).
            if (!inp) {
                const containers = Array.from(document.querySelectorAll(
                    "[class*='betslip' i], [class*='bet-slip' i], [class*='betEntry' i],"
                    + "[class*='bet-entry' i], [aria-label*='betslip' i], [data-test*='betslip' i]"
                )).filter(visible);
                for (const c of containers) {
                    const ins = Array.from(c.querySelectorAll(
                        "input[type='text'], input[type='number'], input:not([type])"
                    )).filter(visible);
                    if (ins.length >= 2) { inp = ins[1]; break; }
                    if (ins.length === 1) { inp = ins[0]; break; }
                }
            }
            // Strategy 3: among all visible inputs, pick the one whose current value
            // is empty or '0' (not the odds field which holds a non-zero decimal).
            if (!inp && allInputs.length > 0) {
                inp = allInputs.find((el) => {
                    const v = (el.value || '').trim();
                    return v === '' || v === '0';
                }) || allInputs[allInputs.length - 1];
            }
            if (!inp) return { error: 'stake input not found',
                inputCount: allInputs.length,
                inputValues: allInputs.map((e) => e.value).slice(0, 5) };
            inp.scrollIntoView({ block: 'center' });
            inp.focus();
            // Clear existing value using React/Angular-compatible setter.
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            );
            if (nativeInputValueSetter && nativeInputValueSetter.set) {
                nativeInputValueSetter.set.call(inp, stakeVal);
            } else {
                inp.value = stakeVal;
            }
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return { stakeSet: true };
            """,
            stake_str,
        )
        if not (isinstance(placed, dict) and placed.get("stakeSet")):
            print(f"[{profile_label}] Betfair: stake fill result: {placed!r}", flush=True)
        else:
            # Wait for Betfair UI to update button label to "Confirm bet".
            time.sleep(1.2)
            confirmed = driver.execute_script(
                r"""
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.visibility !== 'hidden' && cs.display !== 'none';
                }
                const btns = Array.from(document.querySelectorAll(
                    "button, [role='button'], input[type='submit']"
                )).filter(visible).filter((el) => {
                    const t = (el.innerText || el.value || '').trim().toLowerCase();
                    return t === 'confirm bet' || t === 'confirm bets'
                        || t === 'place bet' || t === 'place bets'
                        || t.startsWith('confirm bet') || t.startsWith('place bet');
                });
                if (btns.length === 0) return { error: 'confirm/place button not found' };
                const btn = btns[0];
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return { ok: true, label: (btn.innerText || btn.value || '').trim() };
                """
            )
            if isinstance(confirmed, dict) and confirmed.get("ok"):
                first_label = confirmed.get("label", "")
                print(
                    f"[{profile_label}] Betfair: clicked '{first_label}' — "
                    f"{selection.title()} {line_str} @ {odds_text}, stake {stake_str}"
                )
                # If the first button was "Place bet/bets", Betfair shows a
                # second "Confirm bet" button. Wait and click it.
                if "place" in first_label.lower():
                    time.sleep(1.2)
                    confirmed2 = driver.execute_script(
                        r"""
                        function visible(el) {
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) return false;
                            const cs = window.getComputedStyle(el);
                            return cs.visibility !== 'hidden' && cs.display !== 'none';
                        }
                        const btns = Array.from(document.querySelectorAll(
                            "button, [role='button'], input[type='submit']"
                        )).filter(visible).filter((el) => {
                            const t = (el.innerText || el.value || '').trim().toLowerCase();
                            return t === 'confirm bet' || t === 'confirm bets'
                                || t.startsWith('confirm bet');
                        });
                        if (btns.length === 0) return { error: 'confirm bet button not found' };
                        const btn = btns[0];
                        btn.scrollIntoView({ block: 'center' });
                        btn.click();
                        return { ok: true, label: (btn.innerText || btn.value || '').trim() };
                        """
                    )
                    if isinstance(confirmed2, dict) and confirmed2.get("ok"):
                        print(
                            f"[{profile_label}] Betfair: confirmed — "
                            f"'{confirmed2.get('label')}'"
                        )
                        bet_placed = True
                    else:
                        print(
                            f"[{profile_label}] Betfair: confirm-2 result: {confirmed2!r}",
                            flush=True,
                        )
                else:
                    bet_placed = True
            else:
                print(f"[{profile_label}] Betfair: confirm click result: {confirmed!r}", flush=True)

    return {
        "betfair_selection": f"{selection.title()} {line_str}",
        "betfair_odds": odds_text,
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
        return null;
        """
    )
    if not text:
        raise RuntimeError(f"[{profile_label}] Could not read Betfair balance from top bar.")
    m = re.search(r"[€£$]\s*([\d.,]+)", text)
    if not m:
        raise RuntimeError(f"[{profile_label}] Betfair balance text has no amount: {text!r}")
    amount = m.group(1).replace(",", "")
    return Decimal(amount)


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
    clicked_betting = False
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
                return t === 'betting' && t.length < 20;
            });
            if (cands.length === 0) return false;
            cands[0].scrollIntoView({ block: 'center' });
            cands[0].click();
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
