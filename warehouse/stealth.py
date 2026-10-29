"""Browser fingerprint hardening for sites that challenge automation.

The profile directory matters more than the patches: a persistent Chrome
profile keeps Cloudflare's clearance cookie, so a challenge usually has to be
passed once rather than on every request.

Patches are installed on prototypes, never on instances. Detection scripts
check for own properties such as ``Object.hasOwn(navigator, "webdriver")``,
which an instance-level override makes worse rather than better.
"""

from __future__ import annotations

import os
from typing import Any

STEALTH_JS = """
(() => {
  const proto = (target, prop, value) => {
    try {
      Object.defineProperty(target, prop, {
        get: () => value,
        configurable: true,
      });
    } catch (e) {}
  };

  // Chrome exposes this as a Navigator.prototype getter and reports false for
  // a normal browser; keep the same shape instead of adding an own property.
  proto(Navigator.prototype, 'webdriver', false);

  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
})();
"""


def default_profile_dir() -> str:
    base = os.getenv("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "hurtownie", "chrome-profile")


def apply_stealth(driver: Any) -> None:
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS}
    )
    driver.execute_cdp_cmd("Network.enable", {})


HEADER_PROBE_URL = "https://httpbin.org/headers"
FINGERPRINT_PROBE_URL = "https://bot.sannysoft.com/"
