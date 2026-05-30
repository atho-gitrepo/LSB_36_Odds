import requests
import os
import json
import time
import logging
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
from esd.sofascore import SofascoreClient

# --- DETAILED LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | [%(levelname)s] | %(message)s',
    handlers=[logging.FileHandler("win_prob_prematch.log"), logging.StreamHandler()]
)
logger = logging.getLogger("WinProbPreMatchBot")

# --- ENV VARS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS_JSON", "")

# --- CONFIGURATION SETTINGS ---
MIN_PROBABILITY_THRESHOLD = 70.0  
CHECK_INTERVAL_HOURS = 6 # How often to scan for the day's upcoming matches

# --- LEAGUE FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League', 'Copa Colombia']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u1', 'u23', 'u21', 'u20', 'women', 'college']

# =========================
# FIREBASE STORAGE
# =========================
class FirebaseManager:
    def __init__(self, creds_json):
        self.db = None
        if not creds_json:
            logger.error("[FIREBASE_INIT] ❌ Firebase Credentials missing!")
            return
        try:
            cred_dict = json.loads(creds_json)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("✅ [FIREBASE_INIT] Firestore client successfully connected.")
        except Exception as e:
            logger.error(f"❌ [FIREBASE_INIT] Firebase initialization failure: {e}", exc_info=True)

    def add_prematch_prediction(self, match_id, data):
        """Saves high probability pre-match selections."""
        try:
            if not self.db: return
            data['logged_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            data['status'] = 'PENDING_KICKOFF'
            self.db.collection('prematch_prob_bets').document(str(match_id)).set(data)
            logger.info(f"[DB_WRITE] ✅ Logged pre-match favorite for ID: {match_id}")
        except Exception as e:
            logger.error(f"[DB_WRITE] ❌ Error writing pre-match doc: {e}", exc_info=True)

    def is_match_already_logged(self, match_id):
        """Prevents logging/sending the same pre-match game multiple times."""
        if not self.db: return False
        doc = self.db.collection('prematch_prob_bets').document(str(match_id)).get()
        return doc.exists

# =========================
# TELEGRAM NOTIFIER
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
        if response.status_code == 200:
            logger.info("[TELEGRAM_API] ✅ Pre-match notification delivered.")
        else:
            logger.error(f"[TELEGRAM_API] ❌ Selection rejected: {response.text}")
    except Exception as e:
        logger.error(f"[TELEGRAM_API] ❌ Network error: {e}", exc_info=True)

# ==========================================
# PRE-MATCH ANALYSIS CORE
# ==========================================
def process_prematch_game(match):
    fid = str(match.id)
    
    # 1. Skip if it's already live or finished
    status_desc = match.status.description.upper() if match.status else "UNKNOWN"
    if status_desc not in ['NOT STARTED', 'SCHEDULED']:
        logger.debug(f"[SKIP] Match {fid} skipped because it has already started/ended (Status: {status_desc})")
        return

    # 2. Check Database Duplication Guardrail
    if firebase_manager.is_match_already_logged(fid):
        logger.debug(f"[SKIP] Match {fid} already parsed and logged previously.")
        return

    # 3. Parse Metadata safely
    try:
        league = match.tournament.name if match.tournament else "Unknown League"
        country = match.tournament.category.name if match.tournament and match.tournament.category else "Unknown Country"
        match_name = f"{match.home_team.name} vs {match.away_team.name}" if match.home_team and match.away_team else f"Match ID {fid}"
        kickoff_ts = match.start_timestamp if hasattr(match, 'start_timestamp') else None
        kickoff_time = datetime.utcfromtimestamp(kickoff_ts).strftime('%Y-%m-%d %H:%M UTC') if kickoff_ts else "Unknown"
    except Exception as e:
        logger.error(f"[PARSE_ERROR] Failed parsing basic fields on match {fid}: {e}")
        return

    # 4. Filter Leagues
    full_info = f"{league} {country}".lower()
    if not any(x.lower() in league.lower() for x in ALLOWED_LEAGUES):
        if any(x.lower() in full_info for x in EXCLUDED_LEAGUES + AMATEUR_KEYWORDS):
            return

    # 5. Fetch Pre-Match Probabilities
    logger.info(f"[PREMATCH_CHECK] Fetching pre-match outlook for: {match_name} ({fid})...")
    try:
        stats_data = SOFASCORE_CLIENT.get_stats(int(fid))
        if not stats_data or not hasattr(stats_data, 'win_probability') or stats_data.win_probability is None:
            logger.warning(f"[PREMATCH_CHECK] Win probabilities unavailable for pre-match game: {fid}")
            return

        prob = stats_data.win_probability
        home_prob = float(prob.home) if prob.home is not None else 0.0
        draw_prob = float(prob.draw) if prob.draw is not None else 0.0
        away_prob = float(prob.away) if prob.away is not None else 0.0

        dominant_team = None
        target_side = None
        highest_prob = 0.0

        if home_prob >= MIN_PROBABILITY_THRESHOLD:
            dominant_team = match.home_team.name
            target_side = 'HOME'
            highest_prob = home_prob
        elif away_prob >= MIN_PROBABILITY_THRESHOLD:
            dominant_team = match.away_team.name
            target_side = 'AWAY'
            highest_prob = away_prob

        # 6. Trigger if high-confidence pre-match selection is found
        if dominant_team:
            logger.info(f"🔥 [PRE-MATCH TRIGGER] {dominant_team} discovered with {highest_prob}% pre-match expectation.")
            
            payload = {
                'match_name': match_name,
                'league': league,
                'country': country,
                'kickoff_time': kickoff_time,
                'dominant_team': dominant_team,
                'target_side': target_side,
                'predicted_probability': highest_prob,
                'probabilities_prematch': {
                    'home': home_prob,
                    'draw': draw_prob,
                    'away': away_prob
                }
            }
            
            firebase_manager.add_prematch_prediction(fid, payload)
            
            send_telegram(
                f"📋 **PRE-MATCH HIGH PROBABILITY SELECTION**\n"
                f"🏟 {match_name}\n"
                f"🏆 {league} ({country})\n"
                f"⏰ Kickoff: `{kickoff_time}`\n\n"
                f"🔥 **Pre-Match Favorite:** {dominant_team}\n"
                f"📈 **Win Probability:** `{highest_prob:.1f}%` \n\n"
                f"📊 *Odds Split Consensus:*\n"
                f"🏠 Home: {home_prob:.1f}% | 🤝 Draw: {draw_prob:.1f}% | 🚌 Away: {away_prob:.1f}%"
            )

    except Exception as e:
        logger.error(f"[PREMATCH_CHECK] ❌ Error requesting analytics engine for ID {fid}: {e}", exc_info=True)

# =========================
# RUNTIME ENVIRONMENT CONTROLS
# =========================
def run_prematch_scan_cycle():
    if not SOFASCORE_CLIENT: return
    try:
        logger.info("[SCAN_CYCLE] Fetching today's full scheduled football fixtures...")
        # live=False pulls the pre-match daily schedule dictionary layout
        events = SOFASCORE_CLIENT.get_events(date="today", live=False)
        if not events:
            logger.info("[SCAN_CYCLE] No scheduled fixtures returned for today.")
            return
            
        logger.info(f"[SCAN_CYCLE] Scanning through {len(events)} pre-match fixtures against constraints...")
        for match in events:
            process_prematch_game(match)
            # Short sleep to space out requests and mitigate Cloudflare rate limits/blocks
            time.sleep(1.0)
            
    except Exception as e:
        logger.error(f"[SCAN_CYCLE] ❌ Error during execution: {e}", exc_info=True)

if __name__ == "__main__":
    logger.info("🎬 Launching Pre-Match Win Probability Bot...")
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)
    
    try:
        SOFASCORE_CLIENT = SofascoreClient()
        SOFASCORE_CLIENT.initialize()
        
        while True:
            run_prematch_scan_cycle()
            logger.info(f"[SLEEP] Cycle complete. Next full pre-match scan in {CHECK_INTERVAL_HOURS} hours.")
            time.sleep(CHECK_INTERVAL_HOURS * 3600)
            
    except (KeyboardInterrupt, SystemExit):
        logger.info("[EXIT] Shutting down gracefully.")
        if SOFASCORE_CLIENT: SOFASCORE_CLIENT.close()
