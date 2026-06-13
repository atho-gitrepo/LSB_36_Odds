"""
Utility functions for Sofascore bot (safe version - Playwright only)
"""

import re
import time
import json
import logging
from datetime import datetime
from playwright.sync_api import Page

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
# SAFE JSON FETCH (PLAYWRIGHT ONLY)
# --------------------------------------------------

def get_json(page: Page, url: str, timeout: int = 30000) -> dict:
    """
    Fetch JSON safely using Playwright (ANTI-BLOCK VERSION)

    Args:
        page (Page): Playwright page instance (REQUIRED)
        url (str): API URL
        timeout (int): request timeout

    Returns:
        dict: parsed JSON response

    Raises:
        RuntimeError: if blocked or invalid response
    """

    if page is None:
        raise RuntimeError("❌ Playwright page is required (direct API disabled)")

    try:
        # Navigate safely
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")

        content = page.content()

        # 🚨 Detect blocking
        if any(x in content.lower() for x in ["access denied", "forbidden", "cloudflare"]):
            raise RuntimeError("🚫 Blocked by Sofascore")

        # Extract JSON text
        text = page.evaluate("() => document.body.innerText")

        if not text:
            raise RuntimeError("❌ Empty response body")

        # Parse JSON
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("❌ Invalid JSON response")

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
