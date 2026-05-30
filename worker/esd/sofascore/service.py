# esd/sofascore/service.py

from __future__ import annotations

import os
import logging
import subprocess
import sys
import json
import time
import random

from playwright.sync_api import sync_playwright
from ..utils import get_today
from .endpoints import SofascoreEndpoints
from .types import parse_events

logger = logging.getLogger(__name__)

def install_playwright():
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logger.info("✅ Playwright installed")
    except Exception as e:
        logger.warning(f"Install warning: {e}")

install_playwright()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/119 Safari/537.36",
]

def get_proxy():
    host = os.getenv("PROXY_HOST")
    port = os.getenv("PROXY_PORT")
    user = os.getenv("PROXY_USER")
    pwd = os.getenv("PROXY_PASS")

    if host and port:
        proxy = {"server": f"http://{host}:{port}"}
        if user and pwd:
            proxy["username"] = user
            proxy["password"] = pwd
        return proxy
    return None

def safe_fetch_json(page, url, retries=3):
    for attempt in range(retries):
        try:
            time.sleep(random.uniform(2.5, 5.5))
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            html = page.content()

            if any(x in html.lower() for x in ["access denied", "forbidden", "cloudflare", "blocked"]):
                raise Exception("BLOCKED_RESPONSE")

            text = page.evaluate("() => document.body.innerText")
            return json.loads(text)
        except Exception as e:
            logger.warning(f"Retry {attempt+1}/{retries}: {e}")
            time.sleep(2 ** attempt)

    logger.error(f"❌ FINAL FAIL: {url}")
    return None

class SofascoreService:

    def __init__(self, *args, **kwargs):
        self.logger = logger
        self.endpoints = SofascoreEndpoints()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._init_browser()

    def _init_browser(self):
        proxy = get_proxy()
        for attempt in range(3):
            try:
                self.logger.info(f"🌐 Browser init attempt {attempt+1}")
                self.playwright = sync_playwright().start()
                self.browser = self.playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
                )
                self.context = self.browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": random.randint(1100, 1400), "height": random.randint(700, 900)},
                    locale="en-US"
                )
                self.context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                """)
                self.page = self.context.new_page()
                self.logger.info("✅ Browser ready")
                return
            except Exception as e:
                self.logger.error(f"Browser error: {e}")
                time.sleep(3)
        raise RuntimeError("❌ Browser failed")

    def close(self):
        try:
            if self.page: self.page.close()
            if self.context: self.context.close()
            if self.browser: self.browser.close()
            if self.playwright: self.playwright.stop()
        except: pass

    def get_live_events(self):
        try:
            url = self.endpoints.live_events_endpoint
            data = safe_fetch_json(self.page, url)
            if not data or "events" not in data:
                self.logger.warning("⚠️ Live events blocked")
                return []
            return parse_events(data["events"])
        except Exception as e:
            self.logger.error(f"Live error: {e}")
            return []

    def get_events(self, date="today"):
        try:
            if date == "today": date = get_today()
            url = self.endpoints.events_endpoint.format(date=date)
            data = safe_fetch_json(self.page, url)
            if not data or "events" not in data:
                self.logger.warning("⚠️ Events blocked")
                return []
            return parse_events(data["events"])
        except Exception as e:
            self.logger.error(f"Events error: {e}")
            return []

    # ----------------------------------------------------------------------
    # ✅ FIX: INJECTED SYNCHRONIZED STATISTICS NETWORK ENDPOINT RETRIEVERS
    # ----------------------------------------------------------------------
    def get_raw_statistics(self, event_id: int) -> list[dict[str, any]]:
        """
        Fetches the raw statistics JSON array from match_stats_endpoint.
        """
        try:
            url = self.endpoints.match_stats_endpoint(int(event_id))
            data = safe_fetch_json(self.page, url)
            if not data or "statistics" not in data:
                self.logger.warning(f"⚠️ Statistics payload empty or blocked for event: {event_id}")
                return []
            return data["statistics"]
        except Exception as e:
            self.logger.error(f"Error fetching raw event stats for {event_id}: {e}")
            return []

    def get_raw_probabilities(self, event_id: int) -> dict[str, any]:
        """
        Fetches raw win/draw/away vote distribution using match_probabilities_endpoint.
        """
        try:
            url = self.endpoints.match_probabilities_endpoint(int(event_id))
            data = safe_fetch_json(self.page, url)
            if not data:
                self.logger.warning(f"⚠️ Probabilities payload empty or blocked for event: {event_id}")
                return {}
            return data
        except Exception as e:
            self.logger.error(f"Error fetching raw probabilities for {event_id}: {e}")
            return {}
