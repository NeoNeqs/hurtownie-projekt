from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from scrapy import signals
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.http import HtmlResponse, Request, Response
from selenium.common.exceptions import TimeoutException, WebDriverException
from twisted.internet.threads import deferToThread

from warehouse.browser import BrowserManager, BrowserOptions

logger = logging.getLogger(__name__)

RATE_LIMIT_STATUS = 429


def _retry_after_seconds(response: Response) -> float | None:
    """Read a ``Retry-After`` header in seconds or HTTP-date form."""
    raw = response.headers.get(b"Retry-After")
    if not raw:
        return None
    value = raw.decode("latin-1").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


class NpmRateLimitRetryMiddleware(RetryMiddleware):
    """Rate-limit aware retry for npm crawlers.

    npm answers crawlers with ``{"message":"Too Many Requests", ...}`` when a
    crawl gets chatty. Scrapy's default retry re-queues immediately, gives up
    after RETRY_TIMES and lets the errbacks close the run; this subclass waits
    out a cool-down and keeps retrying while the crawler is being rate limited.

    The pause honours ``Retry-After`` when the server sends it. Otherwise it
    backs off exponentially from ``NPM_RETRY_COOLDOWN_SECONDS`` up to
    ``NPM_RETRY_MAX_COOLDOWN_SECONDS``, with 20% jitter so several workers do
    not wake at the same instant.

    By default the crawler never gives up on a rate-limited request: it keeps
    sleeping and retrying until the site lets it back in. ``NPM_RETRY_MAX_ATTEMPTS``
    and ``NPM_RETRY_MAX_TOTAL_COOLDOWN_SECONDS`` can impose a limit if a bounded
    run is wanted instead.
    """

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.base_cooldown = float(
            settings.getfloat("NPM_RETRY_COOLDOWN_SECONDS", 600.0)
        )
        self.max_cooldown = float(
            settings.getfloat("NPM_RETRY_MAX_COOLDOWN_SECONDS", 3600.0)
        )
        self.rate_limit_attempts = int(
            settings.getint("NPM_RETRY_MAX_ATTEMPTS", 0)
        )
        self.max_total_cooldown = float(
            settings.getfloat("NPM_RETRY_MAX_TOTAL_COOLDOWN_SECONDS", 0.0)
        )

    def _backoff(self, attempts: int) -> float:
        exponent = min(max(attempts - 1, 0), 8)
        delay = min(self.base_cooldown * (2**exponent), self.max_cooldown)
        return min(delay * random.uniform(0.8, 1.2), self.max_cooldown)

    def _wait(self, delay: float) -> bool:
        """Sleep in short slices so Ctrl+C and engine shutdown stay responsive."""
        engine = getattr(getattr(self, "crawler", None), "engine", None)
        if engine is None:
            time.sleep(delay)
            return True
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if not getattr(engine, "running", True):
                return False
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return True

    def _retry(self, request: Request, reason: Any) -> Request | None:
        if request.meta.pop("npm_rate_limit_pending", False):
            attempts = int(request.meta.get("npm_rate_limit_attempts", 1))
            budget = (
                self.rate_limit_attempts
                if self.rate_limit_attempts > 0
                else 10**9
            )
            request.meta["max_retry_times"] = max(
                int(request.meta.get("max_retry_times") or self.max_retry_times),
                budget,
            )
            delay = request.meta.pop("npm_retry_after", None)
            if delay is None:
                delay = self._backoff(attempts)
            spent = float(request.meta.get("npm_rate_limit_total_sleep", 0.0))
            if self.max_total_cooldown > 0 and spent + delay > self.max_total_cooldown:
                logger.error(
                    "HTTP 429 on %s; giving up after %.0f s of cool-down",
                    request.url,
                    spent,
                )
                return None
            request.meta["npm_rate_limit_total_sleep"] = spent + delay
            logger.warning(
                "HTTP 429 on %s; cooling down for %.0f s (attempt %d)",
                request.url,
                delay,
                attempts,
            )
            if not self._wait(delay):
                return None
        return super()._retry(request, reason)

    def process_response(self, request: Request, response: Response, spider: Any) -> Any:
        if (
            response.status == RATE_LIMIT_STATUS
            and not request.meta.get("dont_retry", False)
        ):
            # Rate limited: mark this request so the retry pauses first.
            request.meta["npm_rate_limit_pending"] = True
            request.meta["npm_rate_limit_attempts"] = (
                int(request.meta.get("npm_rate_limit_attempts", 0)) + 1
            )
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                request.meta["npm_retry_after"] = min(
                    retry_after, self.max_cooldown
                )
        return super().process_response(request, response, spider)


