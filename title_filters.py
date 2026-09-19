from __future__ import annotations

import re
import unicodedata
from typing import List
from urllib.parse import urlparse


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def required_title_keywords(config: dict, streamer: dict) -> List[str]:
    """Return configured include-any keywords for this exact streamer/platform."""
    rules = config.get("recording", {}).get("title_include_rules", {})
    if not isinstance(rules, dict):
        return []

    rule = rules.get(str(streamer.get("name") or ""))
    if not isinstance(rule, dict) or rule.get("enabled", True) is False:
        return []

    required_platform = _normalize(rule.get("platform")).casefold()
    actual_platform = _normalize(streamer.get("platform")).casefold()
    if required_platform and required_platform != actual_platform:
        return []

    raw_keywords = rule.get("include_any", [])
    if isinstance(raw_keywords, str):
        raw_keywords = [raw_keywords]
    if not isinstance(raw_keywords, list):
        return []

    result = []
    seen = set()
    for raw in raw_keywords:
        keyword = _normalize(raw)
        key = keyword.casefold()
        if keyword and key not in seen:
            seen.add(key)
            result.append(keyword)
    return result


def title_is_allowed(config: dict, streamer: dict, title: object) -> bool:
    """A configured title gate requires a non-empty title matching any keyword."""
    keywords = required_title_keywords(config, streamer)
    if not keywords:
        return True
    normalized_title = _normalize(title).casefold()
    return bool(normalized_title) and any(
        _normalize(keyword).casefold() in normalized_title for keyword in keywords
    )


def title_is_definitive(streamer: dict, title: object) -> bool:
    """Reject SOOP's browser-tab label, which is not the broadcast title."""
    normalized_title = _normalize(title)
    if not normalized_title:
        return False
    if _normalize(streamer.get("platform")).casefold() != "soop":
        return True

    browser_labels = {f"{_normalize(streamer.get('name'))} - SOOP".casefold()}
    try:
        channel = next(
            (part for part in urlparse(str(streamer.get("url") or "")).path.split("/") if part),
            "",
        )
        if channel:
            browser_labels.add(f"{channel} - SOOP".casefold())
    except Exception:
        pass
    return normalized_title.casefold() not in browser_labels
