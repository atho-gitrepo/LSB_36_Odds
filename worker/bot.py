import requests
import os
import json
import time
import logging
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
from esd.sofascore import SofascoreClient

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler("win_prob_bot.log"), logging.StreamHandler()]
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
            logger.error("Firebase Credentials missing!")
            return
        try:
            cred_dict = json.loads(creds_json)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            logger.info("✅ Firebase Connection Ready.")
        except Exception as e:
            logger.error(f"❌ Firebase Init Error: {e}")

    def add_unresolved_probability_bet(self, match_id, data):
        """Saves high probability predictions that are still running."""
        data['logged_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        if self.db:
            self.db.collection('unresolved_prob_bets').document(str(match_id)).set(data)

    def get_unresolved_probability_bet(self, match_id):
        if not self.db: return None
        doc = self.db.collection('unresolved_prob_bets').document(str(match_id)).get()
        return doc.to_dict() if doc.exists else None

    def move_to_resolved_probability_bet(self, match_id, data, outcome):
        """Moves completed match results to historical logs and purges active references."""
        if not self.db: return
        data.update({
            'outcome': outcome,
            'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'resolution_timestamp': firestore.SERVER_TIMESTAMP
        })
        self.db.collection('resolved_prob_bets').document(str(match_id)).set(data)
        self.db.collection('unresolved_prob_bets').document(str(match_id)).delete()

# =========================
# TELEGRAM NOTIFIER
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ==========================================
# WIN PROBABILITY ANALYSIS CORE
# ==========================================
def process_match(match):
    fid = str(match.id)
    league = match.tournament.name
    country = match.tournament.category.name
    full_info = f"{league} {country}".lower()

    if not any(x.lower() in league.lower() for x in ALLOWED_LEAGUES):
        if any(x.lower() in full_info for x in EXCLUDED_LEAGUES + AMATEUR_KEYWORDS):
            return

    min_elapsed = match.total_elapsed_minutes
    status = match.status.description.upper()
    
    home_score = match.home_score.current if match.home_score else 0
    away_score = match.away_score.current if match.away_score else 0
    score_str = f"{home_score}-{away_score}"
    
    match_name = f"{match.home_team.name} vs {match.away_team.name}"

    # ----------------------------------------------------
    # PHASE 1: EVALUATING ACTIVE LIVE MATCHES FOR TRIPPERS
    # ----------------------------------------------------
    if ('1ST' in status or '2ND' in status) and min_elapsed >= MIN_MINUTES_ELAPSED:
        if fid in LOCAL_TRACKED_MATCHES:
            return

        try:
            stats_data = SOFASCORE_CLIENT.get_stats(fid)
            if not stats_data or not stats_data.win_probability:
                return

            prob = stats_data.win_probability
            home_prob = float(prob.home)
            draw_prob = float(prob.draw)
            away_prob = float(prob.away)

            dominant_team = None
            target_side = None # 'HOME' or 'AWAY'
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
                logger.info(f"🔥 High Probability Match Triggered: {match_name} ({highest_prob}%)")
                
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

        except Exception as e:
            logger.error(f"Error analyzing live probabilities for match {fid}: {e}")

    # ----------------------------------------------------
    # PHASE 2: EVALUATING POST-MATCH COMPLETED RESOLUTIONS
    # ----------------------------------------------------
    elif 'ENDED' in status or 'FT' in status or 'FINISHED' in status:
        unresolved = firebase_manager.get_unresolved_probability_bet(fid)
        
        if unresolved:
            logger.info(f"🏁 Match Finished. Processing settlement data for: {match_name}")
            
            target_side = unresolved.get('target_side')
            outcome = 'loss'
            
            # Settlement Calculations
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
            
            # Safely clear local tracking memory cache 
            LOCAL_TRACKED_MATCHES.pop(fid, None)

# =========================
# INITIALIZATION & EXECUTION
# =========================
def initialize_bot_services():
    global firebase_manager, SOFASCORE_CLIENT
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)
    try:
        SOFASCORE_CLIENT = SofascoreClient()
        SOFASCORE_CLIENT.initialize()
        logger.info("✅ Win Probability Settlement Bot successfully initialized.")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize service components: {e}")
        return False

def shutdown_bot():
    if SOFASCORE_CLIENT:
        try: SOFASCORE_CLIENT.close()
        except: pass

def run_bot_cycle():
    if not SOFASCORE_CLIENT: 
        return
    try:
        events = SOFASCORE_CLIENT.get_events(live=True)
        if not events: 
            return
        for m in events:
            process_match(m)
    except Exception as e:
        logger.error(f"Error inside live collection cycle loop: {e}")

if __name__ == "__main__":
    if initialize_bot_services():
        logger.info("🚀 Bot monitoring engine active...")
        try:
            while True:
                run_bot_cycle()
                time.sleep(SLEEP_TIME)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down bot session gracefully...")
            shutdown_bot()
