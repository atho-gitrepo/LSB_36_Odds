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
    handlers=[logging.FileHandler("win_prob_bot_trace.log"), logging.StreamHandler()]
)
logger = logging.getLogger("WinProbBot")

# --- ENV VARS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS_JSON", "")

# --- CONFIGURATION SETTINGS ---
SLEEP_TIME = 120  
MIN_PROBABILITY_THRESHOLD = 70.0  
MIN_MINUTES_ELAPSED = 15  

# --- LEAGUE FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League', 'Copa Colombia']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u1', 'u23', 'u21', 'u20', 'women', 'college']

LOCAL_TRACKED_MATCHES = {}

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

    def add_unresolved_probability_bet(self, match_id, data):
        """Saves high probability predictions that are still running."""
        try:
            if not self.db:
                logger.warning(f"[DB_WRITE] ⚠️ Cannot add unresolved bet for {match_id}: Database instance not ready.")
                return
            data['logged_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"[DB_WRITE] Writing unresolved prediction to Firestore for match {match_id}...")
            self.db.collection('unresolved_prob_bets').document(str(match_id)).set(data)
            logger.info(f"[DB_WRITE] ✅ Successfully wrote match {match_id} to unresolved_prob_bets.")
        except Exception as e:
            logger.error(f"[DB_WRITE] ❌ Error writing unresolved bet for match {match_id}: {e}", exc_info=True)

    def get_unresolved_probability_bet(self, match_id):
        try:
            if not self.db:
                logger.warning(f"[DB_READ] ⚠️ Cannot read match {match_id}: Database instance not ready.")
                return None
            logger.debug(f"[DB_READ] Fetching document for match {match_id} from unresolved_prob_bets...")
            doc = self.db.collection('unresolved_prob_bets').document(str(match_id)).get()
            if doc.exists:
                logger.info(f"[DB_READ] ✅ Match data found in Firestore for ID: {match_id}")
                return doc.to_dict()
            logger.debug(f"[DB_READ] No existing unresolved bet document found for ID: {match_id}")
            return None
        except Exception as e:
            logger.error(f"[DB_READ] ❌ Error reading unresolved bet document for ID {match_id}: {e}", exc_info=True)
            return None

    def move_to_resolved_probability_bet(self, match_id, data, outcome):
        """Moves completed match results to historical logs and purges active references."""
        try:
            if not self.db:
                logger.warning(f"[DB_RESOLVE] ⚠️ Cannot settle match {match_id}: Database instance not ready.")
                return
            data.update({
                'outcome': outcome,
                'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                'resolution_timestamp': firestore.SERVER_TIMESTAMP
            })
            logger.info(f"[DB_RESOLVE] Moving match {match_id} to resolved_prob_bets with outcome: {outcome.upper()}...")
            
            # Use a batch write or consecutive commands to ensure atomic processing state transfers
            self.db.collection('resolved_prob_bets').document(str(match_id)).set(data)
            logger.info(f"[DB_RESOLVE] Target document created in historical logs. Purging active monitor document...")
            self.db.collection('unresolved_prob_bets').document(str(match_id)).delete()
            logger.info(f"[DB_RESOLVE] ✅ Successfully moved match {match_id} from unresolved to resolved logs.")
        except Exception as e:
            logger.error(f"[DB_RESOLVE] ❌ Error executing transactional result migration for match {match_id}: {e}", exc_info=True)

# =========================
# TELEGRAM NOTIFIER
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    logger.info(f"[TELEGRAM_API] Dispatching outgoing bot packet request payload...")
    try:
        response = requests.post(
            url,
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
        if response.status_code == 200:
            logger.info("[TELEGRAM_API] ✅ Message delivered successfully to channel.")
        else:
            logger.error(f"[TELEGRAM_API] ❌ Server validation rejected payload. Status Code: {response.status_code} | Body: {response.text}")
    except Exception as e:
        logger.error(f"[TELEGRAM_API] ❌ Network level transport exception encountered sending Telegram message: {e}", exc_info=True)

# ==========================================
# WIN PROBABILITY ANALYSIS CORE
# ==========================================
def process_match(match):
    fid = str(match.id)
    
    # Safely fetch complex structural parameters inside structural definitions
    try:
        league = match.tournament.name if match.tournament else "Unknown League"
        country = match.tournament.category.name if match.tournament and match.tournament.category else "Unknown Country"
        match_name = f"{match.home_team.name} vs {match.away_team.name}" if match.home_team and match.away_team else f"Match ID {fid}"
    except Exception as e:
        logger.error(f"[MATCH_PARSE] ❌ Structural parsing crashed for basic attributes on match {fid}: {e}", exc_info=True)
        return

    full_info = f"{league} {country}".lower()

    # --- LEAGUE FILTER PIPELINE LOGS ---
    if not any(x.lower() in league.lower() for x in ALLOWED_LEAGUES):
        if any(x.lower() in full_info for x in EXCLUDED_LEAGUES + AMATEUR_KEYWORDS):
            logger.debug(f"[FILTER] Match {match_name} ({fid}) skipped due to Exclusion list criteria rules.")
            return

    try:
        min_elapsed = match.total_elapsed_minutes
        status = match.status.description.upper() if match.status else "UNKNOWN"
        
        home_score = match.home_score.current if match.home_score and match.home_score.current is not None else 0
        away_score = match.away_score.current if match.away_score and match.away_score.current is not None else 0
        score_str = f"{home_score}-{away_score}"
    except Exception as e:
        logger.error(f"[MATCH_LIVE_ATTR] ❌ Error collecting dynamic tracking indicators for match {match_name} ({fid}): {e}", exc_info=True)
        return

    # ----------------------------------------------------
    # PHASE 1: EVALUATING ACTIVE LIVE MATCHES FOR TRIGGERS
    # ----------------------------------------------------
    if ('1ST' in status or '2ND' in status) and min_elapsed >= MIN_MINUTES_ELAPSED:
        if fid in LOCAL_TRACKED_MATCHES:
            logger.debug(f"[TRACKER_LIVE] Match {match_name} ({fid}) already processed and locked locally. Skipping lookup.")
            return

        logger.info(f"[PROBABILITY_CHECK] Evaluating probabilities for active live match: {match_name} ({fid}) at {min_elapsed}'...")
        try:
            stats_data = SOFASCORE_CLIENT.get_stats(int(fid))
            if not stats_data:
                logger.warning(f"[PROBABILITY_CHECK] Client returned empty stats container wrapper object for {match_name} ({fid})")
                return
                
            if not hasattr(stats_data, 'win_probability') or stats_data.win_probability is None:
                logger.warning(f"[PROBABILITY_CHECK] Win probability metrics are unavailable for match: {match_name} ({fid})")
                return

            prob = stats_data.win_probability
            
            # Use raw fallback default assignment mapping to guarantee variable type safety
            home_prob = float(prob.home) if prob.home is not None else 0.0
            draw_prob = float(prob.draw) if prob.draw is not None else 0.0
            away_prob = float(prob.away) if prob.away is not None else 0.0

            logger.info(f"[PROBABILITY_CHECK] Values parsed for {match_name} -> Home: {home_prob}%, Draw: {draw_prob}%, Away: {away_prob}%")

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

            if dominant_team:
                logger.info(f"🔥 [TRIGGER_ALERT] Threshold met! {dominant_team} has {highest_prob}% win probability.")
                
                payload = {
                    'match_name': match_name,
                    'league': league,
                    'country': country,
                    'trigger_minute': min_elapsed,
                    'trigger_score': score_str,
                    'dominant_team': dominant_team,
                    'target_side': target_side,
                    'predicted_probability': highest_prob,
                    'probabilities_at_trigger': {
                        'home': home_prob,
                        'draw': draw_prob,
                        'away': away_prob
                    }
                }
                
                firebase_manager.add_unresolved_probability_bet(fid, payload)
                
                send_telegram(
                    f"📊 **HIGH WIN PROBABILITY DETECTED**\n"
                    f"⏱ {min_elapsed}' | {match_name}\n"
                    f"🏆 {league} ({country})\n"
                    f"⚽ Current Score: `{score_str}`\n\n"
                    f"🔥 **Target Team:** {dominant_team}\n"
                    f"📈 **Win Probability:** `{highest_prob:.1f}%` \n\n"
                    f"📋 *Full Distribution:*\n"
                    f"🏠 Home: {home_prob:.1f}% | 🤝 Draw: {draw_prob:.1f}% | 🚌 Away: {away_prob:.1f}%"
                )
                
                LOCAL_TRACKED_MATCHES[fid] = True
                logger.info(f"[TRACKER_LIVE] Local process memory lock set for active match tracking ID: {fid}")

        except Exception as e:
            logger.error(f"[PROBABILITY_CHECK] ❌ Exception occurred parsing deep client statistics engine logic for match {fid}: {e}", exc_info=True)

    # ----------------------------------------------------
    # PHASE 2: EVALUATING POST-MATCH COMPLETED RESOLUTIONS
    # ----------------------------------------------------
    elif any(x in status for x in ['ENDED', 'FT', 'FINISHED', 'COMPLETED']):
        logger.debug(f"[RESOLUTION_CHECK] Match tracking ID {fid} has reached full time status: {status}. Querying DB log...")
        unresolved = firebase_manager.get_unresolved_probability_bet(fid)
        
        if unresolved:
            logger.info(f"🏁 [SETTLEMENT] Tracked match finished. Commencing calculation routines for: {match_name} ({fid})")
            try:
                target_side = unresolved.get('target_side')
                outcome = 'loss'
                
                logger.info(f"[SETTLEMENT] Evaluating outcomes -> Pick Side: {target_side} | Full Time Score Matrix: Home {home_score} - Away {away_score}")
                
                if target_side == 'HOME' and home_score > away_score:
                    outcome = 'win'
                elif target_side == 'AWAY' and away_score > home_score:
                    outcome = 'win'
                    
                unresolved.update({
                    'final_score': score_str,
                    'home_final_score': home_score,
                    'away_final_score': away_score
                })
                
                firebase_manager.move_to_resolved_probability_bet(fid, unresolved, outcome)
                
                status_emoji = '✅' if outcome == 'win' else '❌'
                send_telegram(
                    f"{status_emoji} **PROBABILITY RESULT: {outcome.upper()}**\n"
                    f"{match_name}\n"
                    f"🏆 {unresolved.get('league')}\n\n"
                    f"🎯 **Picked Team:** {unresolved.get('dominant_team')} (>{unresolved.get('predicted_probability')}% Win Prob)\n"
                    f"🏁 **Final Score:** {score_str}\n"
                    f"📝 **Outcome:** {'Target team won the match' if outcome == 'win' else 'Target team failed to win'}"
                )
                
                # Safely clear tracking cache entries
                LOCAL_TRACKED_MATCHES.pop(fid, None)
                logger.info(f"[SETTLEMENT] Cleaned and decoupled match tracking ID memory references for: {fid}")
            except Exception as e:
                logger.error(f"[SETTLEMENT] ❌ Error executing accounting settlement loops for completed match tracking ID {fid}: {e}", exc_info=True)

# =========================
# INITIALIZATION & EXECUTION
# =========================
def initialize_bot_services():
    global firebase_manager, SOFASCORE_CLIENT
    logger.info("[INIT] Initializing global ecosystem automation instances...")
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)
    try:
        logger.info("[INIT] Spinning up browser engine configuration layers...")
        SOFASCORE_CLIENT = SofascoreClient()
        SOFASCORE_CLIENT.initialize()
        logger.info("✅ [INIT] All external monitoring engine clients initialized successfully.")
        return True
    except Exception as e:
        logger.error(f"❌ [INIT] Critical error bootstrapping core platform processes: {e}", exc_info=True)
        return False

def shutdown_bot():
    logger.info("[SHUTDOWN] Executing graceful shutdown procedures...")
    if SOFASCORE_CLIENT:
        try: 
            SOFASCORE_CLIENT.close()
            logger.info("[SHUTDOWN] ✅ Browser client process pools closed down cleanly.")
        except Exception as e: 
            logger.error(f"[SHUTDOWN] Error terminating web socket resource links: {e}", exc_info=True)

def run_bot_cycle():
    if not SOFASCORE_CLIENT: 
        logger.error("[CYCLE_EXEC] ❌ Aborting routine loop cycle: Client object reference is set to None.")
        return
    try:
        logger.info("[CYCLE_EXEC] Querying network layer for active live events...")
        events = SOFASCORE_CLIENT.get_events(live=True)
        if not events: 
            logger.info("[CYCLE_EXEC] No match events currently returned as active and live. Standing by...")
            return
            
        logger.info(f"[CYCLE_EXEC] Processing metrics tracking rules for {len(events)} live matches...")
        for m in events:
            process_match(m)
    except Exception as e:
        logger.error(f"[CYCLE_EXEC] ❌ Error caught inside the active loop execution processing thread: {e}", exc_info=True)

if __name__ == "__main__":
    logger.info("🎬 Launching Win Probability Application Environment...")
    if initialize_bot_services():
        logger.info("🚀 Monitoring service loops successfully initialized. System running.")
        try:
            while True:
                run_bot_cycle()
                logger.debug(f"[SLEEP] Cycle execution complete. Sleeping for {SLEEP_TIME} seconds...")
                time.sleep(SLEEP_TIME)
        except (KeyboardInterrupt, SystemExit):
            logger.info("[APP_INTERRUPT] Exit signal registered by system environment.")
            shutdown_bot()
        except Exception as e:
            logger.fatal(f"[CRASH] Unhandled runtime engine core crash: {e}", exc_info=True)
            shutdown_bot()
