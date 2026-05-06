"""
ArbBot — Module Index

Core pipeline:
  utils            config, web3, ABIs, logging, Telegram
  scout            token filtering (GeckoTerminal)
  monitor          hybrid monitoring (Static/WebSocket)
  executor         flash loan arbitrage execution
  arb_math         optimal trade size calculation

Infrastructure:
  database         JSON persistence: watchlist, history, stats
"""
from .utils              import load_config, setup_logging, get_web3, get_account
from .monitor            import ArbMonitor
from .executor           import ArbExecutor
from .scout              import update_watchlist
from .database           import get_stats, init_db
