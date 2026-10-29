from __future__ import annotations

from typing import Any

from scrapy import Request
from scrapy.http import Response

DEFAULT_NEXT_SELECTOR = (
    "a[rel='next']::attr(href), "
    "link[rel='next']::attr(href), "
    "li.next a::attr(href)"
)


def next_page_url(response: Response, selector: str = DEFAULT_NEXT_SELECTOR) -> str | None:
    href = response.css(selector).get()
    if not href:
        return None
    return response.urljoin(href.strip())


def next_page_request(
    response: Response,
    callback: Any,
    selector: str = DEFAULT_NEXT_SELECTOR,
    max_pages: int = 10,
    meta: dict[str, Any] | None = None,
    **request_kwargs: Any,
) -> Request | None:
    current_page = int((meta or {}).get("page", 1))
    if current_page >= max_pages:
        return None

    url = next_page_url(response, selector)
    if not url:
        return None

    next_meta = dict(meta or {})
    next_meta["page"] = current_page + 1
    return Request(
        url,
        callback=callback,
        meta=next_meta,
        **request_kwargs,
    )


def page_urls(
    response: Response, selector: str, limit: int | None = None
) -> list[str]:
    urls = [response.urljoin(href.strip()) for href in response.css(selector).getall()]
    return urls[:limit] if limit is not None else urls
