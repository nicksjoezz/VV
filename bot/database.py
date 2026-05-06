import time
import json
from .persistence import history

def get_stats():
    """Get arbitrage statistics."""
    watchlist_count = 0
    try:
        from .utils import ROOT_DIR
        with open(ROOT_DIR / "logs" / "watchlist.json", "r") as f:
            watchlist_count = len(json.load(f))
    except:
        pass

    return history.get_stats(watchlist_count)

def init_db():
    pass

def record_execution(record):
    history.record_execution(record)

def get_conn():
    return None
