import requests
import os
import json
import time
import logging
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
from esd.sofascore import SofascoreClient

# --- LOGGING SETUP ---
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
SLEEP_TIME = 55  # ⚡ PITCH OPTIMIZED: Guarantees sampling inside the 3-minute window
MINUTES_REGULAR_BET = [35, 36, 37]

# --- FILTERS ---
AMATEUR_KEYWORDS = ['amateur', 'youth', 'reserves', 'friendly', 'u18', 'u17', 'u16', 'u19', 'u22', 'u23', 'u21', 'u20', 'women', 'college']

# --- SMART OPTIMIZATION SETTINGS ---
PREDICT_START_MIN = 30     # start tracking match early
PRE_WARM_WINDOW = (34, 38) # only fully process in this window
MATCH_CACHE = {}           # smart tracking cache

# --- GLOBALS ---
SOFASCORE_CLIENT = None
firebase_manager = None
LOCAL_TRACKED_MATCHES = {}

# =========================
# FIREBASE MANAGER
# =========================
class FirebaseManager:
    def __init__(self, creds_json):
        self.db = None
        if not creds_json:
            logger.error("❌ Firebase Credentials missing from environment variables!")
            return
        try:
            logger.info("Parsing Firebase credential JSON...")
            cred_dict = json.loads(creds_json)
            cred = credentials.Certificate(cred_dict)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
                logger.info("Initializing new Firebase App connection.")
            self.db = firestore.client()
            logger.info("✅ Firebase Connection Successfully Established.")
        except Exception as e:
            logger.exception(f"❌ Firebase Initialization Error: {e}")

    def is_state_locked(self):
        logger.debug("Checking Firebase for unresolved locks...")
        try:
            unresolved_docs = self.db.collection('unresolved_bets').limit(1).get()
            is_locked = len(unresolved_docs) > 0
            if is_locked:
                logger.warning("🔒 Firebase state lock detected. Active bet sequence is outstanding.")
            else:
                logger.debug("🔓 No state lock found. Ready for new bets.")
            return is_locked
        except Exception as e:
            logger.error(f"❌ Error checking Firebase state lock: {e}")
            return True  # Fallback to locked to prevent duplicate betting on DB error

    def get_last_resolved_bet(self):
        logger.info("Fetching last resolved bet to determine progression sequence...")
        try:
            query = self.db.collection('resolved_bets')\
                .order_by('resolution_timestamp', direction=firestore.Query.DESCENDING)\
                .limit(1).get()
            
            for doc in query:
                bet_data = doc.to_dict()
                logger.info(f"📋 Last resolved bet found: ID {doc.id} | Outcome: {bet_data.get('outcome')} | Seq: {bet_data.get('match_sequence')}")
                return bet_data
            
            logger.info("📋 No historical resolved bets discovered. Fresh progression path starting.")
            return None
        except Exception as e:
            logger.exception(f"❌ Error pulling last resolved bet: {e}")
            return None

    def add_unresolved_bet(self, match_id, data):
        placed_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        data['placed_at'] = placed_time
        logger.info(f"💾 Saving unresolved bet to Firebase: Match ID {match_id} | Data: {data}")
        try:
            self.db.collection('unresolved_bets').document(str(match_id)).set(data)
            logger.info(f"✅ Document successfully written to 'unresolved_bets' for ID {match_id}")
        except Exception as e:
            logger.exception(f"❌ Critical: Failed to save unresolved bet for ID {match_id}: {e}")

    def get_unresolved_bet(self, match_id):
        logger.debug(f"🔍 Checking document store for unresolved match ID: {match_id}")
        try:
            doc = self.db.collection('unresolved_bets').document(str(match_id)).get()
            if doc.exists:
                logger.info(f"🎯 Unresolved bet data loaded for Match ID {match_id}")
                return doc.to_dict()
            logger.debug(f"No unresolved bet record exists for Match ID {match_id}")
            return None
        except Exception as e:
            logger.error(f"❌ Error downloading unresolved document {match_id}: {e}")
            return None

    def move_to_resolved(self, match_id, data, outcome):
        resolved_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        data.update({
            'outcome': outcome,
            'resolved_at': resolved_time,
            'resolution_timestamp': firestore.SERVER_TIMESTAMP
        })
        logger.info(f"🔄 Migrating Match ID {match_id} from unresolved -> resolved. Outcome: {outcome.upper()}")
        try:
            self.db.collection('resolved_bets').document(str(match_id)).set(data)
            logger.info(f"✅ Added to 'resolved_bets' collection for ID {match_id}")
            self.db.collection('unresolved_bets').document(str(match_id)).delete()
            logger.info(f"✅ Dropped from 'unresolved_bets' collection for ID {match_id}")
            return True
        except Exception as e:
            logger.exception(f"❌ Error during database migration lifecycle for Match ID {match_id}: {e}")
            return False

