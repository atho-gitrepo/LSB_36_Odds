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
    handlers=[logging.FileHandler("win_prob_prematch_trace.log"), logging.StreamHandler()]
)
logger = logging.getLogger("WinProbPreMatchBot")

# --- ENV VARS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS_JSON", "")

# --- CONFIGURATION SETTINGS ---
MIN_PROBABILITY_THRESHOLD = 70.0  
CHECK_INTERVAL_HOURS = 6 

# --- LEAGUE FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League', 'Copa Colombia']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u1', 'u23', 'u21', 'u20', 'women', 'college']

# Global instances for compatibility tracking
firebase_manager = None
SOFASCORE_CLIENT = None

# =========================
# FIREBASE STORAGE
# =========================
class FirebaseManager:
    def __init__(self, creds_json):
        self.db = None
        if not creds_json:
            logger.error("[FIREBASE_INIT] ❌ Firebase Credentials missing in environment variables!")
            return
        try:
            logger.info("[FIREBASE_INIT] Attempting to parse credentials JSON string...")
            cred_dict = json.loads(creds_json)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
                logger.info("[FIREBASE_INIT] Firebase SDK App initialized successfully.")
            self.db = firestore.client()
            logger.info("✅ [FIREBASE_INIT] Firestore client successfully connected.")
        except Exception as e:
            logger.error(f"❌ [FIREBASE_INIT] Critical Firebase initialization failure: {e}", exc_info=True)

    def add_prematch_prediction(self, match_id, data):
        """Saves high probability pre-match selections into Firestore."""
        try:
            if not self.db:
                logger.warning(f"[DB_WRITE] ⚠️ Database connection not ready. Skipping logging for {match_id}")
                return
            data['logged_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            data['status'] = 'PENDING_KICKOFF'
            logger.info(f"[DB_WRITE] Writing pre-match favorite details for match {match_id}...")
            self.db.collection('prematch_prob_bets').document(str(match_id)).set(data)
            logger.info(f"[DB_WRITE] ✅ Successfully wrote match {match_id} to prematch_prob_bets collection.")
        except Exception as e:
            logger.error(f"[DB_WRITE] ❌ Error writing pre-match configuration document: {e}", exc_info=True)

    def is_match_already_logged(self, match_id):
        """Prevents processing or notifying the same pre-match game multiple times."""
        try:
            if not self.db: return False
            logger.debug(f"[DB_READ] Checking duplication for match ID: {match_id}")
            doc = self.db.collection('prematch_prob_bets').document(str(match_id)).get()
            return doc.exists
        except Exception as e:
            logger.error(f"[DB_READ] ❌ Error fetching duplication check values for ID {match_id}: {e}", exc_info=True)
            return False

# =========================
# TELEGRAM NOTIFIER
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    logger.info("[TELEGRAM_API] Dispatching outgoing alert payload message packet...")
    try:
        response = requests.post(
            url,
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
        if response.status_code == 200:
            logger.info("[TELEGRAM_API] ✅ Selection notification delivered successfully.")
        else:
            logger.error(f"[TELEGRAM_API] ❌ Selection rejected by API endpoint: {response.text}")
    except Exception as e:
        logger.error(f"[TELEGRAM_API] ❌ Network level transport exception encountered: {e}", exc_info=True)

# ==========================================
# PRE-MATCH ANALYSIS CORE
# ==========================================
def process_prematch_game(match):
    fid = str(match.id)
    
    # 1. Skip if it's already live or finished
    try:
        status_desc = match.status.description.upper() if match.status else "UNKNOWN"
        if status_desc not in ['NOT STARTED', 'SCHEDULED']:
            logger.debug(f"[SKIP] Match {fid} skipped because it has already kicked off (Status: {status_desc})")
            return
    except Exception as e:
        logger.error(f"[MATCH_LIVE_ATTR] ❌ Error processing status checks for ID {fid}: {e}")
        return

    # 2. Check Database Duplication Guardrail
    if firebase_manager and firebase_manager.is_match_already_logged(fid):
        logger.debug(f"[SKIP] Match {fid} already parsed and stored previously. Skipping lookup.")
        return

    # 3. Parse Metadata safely
    try:
        league = match.tournament.name if match.tournament else "Unknown League"
        country = match.tournament.category.name if match.tournament and match.tournament.category else "Unknown Country"
        match_name = f"{match.home_team.name} vs {match.away_team.name}" if match.home_team and match.away_team else f"Match ID {fid}"
        kickoff_ts = match.start_timestamp if hasattr(match, 'start_timestamp') else None
        kickoff_time = datetime.utcfromtimestamp(kickoff_ts).strftime('%Y-%m-%d %H:%M UTC') if kickoff_ts else "Unknown"
    except Exception as e:
        logger.error(f"[PARSE_ERROR] Failed parsing basic structural attributes on match {fid}: {e}")
        return

    # 4. Filter Leagues
    full_info = f"{league} {country}".lower()
    if not any(x.lower() in league.lower() for x in ALLOWED_LEAGUES):
        if any(x.lower() in full_info for x in EXCLUDED_LEAGUES + AMATEUR_KEYWORDS):
            logger.debug(f"[FILTER] Match {match_name} ({fid}) bypassed by tracking exclusion filters.")
            return

    # 5. Fetch Pre-Match Probabilities
    logger.info(f"[PREMATCH_CHECK] Evaluating pre-match matrix for: {match_name} ({fid})...")
    try:
        stats_data = SOFASCORE_CLIENT.get_stats(int(fid))
        if not stats_data:
            logger.warning(f"[PREMATCH_CHECK] Client returned empty stats container wrapper object for {match_name} ({fid})")
            return
            
        if not hasattr(stats_data, 'win_probability') or stats_data.win_probability is None:
            logger.warning(f"[PREMATCH_CHECK] Win probability metrics unavailable for pre-match game ID: {fid}")
            return

        prob = stats_data.win_probability
        home_prob = float(prob.home) if prob.home is not None else 0.0
        draw_prob = float(prob.draw) if prob.draw is not None else 0.0
        away_prob = float(prob.away) if prob.away is not None else 0.0

        logger.info(f"[PREMATCH_CHECK] Values loaded for {match_name} -> Home: {home_prob}%, Draw: {draw_prob}%, Away: {away_prob}%")

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
        if dominant_team and firebase_manager:
            logger.info(f"🔥 [PRE-MATCH TRIGGER] High confidence threshold passed! {dominant_team} has {highest_prob}% pre-match expectation.")
            
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
        logger.error(f"[PREMATCH_CHECK] ❌ Exception occurred parsing deep client statistics engine for ID {fid}: {e}", exc_info=True)

# ========================================
# RUNTIME ENVIRONMENT CYCLE CONTROLLER
# ========================================
def run_prematch_scan_cycle():
    if not SOFASCORE_CLIENT: 
        logger.error("[CYCLE_EXEC] ❌ Aborting routine loop cycle: Client object reference is set to None.")
        return
    try:
        logger.info("[CYCLE_EXEC] Querying network layer for today's scheduled football fixtures...")
        events = SOFASCORE_CLIENT.get_events(date="today", live=False)
        if not events:
            logger.info("[CYCLE_EXEC] No scheduled fixtures returned for today.")
            return
            
        logger.info(f"[CYCLE_EXEC] Scanning through {len(events)} pre-match fixtures against constraints...")
        for match in events:
            process_prematch_game(match)
            time.sleep(1.2)
            
    except Exception as e:
        logger.error(f"[CYCLE_EXEC] ❌ Error caught inside the active loop execution processing thread: {e}", exc_info=True)

# ======================================================================
# 🔄 BACKWARD COMPATIBILITY ALIASES & ORCHESTRATION LINKS FOR MAIN.PY
# ======================================================================
SLEEP_TIME = CHECK_INTERVAL_HOUES = CHECK_INTERVAL_HOURS * 3600 if 'CHECK_INTERVAL_HOURS' in locals() else 6 * 3600
SLEEP_TIME = CHECK_INTERVAL_HOURS * 3600
run_bot_cycle = run_prematch_scan_cycle

def initialize_bot_services():
    """
    Exposed wrapper hook expected by main.py setup cycles.
    Prepares both Firestore database mappings and web-scraping components.
    """
    global firebase_manager, SOFASCORE_CLIENT
    logger.info("[INIT] Executing main.py orchestrated setup sequence...")
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)
    try:
        logger.info("[INIT] Bootstrapping automated tracking browser engines...")
        SOFASCORE_CLIENT = SofascoreClient()
        SOFASCORE_CLIENT.initialize()
        logger.info("✅ [INIT] Global tracking contexts are armed and online.")
        return True
    except Exception as e:
        logger.error(f"❌ [INIT] Critical initialization error: {e}", exc_info=True)
        return False

def shutdown_bot():
    logger.info("[SHUTDOWN] Terminating runtime infrastructure tasks...")
    if SOFASCORE_CLIENT:
        try: 
            SOFASCORE_CLIENT.close()
            logger.info("[SHUTDOWN] ✅ Browser client process pools closed down cleanly.")
        except Exception as e: 
            logger.error(f"[SHUTDOWN] Error terminating web socket resource links: {e}", exc_info=True)

# =========================
# SYSTEM ENTRY MAIN TRAP
# =========================
if __name__ == "__main__":
    if initialize_bot_services():
        try:
            while True:
                run_prematch_scan_cycle()
                logger.info(f"[SLEEP] Cycle execution complete. Next full pre-match scan in {CHECK_INTERVAL_HOURS} hours.")
                time.sleep(SLEEP_TIME)
        except (KeyboardInterrupt, SystemExit):
            logger.info("[APP_INTERRUPT] Exit signal registered by system environment.")
            shutdown_bot()
