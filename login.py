"""
Punterplay auto-login script via AdsPower profiles.
Starts two AdsPower browser profiles and logs into punterplay.com from both simultaneously.

Usage:
    pip install -r requirements.txt
    python login.py
"""

import os
import sys
import time
import socket
import subprocess
import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.common.exceptions import NoSuchWindowException, WebDriverException
from selenium.webdriver.firefox.options import Options as FirefoxOptions

load_dotenv()

LOGIN_URL = "https://pro.punterplay.com/#/sign-in"
PUNTERPLAY_URL_PART = "punterplay.com"

ADSPOWER_API_URL = os.getenv("ADSPOWER_API_URL", "http://local.adspower.net:50325")
USERNAME = os.getenv("PUNTER_LOGIN")
PASSWORD = os.getenv("PUNTER_PASSWORD")


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
                print(f"Recovering browser tab after discarded context while searching for {selector}...")
                if not _switch_to_live_window(browser, "punterplay.com"):
                    raise
                return False
            except WebDriverException as exc:
                last_exc = exc
                message = str(exc).lower()
                if "discarded" in message or "no such window" in message:
                    print(f"Recovering browser tab after discarded context while searching for {selector}...")
                    if not _switch_to_live_window(browser, "punterplay.com"):
                        raise
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


def _switch_to_live_window(driver: webdriver.Remote, url_part: str | None = None) -> bool:
    for handle in reversed(driver.window_handles):
        try:
            driver.switch_to.window(handle)
            if not url_part or url_part in driver.current_url:
                return True
        except (NoSuchWindowException, WebDriverException):
            continue
    return False


def ensure_punterplay_tab(driver: webdriver.Remote, profile_label: str) -> None:
    """Switch to an existing Punterplay tab or open the login page once."""
    if _switch_to_live_window(driver, PUNTERPLAY_URL_PART):
        print(f"[{profile_label}] Reusing existing Punterplay tab: {driver.current_url}")
        return

    print(f"[{profile_label}] No Punterplay tab found. Opening login page in a new tab...")
    _open_new_login_tab(driver, profile_label)


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


def _open_new_login_tab(driver: webdriver.Remote, profile_label: str) -> None:
    """Create a new browser tab and navigate it to the Punterplay sign-in page."""
    _switch_to_live_window(driver)
    before_handles = set(driver.window_handles)

    try:
        ActionChains(driver).key_down(Keys.CONTROL).send_keys("t").key_up(Keys.CONTROL).perform()
        WebDriverWait(driver, 10).until(
            lambda browser: len(browser.window_handles) > len(before_handles)
        )
        new_handles = [handle for handle in driver.window_handles if handle not in before_handles]
        driver.switch_to.window(new_handles[-1])
        driver.get(LOGIN_URL)
    except Exception:
        _switch_to_live_window(driver)
        try:
            driver.switch_to.new_window("tab")
            driver.get(LOGIN_URL)
        except Exception:
            driver.execute_script("window.open(arguments[0], '_blank');", LOGIN_URL)
            WebDriverWait(driver, 10).until(
                lambda browser: len(browser.window_handles) > len(before_handles)
            )
            new_handles = [handle for handle in driver.window_handles if handle not in before_handles]
            driver.switch_to.window(new_handles[-1])

    print(f"[{profile_label}] Opening new tab: {driver.current_window_handle}")
    if "punterplay.com" not in driver.current_url:
        driver.get(LOGIN_URL)

    WebDriverWait(driver, 20).until(lambda browser: "punterplay.com" in browser.current_url)
    if not _switch_to_live_window(driver, "punterplay.com"):
        raise RuntimeError("Could not switch to a live Punterplay tab after opening it.")


def open_login_tab(driver: webdriver.Remote, profile_label: str) -> None:
    """Open Punterplay sign-in page in a new tab."""
    print(f"[{profile_label}] Current URL before navigation: {driver.current_url}")
    print(f"[{profile_label}] Open windows: {driver.window_handles}")

    ensure_punterplay_tab(driver, profile_label)
    print(f"[{profile_label}] Navigated to: {driver.current_url}")