# =========================
# TELEGRAM COMMUNICATOR
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    logger.info(f"📱 Dispatching Telegram Message Alert...")
    logger.debug(f"Telegram Text Payload: [ {msg} ]")
    try:
        response = requests.post(
            url,
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
        if response.status_code == 200:
            logger.info("📱 Telegram alert successfully received by Telegram Gateway API Server.")
        else:
            logger.warning(f"⚠️ Telegram returned abnormal response status code: {response.status_code} | Body: {response.text}")
    except Exception as e:
        logger.error(f"❌ Network/Timeout error sending Telegram webhook event: {e}")

# =========================
# WAGER MANAGEMENT
# =========================
def calculate_stake():
    logger.info("Calculating next staking allocation multiplier...")
    last = firebase_manager.get_last_resolved_bet()
    
    if not last:
        logger.info(f"Staking default path: No history. Initializing Base Stake: ${ORIGINAL_STAKE} (Sequence #1)")
        return ORIGINAL_STAKE, 1
        
    outcome = last.get('outcome')
    if outcome == 'win':
        logger.info(f"Staking default path: Prior bet was a WIN. Resetting to Base Stake: ${ORIGINAL_STAKE} (Sequence #1)")
        return ORIGINAL_STAKE, 1
        
    seq = last.get('match_sequence', 1)
    if seq < MAX_CHASE_LEVEL:
        chase_stake = float(ORIGINAL_STAKE * (2**seq))
        next_seq = seq + 1
        logger.warning(f"⚠️ Staking progression path: Prior bet was a LOSS. Progressing to Chase Level {next_seq} | New Calculated Stake: ${chase_stake:.2f}")
        return chase_stake, next_seq
        
    logger.error(f"🚨 CRITICAL: Sequence hit maximum progression limit ({MAX_CHASE_LEVEL}). Hard reset back to Sequence #1. Absorbing loss of tier levels.")
    return ORIGINAL_STAKE, 1

# =========================
# SMART PREDICTION LOGIC
# =========================
def should_pre_warm(minute):
    decision = minute >= PREDICT_START_MIN
    logger.debug(f"Smart Engine Assessment: Minute {minute} >= Threshold {PREDICT_START_MIN}? Result: {decision}")
    return decision

def is_in_active_window(minute):
    decision = PRE_WARM_WINDOW[0] <= minute <= PRE_WARM_WINDOW[1]
    logger.debug(f"Smart Engine Window Bracket Assessment: Minute {minute} inside {PRE_WARM_WINDOW}? Result: {decision}")
    return decision

# =========================
# CORE MATCH LOGIC PIPELINE
# =========================
def process_match(match):
    fid = str(match.id)
    league = match.tournament.name
    country = match.tournament.category.name
    full_info = f"{league} {country}".lower()
    match_name = f"{match.home_team.name} vs {match.away_team.name}"

    logger.debug(f"Processing Match Input Feed: '{match_name}' (ID: {fid})")

    # Filter validations
    if any(keyword.lower() in full_info for keyword in AMATEUR_KEYWORDS):
        logger.debug(f"⏩ Dropping match '{match_name}' ({fid}). Matched restricted amateur keyword constraint rules.")
        return

    status = match.status.description.upper()
    score = f"{match.home_score.current}-{match.away_score.current}"

    # =========================================================================
    # 🎯 FIX: EXCLUSIVE FOCUS ON LIVE MATCH PITCH TIME ONLY
    # =========================================================================
    live_pitch_minute = None
    is_first_half_phase = False

    if status.isdigit():
        live_pitch_minute = int(status)
        if live_pitch_minute <= 45:
            is_first_half_phase = True
    elif status == 'HT' or 'HALFTIME' in status or 'HALF' in status:
        live_pitch_minute = 45
        is_first_half_phase = True
    elif '1ST' in status:
        is_first_half_phase = True
        live_pitch_minute = match.total_elapsed_minutes  # fallback calculation only if digital block missing

    # If the live pitch time could not be parsed, or it's clearly a 2nd half status string, ignore it.
    if live_pitch_minute is None:
        logger.debug(f"⏩ Skipping match processing '{match_name}' ({fid}). Cannot determine live pitch minute from status: {status}")
        return

    # Optimization Filter Gate (Now strictly tracking actual pitch minutes)
    if not should_pre_warm(live_pitch_minute):
        logger.debug(f"⏩ Skipping match processing '{match_name}' ({fid}). Live pitch clock {live_pitch_minute}' sits below tracking threshold.")
        return  

    # CLEAN LOGS: Output relies entirely on the precise physical match timer
    logger.info(f"🔍 Analyzing match state: {match_name} | Live Min: {live_pitch_minute}' | Score: {score} | Status: {status}")

    # Synchronize internal tracking dictionary state updates
    state = LOCAL_TRACKED_MATCHES.get(fid, {
        'bet_placed': False,
        'last_seen': time.time(),
        'active': False
    })
    state['last_seen'] = time.time()

    if is_in_active_window(live_pitch_minute):
        if not state['active']:
            logger.info(f"🔥 Match '{match_name}' has entered its active target evaluation window.")
        state['active'] = True

    LOCAL_TRACKED_MATCHES[fid] = state

    # =========================================================================
    # PHASE 1: BET PLACEMENT EVALUATION (STRICT LIVE PITCH TIME ONLY)
    # =========================================================================
    if is_first_half_phase and (live_pitch_minute in MINUTES_REGULAR_BET) and not state['bet_placed']:
        logger.info(f"🎯 Execution Target Window Hit for '{match_name}' (Live Pitch Min: {live_pitch_minute}'). Checking qualification criteria...")
        
        if firebase_manager.is_state_locked():
            logger.warning(f"🚫 Qualification rejected for '{match_name}'. System is locked awaiting another unresolved wager resolution event.")
        else:
            logger.info(f"Target system metrics passed. Confirming score value line compatibility: [ {score} ]")
            if score in ['1-1', '2-2', '2-1', '2-0']:
                logger.warning(f"⚡ MATCH QUALIFIED! Placing bet on '{match_name}' at live pitch minute {live_pitch_minute}' with live score line {score}!")
                
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
                    f"🎯 **BET PLACED (Match {seq})**\n⏱ Real Min: {live_pitch_minute}' | {match_name}\n🌍 {country} | 🏆 {league}\n🔢 Score: {score}\n💰 Stake: ${stake:.2f}"
                )
            else:
                logger.info(f"❌ Score condition pattern did not meet rules for '{match_name}' (Score: {score}). Skipping bet trigger.")

        state['bet_placed'] = True
        LOCAL_TRACKED_MATCHES[fid] = state

    # =========================
    # PHASE 2: HALFTIME VALUE CHECK
    # =========================
    elif status == 'HT' or 'HALFTIME' in status:
        logger.debug(f"Match context state indicates HALFTIME status for ID {fid}. Querying resolution tracking flags...")
        unresolved = firebase_manager.get_unresolved_bet(fid)

        if unresolved:
            logger.info(f"🏁 Evaluating pending bet resolution for match '{match_name}'...")
            trigger_score = unresolved.get('36_score')
            
            if score == trigger_score:
                outcome = 'win'
                logger.warning(f"✅ WIN VERIFIED! Halftime score matches tracking parameters ({score} == {trigger_score}) for '{match_name}'.")
            else:
                outcome = 'loss'
                logger.warning(f"❌ LOSS VERIFIED! Halftime score diverged from tracking parameters ({score} != {trigger_score}) for '{match_name}'.")
                
            firebase_manager.move_to_resolved(fid, unresolved, outcome)
            send_telegram(
                f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} HT\n{match_name}\nScore: {score}"
            )

            # Purge from memory maps
            LOCAL_TRACKED_MATCHES.pop(fid, None)
            logger.info(f"🧹 Cleaned match entry ID {fid} from volatile memory maps.")

