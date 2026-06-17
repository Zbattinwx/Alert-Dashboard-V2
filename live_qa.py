"""
Live QA + Training Data Collector (WebSocket CLI)
==================================================
Standalone CLI that connects to the dashboard WebSocket and prints the same
per-cell QA report the in-process reporter prints to the backend logs.

In most cases you DO NOT need to run this — the backend now starts the same
reporter in-process whenever `live_qa_enabled = true` (default).  Use this
CLI when:
  - watching a remote backend (separate host)
  - the in-process reporter is disabled
  - you want a separate JSONL file from the backend's training log

Usage:
    python live_qa.py                      # watch-only, no logging
    python live_qa.py --log                # log all cells to data/training_data.jsonl
    python live_qa.py --log --min-score 0  # log every cell, even weak ones
    python live_qa.py --host 192.168.1.50  # connect to remote host
    python live_qa.py --port 3075          # custom port
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import websockets

# Reuse the in-process reporter's display + record builders so the two paths
# never drift out of sync.
import sys
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.live_qa_service import (  # noqa: E402
    LiveQAReporter,
    build_training_record,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LiveQA")


async def run(host: str, port: int, log_file: Path | None, min_score: int, verbose: bool):
    uri = f"ws://{host}:{port}/ws"
    logger.info(f"Connecting to {uri} ...")

    reporter = LiveQAReporter(log_file=None, min_score=min_score, verbose=verbose)
    if log_file:
        logger.info(f"Logging training data to {log_file}")
        log_file.parent.mkdir(parents=True, exist_ok=True)

    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=30) as ws:
                logger.info("Connected.")
                reconnect_delay = 5

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type", "")

                    if msg_type in ("storm_cells", "STORM_CELLS"):
                        cells = msg.get("data") or []
                        # The reporter expects objects with to_dict(); WebSocket
                        # cells are already dicts, so wrap them with a stub.
                        class _Wrap:
                            def __init__(self, d): self._d = d
                            def to_dict(self): return self._d
                        await reporter.on_cells([_Wrap(c) for c in cells])

                        if log_file:
                            scan_ts = datetime.now(timezone.utc).isoformat()
                            with log_file.open("a", encoding="utf-8") as f:
                                for cell in cells:
                                    record = build_training_record(cell, scan_ts)
                                    f.write(json.dumps(record) + "\n")

                    elif msg_type == "AGENT_NOTIFICATION":
                        notif = (msg.get("data") or {})
                        logger.info(f"\n>>> AGENT: {notif.get('content')}\n")

                    elif msg_type in ("radar_status", "RADAR_STATUS"):
                        status = msg.get("data") or {}
                        sites = status.get("active_sites") or []
                        logger.info(f"[radar] sites={sites}  processing={status.get('processing')}")

        except (ConnectionRefusedError, OSError) as e:
            logger.error(f"Connection failed: {e}. Retrying in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Connection closed ({e}). Retrying in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
        except KeyboardInterrupt:
            logger.info("Stopped.")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            await asyncio.sleep(reconnect_delay)


def main():
    parser = argparse.ArgumentParser(description="Alert Dashboard live QA monitor (WebSocket CLI)")
    parser.add_argument("--host",      default="localhost")
    parser.add_argument("--port",      type=int, default=3074)
    parser.add_argument("--log",       action="store_true",
                        help="Append all cells to data/training_data.jsonl")
    parser.add_argument("--log-file",  default="data/training_data.jsonl",
                        help="Override log file path (implies --log)")
    parser.add_argument("--min-score", type=int, default=30,
                        help="Only display cells with score >= this (default 30)")
    parser.add_argument("--verbose",   action="store_true",
                        help="Show detail for every notable cell, not just flagged ones")
    args = parser.parse_args()

    log_file = None
    if args.log or args.log_file != "data/training_data.jsonl":
        log_file = Path(args.log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

    asyncio.run(run(
        host=args.host,
        port=args.port,
        log_file=log_file,
        min_score=args.min_score,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
