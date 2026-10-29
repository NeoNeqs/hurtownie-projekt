from __future__ import annotations

from typing import Any

from scrapy import Request
from scrapy.http import HtmlResponse

from warehouse.actions import click, click_until_gone, scroll_to_load
from warehouse.pagination import next_page_request, next_page_url


class FakeSession:
    def __init__(self, growth: int = 3) -> None:
        self.growth = growth
        self.items = 1
        self.scrolls: list[str] = []
        self.sleeps: list[float] = []
        self.to_top = 0

    def wait_ready(self, timeout: float) -> None:
        pass

    def count(self, selector: str) -> int:
        return self.items

    def scroll(self, direction: str) -> None:
        self.scrolls.append(direction)
        if self.items < 1 + self.growth:
            self.items += 1

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def scroll_to_top(self) -> None:
        self.to_top += 1

    def click_first(self, selector: str) -> bool:
        if self.items == 0:
            return False
        self.items -= 1
        return True


def test_scroll_to_load_stops_when_the_list_stops_growing() -> None:
    session = FakeSession(growth=3)
    request = Request("https://example.test/list", meta={})

    scroll_to_load("div.item", wait=0, settle_steps=1)(session, request)

    assert session.items == 4
    assert len(session.scrolls) == 4
    assert session.to_top == 1
    log = request.meta["selenium_action_log"][0]
    assert log["before"] == 1
    assert log["after"] == 4
    assert log["grew"] is True


def test_scroll_to_load_records_failure_to_grow() -> None:
    session = FakeSession(growth=0)
    request = Request("https://example.test/list", meta={})

    scroll_to_load("div.item", wait=0, settle_steps=1, retries=0)(session, request)

    assert request.meta["selenium_action_log"][0]["grew"] is False


def test_click_until_gone_counts_clicks() -> None:
    session = FakeSession(growth=0)
    session.items = 2
    request = Request("https://example.test/list")

    click_until_gone("button.load", wait=0, max_clicks=5)(session, request)

    assert session.items == 0
    assert request.meta["selenium_action_log"][0]["clicks"] == 2


def test_click_records_missing_selector() -> None:
    session = FakeSession(growth=0)
    session.items = 0
    request = Request("https://example.test/list")

    click("button.missing", wait=0)(session, request)

    assert request.meta["selenium_action_log"][0]["clicks"] == 0


def page(html: str, url: str = "https://example.test/page/1") -> HtmlResponse:
    return HtmlResponse(url=url, body=html.encode(), encoding="utf-8")


def test_next_page_url_supports_rel_next_links() -> None:
    response = page('<a rel="next" href="/page/2">next</a>')
    assert next_page_url(response) == "https://example.test/page/2"


def test_next_page_url_returns_none_without_a_link() -> None:
    assert next_page_url(page("<p>last page</p>")) is None


def test_next_page_request_respects_max_pages() -> None:
    response = page('<a rel="next" href="/page/2">next</a>')

    request = next_page_request(response, callback=print, max_pages=5, meta={"page": 1})
    assert request is not None
    assert request.meta["page"] == 2

    assert next_page_request(response, callback=print, max_pages=2, meta={"page": 2}) is None
