"""Run the Quantro API with Photon webhook payload logging.

Photon pushes inbound iMessages to POST /webhooks/photon, so this runner listens
rather than registering a URL. Expose the port publicly (e.g. a tunnel) and set
https://<public-host>/webhooks/photon as the webhook URL in Photon's dashboard.
Every signature-verified inbound payload is logged at INFO.

    backend/.venv/bin/python backend/scripts/photon_listen.py [--host 0.0.0.0] [--port 8000]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Quantro API with Photon webhook logging.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PHOTON_BACKEND_PORT", "8000")))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.path.insert(0, str(REPO_ROOT))
    logging.getLogger("photon_listen").info(
        "Photon webhook endpoint: http://%s:%d/webhooks/photon", args.host, args.port
    )
    uvicorn.run("backend.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
