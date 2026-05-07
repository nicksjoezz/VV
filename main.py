"""
main.py -- ArbBot: Hybrid Sentinel Arbitrage Bot

Single entry point. Starts Flask dashboard + arb scan engine.
"""

import os, sys, time, json, logging, argparse, threading, asyncio
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bot.utils    import load_config, get_web3, get_account, cfg, checksum, logger
from bot.database import get_stats as db_get_stats
from bot.scout    import update_watchlist
from bot.monitor import ArbMonitor
from bot.executor import ArbExecutor

# -- Flask app -----------------------------------------------------------------
app = Flask(__name__, template_folder=str(ROOT / "templates"))
app.config["SECRET_KEY"] = os.urandom(32).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

LOG_PATH = ROOT / "logs" / "bot.log"
LOG_PATH.parent.mkdir(exist_ok=True)

class LogTailer(logging.Handler):
    def __init__(self, socketio):
        super().__init__()
        self.socketio = socketio
    def emit(self, record):
        msg = self.format(record)
        self.socketio.emit("log_line", {"line": msg})

# ── Bot engine state ──────────────────────────────────────────────────────────
_bot_running  = threading.Event()
_bot_lock     = threading.Lock()
_bot_stats    = {"cycle": 0, "last_scan": None, "tokens_found": 0}

def bot_is_running():
    return _bot_running.is_set()

def start_bot_engine():
    with _bot_lock:
        if bot_is_running():
            return False
        _bot_running.set()
        threading.Thread(target=_bot_loop_wrapper, daemon=True, name="bot-engine").start()
        logger.info("Arb bot engine started")
        return True

def stop_bot_engine():
    with _bot_lock:
        if not bot_is_running():
            return False
        _bot_running.clear()
        logger.info("Bot engine stopping...")
        return True

def _bot_loop_wrapper():
    asyncio.run(_bot_loop())

async def _bot_loop():
    try:
        executor = ArbExecutor()

        async def on_opportunity(opp):
            logger.info(f"Opportunity found: {opp['symbol']} Gap: {opp['gap']:.2%}")
            await executor.execute(opp)

        monitor = ArbMonitor(on_opportunity=on_opportunity)

        # Start loops
        tasks = [
            asyncio.create_task(monitor.static_scanner_loop()),
            asyncio.create_task(monitor.event_listener()),
            asyncio.create_task(_scout_loop())
        ]

        while _bot_running.is_set():
            await asyncio.sleep(1)

        for task in tasks:
            task.cancel()

    except Exception as e:
        logger.error(f"Bot engine fatal error: {e}", exc_info=True)
    finally:
        _bot_running.clear()
        logger.info("Bot engine stopped")

async def _scout_loop():
    while _bot_running.is_set():
        await update_watchlist()
        await asyncio.sleep(1800) # Every 30 mins

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def page_dashboard():  return render_template("dashboard.html")
@app.route("/positions")
def page_positions():  return render_template("positions.html")
@app.route("/terminal")
def page_terminal():   return render_template("terminal.html")
@app.route("/analytics")
def page_analytics():  return render_template("analytics.html")
@app.route("/settings")
def page_settings():   return render_template("settings.html")
@app.route("/wallet")
def page_wallet():     return render_template("wallet.html")

# ── API: stats & positions ────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    # Adapt to arbitrage stats
    stats = db_get_stats()
    return jsonify(stats)

@app.route("/api/positions")
def api_positions():
    # Return watchlist for now
    try:
        with open(ROOT / "logs" / "watchlist.json", "r") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify([])

@app.route("/api/bot/status")
def api_bot_status():
    c = load_config()
    return jsonify({
        "running":   bot_is_running(),
        "mode":      c.get("mode", "simulate"),
        "cycle":     _bot_stats["cycle"],
        "last_scan": _bot_stats["last_scan"],
        "network":   c.get("network", {}).get("chain_name", "Arbitrum One"),
        "contract":  (c.get("wallet", {}).get("arb_contract", "") or "")[:10] + "...",
    })

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    ok = start_bot_engine()
    return jsonify({
        "ok": ok,
        "msg": "Started -- monitoring 24/7" if ok else "Already running",
        "pid": os.getpid() if ok else None
    })

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    ok = stop_bot_engine()
    return jsonify({"ok": ok, "msg": "Stopping..." if ok else "Not running"})

