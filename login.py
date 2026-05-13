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
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import NoSuchWindowException, WebDriverException
from selenium.webdriver.firefox.options import Options as FirefoxOptions

load_dotenv()

LOGIN_URL = "https://betinasia.com"
BETINASIA_URL_PART = "betinasia.com"
PORTAL_URL = "https://portal.betinasia.com/Dashboard/Products"
PORTAL_LOGIN_URL = "https://portal.betinasia.com/Account/Login"
BLACK_URL_PART = "black.betinasia.com"
BLACK_URL = "https://black.betinasia.com"

ADSPOWER_API_URL = os.getenv("ADSPOWER_API_URL", "http://local.adspower.net:50325")
BETINASIA_EMAIL = os.getenv("BETINASIA_EMAIL")
BETINASIA_PASSWORD = os.getenv("BETINASIA_PASSWORD")
BLACK_USERNAME = os.getenv("BLACK_USERNAME")
BLACK_PASSWORD = os.getenv("BLACK_PASSWORD")
STAKE_PERCENT = Decimal(os.getenv("STAKE_PERCENT", "5"))
EURO_SYMBOL = "\u20ac"


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
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    element.send_keys(value)

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
    for _ in range(5):
        opened = bool(driver.execute_script(
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
        const textOf = (element) => (element.innerText || element.textContent || '').trim();
        const pageHasSettings = () => (document.body?.innerText || '').toLowerCase().includes('settings');
        const euro = String.fromCharCode(8364);
        const fire = (target) => {
            const clickable = target?.closest?.('button,a,[role="button"]') || target;
            if (!clickable) return false;
            clickable.scrollIntoView?.({ block: 'center', inline: 'center' });
            for (const name of ['mouseover', 'mouseenter', 'mousemove', 'pointerover', 'pointerenter']) {
                clickable.dispatchEvent(new MouseEvent(name, { bubbles: true, cancelable: true, view: window }));
            }
            if (pageHasSettings()) return true;
            for (const name of ['mousedown', 'mouseup', 'click']) {
                clickable.dispatchEvent(new MouseEvent(name, { bubbles: true, cancelable: true, view: window }));
            }
            try { clickable.click?.(); } catch (error) {}
            return pageHasSettings();
        };
        const candidates = Array.from(document.querySelectorAll('button,a,[role="button"],div,span,svg'))
            .filter(isVisible)
            .filter((element) => {
                const rect = element.getBoundingClientRect();
                const text = textOf(element);
                const marker = [text, element.getAttribute('aria-label') || '', element.className?.toString() || '']
                    .join(' ')
                    .toLowerCase();
                return rect.y < 140
                    && rect.x > window.innerWidth * 0.55
                    && (text.includes(euro)
                        || marker.includes('account')
                        || marker.includes('profile')
                        || marker.includes('user')
                        || marker.includes('balance')
                        || marker.includes('alexiq'));
            })
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return br.x - ar.x || (ar.width * ar.height) - (br.width * br.height);
            });
        for (const target of candidates.slice(0, 12)) {
            if (fire(target)) return true;
        }

        const yValues = [88, 96, 104, 56];
        const xValues = [28, 52, 84, 120, 160, 205, 245].map((offset) => window.innerWidth - offset);
        for (const y of yValues) {
            for (const x of xValues) {
                let target = document.elementFromPoint(x, y);
                for (let depth = 0; target && depth < 4; depth += 1, target = target.parentElement) {
                    if (fire(target)) return true;
                }
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


def _read_black_balance(driver: webdriver.Remote, profile_label: str) -> Decimal:
    _open_black_account_menu(driver, profile_label)
    text = _visible_page_text(driver)
    balance_match = re.search(r"Balance\s*\n?\s*(\u20ac\s*[0-9]+(?:[.,][0-9]+)?)", text, re.IGNORECASE)
    if not balance_match:
        balance_match = re.search(r"Funds\s*\n?\s*(\u20ac\s*[0-9]+(?:[.,][0-9]+)?)", text, re.IGNORECASE)
    if not balance_match:
        money_values = re.findall(r"\u20ac\s*[0-9]+(?:[.,][0-9]+)?", text)
        if not money_values:
            raise RuntimeError("Could not read Black balance from the account menu.")
        balance_text = money_values[0]
    else:
        balance_text = balance_match.group(1)
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

    updated = bool(driver.execute_script(
        """
        const stake = arguments[0];
        const isVisible = (element) => {
            const rect = element.getBoundingClientRect();
            return rect.width && rect.height && rect.x < window.innerWidth && rect.y < window.innerHeight;
        };
        const textOf = (element) => (element.innerText || element.textContent || '').trim().toLowerCase();
        const labels = Array.from(document.querySelectorAll('label,div,section,p,span'))
            .filter(isVisible)
            .filter((element) => textOf(element) === 'default stake' || textOf(element).includes('default stake'));
        for (const label of labels) {
            let root = label.parentElement;
            for (let depth = 0; root && depth < 6; depth += 1, root = root.parentElement) {
                const input = Array.from(root.querySelectorAll('input'))
                    .find((element) => isVisible(element) && !element.disabled && !element.readOnly);
                if (!input) continue;
                input.scrollIntoView({ block: 'center', inline: 'center' });
                input.focus();
                const setter = Object.getOwnPropertyDescriptor(input.__proto__, 'value')?.set;
                if (setter) {
                    setter.call(input, stake);
                } else {
                    input.value = stake;
                }
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.blur();
                input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
                input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
                return input.value === stake;
            }
        }
        return false;
        """,
        stake,
    ))
    if not updated:
        raise RuntimeError("Could not update Black Default Stake input.")
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


def login(driver: webdriver.Remote, profile_label: str) -> None:
    """Perform BetInAsia portal login, open Black, then perform Black login."""
    time.sleep(2)
    if BETINASIA_URL_PART not in driver.current_url.lower():
        driver.get(LOGIN_URL)

    _login_betinasia_portal(driver, profile_label)
    _open_black_product(driver, profile_label)
    _login_black(driver, profile_label)


def run_profile(profile_id: str, profile_label: str, login_enabled: bool = True) -> dict:
    """Full lifecycle: start profile, log into BetInAsia/Black, keep open."""
    driver = None
    was_already_running = False
    try:
        print(f"[{profile_label}] Preparing AdsPower profile: {profile_id}")
        browser_info, was_already_running = start_adspower_profile(profile_id)

        if not login_enabled:
            print(f"[{profile_label}] Skipping BetInAsia/Black login; only the first profile may use the account.")
            return {
                "profile_id": profile_id,
                "profile_label": profile_label,
                "browser_info": browser_info,
                "was_already_running": was_already_running,
                "login_enabled": False,
            }

        # Wait for the browser to fully initialize before connecting
        time.sleep(4)

        driver = connect_to_browser(browser_info, profile_label)
        open_login_tab(driver, profile_label)
        close_driver_bridge(driver)
        driver = None

        time.sleep(2)
        print(f"[{profile_label}] Reconnecting after opening tab...")
        driver = connect_to_browser(browser_info, profile_label)
        login(driver, profile_label)
        stake_result = update_black_default_stake(driver, profile_label)
        print(
            f"[{profile_label}] Default stake refreshed on startup: "
            f"balance EUR {stake_result['balance']}, stake EUR {stake_result['stake']} ({stake_result['percent']}%)."
        )
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
        if driver and not was_already_running:
            driver.quit()
        else:
            close_driver_bridge(driver)
        if not was_already_running:
            stop_adspower_profile(profile_id)
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

    print("\nAdsPower profiles are ready. BetInAsia/Black login was performed only on Profile-1.")

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
