from __future__ import annotations

import atexit
import logging
import os
import queue
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Iterator

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from warehouse.stealth import apply_stealth, default_profile_dir

logger = logging.getLogger(__name__)

CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "checking your browser",
    "verify you are human",
    "attention required",
    "client challenge",
)

# Fastly fronts pypi.org and serves this interstitial for uncached pages.
CHALLENGE_SOURCE_MARKERS = (
    "cdn-cgi/challenge-platform",
    "_fs-ch-",
)

CHALLENGE_FRAME_SELECTORS = (
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='challenge-platform']",
    "iframe[title*='challenge' i]",
    "iframe[title*='cloudflare' i]",
    "iframe[title*='human' i]",
)

CHALLENGE_CONTROL_SELECTORS = (
    "input[type='checkbox']",
    "[role='checkbox']",
    "label",
    "#challenge-stage",
    "button",
)


@dataclass(frozen=True)
class BrowserOptions:
    headless: bool = False
    window_size: str = "1440,900"
    page_load_timeout: float = 45.0
    pool_size: int = 1
    driver_path: str | None = None
    binary_path: str | None = None
    user_agent: str | None = None
    extra_args: tuple[str, ...] = ()
    profile_dir: str | None = None
    stealth: bool = True
    humanize: bool = True
    challenge_timeout: float = 25.0
    manual_challenge_timeout: float = 0.0

    @classmethod
    def from_settings(cls, settings: Any) -> "BrowserOptions":
        raw = settings.get("BROWSER_OPTIONS") or {}
        headless = raw.get("headless", False)
        if isinstance(headless, str):
            headless = headless.strip().lower() in {"1", "true", "yes", "on"}

        extra_args = raw.get("extra_args") or ()
        if isinstance(extra_args, str):
            extra_args = tuple(arg for arg in extra_args.split(",") if arg)
        else:
            extra_args = tuple(extra_args)

        return cls(
            headless=bool(headless),
            window_size=str(raw.get("window_size", "1440,900")),
            page_load_timeout=float(raw.get("page_load_timeout", 45.0)),
            pool_size=max(1, int(raw.get("pool_size", 1))),
            driver_path=raw.get("driver_path") or None,
            binary_path=raw.get("binary_path") or None,
            user_agent=raw.get("user_agent") or None,
            extra_args=extra_args,
            profile_dir=raw.get("profile_dir") or default_profile_dir(),
            stealth=bool(raw.get("stealth", True)),
            humanize=bool(raw.get("humanize", True)),
            challenge_timeout=float(raw.get("challenge_timeout", 25.0)),
            manual_challenge_timeout=float(raw.get("manual_challenge_timeout", 0.0)),
        )


class BrowserSession:
    def __init__(self, driver: webdriver.Chrome) -> None:
        self.driver = driver

    def open(self, url: str) -> None:
        self.driver.get(url)

    def wait_ready(self, timeout: float) -> None:
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def wait_for(self, selector: str, timeout: float) -> bool:
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return True
        except TimeoutException:
            return False

    def is_challenge(self) -> bool:
        try:
            title = (self.driver.title or "").lower()
            source = (self.driver.page_source or "")[:100_000].lower()
        except WebDriverException:
            return True
        return any(marker in title for marker in CHALLENGE_MARKERS) or any(
            marker in source for marker in CHALLENGE_SOURCE_MARKERS
        )

    def wait_until_clear(self, timeout: float, poll: float = 0.75) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_challenge():
                return True
            self.sleep(poll)
        return not self.is_challenge()

    def solve_challenge(self) -> bool:
        """Click a Cloudflare Turnstile widget without using Selenium clicks.

        Selenium's element click is easy for the widget to distinguish, so the
        press is dispatched through CDP with a small human-like pause.
        """
        clicked = False

        for frame in self._challenge_frames():
            try:
                self.driver.switch_to.frame(frame)
                for selector in CHALLENGE_CONTROL_SELECTORS:
                    for control in self.driver.find_elements(By.CSS_SELECTOR, selector):
                        if control.is_displayed():
                            self.human_pause()
                            control.click()
                            clicked = True
                            break
                    if clicked:
                        break
            except WebDriverException:
                pass
            finally:
                self.driver.switch_to.default_content()
            if clicked:
                break

        if not clicked:
            for frame in self._challenge_frames():
                rect = frame.rect
                if rect["width"] and rect["height"]:
                    # Turnstile places its checkbox near the left edge.
                    self.click_point(
                        rect["x"] + min(28, rect["width"] / 2),
                        rect["y"] + rect["height"] / 2,
                    )
                    clicked = True
                    break

        if not clicked:
            for element in self.driver.find_elements(
                By.XPATH,
                "//button[contains(translate(., 'VERIFY', 'verify'), 'verify')]"
                " | //*[@role='checkbox']",
            ):
                if element.is_displayed():
                    self.human_pause()
                    element.click()
                    clicked = True
                    break

        if clicked:
            logger.info("Clicked a challenge widget on %s", self.current_url)
        return clicked

    def _challenge_frames(self) -> list[Any]:
        for selector in CHALLENGE_FRAME_SELECTORS:
            frames = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if frames:
                return frames
        return []

    def click_point(self, x: float, y: float) -> None:
        jitter_x = x + random.uniform(-1.5, 1.5)
        jitter_y = y + random.uniform(-1.5, 1.5)
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            payload = {"type": event, "x": jitter_x, "y": jitter_y}
            if event != "mouseMoved":
                payload |= {"button": "left", "clickCount": 1}
            self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", payload)
            self.sleep(random.uniform(0.03, 0.09))

    def human_pause(self) -> None:
        self.sleep(random.uniform(0.3, 0.9))

    def count(self, selector: str) -> int:
        return len(self.driver.find_elements(By.CSS_SELECTOR, selector))

    def scroll(self, direction: str) -> None:
        if direction == "down":
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
        else:
            self.driver.execute_script("window.scrollTo(0, 0);")

    def scroll_to_top(self) -> None:
        self.driver.execute_script("window.scrollTo(0, 0);")

    def scroll_position(self) -> int:
        return int(self.driver.execute_script("return window.scrollY") or 0)

    def click_first(self, selector: str) -> bool:
        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
        if not elements:
            return False
        elements[0].click()
        return True

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @property
    def page_source(self) -> str:
        return self.driver.page_source

    @property
    def current_url(self) -> str:
        return self.driver.current_url

    @property
    def title(self) -> str:
        return self.driver.title