# =========================
# SERVICE LIFECYCLE MANAGEMENT
# =========================
def initialize_bot_services():
    global firebase_manager, SOFASCORE_CLIENT
    logger.info("Initializing background processing services...")
    
    firebase_manager = FirebaseManager(FIREBASE_CREDENTIALS)

    try:
        logger.info("Instantiating headless connection interfaces for Sofascore client driver layers...")
        SOFASCORE_CLIENT = SofascoreClient()
        SOFASCORE_CLIENT.initialize()
        logger.info("✅ Background Sofascore scraping context driver initialized successfully.")
        return True
    except Exception as e:
        logger.exception(f"❌ Failed to spin up system components or network bindings: {e}")
        return False

def shutdown_bot():
    logger.warning("Application shutdown lifecycle initiated.")
    if SOFASCORE_CLIENT:
        try:
            logger.info("Closing active driver ports and browser sessions safely...")
            SOFASCORE_CLIENT.close()
            logger.info("✅ Driver connections terminated cleanly.")
        except Exception as e:
            logger.error(f"❌ Error caught closing service contexts during shutdown: {e}")

# =========================
# PERIODIC LOOP INSTANCE
# =========================
def run_bot_cycle():
    if not SOFASCORE_CLIENT:
        logger.error("Skipping scraping cycle execution: Sofascore driver interface object is uninitialized or dead.")
        return

    try:
        logger.info("Requesting live matches event feed array from underlying data services...")
        events = SOFASCORE_CLIENT.get_events(live=True)

        if not events:
            logger.warning("⚠️ Received empty response array structure from client query endpoint framework.")
            return

        logger.info(f"🔄 Scan metrics updated: Tracking {len(events)} real-time live events. Dispatching evaluations...")

        for m in events:
            try:
                process_match(m)
            except Exception as inner_ex:
                logger.error(f"❌ Exception caught processing evaluation steps for single index node: {inner_ex}", exc_info=True)

    except Exception as e:
        logger.error(f"💥 Runtime Exception error caught running central execution script loops: {e}", exc_info=True)

