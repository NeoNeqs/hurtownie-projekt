from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse, Response
from scrapy.settings import Settings

from warehouse.middlewares import NpmRateLimitRetryMiddleware, RATE_LIMIT_STATUS


class _FakeStats:
    def inc_value(self, key: str, count: int = 1) -> None:
        return None


def _make_middleware(**settings: object) -> NpmRateLimitRetryMiddleware:
    values = Settings()
    for key, value in settings.items():
        values.set(key, value)
    middleware = NpmRateLimitRetryMiddleware(values)
    fake_crawler = SimpleNamespace(settings=values, stats=_FakeStats())
    fake_spider = SimpleNamespace(crawler=fake_crawler)
    middleware.crawler = SimpleNamespace(
        spider=fake_spider, stats=_FakeStats(), settings=values
    )
    return middleware


def _response(
    request: Request, status: int, headers: dict[str, str] | None = None
) -> HtmlResponse:
    return HtmlResponse(
        url=request.url,
        body=b"",
        status=status,
        headers=headers or {},
        request=request,
    )


def test_rate_limited_response_arms_cool_down_before_retry() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    response = _response(request, 429)

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware.process_response(request, response, None)

    sleep.assert_called_once()
    delay = sleep.call_args[0][0]
    assert 480 <= delay <= 720  # 600 s base with 20% jitter
    assert isinstance(retry, Request)
    assert "npm_rate_limit_pending" not in request.meta
    assert request.meta["npm_rate_limit_attempts"] == 1


def test_retry_after_rate_limit_uses_server_delay() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    response = _response(request, 429, {"Retry-After": "120"})

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware.process_response(request, response, None)

    sleep.assert_called_once_with(120.0)
    assert isinstance(retry, Request)


def test_consecutive_rate_limits_back_off_exponentially() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    request.meta["npm_rate_limit_attempts"] = 2

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        middleware.process_response(request, _response(request, 429), None)

    delay = sleep.call_args[0][0]
    assert 1920 <= delay <= 2880  # 600 * 2**2 with jitter
    assert request.meta["npm_rate_limit_attempts"] == 3


def test_backoff_is_capped() -> None:
    middleware = _make_middleware(NPM_RETRY_MAX_COOLDOWN_SECONDS=900)
    request = Request("https://registry.npmjs.org/react")
    request.meta["npm_rate_limit_attempts"] = 8

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        middleware.process_response(request, _response(request, 429), None)

    delay = sleep.call_args[0][0]
    assert delay == 900


def test_retry_after_date_is_converted_to_seconds() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    response = _response(
        request, 429, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}
    )

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        middleware.process_response(request, response, None)

    # A date far in the future is capped at the configured maximum.
    sleep.assert_called_once_with(3600.0)


def test_rate_limit_retries_forever_by_default() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    request.meta["npm_rate_limit_pending"] = True
    request.meta["npm_rate_limit_attempts"] = 50

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware._retry(request, "429")

    sleep.assert_called_once()
    assert isinstance(retry, Request)
    assert request.meta["max_retry_times"] >= 10**9


def test_total_cool_down_cap_gives_up_instead_of_retrying() -> None:
    middleware = _make_middleware(NPM_RETRY_MAX_TOTAL_COOLDOWN_SECONDS=1000)
    request = Request("https://registry.npmjs.org/react")
    request.meta["npm_rate_limit_pending"] = True
    request.meta["npm_rate_limit_attempts"] = 3
    request.meta["npm_rate_limit_total_sleep"] = 900.0

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware._retry(request, "429")

    sleep.assert_not_called()
    assert retry is None


def test_retry_after_rate_limit_sleeps_cool_down_then_retries() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    request.meta["npm_rate_limit_pending"] = True

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware._retry(request, "429")

    sleep.assert_called_once()
    delay = sleep.call_args[0][0]
    assert 480 <= delay <= 720
    assert request.meta["max_retry_times"] >= 10
    assert isinstance(retry, Request)


def test_other_responses_pass_through_without_cool_down() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    response = _response(request, 200)

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        result = middleware.process_response(request, response, None)

    sleep.assert_not_called()
    assert isinstance(result, Response)
    assert result.status == 200
