from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, ClassVar

from scrapy import Request, Spider
from scrapy.http import Response

from warehouse.items import PackageItem
from warehouse.loaders import PackageLoader


class BrowserSpider(Spider):
    ecosystem: ClassVar[str] = ""
    source: ClassVar[str] = ""
    selenium_meta: ClassVar[dict[str, Any]] = {"selenium": True, "selenium_wait": 0}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.collection_errors: list[dict[str, Any]] = []

    async def start(self) -> AsyncIterator[Request]:
        for url in self.start_urls:
            yield self.browser_request(url)

    def browser_request(
        self,
        url: str,
        callback: Any = None,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Request:
        request_meta = dict(self.selenium_meta)
        request_meta["selenium"] = True
        if meta:
            request_meta.update(meta)
        errback = kwargs.pop("errback", self.errback)
        return Request(
            url,
            callback=callback or self.parse,
            errback=errback,
            meta=request_meta,
            **kwargs,
        )

    def load_package(self, ref: dict[str, Any]) -> PackageItem:
        loader = PackageLoader(item=PackageItem())
        for key, value in ref.items():
            if value is not None:
                loader.add_value(key, value)
        return loader.load_item()

    def parse(self, response: Response) -> Any:
        raise NotImplementedError(f"{type(self).__name__}.parse is not implemented")

    def errback(self, failure: Any) -> None:
        request = failure.request
        self.record_error(
            url=request.url,
            stage="download",
            error_type=type(failure.value).__name__,
            message=str(failure.value),
        )
        self.logger.error("Request failed: %s (%s)", request.url, failure.value)

    def record_error(
        self,
        url: str | None,
        stage: str,
        error_type: str,
        message: str,
    ) -> None:
        self.collection_errors.append(
            {
                "url": url,
                "stage": stage,
                "error_type": error_type,
                "message": message,
                "occurred_at": datetime.now(timezone.utc),
            }
        )