# =========================
# APPLICATION INSTANTIATION
# =========================
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("🚀 BETTING STRATEGY BOT INITIALIZATION SEQUENCE STARTING")
    logger.info("=" * 60)
    
    if not initialize_bot_services():
        logger.critical("❌ Core systems boot dependency failure. Program terminating.")
        exit(1)
        
    cycle_counter = 0
    try:
        while True:
            cycle_counter += 1
            logger.info(f"🏁 --- EXECUTION RUNNING FOR CYCLE INTERVAL BLOCK #{cycle_counter} ---")
            
            run_bot_cycle()
            
            logger.info(f"Memory Diagnostics: Currently monitoring {len(LOCAL_TRACKED_MATCHES)} matches inside internal maps.")
            logger.info(f"💤 Cycle completed. Putting application thread to sleep for {SLEEP_TIME} seconds...")
            time.sleep(SLEEP_TIME)
            
    except KeyboardInterrupt:
        logger.warning("🛑 Key-interruption event triggered by host administrator console. Terminating bot loops.")
    except Exception as fatal_ex:
        logger.critical(f"💥 System crashed from fatal root application thread breakdown exception: {fatal_ex}", exc_info=True)
    finally:
        shutdown_bot()
        logger.info("👋 Engine terminated. Execution stream closed.")
