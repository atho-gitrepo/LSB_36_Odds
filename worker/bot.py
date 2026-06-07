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

# --- NEW: OVER 0.5 SETTINGS ---
OVER05_TRIGGER_MINUTE = 20
OVER05_STAKE = 10.0
OVER05_CHECK_MINUTE = 45  # Check at halftime

# --- FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'U19', 'U21', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u23', 'u21','u20', 'women', 'college']

# --- SMART OPTIMIZATION SETTINGS ---
PREDICT_START_MIN = 30
PRE_WARM_WINDOW = (34, 38)
MATCH_CACHE = {}

# --- GLOBALS ---
SOFASCORE_CLIENT = None
firebase_manager = None
LOCAL_TRACKED_MATCHES = {}
OVER05_TRACKED_MATCHES = {}  # NEW: Separate tracking for over 0.5 logic

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

    def is_over05_state_locked(self):
        """Check if there's an unresolved over 0.5 bet"""
        try:
            return len(self.db.collection('unresolved_over05_bets').limit(1).get()) > 0
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

    def get_last_resolved_over05_bet(self):
        """Get last resolved over 0.5 bet for sequence tracking"""
        try:
            query = self.db.collection('resolved_over05_bets')\
                .order_by('resolution_timestamp', direction=firestore.Query.DESCENDING)\
                .limit(1).get()
            for doc in query:
                return doc.to_dict()
        except:
            return None

    def add_unresolved_bet(self, match_id, data):
        data['placed_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        self.db.collection('unresolved_bets').document(str(match_id)).set(data)

    def add_unresolved_over05_bet(self, match_id, data):
        """Add unresolved over 0.5 bet - separate collection"""
        data['placed_at'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        self.db.collection('unresolved_over05_bets').document(str(match_id)).set(data)

    def get_unresolved_bet(self, match_id):
        doc = self.db.collection('unresolved_bets').document(str(match_id)).get()
        return doc.to_dict() if doc.exists else None

    def get_unresolved_over05_bet(self, match_id):
        """Get unresolved over 0.5 bet"""
        doc = self.db.collection('unresolved_over05_bets').document(str(match_id)).get()
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

    def move_over05_to_resolved(self, match_id, data, outcome):
        """Move over 0.5 bet to resolved"""
        data.update({
            'outcome': outcome,
            'resolved_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'resolution_timestamp': firestore.SERVER_TIMESTAMP
        })
        self.db.collection('resolved_over05_bets').document(str(match_id)).set(data)
        self.db.collection('unresolved_over05_bets').document(str(match_id)).delete()
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
# STAKE CALCULATION (Regular Sequence)
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
# STAKE CALCULATION (Over 0.5 Separate Sequence)
# =========================
def calculate_over05_stake():
    """Separate chase sequence for over 0.5 bets"""
    last = firebase_manager.get_last_resolved_over05_bet()
    if not last or last.get('outcome') == 'win':
        return OVER05_STAKE, 1
    seq = last.get('match_sequence', 1)
    if seq < MAX_CHASE_LEVEL:
        return float(OVER05_STAKE * (2**seq)), seq + 1
    return OVER05_STAKE, 1

# =========================
# SMART PREDICTION ENGINE
# =========================
def should_pre_warm(minute):
    return minute >= PREDICT_START_MIN

def is_in_active_window(minute):
    return PRE_WARM_WINDOW[0] <= minute <= PRE_WARM_WINDOW[1]

# =========================
# MATCH PROCESS (UPDATED WITH OVER 0.5 LOGIC)
# =========================
def process_match(match):
    fid = str(match.id)
    league = match.tournament.name
    country = match.tournament.category.name
    full_info = f"{league} {country}".lower()

    # basic filter
    if not any(x.lower() in league.lower() for x in ALLOWED_LEAGUES):
        if any(x.lower() in full_info for x in EXCLUDED_LEAGUES + AMATEUR_KEYWORDS):
            return

    min_elapsed = match.total_elapsed_minutes
    status = match.status.description.upper()
    score = f"{match.home_score.current}-{match.away_score.current}"
    home_score = match.home_score.current
    away_score = match.away_score.current
    total_goals = home_score + away_score

    match_name = f"{match.home_team.name} vs {match.away_team.name}"

    # =========================
    # 🧠 SMART PRE-WARM LOGIC
    # =========================
    if not should_pre_warm(min_elapsed):
        return

    # REGULAR SEQUENCE TRACKING
    state = LOCAL_TRACKED_MATCHES.get(fid, {
        'bet_placed': False,
        'last_seen': time.time(),
        'active': False
    })

    state['last_seen'] = time.time()

    if is_in_active_window(min_elapsed):
        state['active'] = True

    LOCAL_TRACKED_MATCHES[fid] = state

    # =========================
    # NEW: OVER 0.5 LOGIC (SEPARATE SEQUENCE)
    # =========================
    over05_state = OVER05_TRACKED_MATCHES.get(fid, {
        'bet_placed': False,
        'triggered': False,
        'last_seen': time.time()
    })

    over05_state['last_seen'] = time.time()
    OVER05_TRACKED_MATCHES[fid] = over05_state

    # --- OVER 0.5 TRIGGER: Score 0-0 at exactly minute 20 ---
    if (status == '1ST_HALF' or '1ST' in status) and \
       min_elapsed == OVER05_TRIGGER_MINUTE and \
       score == '0-0' and \
       not over05_state['bet_placed'] and \
       not firebase_manager.is_over05_state_locked():
        
        # Calculate stake from separate sequence
        stake, seq = calculate_over05_stake()
        
        # Prepare bet data
        bet_data = {
            'match_name': match_name,
            'league': league,
            'country': country,
            'trigger_score': score,
            'trigger_minute': OVER05_TRIGGER_MINUTE,
            'stake': stake,
            'match_sequence': seq,
            'bet_type': 'over_0.5'
        }
        
        # Save to Firebase (separate collection)
        firebase_manager.add_unresolved_over05_bet(fid, bet_data)
        
        # Send alert
        send_telegram(
            f"🎯 **OVER 0.5 BET PLACED (Seq {seq})**\n"
            f"⏱ {OVER05_TRIGGER_MINUTE}' | {match_name}\n"
            f"🌍 {country} | 🏆 {league}\n"
            f"🔢 Score: {score}\n"
            f"💰 Stake: ${stake:.2f}\n"
            f"📊 Bet: Over 0.5 Goals"
        )
        
        over05_state['bet_placed'] = True
        OVER05_TRACKED_MATCHES[fid] = over05_state
        
        logger.info(f"🚀 Over 0.5 bet placed: {match_name} | {score} at {OVER05_TRIGGER_MINUTE}'")

    # --- OVER 0.5 CHECK: At halftime ---
    if 'HALFTIME' in status and over05_state['bet_placed']:
        unresolved_over05 = firebase_manager.get_unresolved_over05_bet(fid)
        
        if unresolved_over05:
            # Check if there were any goals in first half
            outcome = 'win' if total_goals > 0 else 'loss'
            
            firebase_manager.move_over05_to_resolved(fid, unresolved_over05, outcome)
            
            send_telegram(
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} OVER 0.5 HT\n"
                f"{match_name}\n"
                f"Score: {score} | Goals: {total_goals}\n"
                f"Bet placed at {OVER05_TRIGGER_MINUTE}' with score 0-0"
            )
            
            OVER05_TRACKED_MATCHES.pop(fid, None)
            
            logger.info(f"🔍 Over 0.5 result: {outcome} | {match_name} | Score: {score}")

    # =========================
    # 1. REGULAR SEQUENCE BET (UNCHANGED)
    # =========================
    if '1ST' in status and min_elapsed in MINUTES_REGULAR_BET and not state['bet_placed']:
        if not firebase_manager.is_state_locked():
            if score in ['1-1', '2-2', '3-3']:
                stake, seq = calculate_stake()
                data = {
                    'match_name': match_name,
                    'league': league,
                    'country': country,
                    '36_score': score,
                    'stake': stake,
                    'match_sequence': seq,
                    'bet_type': 'regular'
                }

                firebase_manager.add_unresolved_bet(fid, data)

                send_telegram(
                    f"🎯 **REGULAR BET PLACED (Match {seq})**\n"
                    f"⏱ 36' | {match_name}\n"
                    f"🌍 {country} | 🏆 {league}\n"
                    f"🔢 Score: {score}\n"
                    f"💰 Stake: ${stake:.2f}"
                )

                state['bet_placed'] = True
                LOCAL_TRACKED_MATCHES[fid] = state

    # =========================
    # 2. REGULAR HT CHECK (UNCHANGED)
    # =========================
    elif 'HALFTIME' in status and state['bet_placed']:
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            outcome = 'win' if score == unresolved['36_score'] else 'loss'
            firebase_manager.move_to_resolved(fid, unresolved, outcome)

            send_telegram(
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} REGULAR HT\n"
                f"{match_name}\n"
                f"Score: {score}"
            )

            LOCAL_TRACKED_MATCHES.pop(fid, None)

    # Cleanup old entries
    cleanup_old_entries()

