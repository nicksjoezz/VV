import json
import time
from pathlib import Path
from .utils import ROOT_DIR

LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

WATCHLIST_PATH = LOGS_DIR / "watchlist.json"
HISTORY_PATH = LOGS_DIR / "history.json"
STATS_PATH = LOGS_DIR / "stats.json"

def _load_json(path, default):
    if not path.exists(): return default
    try:
        with open(path, "r") as f: return json.load(f)
    except: return default

def _save_json(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2)

class HistoryManager:
    def get_stats(self, watchlist_count):
        h = _load_json(HISTORY_PATH, [])
        total_profit = sum(float(r.get("estimated_profit", 0)) for r in h)

        # Today's stats
        now = time.time()
        today_start = now - (now % 86400)
        today_h = [r for r in h if r.get("timestamp", 0) >= today_start]
        today_profit = sum(float(r.get("estimated_profit", 0)) for r in today_h)

        max_gap = 0
        if h:
            try:
                # Include gap if it exists (from simulations)
                max_gap = max(float(r.get("gap", 0)) for r in h)
            except:
                pass

        return {
            "total_profit_est_usd": total_profit,
            "today_profit_est_usd": today_profit,
            "total_arbs": len(h),
            "today_arbs": len(today_h),
            "watchlist_count": watchlist_count,
            "max_gap": f"{max_gap:.2%}" if max_gap > 0 else "0.00%",
            "recent": h[::-1][:10]
        }

    def record_execution(self, record):
        h = _load_json(HISTORY_PATH, [])
        h.append(record)
        _save_json(HISTORY_PATH, h[-100:])

history = HistoryManager()
