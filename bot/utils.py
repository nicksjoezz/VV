"""
utils.py — Config loading, Web3 setup, ABIs, shared helpers
"""

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Optional, List
from web3 import Web3
from eth_account import Account

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT_DIR   = Path(__file__).parent.parent
CONFIG_PATH = ROOT_DIR / "config.json"

# ── Config ───────────────────────────────────────────────────────────────────
_config: Optional[dict] = None

def load_config() -> dict:
    global _config
    if _config is None:
        with open(CONFIG_PATH) as f:
            _config = json.load(f)
    return _config

def cfg(*keys):
    """Deep-get config values: cfg('strategy','min_profit_usd')"""
    d = load_config()
    for k in keys:
        d = d[k]
    return d

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    config = load_config()
    log_cfg = config["logging"]
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    level = getattr(logging, log_cfg["level"].upper(), logging.INFO)
    logger = logging.getLogger("arb_bot")
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.handlers.RotatingFileHandler(
        ROOT_DIR / log_cfg["file"],
        maxBytes=log_cfg["max_bytes"],
        backupCount=log_cfg["backup_count"]
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger

logger = setup_logging()

# ── Web3 ──────────────────────────────────────────────────────────────────────
_public_w3: Optional[Web3] = None
_alchemy_w3: Optional[Web3] = None
_current_key_idx = 0

def get_public_web3() -> Web3:
    global _public_w3
    if _public_w3 is None or not _public_w3.is_connected():
        rpc = cfg("network", "rpc_http")
        _public_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 60}))
    return _public_w3

def get_alchemy_web3() -> Web3:
    """Alchemy connection with key rotation."""
    global _alchemy_w3, _current_key_idx

    keys = load_config()["network"].get("alchemy_keys", [])
    if not keys:
        return get_public_web3()

    # If already connected, just return it
    if _alchemy_w3 and _alchemy_w3.is_connected():
        return _alchemy_w3

    # Try keys sequentially
    for _ in range(len(keys)):
        key = keys[_current_key_idx]
        if key.startswith("http"):
            rpc = key
        else:
            rpc = f"https://arb-mainnet.g.alchemy.com/v2/{key}"
            
        try:
            _alchemy_w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if _alchemy_w3.is_connected():
                logger.info(f"Connected to Alchemy using key index {_current_key_idx}")
                return _alchemy_w3
        except Exception as e:
            logger.warning(f"Alchemy key {_current_key_idx} failed: {e}")

        # Rotate to next key
        _current_key_idx = (_current_key_idx + 1) % len(keys)

    logger.warning("All Alchemy keys failed, using Public fallback")
    return get_public_web3()

def get_web3() -> Web3:
    return get_alchemy_web3()

def get_account():
    try:
        pk = cfg("wallet", "private_key")
        if not pk or pk == "YOUR_PRIVATE_KEY_HERE":
            return None
        return Account.from_key(pk)
    except Exception:
        return None

# ── ABIs ──────────────────────────────────────────────────────────────────────
MULTICALL3_ADDR = cfg("network", "multicall3")
MULTICALL3_ABI  = json.loads('[{"inputs":[{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"aggregate","outputs":[{"internalType":"uint256","name":"blockNumber","type":"uint256"},{"internalType":"bytes[]","name":"returnData","type":"bytes[]"}],"stateMutability":"payable","type":"function"}]')

def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)

def notify(message: str):
    # Telegram notifications logic if needed
    pass
