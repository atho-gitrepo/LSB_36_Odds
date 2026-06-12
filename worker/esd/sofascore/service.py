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
from .endpoints import HybridEndpoints
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
        self.endpoints = HybridEndpoints()
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

    # ----------------------------------------------------------------------
    # 🔄 DATA STRUCTURE TRANSLATION ADAPTER FOR LIVESCORE
    # ----------------------------------------------------------------------
    def _normalize_livescore_events(self, livescore_data: dict) -> list:
        """
        Extracts raw items from nested stages to pass them directly 
        to the universal hybrid parse_events pipeline.
        """
        extracted_events = []
        if not livescore_data or "Stages" not in livescore_data:
            return extracted_events
            
        for stage in livescore_data["Stages"]:
            for event in stage.get("Events", []):
                event["Stg"] = {"Nm": stage.get("Nm", "Unknown Tournament")}
                extracted_events.append(event)
                
        return parse_events(extracted_events)

    # ----------------------------------------------------------------------
    # ⚽ TRACKING CORE CAPABILITIES WITH ACTIVE AUTO-SWITCH OVER
    # ----------------------------------------------------------------------
    def get_live_events(self):
        try:
            url = self.endpoints.get_live_events_endpoint(provider="sofascore")
            data = safe_fetch_json(self.page, url)
            if data and "events" in data:
                return parse_events(data["events"])
            self.logger.warning("⚠️ SofaScore live events empty/blocked. Falling back to LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Live Fetch Failed: {e}. Trying LiveScore...")

        try:
            url = self.endpoints.get_live_events_endpoint(provider="livescore")
            data = safe_fetch_json(self.page, url)
            return self._normalize_livescore_events(data)
        except Exception as e:
            self.logger.error(f"❌ Both engines completely failed for live data extraction: {e}")
            return []

    def get_events(self, date="today"):
        if date == "today": 
            date = get_today()
        
        try:
            url = self.endpoints.get_events_endpoint(date=date, provider="sofascore")
            data = safe_fetch_json(self.page, url)
            if data and "events" in data:
                return parse_events(data["events"])
            self.logger.warning(f"⚠️ SofaScore date blocked for {date}. Falling back to LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Date Fetch Failed: {e}. Trying LiveScore...")

        try:
            livescore_date = date.replace("-", "")
            url = self.endpoints.get_events_endpoint(date=livescore_date, provider="livescore")
            data = safe_fetch_json(self.page, url)
            return self._normalize_livescore_events(data)
        except Exception as e:
            self.logger.error(f"❌ Both engines completely failed for scheduling date {date}: {e}")
            return []

    def get_raw_statistics(self, event_id: int) -> dict | list:
        try:
            url = self.endpoints.match_stats_endpoint(int(event_id), provider="sofascore")
            data = safe_fetch_json(self.page, url)
            if data and "statistics" in data:
                return data["statistics"]
            self.logger.warning(f"⚠️ SofaScore statistics empty/blocked for ID {event_id}. Trying LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Stats Extraction Failure for ID {event_id}: {e}")

        try:
            url = self.endpoints.match_stats_endpoint(str(event_id), provider="livescore")
            data = safe_fetch_json(self.page, url)
            return data if data else {}
        except Exception as e:
            self.logger.error(f"❌ Extraction methods exhausted. Stats for {event_id} failed: {e}")
            return {}

    def get_raw_probabilities(self, event_id: int) -> dict[str, any]:
        try:
            url = self.endpoints.match_probabilities_endpoint(int(event_id), provider="sofascore")
            data = safe_fetch_json(self.page, url)
            if data:
                return data
            self.logger.warning(f"⚠️ Probabilities context empty or blocked for event {event_id}.")
        except Exception as e:
            self.logger.error(f"Error extracting SofaScore probability vectors for {event_id}: {e}")
            
        return {}
