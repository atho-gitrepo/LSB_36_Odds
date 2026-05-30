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
    handlers=[logging.FileHandler("bot_activity.log"), logging.StreamHandler()]
)
logger = logging.getLogger("BetBot")

# --- ENV VARS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS_JSON", "")

# --- SETTINGS ---
ORIGINAL_STAKE = 10.0
MAX_CHASE_LEVEL = 4
SLEEP_TIME = 95
MINUTES_REGULAR_BET = [35, 36, 37]

# --- FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League', 'Copa Colombia']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u1', 'u23', 'u21', 'u20', 'women', 'college']

# --- SMART OPTIMIZATION SETTINGS ---
PREDICT_START_MIN = 30     
PRE_WARM_WINDOW = (34, 38) 
LOCAL_TRACKED_MATCHES = {}

# =========================
# FIREBASE
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

    def is_state_locked(self):
        try:
            return len(self.db.collection('unresolved_bets').limit(1).get()) > 0
        except:
            return False

    def get_last_resolved_bet(self):
        try:
            query = self.db.collection('resolved_bets')\
                .order_by('resolution_timestamp', direction=firestore.Query.DESCENDING)\
                .limit(1).get()
            for doc in query:
                return doc.to_dict()
        except:
            return None

    def add_unresolved_bet(self, match_id, data):
        data['placed_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        self.db.collection('unresolved_bets').document(str(match_id)).set(data)

    def get_unresolved_bet(self, match_id):
        doc = self.db.collection('unresolved_bets').document(str(match_id)).get()
        return doc.to_dict() if doc.exists else None

    def move_to_resolved(self, match_id, data, outcome):
        data.update({
            'outcome': outcome,
            'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'resolution_timestamp': firestore.SERVER_TIMESTAMP
        })
        self.db.collection('resolved_bets').document(str(match_id)).set(data)
        self.db.collection('unresolved_bets').document(str(match_id)).delete()
        return True

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
    except:
        pass

# =========================
# STAKE
# =========================
def calculate_stake():
    last = firebase_manager.get_last_resolved_bet()
    if not last or last.get('outcome') == 'win':
        return ORIGINAL_STAKE, 1
    seq = last.get('match_sequence', 1)
    if seq < MAX_CHASE_LEVEL:
        return float(ORIGINAL_STAKE * (2**seq)), seq + 1
    return ORIGINAL_STAKE, 1

# =========================
# SMART PREDICTION ENGINE
# =========================
def should_pre_warm(minute):
    return minute >= PREDICT_START_MIN

def is_in_active_window(minute):
    return PRE_WARM_WINDOW[0] <= minute <= PRE_WARM_WINDOW[1]

# ==========================================
# SAFE CORNER EXTRACTION FUNCTION
# ==========================================
def get_live_1st_half_corners(match_id):
    """
    Queries SofascoreClient statistics payload directly and extracts
    the exact 1st half corner totals using your match_stats data model fields.
    """
    try:
        # Request full statistics payload using the event ID
        stats_data = SOFASCORE_CLIENT.get_stats(match_id)
        if not stats_data or not hasattr(stats_data, 'first_half') or stats_data.first_half is None:
            return 0
            
        corner_item = stats_data.first_half.match_overview.corner_kicks
        home_corners = corner_item.home_total if corner_item.home_total is not None else 0
        away_corners = corner_item.away_total if corner_item.away_total is not None else 0
        
        return int(home_corners + away_corners)
    except Exception as e:
        logger.error(f"Error fetching Sofascore deep stats for match {match_id}: {e}")
        return 0

# =========================
# MATCH PROCESS
# =========================
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
    score = f"{match.home_score.current}-{match.away_score.current}"
    match_name = f"{match.home_team.name} vs {match.away_team.name}"

    if not should_pre_warm(min_elapsed):
        return  

    state = LOCAL_TRACKED_MATCHES.get(fid, {
        'bet_placed': False,
        'last_seen': time.time(),
        'active': False,
        'processed_ht': False  
    })
    state['last_seen'] = time.time()

    if is_in_active_window(min_elapsed):
        state['active'] = True

    LOCAL_TRACKED_MATCHES[fid] = state

    # =============================================
    # 1. PLACE PARLAY BET (TRIGGER WINDOW ACTION)
    # =============================================
    if '1ST' in status and min_elapsed in MINUTES_REGULAR_BET and not state['bet_placed']:
        if not firebase_manager.is_state_locked():
            
            if score in ['1-1', '2-2', '3-3']:
                # Pull correct corner values directly from match_stats.py schema model
                current_total_corners = get_live_1st_half_corners(fid)
                target_corner_line = float(current_total_corners + 0.5)

                stake, seq = calculate_stake()
                data = {
                    'match_name': match_name,
                    'league': league,
                    'trigger_score': score,
                    'trigger_corners': current_total_corners,
                    'target_corner_line': target_corner_line,
                    'stake': stake,
                    'match_sequence': seq,
                    'bet_type': 'parlay_score_corners'
                }

                firebase_manager.add_unresolved_bet(fid, data)

                send_telegram(
                    f"🎯 **PARLAY BET PLACED (Match {seq})**\n⏱ {min_elapsed}' | {match_name}\n🌍 {country} | 🏆 {league}\n\n"
                    f"🎰 **Parlay Leg 1:** Score to remain {score} at HT\n"
                    f"📐 **Parlay Leg 2:** 1st Half Corners Over {target_corner_line} (Current: {current_total_corners})"
                    f"\n💰 Stake: ${stake:.2f}"
                )

        state['bet_placed'] = True

    # =============================================
    # 2. HALFTIME CHECK & PARLAY EVALUATION
    # =============================================
    elif 'HALFTIME' in status and not state['processed_ht']:
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            # Query fresh final half-time statistics payload
            final_ht_corners = get_live_1st_half_corners(fid)

            # Leg 1: Trigger score matches the final half-time scoreline
            score_leg_win = (score == unresolved['trigger_score'])
            
            # Leg 2: Total corners taken at halftime exceeds our +0.5 goal threshold line
            corner_leg_win = (float(final_ht_corners) > unresolved['target_corner_line'])

            # Parlay win conditions met
            parlay_win = score_leg_win and corner_leg_win
            outcome = 'win' if parlay_win else 'loss'
            
            stake = unresolved.get('stake', 0.0)
            pnl = -stake if outcome == 'loss' else 0.0 

            unresolved.update({
                'final_ht_score': score,
                'final_ht_corners': final_ht_corners,
                'score_leg_result': 'WIN' if score_leg_win else 'LOSS',
                'corner_leg_result': 'WIN' if corner_leg_win else 'LOSS',
                'pnl': round(pnl, 2)
            })

            firebase_manager.move_to_resolved(fid, unresolved, outcome)

            status_emoji = '✅' if parlay_win else '❌'
            send_telegram(
                f"{status_emoji} **PARLAY RESULT: {outcome.upper()}**\n{match_name}\n\n"
                f"🎰 Score Leg: {unresolved['trigger_score']} -> HT Score: {score} ({unresolved['score_leg_result']})\n"
                f"📐 Corner Leg: Target Over {unresolved['target_corner_line']} -> HT Corners: {final_ht_corners} ({unresolved['corner_leg_result']})\n\n"
                f"📊 Session PnL: ${unresolved['pnl']:.2f}"
            )
            
            state['processed_ht'] = True
            LOCAL_TRACKED_MATCHES[fid] = state
            LOCAL_TRACKED_MATCHES.pop(fid, None)

# =========================
# INIT / MAIN
# =========================
def initialize_bot_services():
    global firebase_manager, SOFASCORE_CLIENT
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)
    try:
        SOFASCORE_CLIENT = SofascoreClient()
        SOFASCORE_CLIENT.initialize()
        logger.info("✅ Sofascore initialized")
        return True
    except:
        return False

def shutdown_bot():
    if SOFASCORE_CLIENT:
        try: SOFASCORE_CLIENT.close()
        except: pass

def run_bot_cycle():
    if not SOFASCORE_CLIENT: return
    try:
        events = SOFASCORE_CLIENT.get_events(live=True)
        if not events: return
        for m in events:
            process_match(m)
    except Exception as e:
        logger.error(f"Error in execution loop: {e}")
