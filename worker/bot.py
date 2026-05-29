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
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "YOUR_API_KEY_HERE")

# --- SETTINGS ---
ORIGINAL_STAKE = 10.0
MAX_CHASE_LEVEL = 4
SLEEP_TIME = 95
MINUTES_REGULAR_BET = [35,36,37]
BOOKMAKERS_TO_CHECK = ['Bet365', '1xBet']

# --- FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'U19', 'U21', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u23', 'u21','u20', 'women', 'college']

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
# EXTRA FEATURE: JUST-IN-TIME ODDS
# =========================
def fetch_odds_triggered(home_team, away_team):
    """Fetches odds updated in the last 60 seconds specifically for Bet365 and 1xBet."""
    since = int(time.time()) - 60
    results = {'Bet365': 0.0, '1xBet': 0.0}
    
    for bkr in BOOKMAKERS_TO_CHECK:
        url = f"https://api.odds-api.io/v3/odds/updated?apiKey={ODDS_API_KEY}&since={since}&bookmaker={bkr}&sport=Football"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for match_odds in data.get('odds', []):
                    # Direct and basic fuzzy verification for team name strings
                    if home_team.lower() in match_odds['home'].lower() or away_team.lower() in match_odds['away'].lower():
                        draw_odds = next((o['odds'] for o in match_odds.get('outcomes', []) if o['name'].lower() in ['draw', 'x']), 0.0)
                        results[bkr] = float(draw_odds)
        except Exception as e:
            logger.error(f"Odds API Error for {bkr}: {e}")
    return results

# =========================
# MATCH PROCESS (UPDATED SMART)
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
            if score in ['1-1', '2-2', '3-3']:
                
                # --- LIVE EXTRACTION ONLY AT STRATEGY TRIGGER TIME ---
                odds_data = fetch_odds_triggered(match.home_team.name, match.away_team.name)
                
                stake, seq = calculate_stake()
                data = {
                    'match_name': match_name,
                    'league': league,
                    '36_score': score,
                    'stake': stake,
                    'match_sequence': seq,
                    'bet_type': 'regular',
                    'odds_bet365': odds_data.get('Bet365', 0.0),
                    'odds_1xbet': odds_data.get('1xBet', 0.0)
                }

                firebase_manager.add_unresolved_bet(fid, data)

                send_telegram(
                    f"🎯 **BET PLACED (Match {seq})**\n"
                    f"⏱ {min_elapsed}' | {match_name}\n"
                    f"🌍 {country} | 🏆 {league}\n"
                    f"🔢 Score: {score}\n"
                    f"💰 Stake: ${stake:.2f}\n"
                    f"📈 Bet365: {data['odds_bet365']} | 1xBet: {data['odds_1xbet']}"
                )

        state['bet_placed'] = True

    # =========================
    # 2. HT CHECK (UNCHANGED LOGIC)
    # =========================
    elif 'HALFTIME' in status:
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            outcome = 'win' if score == unresolved['36_score'] else 'loss'
            
            # Select the highest execution market price available for analytical valuation calculations
            execution_odds = max(unresolved.get('odds_bet365', 0.0), unresolved.get('odds_1xbet', 0.0))
            stake = unresolved.get('stake', 0.0)
            
            if outcome == 'win':
                pnl = (stake * execution_odds) - stake if execution_odds > 0 else 0.0
            else:
                pnl = -stake
                
            # Enrich original structural map with dashboard-friendly properties
            unresolved.update({
                'final_ht_score': score,
                'pnl': round(pnl, 2),
                'bet_odds_used': execution_odds,
                'roi_percentage': round((pnl / stake) * 100, 2) if stake > 0 else 0.0
            })

            firebase_manager.move_to_resolved(fid, unresolved, outcome)

            send_telegram(
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} HT\n"
                f"{match_name}\n"
                f"Score: {score}\n"
                f"📊 PnL: ${unresolved['pnl']:.2f} ({unresolved['roi_percentage']}% ROI)"
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
        logger.error(f"Error in execution loop: {e}")