class SeleniumMiddleware:
    def __init__(self, manager: BrowserManager, stats: Any) -> None:
        self.manager = manager
        self.stats = stats

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "SeleniumMiddleware":
        middleware = cls(
            BrowserManager(BrowserOptions.from_settings(crawler.settings)),
            crawler.stats,
        )
        crawler.signals.connect(middleware.spider_closed, signal=signals.spider_closed)
        return middleware

    def process_request(self, request: Request) -> Any:
        if not request.meta.get("selenium"):
            return None
        return deferToThread(self._render, request)

    def spider_closed(self, spider: Any, reason: str | None = None) -> None:
        self.manager.close()

    def _render(self, request: Request) -> Response:
        started = time.monotonic()
        try:
            with self.manager.borrow() as session:
                try:
                    session.open(request.url)
                except TimeoutException:
                    self.stats.inc_value("browser/timeouts")
                    logger.warning("Browser page load timed out: %s", request.url)
                    return _error_response(request, 504)

                if session.is_challenge():
                    if not self._resolve_challenge(session, request):
                        self.stats.inc_value("browser/challenge_failed")
                        request.meta["selenium_challenge"] = True
                        request.meta["dont_retry"] = True
                        logger.error("Anti-bot challenge not cleared: %s", request.url)
                        return _error_response(request, 503)

                if self.manager.options.humanize:
                    session.human_pause()

                wait_for = request.meta.get("selenium_wait_for")
                if wait_for and not session.wait_for(
                    wait_for, self.manager.options.page_load_timeout
                ):
                    self.stats.inc_value("browser/wait_for_missing")
                    request.meta["selenium_wait_for_missing"] = wait_for
                    logger.warning(
                        "Selector %r did not appear on %s", wait_for, request.url
                    )

                self._run_actions(session, request)

                extra_wait = float(request.meta.get("selenium_wait", 0) or 0)
                if extra_wait:
                    session.sleep(extra_wait)

                body = session.page_source.encode("utf-8")
                response = HtmlResponse(
                    url=session.current_url,
                    body=body,
                    encoding="utf-8",
                    request=request,
                    status=200,
                )
        except WebDriverException as exc:
            self.stats.inc_value("browser/driver_errors")
            logger.warning("Browser failed on %s: %s", request.url, exc)
            return _error_response(request, 502)
        except Exception:
            self.stats.inc_value("browser/render_errors")
            logger.exception("Unexpected browser error on %s", request.url)
            return _error_response(request, 500)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        request.meta["selenium_render_ms"] = elapsed_ms
        self.stats.inc_value("browser/rendered")
        self.stats.set_value("browser/last_render_ms", elapsed_ms)
        logger.info("Rendered %s in %d ms (%d bytes)", request.url, elapsed_ms, len(body))
        return response

    def _resolve_challenge(self, session: Any, request: Request) -> bool:
        auto_timeout = float(
            request.meta.get(
                "selenium_challenge_timeout", self.manager.options.challenge_timeout
            )
        )
        manual_timeout = float(
            request.meta.get(
                "selenium_manual_challenge_timeout",
                self.manager.options.manual_challenge_timeout,
            )
        )

        self.stats.inc_value("browser/challenges")
        logger.warning("Anti-bot challenge on %s", request.url)

        if auto_timeout > 0 and session.wait_until_clear(min(auto_timeout, 6.0)):
            self.stats.inc_value("browser/challenge_passed")
            return True

        if session.solve_challenge():
            self.stats.inc_value("browser/challenge_clicked")
            if session.wait_until_clear(auto_timeout):
                self.stats.inc_value("browser/challenge_passed")
                return True

        if manual_timeout > 0 and not self.manager.options.headless:
            logger.warning(
                "Solve the challenge in the Chrome window for %s (waiting up to %d s)",
                request.url,
                int(manual_timeout),
            )
            if session.wait_until_clear(manual_timeout):
                self.stats.inc_value("browser/challenge_passed")
                return True

        try:
            session.open(request.url)
        except TimeoutException:
            return False

        if auto_timeout > 0 and session.wait_until_clear(min(auto_timeout, 6.0)):
            self.stats.inc_value("browser/challenge_passed")
            return True

        if session.solve_challenge() and session.wait_until_clear(auto_timeout):
            self.stats.inc_value("browser/challenge_passed")
            return True
        return False

    def _run_actions(self, session: Any, request: Request) -> None:
        actions = request.meta.get("selenium_actions")
        if not actions:
            return
        if callable(actions):
            actions = [actions]

        for action in actions:
            try:
                action(session, request)
            except Exception as exc:
                self.stats.inc_value("browser/action_errors")
                request.meta.setdefault("selenium_action_errors", []).append(str(exc))
                logger.warning("Browser action failed on %s: %s", request.url, exc)


def _error_response(request: Request, status: int) -> HtmlResponse:
    return HtmlResponse(
        url=request.url,
        body=b"",
        encoding="utf-8",
        request=request,
        status=status,
    )