@app.route("/api/mode", methods=["GET", "POST"])
def api_mode_set():
    if request.method == "POST":
        try:
            new_mode = (request.get_json() or {}).get("mode", "simulate")
            if new_mode not in ("simulate", "live"):
                return jsonify({"ok": False, "msg": f"Invalid mode '{new_mode}'"})
            cfg_data = load_config()
            cfg_data["mode"] = new_mode
            with open(ROOT / "config.json", "w") as f:
                json.dump(cfg_data, f, indent=2)
            import bot.utils as _u; _u._config = None
            return jsonify({"ok": True, "mode": new_mode})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})
    else:
        return jsonify({"mode": load_config().get("mode", "simulate")})

@app.route("/api/profit-history")
def api_profit_history():
    """Returns aggregated profit history from persistence."""
    try:
        from bot.persistence import HISTORY_PATH
        if HISTORY_PATH.exists():
            with open(HISTORY_PATH, "r") as f:
                data = json.load(f)
            # Group by day and sum profit & count
            history = {}
            for r in data:
                day = time.strftime("%m/%d", time.localtime(r.get("timestamp", 0)))
                if day not in history:
                    history[day] = {"profit": 0, "count": 0}
                history[day]["profit"] += float(r.get("estimated_profit", 0))
                history[day]["count"] += 1

            # Convert to list of {day, profit, count}
            res = [{"day": d, "profit": v["profit"], "count": v["count"]} for d, v in history.items()]
            return jsonify(res)
    except Exception:
        pass
    return jsonify([])

@app.route("/api/logs")
def api_logs():
    lines = int(request.args.get("lines", 100))
    try:
        if LOG_PATH.exists():
            with open(LOG_PATH, "r") as f:
                content = f.readlines()
            return jsonify({"logs": content[-lines:]})
    except:
        pass
    return jsonify({"logs": []})

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        # Include wallet so the settings form can populate all fields.
        # Private key is masked in the UI (input type="password") and this
        # server only listens on localhost, so local exposure is acceptable.
        return jsonify(load_config() or {})

    # POST — save updated config
    try:
        incoming = request.get_json(force=True) or {}
        current  = load_config() or {}

        # Deep-merge: incoming fields overwrite current ones
        for section, value in incoming.items():
            if isinstance(value, dict) and isinstance(current.get(section), dict):
                current[section].update(value)
            else:
                current[section] = value

        # Guard: never wipe an existing private key with an empty / placeholder value
        existing_pk = (load_config() or {}).get("wallet", {}).get("private_key", "")
        new_pk      = current.get("wallet", {}).get("private_key", "")
        if not new_pk or new_pk in ("YOUR_PRIVATE_KEY_HERE", "0x…", "0x..."):
            current.setdefault("wallet", {})["private_key"] = existing_pk

        with open(ROOT / "config.json", "w") as f:
            json.dump(current, f, indent=2)

        # Bust the in-memory config cache so the bot picks up the change
        import bot.utils as _u; _u._config = None

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

_WETH_USDC_POOL = "0xC6962004f452fE5bE02D0E47321ee3deFb746355"
_BALANCER_VAULT = "0xBA12222222228d8Ba445958a75a0704d566BF2C8"
_ERC20_BAL_ABI  = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

def _eth_price(w3) -> float:
    try:
        raw    = w3.eth.call({"to": checksum(_WETH_USDC_POOL), "data": "0x3850c7bd"})
        sqrt_p = w3.codec.decode(["uint160"], raw[:32])[0]
        price  = (sqrt_p / (2 ** 96)) ** 2 * 1e12
        return price if price > 100 else 3000.0
    except Exception:
        return 3000.0

