import requests
import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode
import firebase_admin
from firebase_admin import credentials, firestore
from esd.sofascore import SofascoreClient
import websocket

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
GLOBAL_ODDS_CLIENT = None  # Holds the running WebSocket background stream

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
# INTEGRATED WEBSOCKET REAL-TIME STREAM CLIENT
# =========================
class OddsWebSocketClient:
    def __init__(self, api_key, markets, sport=None, leagues=None, status=None, bookmakers=None):
        self.api_key = api_key
        self.markets = markets
        self.sport = sport
        self.leagues = leagues
        self.status = status
        self.bookmakers = bookmakers
        self.ws = None
        self.should_reconnect = True
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self._reconnect_timer = None
        self.last_seq = 0
        self.odds_store = {}  # Cache structure: { "Home vs Away": { "Bet365": 2.10, "1xBet": 2.15 } }

    def build_url(self):
        params = {"apiKey": self.api_key, "markets": self.markets}
        if self.sport: params["sport"] = self.sport
        if self.leagues: params["leagues"] = self.leagues
        if self.status: params["status"] = self.status
        if self.last_seq > 0: params["lastSeq"] = str(self.last_seq)
        return f"wss://api.odds-api.io/v3/ws?{urlencode(params)}"

    def on_message(self, ws, message):
        for line in message.strip().split('\n'):
            line = line.strip()
            if not line: continue
            try:
                self._handle_parsed(json.loads(line))
            except Exception as e:
                logger.error(f"WebSocket parser error: {e}")

    def _handle_parsed(self, data):
        msg_type = data.get('type')
        seq = data.get('seq')
        if seq and seq > self.last_seq:
            self.last_seq = seq

        if msg_type in ('created', 'updated'):
            home = data.get('home', '')
            away = data.get('away', '')
            if not home or not away: return
            
            match_key = f"{home.lower()} vs {away.lower()}"
            bookie = data.get('bookie', '')
            
            if bookie in BOOKMAKERS_TO_CHECK:
                if match_key not in self.odds_store:
                    self.odds_store[match_key] = {}
                
                for market in data.get('markets', []):
                    if market.get('name') in ['ML', 'h2h']:
                        for outcome in market.get('odds', []):
                            if outcome.get('name', '').lower() in ['draw', 'x']:
                                self.odds_store[match_key][bookie] = float(outcome.get('odds', 0.0))

        elif msg_type == 'deleted':
            home = data.get('home', '')
            away = data.get('away', '')
            match_key = f"{home.lower()} vs {away.lower()}"
            self.odds_store.pop(match_key, None)

    def on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logger.warning(f"WebSocket disconnected. Reconnecting...")
        if self.should_reconnect:
            self.reconnect_attempts += 1
            if self.reconnect_attempts <= self.max_reconnect_attempts:
                delay = min(2 ** (self.reconnect_attempts - 1), 30)
                self._reconnect_timer = threading.Timer(delay, self._start_ws)
                self._reconnect_timer.daemon = True
                self._reconnect_timer.start()

    def on_open(self, ws):
        logger.info("✅ Live Odds WebSocket Feed Connected successfully.")
        self.reconnect_attempts = 0

    def _start_ws(self):
        self.ws = websocket.WebSocketApp(
            self.build_url(),
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        ws_thread = threading.Thread(target=self.ws.run_forever, kwargs={"ping_interval": 30, "ping_timeout": 10})
        ws_thread.daemon = True
        ws_thread.start()

    def start(self):
        self._start_ws()

    def lookup_cached_odds(self, home_team, away_team):
        """Cross-references Sofascore strings with WebSocket cache objects using clean normalization."""
        h_clean = home_team.lower()
        a_clean = away_team.lower()
        
        # Exact match verification
        direct_key = f"{h_clean} vs {a_clean}"
        if direct_key in self.odds_store:
            return self.odds_store[direct_key]
            
        # Segment search fallback for dynamic naming structures
        for stored_key, odds in self.odds_store.items():
            if h_clean in stored_key or a_clean in stored_key:
                return odds
        return {}

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
                
                # --- INSTANT IN-MEMORY LOCAL DATA EXTRACTION ---
                odds_data = {}
                if GLOBAL_ODDS_CLIENT:
                    odds_data = GLOBAL_ODDS_CLIENT.lookup_cached_odds(match.home_team.name, match.away_team.name)
                
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
                    f"📈 WebSocket Feed -> Bet365: {data['odds_bet365']} | 1xBet: {data['odds_1xbet']}"
                )

        state['bet_placed'] = True

    # =========================
    # 2. HT CHECK (UNCHANGED LOGIC)
    # =========================
    elif 'HALFTIME' in status:
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            outcome = 'win' if score == unresolved['36_score'] else 'loss'
            
            # Select best price from captured snapshot fields for performance analytics
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
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} HT\n{match_name}\nScore: {score}\n💵 PnL: ${unresolved['pnl']:.2f}"
            )

            LOCAL_TRACKED_MATCHES.pop(fid, None)

# =========================
# INIT
# =========================
def initialize_bot_services():
    global firebase_manager, SOFASCORE_CLIENT, GLOBAL_ODDS_CLIENT
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)

    # Initialize and spin up WebSocket Feed in background thread
    try:
        GLOBAL_ODDS_CLIENT = OddsWebSocketClient(
            api_key=ODDS_API_KEY,
            markets="ML",
            sport="football",
            status="live"
        )
        GLOBAL_ODDS_CLIENT.start()
        logger.info("✅ Live Streaming Infrastructure Initialized.")
    except Exception as e:
        logger.error(f"Failed to load background web stream: {e}")

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
        logger.error(f"Loop run structural fault encountered: {e}")
