"""Independent durable CrowdSec polling and notification worker."""
import argparse
import time

from .auth_store import AuthError
from .protection import ProtectionStore, configured, deliver_notifications


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    store = ProtectionStore()
    store.initialize()
    while True:
        with store.connect() as c:
            c.execute("UPDATE engine_state SET worker_seen=? WHERE id=1", (time.time(),))
            due = c.execute("SELECT next_attempt FROM engine_state WHERE id=1").fetchone()[0]
        if configured() and due <= time.time():
            try:
                store.sync()
            except AuthError:
                pass
        deliver_notifications(store)
        if args.once:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