def login(driver: webdriver.Remote, profile_label: str) -> None:
    """Perform login in an already opened Punterplay tab."""

    # SPA needs time to bootstrap after navigation
    time.sleep(4)

    if not _switch_to_live_window(driver, "punterplay.com"):
        raise RuntimeError("Punterplay tab is no longer available after navigation.")

    WebDriverWait(driver, 20).until(
        lambda browser: browser.execute_script("return document.readyState") in {"interactive", "complete"}
    )

    if "#/sign-in" not in driver.current_url.lower():
        print(f"[{profile_label}] Already signed in or redirected to: {driver.current_url}")
        return

    username_input = _find_first_visible(driver, [
        (By.CSS_SELECTOR, "input[name='login']"),
        (By.CSS_SELECTOR, "input[name='username']"),
        (By.CSS_SELECTOR, "input[name='email']"),
        (By.CSS_SELECTOR, "input[autocomplete='username']"),
        (By.CSS_SELECTOR, "input[autocomplete='email']"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input[type='text']"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'username')]"),
        (By.XPATH, "//input[contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'email')]"),
    ])

    password_input = _find_first_visible(driver, [
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.CSS_SELECTOR, "input[name='password']"),
        (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
    ])

    print(f"[{profile_label}] Found username field: {username_input.get_attribute('outerHTML')[:120]}")
    print(f"[{profile_label}] Filling credentials...")
    _set_input_value(driver, username_input, USERNAME)
    time.sleep(0.5)

    _set_input_value(driver, password_input, PASSWORD)
    time.sleep(0.5)

    username_value = username_input.get_attribute("value") or ""
    password_value = password_input.get_attribute("value") or ""
    print(
        f"[{profile_label}] Field values after fill: "
        f"username_len={len(username_value)}, password_len={len(password_value)}"
    )

    login_btn = _find_first_clickable(driver, [
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]") ,
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]") ,
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'log in')]") ,
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'войти')]") ,
    ])
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", login_btn)
    WebDriverWait(driver, 10).until(lambda browser: login_btn.is_enabled())
    print(f"[{profile_label}] Clicking login button: {login_btn.get_attribute('outerHTML')[:160]}")
    before_submit_url = driver.current_url
    login_btn.click()

    # Wait for URL change or dashboard element as sign of successful login
    try:
        WebDriverWait(driver, 15).until(EC.url_changes(before_submit_url))
        print(f"[{profile_label}] Login successful — URL changed to: {driver.current_url}")
    except Exception:
        print(f"[{profile_label}] URL did not change after click. Pressing Enter in password field...")
        password_input.send_keys(Keys.ENTER)
        try:
            WebDriverWait(driver, 15).until(EC.url_changes(before_submit_url))
            print(f"[{profile_label}] Login successful — URL changed to: {driver.current_url}")
            return
        except Exception:
            pass

        # Some SPAs keep the same base URL; check for absence of login form instead
        try:
            WebDriverWait(driver, 5).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
            )
            print(f"[{profile_label}] Login successful — login form disappeared.")
        except Exception:
            print(f"[{profile_label}] WARNING: Could not confirm login success. Current URL: {driver.current_url}")


def run_profile(profile_id: str, profile_label: str) -> dict:
    """Full lifecycle: start profile, login, keep open."""
    driver = None
    was_already_running = False
    try:
        print(f"[{profile_label}] Preparing AdsPower profile: {profile_id}")
        browser_info, was_already_running = start_adspower_profile(profile_id)

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
        close_driver_bridge(driver)

        print(f"[{profile_label}] Done. Browser will remain open. Press Ctrl+C to exit.")
        return {
            "profile_id": profile_id,
            "profile_label": profile_label,
            "browser_info": browser_info,
            "was_already_running": was_already_running,
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
    """Validate environment required for Punterplay login."""
    missing = []
    if not USERNAME:
        missing.append("PUNTER_LOGIN")
    if not PASSWORD:
        missing.append("PUNTER_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def run_all_profiles(expected_profiles: int = 2, wait_for_enter: bool = False) -> list[dict]:
    """Start or reuse AdsPower profiles, open Punterplay, and log in."""
    validate_required_env()

    profile_ids = fetch_profile_ids(expected=expected_profiles)

    sessions = []
    for idx, profile_id in enumerate(profile_ids, start=1):
        label = f"Profile-{idx}"
        sessions.append(run_profile(profile_id, label))

    print("\nAll profiles have completed login. Browsers remain open.")

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
