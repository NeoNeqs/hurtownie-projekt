import re
from datetime import datetime, timezone

_WHITESPACE_RE = re.compile(r"\s+")


def strip_whitespace(value: object) -> str:
    return str(value).strip()


def collapse_whitespace(value: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


def normalize_url(value: object) -> str:
    url = str(value).strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def normalize_repository_url(value: object) -> str:
    url = normalize_url(value)
    if url.startswith("git+"):
        url = url[4:]
    if url.startswith("git://"):
        url = f"https://{url[6:]}"
    if url.endswith(".git"):
        url = url[:-4]
    return url


def normalize_license(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("type", "name", "spdx_id"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
        return ", ".join(parts) or None
    text = str(value).strip()
    return text or None


def parse_timestamp(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_package_name(value: object) -> str:
    return str(value).strip().lower()


def dedupe_key(ecosystem: str, name: str) -> tuple[str, str]:
    return ecosystem, normalize_package_name(name)
