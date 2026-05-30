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
MINUTES_REGULAR_BET = [35, 36, 37]
BOOKMAKERS_TO_CHECK = ['Bet365', '1xBet']

# --- FILTERS ---
ALLOWED_LEAGUES = ['Campeonato Brasileiro Série A', 'Segunda Division, Apertura', 'Copa do Brasil', 'Premier League', 'Copa Colombia']
EXCLUDED_LEAGUES = ['USA', 'Poland','Australia', 'Mexico', 'Wales', 'Germany', 'England Amateur', 'U19', 'U21', 'Friendly']
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u23', 'u21','u20', 'women', 'college']

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

# =========================
# DYNAMIC FIRST HALF JIT ODDS CALCULATOR (FIXED)
# =========================
def fetch_odds_triggered(home_team, away_team, current_score):
    """
    Robust JIT fetching targeting 1st Half Goals Over/Under markets dynamically.
    Includes token-based team matching algorithms and wider time parsing bounds.
    """
    # FIX: Expanded looking window from 60 to 300 seconds to prevent empty returns due to container time drift
    since = int(time.time()) - 300
    results = {'Bet365': 0.0, '1xBet': 0.0}
    
    try:
        home_g, away_g = map(int, current_score.split('-'))
        total_current_goals = home_g + away_g
        target_threshold = float(total_current_goals + 0.5) 
    except Exception as e:
        logger.error(f"Failed parsing score line mapping to 1st half totals target: {e}")
        return results

    # Optimize tokenization arrays for fallback cross-matching variations
    h_tokens = [t.strip().lower() for t in home_team.split(' ') if len(t.strip()) > 3]
    a_tokens = [t.strip().lower() for t in away_team.split(' ') if len(t.strip()) > 3]

    for bkr in BOOKMAKERS_TO_CHECK:
        url = f"https://api.odds-api.io/v3/odds/updated?apiKey={ODDS_API_KEY}&since={since}&bookmaker={bkr}&sport=Football&market=1st_half_totals"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for match_odds in data.get('odds', []):
                    o_home = match_odds['home'].lower()
                    o_away = match_odds['away'].lower()
                    
                    # FIX: Token-containment validation logic to resolve alternative naming naming conventions
                    matched = (home_team.lower() in o_home or any(tk in o_home for tk in h_tokens)) and \
                              (away_team.lower() in o_away or any(tk in o_away for tk in a_tokens))
                              
                    if matched:
                        under_odds = next(
                            (o['odds'] for o in match_odds.get('outcomes', []) 
                             if o['name'].lower() == 'under' and float(o.get('handicap', o.get('param', 0))) == target_threshold), 
                            0.0
                        )
                        results[bkr] = float(under_odds)
                        break # break the match inner search loop once verified
        except Exception as e:
            logger.error(f"REST API 1st Half Totals fetch error for {bkr}: {e}")
            
    return results

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
        'processed_ht': False  # FIX: Escape track to handle multi-cycle lockups at half-time
    })
    state['last_seen'] = time.time()

    if is_in_active_window(min_elapsed):
        state['active'] = True

    LOCAL_TRACKED_MATCHES[fid] = state

    # =========================
    # 1. PLACE BET (TRIGGER DRIVEN)
    # =========================
    if '1ST' in status and min_elapsed in MINUTES_REGULAR_BET and not state['bet_placed']:
        if not firebase_manager.is_state_locked():
            
            if score in ['1-1', '2-2', '3-3']:
                home_goals = int(score.split('-')[0])
                market_label = f"1st Half Under {int(home_goals * 2) + 0.5}" 
                
                logger.info(f"🎯 Strategy Triggered for {match_name} ({score}). Pulling {market_label} lines...")
                odds_data = fetch_odds_triggered(match.home_team.name, match.away_team.name, score)
                
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
                    f"🎯 **BET PLACED (Match {seq})**\n⏱ {min_elapsed}' | {match_name}\n🌍 {country} | 🏆 {league}\n🔢 Score: {score}\n💰 Stake: ${stake:.2f}\n"
                    f"📈 {market_label} Market -> Bet365: {data['odds_bet365']} | 1xBet: {data['odds_1xbet']}"
                )

        state['bet_placed'] = True

    # =========================
    # 2. HT CHECK (FIXED SINGLE-SHOT EXECUTION)
    # =========================
    elif 'HALFTIME' in status and not state['processed_ht']:
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            outcome = 'win' if score == unresolved['36_score'] else 'loss'
            
            execution_odds = max(unresolved.get('odds_bet365', 0.0), unresolved.get('odds_1xbet', 0.0))
            stake = unresolved.get('stake', 0.0)
            
            if outcome == 'win':
                pnl = (stake * execution_odds) - stake if execution_odds > 0 else 0.0
            else:
                pnl = -stake
                
            unresolved.update({
                'final_ht_score': score,
                'pnl': round(pnl, 2),
                'bet_odds_used': execution_odds,
                'roi_percentage': round((pnl / stake) * 100, 2) if stake > 0 else 0.0
            })

            firebase_manager.move_to_resolved(fid, unresolved, outcome)

            send_telegram(
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} HT\n{match_name}\n"
                f"Score: {score}\n📊 PnL: ${unresolved['pnl']:.2f} ({unresolved['roi_percentage']}% ROI)"
            )
            
            # FIX: Mutate execution check flag status so subsequent sleep cycles bypass re-evaluation
            state['processed_ht'] = True
            LOCAL_TRACKED_MATCHES[fid] = state
            
            # Safely scrub tracking index
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
