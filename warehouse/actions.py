from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from warehouse.browser import BrowserSession

if TYPE_CHECKING:
    from scrapy.http import Request

ScrollDirection = Literal["down", "up"]

Action = Callable[[BrowserSession, "Request"], None]


def compose(*actions: Action) -> Action:
    def combined(session: BrowserSession, request: "Request") -> None:
        for action in actions:
            action(session, request)

    return combined


def scroll_to_load(
    selector: str,
    directions: tuple[ScrollDirection, ...] = ("down",),
    wait: float = 1.0,
    max_steps: int = 15,
    settle_steps: int = 2,
    retries: int = 2,
    ready_wait: float = 30.0,
    return_to_top: bool = True,
) -> Action:
    def action(session: BrowserSession, request: "Request") -> None:
        session.wait_ready(ready_wait)
        before = session.count(selector)
        grew = False
        steps = 0

        for direction in directions:
            for attempt in range(retries + 1):
                grew_now, used = _scroll_until_stable(
                    session, selector, direction, wait, max_steps, settle_steps
                )
                steps += used
                if grew_now:
                    grew = True
                    break
                if attempt < retries:
                    session.sleep(wait + 1)

        if return_to_top:
            session.scroll_to_top()
            session.sleep(0.5)

        _record(
            request,
            action="scroll_to_load",
            selector=selector,
            before=before,
            after=session.count(selector),
            grew=grew,
            steps=steps,
        )

    return action


def _scroll_until_stable(
    session: BrowserSession,
    selector: str,
    direction: ScrollDirection,
    wait: float,
    max_steps: int,
    settle_steps: int,
) -> tuple[bool, int]:
    start = current = session.count(selector)
    stale = 0
    steps = 0

    for _ in range(max_steps):
        session.scroll(direction)
        session.sleep(wait)
        steps += 1

        new_count = session.count(selector)
        if new_count > current:
            stale = 0
            current = new_count
            continue

        stale += 1
        if stale >= settle_steps:
            break

    return current > start, steps


def click_until_gone(
    selector: str, wait: float = 1.0, max_clicks: int = 20
) -> Action:
    def action(session: BrowserSession, request: "Request") -> None:
        clicks = 0
        for _ in range(max_clicks):
            if not session.click_first(selector):
                break
            clicks += 1
            session.sleep(wait)
        _record(
            request,
            action="click_until_gone",
            selector=selector,
            clicks=clicks,
            exhausted=clicks < max_clicks,
        )

    return action


def click(selector: str, times: int = 1, wait: float = 1.0) -> Action:
    def action(session: BrowserSession, request: "Request") -> None:
        clicks = 0
        for _ in range(times):
            if not session.click_first(selector):
                break
            clicks += 1
            session.sleep(wait)
        _record(request, action="click", selector=selector, clicks=clicks)

    return action


def wait_seconds(seconds: float) -> Action:
    def action(session: BrowserSession, _request: "Request") -> None:
        session.sleep(seconds)

    return action


def _record(request: "Request", **entry: object) -> None:
    request.meta.setdefault("selenium_action_log", []).append(entry)
