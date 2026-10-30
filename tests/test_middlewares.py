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


def _make_middleware() -> NpmRateLimitRetryMiddleware:
    middleware = NpmRateLimitRetryMiddleware(Settings())
    fake_crawler = SimpleNamespace(settings=Settings(), stats=_FakeStats())
    fake_spider = SimpleNamespace(crawler=fake_crawler)
    middleware.crawler = SimpleNamespace(spider=fake_spider, stats=_FakeStats(), settings=Settings())
    return middleware


def _response(request: Request, status: int) -> HtmlResponse:
    return HtmlResponse(url=request.url, body=b"", status=status, request=request)


def test_rate_limited_response_arms_cool_down_before_retry() -> None:
    middleware = _make_middleware()
    request = Request("https://registry.npmjs.org/react")
    response = _response(request, 429)

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware.process_response(request, response, None)

    sleep.assert_called_once()
    assert sleep.call_args[0][0] == 600
    assert isinstance(retry, Request)
    assert middleware._cool_down_pending is False


def test_retry_after_rate_limit_sleeps_cool_down_then_retries() -> None:
    middleware = _make_middleware()
    middleware._cool_down_pending = True
    request = Request("https://registry.npmjs.org/react")

    with mock.patch("warehouse.middlewares.time.sleep") as sleep:
        retry = middleware._retry(request, "429")

    sleep.assert_called_once()
    assert sleep.call_args[0][0] == 600
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
