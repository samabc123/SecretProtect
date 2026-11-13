"""Single-process service entry; paths are independent of the caller's cwd."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "platform"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("DATA_SOURCE", "sqlite")
os.environ.setdefault("CONNECTOR_NETWORK_ENABLED", "false")
os.environ.setdefault("VERIFICATION_NETWORK_ENABLED", "false")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["web", "worker"])
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.service == "web":
        import uvicorn
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, proxy_headers=False)
    else:
        sys.argv = [sys.argv[0]]
        from app.run_protection_worker import main
        main()