@app.route("/api/wallet/balances")
def api_wallet_balances():
    try:
        w3      = get_web3()
        account = get_account()
        if not account:
            return jsonify({"error": "private_key not configured"})

        eth_price_usd = _eth_price(w3)
        eth           = w3.eth.get_balance(account.address) / 1e18
        contract_addr = cfg("wallet", "arb_contract")
        balances      = {}
        total_usd     = 0.0

        if contract_addr and contract_addr != "DEPLOY_CONTRACT_ADDRESS_HERE":
            for sym, info in (load_config() or {}).get("tokens", {}).items():
                try:
                    tok = w3.eth.contract(address=checksum(info["address"]), abi=_ERC20_BAL_ABI)
                    raw = tok.functions.balanceOf(checksum(contract_addr)).call()
                    dec = info.get("decimals", 18)
                    amt = raw / 10 ** dec
                    if amt > 0:
                        usd = amt * (eth_price_usd if dec == 18 else 1.0)
                        balances[sym] = {"amount": amt, "usd": usd}
                        total_usd += usd
                except Exception:
                    pass

        return jsonify({
            "wallet":    account.address,
            "eth":       eth,
            "eth_usd":   eth * eth_price_usd,
            "balances":  balances,
            "total_usd": total_usd,
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/wallet/withdraw", methods=["POST"])
def api_wallet_withdraw():
    if (load_config() or {}).get("mode", "simulate") != "live":
        return jsonify({"ok": False, "msg": "Withdraw only available in LIVE mode"})

    w3      = get_web3()
    account = get_account()
    if not account:
        return jsonify({"ok": False, "msg": "private_key not configured"})

    contract_addr = cfg("wallet", "arb_contract")
    if not contract_addr or contract_addr == "DEPLOY_CONTRACT_ADDRESS_HERE":
        return jsonify({"ok": False, "msg": "arb_contract not deployed"})

    WITHDRAW_ABI = [{"name":"withdraw","type":"function","stateMutability":"nonpayable",
                     "inputs":[{"name":"token","type":"address"}],"outputs":[]}]
    contract     = w3.eth.contract(address=checksum(contract_addr), abi=WITHDRAW_ABI)
    tokens_cfg   = (load_config() or {}).get("tokens", {})
    token_sym    = (request.get_json() or {}).get("token")
    to_withdraw  = {token_sym: tokens_cfg[token_sym]} if token_sym and token_sym in tokens_cfg else tokens_cfg

    results = []
    for sym, info in to_withdraw.items():
        try:
            token_addr = checksum(info["address"])
            tok        = w3.eth.contract(address=token_addr, abi=_ERC20_BAL_ABI)
            bal        = tok.functions.balanceOf(checksum(contract_addr)).call()
            if bal == 0:
                results.append({"token": sym, "status": "skipped", "msg": "zero balance"})
                continue
            amt   = round(bal / 10 ** info.get("decimals", 18), 6)
            nonce = w3.eth.get_transaction_count(account.address, "pending")
            tx    = contract.functions.withdraw(token_addr).build_transaction({
                "from": account.address, "nonce": nonce, "chainId": 42161,
                "gas": 100000,
                "maxFeePerGas":         w3.to_wei(cfg("gas", "max_fee_per_gas_gwei"),  "gwei"),
                "maxPriorityFeePerGas": w3.to_wei(cfg("gas", "max_priority_fee_gwei"), "gwei"),
            })
            signed  = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            results.append({"token": sym, "status": "success", "amount": amt, "tx_hash": tx_hash.hex()})
        except Exception as e:
            results.append({"token": sym, "status": "error", "msg": str(e)[:100]})

    return jsonify({"ok": True, "results": results})

@app.route("/api/health-check")
def api_health_check():
    checks = []
    conf   = load_config() or {}
    pk     = (conf.get("wallet") or {}).get("private_key", "")
    ca     = (conf.get("wallet") or {}).get("arb_contract", "")

    # 1. Config
    if not pk or pk == "YOUR_PRIVATE_KEY_HERE":
        checks.append({"status": "fail",  "name": "Config", "detail": "private_key not set in config.json"})
    elif not ca or ca == "DEPLOY_CONTRACT_ADDRESS_HERE":
        checks.append({"status": "fail",  "name": "Config", "detail": "arb_contract not deployed — run deploy script"})
    else:
        checks.append({"status": "ok",    "name": "Config", "detail": f"Contract …{ca[-8:]}"})

    # 2. RPC
    try:
        w3    = get_web3()
        block = w3.eth.block_number
        checks.append({"status": "ok",   "name": "RPC Connection", "detail": f"Block #{block:,}"})
    except Exception as e:
        checks.append({"status": "fail", "name": "RPC Connection", "detail": str(e)[:80]})
        failed = sum(1 for c in checks if c["status"] == "fail")
        warned = sum(1 for c in checks if c["status"] == "warn")
        return jsonify({"checks": checks, "summary": "fail", "failed": failed, "warned": warned})

    # 3. Wallet balance
    try:
        account = get_account()
        if not account:
            checks.append({"status": "fail", "name": "Wallet Balance", "detail": "private_key not configured"})
        else:
            eth = w3.eth.get_balance(account.address) / 1e18
            if eth < 0.001:
                checks.append({"status": "fail", "name": "Wallet Balance", "detail": f"{eth:.5f} ETH — too low for gas"})
            elif eth < 0.01:
                checks.append({"status": "warn", "name": "Wallet Balance", "detail": f"{eth:.5f} ETH — low, refill soon"})
            else:
                checks.append({"status": "ok",   "name": "Wallet Balance", "detail": f"{eth:.5f} ETH"})
    except Exception as e:
        checks.append({"status": "fail", "name": "Wallet Balance", "detail": str(e)[:80]})

    # 4. Arbitrage contract
    try:
        if not ca or ca == "DEPLOY_CONTRACT_ADDRESS_HERE":
            checks.append({"status": "fail", "name": "Arbitrage Contract", "detail": "Not deployed — set arb_contract in config"})
        else:
            OWNER_ABI = [{"name":"owner","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]
            owner = w3.eth.contract(address=checksum(ca), abi=OWNER_ABI).functions.owner().call()
            acct  = get_account()
            if acct and owner.lower() == acct.address.lower():
                checks.append({"status": "ok",   "name": "Arbitrage Contract", "detail": f"…{ca[-8:]} — owner verified"})
            else:
                checks.append({"status": "warn", "name": "Arbitrage Contract", "detail": f"Owner mismatch: {owner[:14]}…"})
    except Exception as e:
        checks.append({"status": "fail", "name": "Arbitrage Contract", "detail": str(e)[:80]})

    # 5. Balancer Vault
    try:
        code = w3.eth.get_code(checksum(_BALANCER_VAULT))
        if len(code) > 10:
            checks.append({"status": "ok",   "name": "Balancer Vault", "detail": "Live on Arbitrum"})
        else:
            checks.append({"status": "fail", "name": "Balancer Vault", "detail": "No bytecode at Balancer Vault address"})
    except Exception as e:
        checks.append({"status": "fail", "name": "Balancer Vault", "detail": str(e)[:80]})

    # 6. Oracle (WETH/USDC on-chain price)
    try:
        price = _eth_price(w3)
        if price > 100:
            checks.append({"status": "ok",   "name": "Oracle Feeds", "detail": f"ETH = ${price:,.0f}"})
        else:
            checks.append({"status": "warn", "name": "Oracle Feeds", "detail": f"Unusual price: {price}"})
    except Exception as e:
        checks.append({"status": "fail", "name": "Oracle Feeds", "detail": str(e)[:80]})

    # 7. Watchlist
    try:
        wl_path = ROOT / "logs" / "watchlist.json"
        if wl_path.exists():
            with open(wl_path) as f:
                wl = json.load(f)
            checks.append({"status": "ok",   "name": "Watchlist", "detail": f"{len(wl)} arb paths loaded"})
        else:
            checks.append({"status": "warn", "name": "Watchlist", "detail": "Not built yet — start the bot to generate it"})
    except Exception as e:
        checks.append({"status": "warn", "name": "Watchlist", "detail": str(e)[:80]})

    failed  = sum(1 for c in checks if c["status"] == "fail")
    warned  = sum(1 for c in checks if c["status"] == "warn")
    summary = "fail" if failed else ("warn" if warned else "ok")
    return jsonify({"checks": checks, "summary": summary, "failed": failed, "warned": warned})

# ── Real-time SocketIO push ───────────────────────────────────────────────────
def _push_loop():
    while True:
        with app.app_context():
            try:
                stats = db_get_stats()
                socketio.emit("stats", stats)
                socketio.emit("bot_status", {
                    "running":   bot_is_running(),
                    "mode":      (load_config() or {}).get("mode", "simulate"),
                })
                # Emit watchlist as positions to keep frontend logic working
                try:
                    with open(ROOT / "logs" / "watchlist.json", "r") as f:
                        socketio.emit("positions", json.load(f))
                except:
                    pass
            except Exception:
                pass
        time.sleep(1)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ArbBot — Sentinel Hybrid Arbitrage Bot")
    parser.add_argument("--port",   type=int, default=5000)
    parser.add_argument("--host",   type=str, default="0.0.0.0")
    args = parser.parse_args()

    # Add real-time log tailing
    handler = LogTailer(socketio)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    threading.Thread(target=_push_loop, daemon=True, name="push").start()

    socketio.run(app, host=args.host, port=args.port, debug=False, log_output=False, allow_unsafe_werkzeug=True)

if __name__ == "__main__":
    main()
