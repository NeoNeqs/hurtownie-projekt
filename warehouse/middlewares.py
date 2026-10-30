from __future__ import annotations

import logging
import os
import time
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
RATE_LIMIT_COOLDOWN_SECONDS = float(os.getenv("NPM_RETRY_COOLDOWN_SECONDS", "600") or 600)


class NpmRateLimitRetryMiddleware(RetryMiddleware):
    """Rate-limit aware retry for npm crawlers.

    npm answers crawlers with ``{"message":"Too Many Requests", ...}`` when a
    crawl gets chatty. Scrapy's default retry re-queues immediately, so this
    subclass sleeps a cool-down before re-trying after a rate-limit response.
    """

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._cool_down_pending = False

    def _retry(self, request: Request, reason: Any) -> Request | None:
        if getattr(self, "_cool_down_pending", False):
            time.sleep(RATE_LIMIT_COOLDOWN_SECONDS)
            self._cool_down_pending = False
        return super()._retry(request, reason)

    def process_response(self, request: Request, response: Response, spider: Any) -> Any:
        if (
            response.status == RATE_LIMIT_STATUS
            and not request.meta.get("dont_retry", False)
        ):
            # Rate limited: arm the cool-down; the retry will pause first.
            self._cool_down_pending = True
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
