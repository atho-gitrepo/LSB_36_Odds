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
MINUTES_REGULAR_BET = [35,36,37]

# --- FILTERS ---
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u18', 'u17', 'u16', 'u19', 'u22', 'u23', 'u21','u20', 'women', 'college']

# --- SMART OPTIMIZATION SETTINGS (NEW) ---
PREDICT_START_MIN = 30     # start tracking match early
PRE_WARM_WINDOW = (34, 38) # only fully process in this window
MATCH_CACHE = {}           # smart tracking cache

# --- GLOBALS ---
SOFASCORE_CLIENT = None
firebase_manager = None
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
# 🧠 NEW: SMART PREDICTION ENGINE
# =========================
def should_pre_warm(minute):
    return minute >= PREDICT_START_MIN

def is_in_active_window(minute):
    return PRE_WARM_WINDOW[0] <= minute <= PRE_WARM_WINDOW[1]

# =========================
# MATCH PROCESS (UPDATED SMART)
# =========================
def process_match(match):
    fid = str(match.id)
    league = match.tournament.name
    country = match.tournament.category.name
    full_info = f"{league} {country}".lower()

    # Basic filter: Only exclude amateur/youth keywords
    if any(x.lower() in full_info for x in AMATEUR_KEYWORDS):
        return

    min_elapsed = match.total_elapsed_minutes
    status = match.status.description.upper()
    score = f"{match.home_score.current}-{match.away_score.current}"

    match_name = f"{match.home_team.name} vs {match.away_team.name}"

    # =========================
    # 🧠 SMART PRE-WARM LOGIC (NEW)
    # =========================
    if not should_pre_warm(min_elapsed):
        return  # skip early matches completely

    # cache tracking
    state = LOCAL_TRACKED_MATCHES.get(fid, {
        'bet_placed': False,
        'last_seen': time.time(),
        'active': False
    })

    state['last_seen'] = time.time()

    # activate only near window
    if is_in_active_window(min_elapsed):
        state['active'] = True

    LOCAL_TRACKED_MATCHES[fid] = state

    # =========================
    # 1. PLACE BET (UNCHANGED LOGIC)
    # =========================
    if '1ST' in status and min_elapsed in MINUTES_REGULAR_BET and not state['bet_placed']:
        if not firebase_manager.is_state_locked():
            if score in ['1-1', '2-2', '2-1', '2-0']:
                stake, seq = calculate_stake()
                data = {
                    'match_name': match_name,
                    'league': league,
                    '36_score': score,
                    'stake': stake,
                    'match_sequence': seq,
                    'bet_type': 'regular'
                }

                firebase_manager.add_unresolved_bet(fid, data)

                send_telegram(
                    f"🎯 **BET PLACED (Match {seq})**\n⏱ 36' | {match_name}\n🌍 {country} | 🏆 {league}\n🔢 Score: {score}\n💰 Stake: ${stake:.2f}"
                )

        state['bet_placed'] = True

    # =========================
    # 2. HT CHECK (UNCHANGED LOGIC)
    # =========================
    elif 'HALFTIME' in status:
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            outcome = 'win' if score == unresolved['36_score'] else 'loss'
            firebase_manager.move_to_resolved(fid, unresolved, outcome)

            send_telegram(
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} HT\n{match_name}\nScore: {score}"
            )

            LOCAL_TRACKED_MATCHES.pop(fid, None)

# =========================
# INIT
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

# =========================
# SHUTDOWN
# =========================
def shutdown_bot():
    if SOFASCORE_CLIENT:
        try:
            SOFASCORE_CLIENT.close()
        except:
            pass

# =========================
# MAIN CYCLE (OPTIMIZED)
# =========================
def run_bot_cycle():
    if not SOFASCORE_CLIENT:
        return

    try:
        events = SOFASCORE_CLIENT.get_events(live=True)

        if not events:
            logger.warning("No events received")
            return

        logger.info(f"Scanning {len(events)} live matches")

        for m in events:
            process_match(m)

    except Exception as e:
        logger.error(f"Error in bot cycle: {e}")
