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
OVER05_TRIGGER_MINUTES = [20, 21, 22]
OVER05_STAKE = 10.0

# --- GLOBALS ---
SOFASCORE_CLIENT = None
firebase_manager = None
LOCAL_TRACKED_MATCHES = {}
OVER05_TRACKED_MATCHES = {}

# =========================
# FIREBASE MANAGER
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

    def is_state_locked(self, collection):
        return len(self.db.collection(collection).limit(1).get()) > 0

    def get_last_resolved(self, collection):
        query = self.db.collection(collection).order_by('resolution_timestamp', direction=firestore.Query.DESCENDING).limit(1).get()
        for doc in query: return doc.to_dict()
        return None

    def add_unresolved(self, collection, match_id, data):
        data['placed_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        self.db.collection(collection).document(str(match_id)).set(data)

    def move_to_resolved(self, source, dest, match_id, data, outcome):
        data.update({'outcome': outcome, 'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'resolution_timestamp': firestore.SERVER_TIMESTAMP})
        self.db.collection(dest).document(str(match_id)).set(data)
        self.db.collection(source).document(str(match_id)).delete()

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=15)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"❌ Telegram Send Failed: {e}")

# =========================
# STAKE LOGIC
# =========================
def get_stake_and_seq(collection, base_stake):
    last = firebase_manager.get_last_resolved(collection)
    if not last or last.get('outcome') == 'win':
        return base_stake, 1
    seq = last.get('match_sequence', 1)
    return (float(base_stake * (2**seq)), seq + 1) if seq < MAX_CHASE_LEVEL else (base_stake, 1)

# =========================
# MATCH PROCESSING
# =========================
def process_match(match):
    fid = str(match.id)
    min_elapsed = match.total_elapsed_minutes
    status = match.status.description.upper()
    score = f"{match.home_score.current}-{match.away_score.current}"
    match_name = f"{match.home_team.name} vs {match.away_team.name}"
    league = match.tournament.name
    country = match.tournament.category.name

    if fid not in LOCAL_TRACKED_MATCHES: LOCAL_TRACKED_MATCHES[fid] = {'placed': False}
    if fid not in OVER05_TRACKED_MATCHES: OVER05_TRACKED_MATCHES[fid] = {'placed': False}

    # 1. OVER 0.5 TRIGGER
    if '1ST' in status and min_elapsed in OVER05_TRIGGER_MINUTES and score == '0-0' and not OVER05_TRACKED_MATCHES[fid]['placed']:
        if not firebase_manager.is_state_locked('unresolved_over05_bets'):
            stake, seq = get_stake_and_seq('resolved_over05_bets', OVER05_STAKE)
            data = {'match_name': match_name, 'league': league, 'country': country, 'stake': stake, 'match_sequence': seq}
            firebase_manager.add_unresolved('unresolved_over05_bets', fid, data)
            send_telegram(f"🎯 **OVER 0.5 BET PLACED (Seq {seq})**\n⏱ {min_elapsed}' | {match_name}\n💰 Stake: ${stake:.2f}")
            OVER05_TRACKED_MATCHES[fid]['placed'] = True

    # 2. REGULAR TRIGGER
    if '1ST' in status and min_elapsed in MINUTES_REGULAR_BET and not LOCAL_TRACKED_MATCHES[fid]['placed']:
        if not firebase_manager.is_state_locked('unresolved_bets') and score in ['1-1', '2-2', '3-3']:
            stake, seq = get_stake_and_seq('resolved_bets', ORIGINAL_STAKE)
            data = {'match_name': match_name, '36_score': score, 'stake': stake, 'match_sequence': seq}
            firebase_manager.add_unresolved('unresolved_bets', fid, data)
            send_telegram(f"🎯 **REGULAR BET PLACED (Seq {seq})**\n⏱ 36' | {match_name}\n💰 Stake: ${stake:.2f}")
            LOCAL_TRACKED_MATCHES[fid]['placed'] = True

    # 3. RESOLUTION (HT CHECK)
    if 'HALFTIME' in status:
        # Resolve Over 0.5
        unr_o5 = firebase_manager.db.collection('unresolved_over05_bets').document(fid).get()
        if unr_o5.exists and OVER05_TRACKED_MATCHES[fid]['placed']:
            outcome = 'win' if (match.home_score.current + match.away_score.current) > 0 else 'loss'
            firebase_manager.move_to_resolved('unresolved_over05_bets', 'resolved_over05_bets', fid, unr_o5.to_dict(), outcome)
            send_telegram(f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} OVER 0.5 HT\n{match_name}")
            OVER05_TRACKED_MATCHES[fid]['placed'] = False
        
        # Resolve Regular
        unr_reg = firebase_manager.db.collection('unresolved_bets').document(fid).get()
        if unr_reg.exists and LOCAL_TRACKED_MATCHES[fid]['placed']:
            outcome = 'win' if score == unr_reg.to_dict().get('36_score') else 'loss'
            firebase_manager.move_to_resolved('unresolved_bets', 'resolved_bets', fid, unr_reg.to_dict(), outcome)
            send_telegram(f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} REGULAR HT\n{match_name}")
            LOCAL_TRACKED_MATCHES[fid]['placed'] = False

# =========================
# MAIN EXECUTION
# =========================
if __name__ == "__main__":
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)
    SOFASCORE_CLIENT = SofascoreClient()
    SOFASCORE_CLIENT.initialize()
    logger.info("🚀 Bot running...")
    while True:
        try:
            for event in SOFASCORE_CLIENT.get_events(live=True):
                process_match(event)
        except Exception as e: logger.error(f"Loop Error: {e}")
        time.sleep(SLEEP_TIME)