class BrowserManager:
    def __init__(self, options: BrowserOptions) -> None:
        if options.profile_dir and options.pool_size > 1:
            logger.warning(
                "A persistent Chrome profile can only back one driver; "
                "reducing browser pool from %d to 1",
                options.pool_size,
            )
            options = replace(options, pool_size=1)

        self.options = options
        self._pool: queue.Queue[webdriver.Chrome] = queue.Queue()
        self._created = 0
        self._drivers: set[webdriver.Chrome] = set()
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    @contextmanager
    def borrow(self) -> Iterator[BrowserSession]:
        driver = self._acquire()
        session = BrowserSession(driver)
        try:
            yield session
        except BaseException as exc:
            if isinstance(exc, WebDriverException):
                self._discard(driver)
            else:
                self._release(driver)
            raise
        else:
            self._release(driver)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            drivers = list(self._drivers)
            self._drivers.clear()
            self._created = 0

        while True:
            try:
                self._pool.get_nowait()
            except queue.Empty:
                break

        for driver in drivers:
            try:
                driver.quit()
            except Exception:
                pass

    def _acquire(self) -> webdriver.Chrome:
        create = False
        with self._lock:
            if self._closed:
                raise RuntimeError("browser manager is closed")
            try:
                return self._pool.get_nowait()
            except queue.Empty:
                if self._created < self.options.pool_size:
                    self._created += 1
                    create = True

        if create:
            try:
                return self._create_driver()
            except BaseException:
                with self._lock:
                    self._created -= 1
                raise

        while True:
            if self._closed:
                raise RuntimeError("browser manager is closed")
            try:
                return self._pool.get(timeout=0.5)
            except queue.Empty:
                continue

    def _release(self, driver: webdriver.Chrome) -> None:
        with self._lock:
            if self._closed:
                self._drivers.discard(driver)
            else:
                self._pool.put(driver)
                return
        try:
            driver.quit()
        except Exception:
            pass

    def _discard(self, driver: webdriver.Chrome) -> None:
        try:
            driver.quit()
        except Exception:
            pass
        with self._lock:
            self._drivers.discard(driver)
            self._created = max(0, self._created - 1)

    def _create_driver(self) -> webdriver.Chrome:
        options = Options()
        if self.options.headless:
            options.add_argument("--headless=new")

        if self.options.profile_dir:
            os.makedirs(self.options.profile_dir, exist_ok=True)
            options.add_argument(f"--user-data-dir={self.options.profile_dir}")

        options.add_argument(f"--window-size={self.options.window_size}")
        options.add_argument("--lang=en-US")
        if self.options.headless:
            # Software rendering in headless mode otherwise reports no WebGL.
            options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option(
            "prefs",
            {
                "intl.accept_languages": "en-US,en",
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )

        if self.options.user_agent:
            options.add_argument(f"--user-agent={self.options.user_agent}")
        if self.options.binary_path:
            options.binary_location = self.options.binary_path
        for arg in self.options.extra_args:
            options.add_argument(arg)

        service = (
            Service(executable_path=self.options.driver_path)
            if self.options.driver_path
            else Service()
        )
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(self.options.page_load_timeout)

        try:
            if self.options.stealth:
                apply_stealth(driver)
        except Exception:
            driver.quit()
            raise

        with self._lock:
            self._drivers.add(driver)
        return driver
