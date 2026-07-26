"""Production soak: run the real app against the burst-capable mock venue for N minutes.

Use: python scripts/soak.py [--minutes 26] [--port 5560]
Depends on: tests.mock_hl_server, a repo checkout. Validates the full loop -
signals -> DB -> outcomes -> /stats - plus zero drops and a clean shutdown.
Artifacts land in data/soak/ (config, app log, final stats/health JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal as _signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.mock_hl_server import MockHLServer  # noqa: E402


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


async def main() -> int:
    ap = argparse.ArgumentParser(description="TapeScreen production soak vs mock venue")
    ap.add_argument("--minutes", type=float, default=26.0)
    ap.add_argument("--port", type=int, default=5560)
    args = ap.parse_args()
    min_uptime = args.minutes * 60.0
    hard_stop = min_uptime + 20 * 60.0

    out = REPO / "data" / "soak"
    out.mkdir(parents=True, exist_ok=True)
    cfg = (REPO / "config.yaml").read_text()
    coins = ["BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE", "NEAR", "TAO", "WLD",
             "ZEC", "AERO", "PUMP", "LIT"]
    async with MockHLServer(coins, trade_interval=0.1, book_interval=2.0,
                            burst_every_s=120.0, burst_len_s=30.0) as mock:
        cfg = cfg.replace("wss://api.hyperliquid.xyz/ws", mock.url)
        cfg = cfg.replace("https://api.hyperliquid.xyz/info", "http://127.0.0.1:9/info")
        cfg = cfg.replace("port: 5560", f"port: {args.port}")
        cfg = cfg.replace("path: recordings", f"path: {out}/recordings")
        cfg = cfg.replace("path: data/tapescreen.db", f"path: {out}/soak.db")
        cfg = cfg.replace("path: logs", f"path: {out}/logs")
        # symbols_file resolves relative to the config file's directory
        cfg = cfg.replace("symbols_file: symbol_map.yaml",
                          f"symbols_file: {REPO}/symbol_map.yaml")
        cfg = cfg.replace("baseline_window_s: 1800", "baseline_window_s: 300")
        cfg_path = out / "soak.yaml"
        cfg_path.write_text(cfg)

        app = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tapescreen", "--config", str(cfg_path),
            cwd=REPO, stdout=(out / "app.log").open("w"), stderr=subprocess.STDOUT,
        )
        t0 = time.time()
        ok, reason, rc = False, "timeout", -1
        try:
            await asyncio.sleep(10)
            while time.time() - t0 < hard_stop:
                if app.returncode is not None:
                    reason = f"app exited early rc={app.returncode}"
                    break
                try:
                    h = _get(args.port, "/health")
                    s = _get(args.port, "/stats")
                except Exception as e:  # noqa: BLE001 - report and stop the soak
                    reason = f"endpoint error: {e}"
                    break
                eng, feed = h["engine"], h["feed"]
                completed = s["totals"].get("completed") or 0
                print(f"[{eng['uptime_s']:7.0f}s] events={eng['events_total']} "
                      f"signals={eng['signals_total']} outcomes={completed} "
                      f"qdrops={feed.get('queue_drops')} dbErr={eng['db_write_errors']} "
                      f"stale={len(eng['stale_symbols'])}", flush=True)
                if eng["uptime_s"] >= min_uptime and completed >= 3 and eng["signals_total"] >= 3:
                    ok, reason = True, "criteria met"
                    break
                await asyncio.sleep(30)

            try:
                final_stats = _get(args.port, "/stats")
                final_health = _get(args.port, "/health")
                (out / "final_stats.json").write_text(json.dumps(final_stats, indent=1))
                (out / "final_health.json").write_text(json.dumps(final_health, indent=1))
                eng, feed = final_health["engine"], final_health["feed"]
                checks = {
                    "signals_logged": eng["signals_total"] >= 3,
                    "outcomes_completed>=3": (final_stats["totals"].get("completed") or 0) >= 3,
                    "zero_queue_drops": feed.get("queue_drops") == 0,
                    "zero_db_errors": eng["db_write_errors"] == 0,
                    "no_stale_symbols": len(eng["stale_symbols"]) == 0,
                }
                print("FINAL:", json.dumps(checks), flush=True)
                ok = ok and all(checks.values())
            except Exception as e:  # noqa: BLE001 - app already gone: report, fail
                print(f"final capture failed: {e}", flush=True)
                ok = False
        finally:
            try:
                app.send_signal(_signal.SIGTERM)
                rc = await asyncio.wait_for(app.wait(), timeout=20)
            except ProcessLookupError:
                rc = app.returncode if app.returncode is not None else -1
            except TimeoutError:
                app.kill()
                rc = -9
            print(f"app shutdown rc={rc}; soak={'PASS' if ok and rc == 0 else 'FAIL'} ({reason})",
                  flush=True)
        return 0 if ok and rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
