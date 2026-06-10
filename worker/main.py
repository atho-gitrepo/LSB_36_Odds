import time
import signal
import sys
import os

from bot import (
    run_bot_cycle,
    SLEEP_TIME,
    initialize_bot_services,
    shutdown_bot,
    send_telegram
)

WATCHDOG_LIMIT = 300
REBOOT_LIMIT = 7200

RUNNING = True
LAST_REBOOT = time.time()
LAST_HEARTBEAT = time.time()

# --------------------------------------------------
# SIGNAL HANDLER
# --------------------------------------------------

def signal_handler(signum, frame):
    global RUNNING
    RUNNING = False

# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    global LAST_REBOOT, LAST_HEARTBEAT

    print(f"🚀 Bot Starting... PID={os.getpid()}")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # INIT
    if not initialize_bot_services():
        print("❌ Init failed")
        sys.exit(1)

    send_telegram("🚀 Bot Started Successfully")

    while RUNNING:
        try:
            start = time.time()

            run_bot_cycle()

            # WATCHDOG
            if time.time() - start > WATCHDOG_LIMIT:
                send_telegram("⚠️ Watchdog restart triggered")
                shutdown_bot()
                time.sleep(10)
                initialize_bot_services()

            # REBOOT SYSTEM (every 2h)
            if time.time() - LAST_REBOOT > REBOOT_LIMIT:
                send_telegram("🔄 Scheduled restart")
                shutdown_bot()
                time.sleep(5)
                initialize_bot_services()
                LAST_REBOOT = time.time()

            # HEARTBEAT
            if time.time() - LAST_HEARTBEAT > 14400:
                send_telegram("💓 Bot alive")
                LAST_HEARTBEAT = time.time()

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

        finally:
            if RUNNING:
                time.sleep(SLEEP_TIME)

    print("🛑 Stopping bot...")
    shutdown_bot()

# --------------------------------------------------
# ENTRY
# --------------------------------------------------

if __name__ == "__main__":
    main()