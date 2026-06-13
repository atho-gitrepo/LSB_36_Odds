"""
Utility functions for Sofascore bot (Optimized API Version - Non-Blocking)
"""

import re
import time
import json
import logging
from datetime import datetime
import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------
# DATE HELPERS
# --------------------------------------------------

def get_today() -> str:
    """Return current date as YYYY-MM-DD"""
    return time.strftime("%Y-%m-%d")


def current_year(shift: int = 0) -> int:
    """Return current year with optional shift"""
    return datetime.now().year + shift


# --------------------------------------------------
# STRING UTILS
# --------------------------------------------------

def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case"""
    return re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    ).lower()


# --------------------------------------------------
# SAFE JSON FETCH (LIGHTWEIGHT REQUESTS CONVERSION)
# --------------------------------------------------

def get_json(url: str, timeout: int = 30) -> dict:
    """
    Fetch JSON cleanly using requests connection pools.
    Signature adapted to eliminate Playwright footprint dependencies.

    Args:
        url (str): API URL
        timeout (int): request timeout in seconds

    Returns:
        dict: parsed JSON response

    Raises:
        RuntimeError: if blocked or invalid response
    """
    # Standard enterprise user-agent to mask programmatic footprints
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=timeout)

        # 🚨 Detect blocking
        if response.status_code in [403, 429]:
            raise RuntimeError(f"🚫 Blocked by provider. Status Code: {response.status_code}")

        if not response.text:
            raise RuntimeError("❌ Empty response body")

        # Parse JSON
        try:
            data = response.json()
        except Exception:
            raise RuntimeError("❌ Invalid JSON response parsed from target body stream")

        # Optional API error handling
        if isinstance(data, dict) and "error" in data:
            logger.warning(f"Sofascore API error: {data['error']}")
            return {}

        return data

    except Exception as e:
        logger.error(f"get_json error: {e}")
        raise


# --------------------------------------------------
# VALIDATION
# --------------------------------------------------

def is_available_date(date: str, pattern: str) -> None:
    """
    Validate date format

    Raises:
        ValueError if invalid
    """
    date_pattern = re.compile(pattern)

    if date_pattern.match(date):
        datetime.strptime(date, "%d-%m-%Y")
    else:
        raise ValueError("Invalid date format")
