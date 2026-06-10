import time
import signal
import sys
import os

from bot import (
    run_bot_cycle,       # Matches bot.py function
    SLEEP_TIME,          # Matches bot.py variable
    initialize_bot_services, # Matches bot.py function
    shutdown_bot,        # Matches bot.py function
    send_telegram        # Matches bot.py function
)

# Watchdog settings
WATCHDOG_LIMIT = 300
REBOOT_LIMIT = 7200

RUNNING = True
LAST_REBOOT = time.time()
LAST_HEARTBEAT = time.time()

def signal_handler(signum, frame):
    global RUNNING
    RUNNING = False

def main():
    global LAST_REBOOT, LAST_HEARTBEAT

    print(f"🚀 Bot Starting... PID={os.getpid()}")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Initialize services from bot.py
    if not initialize_bot_services():
        print("❌ Init failed")
        sys.exit(1)

    send_telegram("🚀 Bot Started Successfully")

    while RUNNING:
        try:
            start = time.time()

            # Execute the unified cycle from bot.py
            run_bot_cycle()

            # Watchdog Logic
            if time.time() - start > WATCHDOG_LIMIT:
                send_telegram("⚠️ Watchdog restart triggered")
                shutdown_bot()
                time.sleep(10)
                initialize_bot_services()

            # Scheduled Reboot (Every 2 hours)
            if time.time() - LAST_REBOOT > REBOOT_LIMIT:
                send_telegram("🔄 Scheduled restart")
                shutdown_bot()
                time.sleep(5)
                initialize_bot_services()
                LAST_REBOOT = time.time()

            # Heartbeat (Every 4 hours)
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

if __name__ == "__main__":
    main()
