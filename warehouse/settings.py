import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


BOT_NAME = "warehouse"
SPIDER_MODULES = ["warehouse.spiders"]
NEWSPIDER_MODULE = "warehouse.spiders"

SUPPORTED_ECOSYSTEMS = [
    "npm",
    "PyPI",
    "crates.io",
    "RubyGems",
    "NuGet",
    "Packagist",
    "Hex",
]

REQUIRED_FIELDS = ("ecosystem", "name", "sampling_category", "source", "collected_at")

ROBOTSTXT_OBEY = True

DOWNLOAD_DELAY = _env_float("SCRAPY_DOWNLOAD_DELAY", 0.5)
CONCURRENT_REQUESTS = _env_int("SCRAPY_CONCURRENT_REQUESTS", 8)
CONCURRENT_REQUESTS_PER_DOMAIN = _env_int("SCRAPY_CONCURRENT_PER_DOMAIN", 1)
DOWNLOAD_TIMEOUT = _env_float("SCRAPY_DOWNLOAD_TIMEOUT", 90)

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 0.5
AUTOTHROTTLE_MAX_DELAY = 10.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [408, 429, 500, 502, 503, 504, 522, 524]

# Selenium calls leave the reactor thread, so the pool must cover the browser pool.
REACTOR_THREADPOOL_MAXSIZE = _env_int("SCRAPY_THREADPOOL_SIZE", 8)

USER_AGENT = os.getenv(
    "SCRAPY_USER_AGENT",
    "hurtownie-warehouse/0.1 (academic package-security research)",
)
# robots.txt on npmjs.com sits behind the same bot check as the pages, so this
# request is deliberately indistinguishable from a normal browser fetch.
ROBOTSTXT_USER_AGENT = os.getenv(
    "SCRAPY_ROBOTS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36",
)

DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

BROWSER_OPTIONS = {
    # Headless Chrome is easier for Cloudflare to fingerprint; a real window
    # plus the persistent profile below is what survives the challenge.
    "headless": _env_bool("BROWSER_HEADLESS", False),
    "pool_size": _env_int("BROWSER_POOL_SIZE", 1),
    "window_size": os.getenv("BROWSER_WINDOW_SIZE", "1440,900"),
    "page_load_timeout": _env_float("BROWSER_PAGE_LOAD_TIMEOUT", 45.0),
    "binary_path": os.getenv("BROWSER_BINARY"),
    "driver_path": os.getenv("BROWSER_DRIVER"),
    "user_agent": os.getenv("BROWSER_USER_AGENT"),
    "profile_dir": os.getenv("BROWSER_PROFILE_DIR") or None,
    "stealth": _env_bool("BROWSER_STEALTH", True),
    "humanize": _env_bool("BROWSER_HUMANIZE", True),
    "challenge_timeout": _env_float("BROWSER_CHALLENGE_TIMEOUT", 25.0),
    "manual_challenge_timeout": _env_float(
        "BROWSER_MANUAL_CHALLENGE_TIMEOUT", 0.0
    ),
    "extra_args": [arg for arg in os.getenv("BROWSER_EXTRA_ARGS", "").split(",") if arg],
}

DOWNLOADER_MIDDLEWARES = {
    "warehouse.middlewares.SeleniumMiddleware": 543,
}

ITEM_PIPELINES = {
    "warehouse.pipelines.ValidationPipeline": 100,
    "warehouse.pipelines.DuplicatePackagePipeline": 200,
    "warehouse.pipelines.StoragePipeline": 300,
}

FEEDS = {
    "output/%(name)s/%(time)s.jsonl": {
        "format": "jsonlines",
        "encoding": "utf-8",
        "overwrite": False,
    },
}
FEED_EXPORT_ENCODING = "utf-8"

LOG_LEVEL = os.getenv("SCRAPY_LOG_LEVEL", "INFO")
if _env_bool("WAREHOUSE_LOG_JSON", False):
    LOG_FORMATTER = "warehouse.logging.JsonLogFormatter"

TELNETCONSOLE_ENABLED = False

NPM_POPULAR_COUNT = _env_int("NPM_POPULAR_COUNT", 25)
NPM_RANDOM_COUNT = _env_int("NPM_RANDOM_COUNT", 5)
NPM_SAMPLE_SEED = _env_int("NPM_SAMPLE_SEED", 1)
NPM_SEARCH_QUERIES = [
    query.strip()
    for query in os.getenv(
        "NPM_SEARCH_QUERIES", "framework,http,cli,testing,react"
    ).split(",")
    if query.strip()
]
# Versions are sampled across the rendered release history; "downloads" keeps
# only the most downloaded ones.
NPM_MAX_VERSIONS = _env_int("NPM_MAX_VERSIONS", 10)
NPM_VERSION_SELECTION = os.getenv("NPM_VERSION_SELECTION", "spread")