def cleanup_old_entries():
    """Remove matches not seen for more than 10 minutes"""
    current_time = time.time()
    
    # Clean regular matches
    for fid in list(LOCAL_TRACKED_MATCHES.keys()):
        if current_time - LOCAL_TRACKED_MATCHES[fid]['last_seen'] > 600:  # 10 minutes
            LOCAL_TRACKED_MATCHES.pop(fid, None)
    
    # Clean over 0.5 matches
    for fid in list(OVER05_TRACKED_MATCHES.keys()):
        if current_time - OVER05_TRACKED_MATCHES[fid]['last_seen'] > 600:
            OVER05_TRACKED_MATCHES.pop(fid, None)

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
# STATUS REPORT
# =========================
def send_status_report():
    """Send periodic status report"""
    regular_tracked = len(LOCAL_TRACKED_MATCHES)
    over05_tracked = len(OVER05_TRACKED_MATCHES)
    regular_active = sum(1 for m in LOCAL_TRACKED_MATCHES.values() if m.get('active'))
    
    msg = f"📊 **Bot Status**\n"
    msg += f"🔄 Regular: {regular_tracked} tracked ({regular_active} active)\n"
    msg += f"⚽ Over 0.5: {over05_tracked} tracked\n"
    msg += f"🔒 Regular Locked: {firebase_manager.is_state_locked()}\n"
    msg += f"🔒 Over 0.5 Locked: {firebase_manager.is_over05_state_locked()}"
    
    send_telegram(msg)

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

        logger.info(f"Scanning {len(events)} live matches | Regular: {len(LOCAL_TRACKED_MATCHES)} | Over05: {len(OVER05_TRACKED_MATCHES)}")

        for m in events:
            process_match(m)

    except Exception as e:
        logger.error(f"Error in bot cycle: {e}")

# =========================
# MAIN ENTRY POINT
# =========================
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("🚀 BETTING BOT STARTING")
    logger.info("=" * 50)
    logger.info(f"📊 Regular bets: Minutes {MINUTES_REGULAR_BET} | Scores: 1-1, 2-2, 3-3")
    logger.info(f"⚽ Over 0.5 bets: Trigger at {OVER05_TRIGGER_MINUTE}' | Score: 0-0 | Check at HT")
    logger.info(f"💰 Separate sequences for both strategies")
    
    if not initialize_bot_services():
        logger.error("❌ Failed to initialize bot services")
        exit(1)
    
    last_status_time = time.time()
    
    try:
        while True:
            run_bot_cycle()
            
            # Send status report every 30 minutes
            if time.time() - last_status_time > 1800:
                send_status_report()
                last_status_time = time.time()
            
            time.sleep(SLEEP_TIME)
            
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        shutdown_bot()
        logger.info("Bot shutdown complete")