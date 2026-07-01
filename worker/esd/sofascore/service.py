from __future__ import annotations

import os
import logging
import json
import time
import random
import requests

from ..utils import get_today
from .endpoints import HybridEndpoints
from .types import parse_events

logger = logging.getLogger("BetBot.Service")

class SofascoreService:
    """
    A low-latency network service utilizing connection pooling and custom signatures
    to extract data from SofaScore and LiveScore without heavy browser overhead.
    """

    def __init__(self, *args, **kwargs):
        self.logger = logger
        self.endpoints = HybridEndpoints()
        self.session = None
        self._init_session()

    def _init_session(self):
        """
        Initializes a long-lived pooled connection session equipped with proxy fallbacks.
        """
        try:
            self.session = requests.Session()
            
            # Configure Proxy Environment Credentials
            host = os.getenv("PROXY_HOST")
            port = os.getenv("PROXY_PORT")
            user = os.getenv("PROXY_USER")
            pwd = os.getenv("PROXY_PASS")

            if host and port:
                proxy_url = f"http://{user}:{pwd}@{host}:{port}" if user and pwd else f"http://{host}:{port}"
                self.session.proxies = {
                    "http": proxy_url,
                    "https": proxy_url
                }
                self.logger.info(f"🌐 Routed Service Network Session through proxy gateway: [{host}:{port}]")
            else:
                self.logger.info("🌐 Routing Service Network via standard host adapter (No proxy).")
                
            self.logger.info("✅ Connection session pool initialized successfully.")
        except Exception as e:
            self.logger.critical(f"💥 Failed to establish basic network session configurations: {e}")
            raise RuntimeError(f"Service initialization failed: {e}")

    def safe_fetch_json(self, url: str, params: dict, provider: str, retries: int = 3) -> dict | None:
        """
        Low-overhead HTTP connection routine with dynamic header profiles and error recovery.
        """
        profile = self.endpoints.get_provider_profile(provider)
        headers = profile["headers"]
        timeout = profile["timeout"]

        for attempt in range(retries):
            try:
                # Add light jitter variation to prevent precise behavioral signature tracking
                if attempt > 0:
                    time.sleep(random.uniform(1.0, 2.5) * attempt)

                response = self.session.get(url, headers=headers, params=params, timeout=timeout)
                
                if response.status_code == 403 or response.status_code == 429:
                    self.logger.warning(f"⚠️ [Attempt {attempt+1}/{retries}] Intercepted Provider Block ({response.status_code}) on {provider}.")
                    continue
                    
                response.raise_for_status()
                return response.json()
                
            except requests.exceptions.HTTPError as http_err:
                self.logger.warning(f"⚠️ [Attempt {attempt+1}/{retries}] HTTP Error calling {provider}: {http_err}")
            except Exception as e:
                self.logger.warning(f"⚠️ [Attempt {attempt+1}/{retries}] Connection anomaly encountered: {e}")

        self.logger.error(f"❌ Connection attempts exhausted. Core target unresolvable: [{url}]")
        return None

    def initialize(self):
        """
        Interface parity method ensuring compatibility with lifecycle controllers.
        """
        if not self.session:
            self._init_session()

    def close(self):
        """
        Closes connection pools and cleans up network resources.
        """
        if self.session:
            self.session.close()
            self.session = None
            self.logger.info("🧹 Service session connection adapters closed cleanly.")

    # ----------------------------------------------------------------------
    # 🔄 DATA STRUCTURE TRANSLATION ADAPTER FOR LIVESCORE
    # ----------------------------------------------------------------------
    def _normalize_livescore_events(self, livescore_data: dict) -> list:
        """
        Transforms Livescore data structure to match what parse_events() expects.
        """
        extracted_events = []
        if not livescore_data or "Stages" not in livescore_data:
            return extracted_events
            
        for stage in livescore_data["Stages"]:
            # Create a complete Stg object with all tournament data
            stage_data = {
                "Nm": stage.get("Nm", "Unknown Tournament"),
                "Tid": stage.get("Tid") or stage.get("CompId"),
                "Cid": stage.get("Cid") or stage.get("CategoryId"),
                "Cnm": stage.get("Cnm") or stage.get("Rgn") or stage.get("Country"),
                "Ccd": stage.get("Ccd")
            }
            
            for event in stage.get("Events", []):
                # Attach the full stage data to the event
                event["Stg"] = stage_data
                extracted_events.append(event)
                
        return parse_events(extracted_events)

    # ----------------------------------------------------------------------
    # ⚽ TRACKING CORE CAPABILITIES WITH ACTIVE AUTO-SWITCH OVER
    # ----------------------------------------------------------------------
    def get_live_events(self):
        try:
            url, params = self.endpoints.get_live_events_endpoint(provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data and "events" in data:
                return parse_events(data["events"])
            self.logger.warning("⚠️ SofaScore live events empty/blocked. Falling back to LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Live Fetch Failed: {e}. Trying LiveScore...")

        try:
            url, params = self.endpoints.get_live_events_endpoint(provider="livescore")
            data = self.safe_fetch_json(url, params, provider="livescore")
            return self._normalize_livescore_events(data)
        except Exception as e:
            self.logger.error(f"❌ Both engines completely failed for live data extraction: {e}")
            return []

    def get_events(self, date="today"):
        if date == "today": 
            date = get_today()
        
        try:
            url, params = self.endpoints.get_events_endpoint(date=date, provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data and "events" in data:
                return parse_events(data["events"])
            self.logger.warning(f"⚠️ SofaScore date blocked for {date}. Falling back to LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Date Fetch Failed: {e}. Trying LiveScore...")

        try:
            url, params = self.endpoints.get_events_endpoint(date=date, provider="livescore")
            data = self.safe_fetch_json(url, params, provider="livescore")
            return self._normalize_livescore_events(data)
        except Exception as e:
            self.logger.error(f"❌ Both engines completely failed for scheduling date {date}: {e}")
            return []

    def get_raw_statistics(self, event_id: int) -> dict | list:
        try:
            url, params = self.endpoints.match_stats_endpoint(int(event_id), provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data and "statistics" in data:
                return data["statistics"]
            self.logger.warning(f"⚠️ SofaScore statistics empty/blocked for ID {event_id}. Trying LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Stats Extraction Failure for ID {event_id}: {e}")

        try:
            url, params = self.endpoints.match_stats_endpoint(str(event_id), provider="livescore")
            data = self.safe_fetch_json(url, params, provider="livescore")
            return data if data else {}
        except Exception as e:
            self.logger.error(f"❌ Extraction methods exhausted. Stats for {event_id} failed: {e}")
            return {}

    def get_raw_probabilities(self, event_id: int) -> dict[str, any]:
        try:
            url, params = self.endpoints.match_probabilities_endpoint(int(event_id), provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data:
                return data
            self.logger.warning(f"⚠️ Probabilities context empty or blocked for event {event_id}.")
            return {}
        except Exception as e:
            self.logger.error(f"Error extracting SofaScore probability vectors for {event_id}: {e}")
            return {}

    def get_event(self, event_id: int) -> dict | None:
        """
        Fetch detailed event information for a specific match ID.
        Attempts SofaScore first, then falls back to LiveScore.
        """
        try:
            url, params = self.endpoints.event_endpoint(int(event_id), provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data:
                return data
            self.logger.warning(f"⚠️ SofaScore event {event_id} empty/blocked. Trying LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Event Fetch Failed for {event_id}: {e}")

        try:
            url, params = self.endpoints.event_endpoint(str(event_id), provider="livescore")
            data = self.safe_fetch_json(url, params, provider="livescore")
            return data
        except Exception as e:
            self.logger.error(f"❌ Both engines failed for event {event_id}: {e}")
            return None

    def get_player(self, player_id: int) -> dict | None:
        """
        Fetch detailed player information for a specific player ID.
        """
        try:
            url, params = self.endpoints.player_endpoint(int(player_id), provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data:
                return data
            self.logger.warning(f"⚠️ SofaScore player {player_id} empty/blocked. Trying LiveScore...")
        except Exception as e:
            self.logger.warning(f"SofaScore Player Fetch Failed for {player_id}: {e}")

        try:
            url, params = self.endpoints.player_endpoint(str(player_id), provider="livescore")
            data = self.safe_fetch_json(url, params, provider="livescore")
            return data
        except Exception as e:
            self.logger.error(f"❌ Both engines failed for player {player_id}: {e}")
            return None

    def search(self, query: str, entity: str = "all") -> list:
        """
        Search for entities across both providers.
        """
        results = []
        
        # Try SofaScore
        try:
            url, params = self.endpoints.search_endpoint(query, entity, provider="sofascore")
            data = self.safe_fetch_json(url, params, provider="sofascore")
            if data and "results" in data:
                results.extend(data["results"])
        except Exception as e:
            self.logger.warning(f"SofaScore search failed: {e}")

        # Try LiveScore
        try:
            url, params = self.endpoints.search_endpoint(query, entity, provider="livescore")
            data = self.safe_fetch_json(url, params, provider="livescore")
            if data and "results" in data:
                results.extend(data["results"])
        except Exception as e:
            self.logger.warning(f"LiveScore search failed: {e}")

        return results

    def get_tournament(self, tournament_id: int) -> dict | None:
        """
        Fetch tournament details including country information.
        """
        try:
            # Try to get tournament details from SofaScore
            url = f"{self.endpoints.sofa_base}/unique-tournament/{tournament_id}"
            data = self.safe_fetch_json(url, {}, provider="sofascore")
            if data:
                return data
        except Exception as e:
            self.logger.warning(f"SofaScore tournament fetch failed: {e}")

        try:
            # Try LiveScore
            url = f"{self.endpoints.live_base}/competitions/soccer/{tournament_id}"
            data = self.safe_fetch_json(url, {}, provider="livescore")
            return data
        except Exception as e:
            self.logger.error(f"Both engines failed for tournament {tournament_id}: {e}")
            return None