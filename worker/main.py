#worker/main.py
# main.py
"""
Orchestration supervisor engine managing thread execution, 
health assertions, signal traps, and Prometheus telemetry metrics exposition.
"""

import time
import signal
import sys
import os
import logging
from prometheus_client import start_http_server

# Import core loop routines and internal application memory states
from bot import (
    run_bot_cycle,
    SLEEP_TIME,
    initialize_bot_services,
    shutdown_bot,
    send_telegram,
    LOCAL_TRACKED_MATCHES
)

# Import shared metric counters to expose and manipulate state updates
from metrics import CYCLE_COUNTER, MATCHES_TRACKED_GAUGE, API_FAILURES

# Setup isolated logger namespace for supervisor context tracking
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler("bot_activity.log"), logging.StreamHandler()]
)
logger = logging.getLogger("BetBot.Supervisor")

# --- CONSTRAINTS ---
WATCHDOG_LIMIT = 300       
REBOOT_LIMIT = 86400       
HEARTBEAT_LIMIT = 3600     
METRICS_PORT = int(os.getenv("PORT", 8000))  # Auto-binds to your Railway environment port allocation

RUNNING = True
LAST_REBOOT = time.time()
LAST_HEARTBEAT = time.time()

# --------------------------------------------------
# SIGNAL TRAP HANDLERS
# --------------------------------------------------
def handle_shutdown_signal(signum, frame):
    global RUNNING
    logger.warning(f"⚠️ OS Termination interrupt captured ({signum}). Setting loop exit parameters...")
    RUNNING = False

# --------------------------------------------------
# SYSTEM RESTORATION ROUTINES
# --------------------------------------------------
def execute_safe_recovery_handshake() -> bool:
    logger.warning("🔄 Initiating service recovery loop execution...")
    send_telegram("⚠️ Supervisor Core: Initiating automatic service layer recovery sequence...")
    API_FAILURES.inc()  # 📊 Increment recovery/failure diagnostic gauges
    
    try:
        shutdown_bot()
    except Exception as e:
        logger.error(f"Error dropping active services during recovery context: {e}")

    retry_count = 0
    while RUNNING:
        retry_count += 1
        logger.info(f"Attempting full platform re-mount sequence step #{retry_count}...")
        if initialize_bot_services():
            logger.info("✅ Platform modules successfully recovered.")
            send_telegram("✅ Supervisor Core: Service recovery completed. Bot is back online.")
            return True
        time.sleep(30)
    return False

def can_safely_reboot() -> bool:
    if not LOCAL_TRACKED_MATCHES:
        return True
    active_matches = [fid for fid, s in LOCAL_TRACKED_MATCHES.items() if s.get('active', False)]
    return len(active_matches) == 0

# --------------------------------------------------
# APPLICATION RUNTIME ORCHESTRATION ENTRY
# --------------------------------------------------
def main():
    global LAST_REBOOT, LAST_HEARTBEAT

    logger.info(f"🚀 INITIALIZING SUPERVISOR MANAGER ASSET PROCESS. PID={os.getpid()}")

    # Attach kernel operational interrupt triggers
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)

    # 📊 START THE PROMETHEUS METRICS ENDPOINT SERVER
    try:
        start_http_server(METRICS_PORT)
        logger.info(f"📊 Prometheus telemetry instrumentation pipeline online at port :{METRICS_PORT}/metrics")
    except Exception as metrics_ex:
        logger.critical(f"💥 Failed to lock Prometheus HTTP socket port allocation: {metrics_ex}")

    # Core Startup Routine
    if not initialize_bot_services():
        logger.critical("💥 Initial core component initialization failed! Terminating Supervisor process.")
        sys.exit(1)

    send_telegram("🚀 **BetBot Daemon Supervisor Online**\nTelemetry Tracking Server: Active")

    while RUNNING:
        cycle_start_time = time.time()
        CYCLE_COUNTER.inc()  # 📊 Telemetry: Increment absolute monitoring iterations metric

        try:
            # Execute business logic queries
            run_bot_cycle()

            # Push live cache data structure updates to Prometheus Gauge
            MATCHES_TRACKED_GAUGE.set(len(LOCAL_TRACKED_MATCHES))

            # Watchdog Execution Time Limit Assertion
            elapsed_cycle_time = time.time() - cycle_start_time
            if elapsed_cycle_time > WATCHDOG_LIMIT:
                logger.error(f"🚨 Watchdog breached: Process execution took {elapsed_cycle_time:.2f}s.")
                if not execute_safe_recovery_handshake():
                    break

            # Smart Maintenance Window Assertion
            if time.time() - LAST_REBOOT > REBOOT_LIMIT:
                if can_safely_reboot():
                    logger.warning("Scheduled maintenance window verified open. Executing restart execution pass...")
                    if execute_safe_recovery_handshake():
                        LAST_REBOOT = time.time()
                else:
                    LAST_REBOOT += 300  # Shift evaluation window forward 5 minutes

            # Telegram Heartbeat Status Update Verification Node
            if time.time() - LAST_HEARTBEAT > HEARTBEAT_LIMIT:
                send_telegram(f"💓 **Heartbeat Pulse:** Bot Status: Active\n📊 Cached Matches: {len(LOCAL_TRACKED_MATCHES)}")
                LAST_HEARTBEAT = time.time()

        except Exception as e:
            logger.error(f"💥 Top level supervisor loop runtime exception failure: {e}", exc_info=True)
            API_FAILURES.inc()
            time.sleep(15)

        finally:
            if RUNNING:
                # Calculate processing execution latency to keep loop sleep iterations perfectly consistent
                execution_latency = time.time() - cycle_start_time
                dynamic_sleep = max(1.0, SLEEP_TIME - execution_latency)
                time.sleep(dynamic_sleep)

    # Clean Exit Path Execution Blocks
    logger.warning("🛑 Closing application loops. Freeing structural process parameters...")
    try:
        shutdown_bot()
    except Exception as e:
        logger.error(f"Error handling system breakdown operations: {e}")

if __name__ == "__main__":
    main()
