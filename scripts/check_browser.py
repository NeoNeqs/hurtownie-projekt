"""Print the headers and fingerprint the browser presents to a test page."""

from __future__ import annotations

import json

from scrapy.utils.project import get_project_settings
from selenium.webdriver.common.by import By

from warehouse.browser import BrowserManager, BrowserOptions
from warehouse.stealth import FINGERPRINT_PROBE_URL, HEADER_PROBE_URL


def main() -> int:
    options = BrowserOptions.from_settings(get_project_settings())
    manager = BrowserManager(options)
    try:
        with manager.borrow() as session:
            session.open(HEADER_PROBE_URL)
            headers = json.loads(
                session.driver.find_element(By.TAG_NAME, "pre").text
            )["headers"]
            print("== request headers ==")
            for name, value in headers.items():
                print(f"{name}: {value}")

            print("\n== JavaScript fingerprint ==")
            for prop in ("webdriver", "languages", "platform", "vendor", "hardwareConcurrency"):
                print(f"navigator.{prop}: {session.driver.execute_script(f'return navigator.{prop}')}")
            print(f"window.chrome: {session.driver.execute_script('return typeof window.chrome')}")

            session.open(FINGERPRINT_PROBE_URL)
            session.sleep(3)
            failed = session.driver.find_elements(By.CSS_SELECTOR, "td.failed")
            print(f"\n== bot.sannysoft.com: {len(failed)} failed checks ==")
            for cell in failed:
                print(cell.get_attribute("textContent").strip())

            session.open("https://www.npmjs.com/package/react")
            print("\n== npmjs.com ==")
            print(f"title: {session.title}")
            print(f"challenge: {session.is_challenge()}")
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
