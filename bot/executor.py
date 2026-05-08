import time
import threading
import urllib.request
import json
from .utils import get_web3, get_account, cfg, checksum, logger
from .database import record_execution
from .arb_math import calculate_optimal_multihop

ARB_CONTRACT_ABI = [
    {
        "name": "executeArb", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountFlash", "type": "uint256"},
            {"name": "tokenFlash",  "type": "address"},
            {"components": [
                {"name": "dex",      "type": "uint8"},
                {"name": "tokenIn",  "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee",      "type": "uint24"}
            ], "name": "steps", "type": "tuple[]"},
            {"name": "minProfit", "type": "uint256"}
        ],
        "outputs": []
    },
    {
        "name": "withdraw", "type": "function", "stateMutability": "nonpayable",
        "inputs":  [{"name": "token", "type": "address"}],
        "outputs": []
    },
    {
        "name": "owner", "type": "function", "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"type": "address"}]
    }
]

DEX_TYPE_MAP = {
    "univ2":     0,
    "univ3":     1,
    "camelotv2": 2,
    "camelotv3": 3,
    "algebra":   3,
    "sushiv2":   4,
}

_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=ethereum&vs_currencies=usd"
)

# Maximum sane flash-loan size and profit in USD — guards against ghost
# opportunities from pools with inflated/fake reserves.
_MAX_FLASH_USD  = 100_000.0   # reject if flash loan > $100k
_MAX_PROFIT_USD =  50_000.0   # reject if projected profit > $50k


def get_mode() -> str:
    try:
        import bot.utils as _u
        raw = (_u.load_config() or {}).get("mode", "simulate")
        return raw if raw in ("simulate", "live") else "simulate"
    except Exception:
        return "simulate"


class ArbExecutor:
    def __init__(self):
        self._w3      = get_web3()
        self._account = get_account()
        contract_addr = cfg("wallet", "arb_contract")

        if not contract_addr or contract_addr.upper().startswith("DEPLOY_"):
            self._contract = None
            logger.warning("arb_contract not set — execution disabled")
        else:
            self._contract = self._w3.eth.contract(
                address=checksum(contract_addr),
                abi=ARB_CONTRACT_ABI
            )

        # ETH/USD price cache — refreshed every 5 minutes
        self._eth_price: float      = 3000.0
        self._eth_price_ts: float   = 0.0

        # Thread-safe nonce counter to prevent races when multiple opportunities
        # fire concurrently (all would get the same pending nonce otherwise).
        self._nonce_lock  = threading.Lock()
        self._nonce: int  = -1

        # Per-path execution cooldown — prevents re-firing the same symbol
        # every scan cycle when txs revert (gap stays open, bot loops forever).
        self._cooldown: dict  = {}   # symbol -> last execute timestamp
        self._cooldown_sec    = 60   # seconds to suppress a path after execution

        logger.info(
            f"ArbExecutor ready | Contract: "
            f"{contract_addr[:10] if self._contract else 'None'}"
        )

    # ── ETH price ─────────────────────────────────────────────────────────────

    def _get_eth_price_usd(self) -> float:
        """Returns ETH/USD from CoinGecko public API, cached for 5 minutes."""
        if time.time() - self._eth_price_ts < 300:
            return self._eth_price

        try:
            with urllib.request.urlopen(_COINGECKO_URL, timeout=5) as resp:
                data  = json.loads(resp.read())
                price = float(data["ethereum"]["usd"])
            if price > 100:
                self._eth_price    = price
                self._eth_price_ts = time.time()
                logger.debug(f"ETH price refreshed: ${price:,.0f}")
        except Exception as e:
            logger.warning(f"ETH price fetch failed, using cached ${self._eth_price:,.0f}: {e}")
            self._eth_price_ts = time.time()

        return self._eth_price

    # ── Gas & profit checks ───────────────────────────────────────────────────

    def _to_usd(self, amount_tokens: float, dec_in: int, token_addr: str = "") -> float:
        """Convert a token amount to approximate USD value.

        Only USDC (dec=6) and WETH get a real USD price.
        All other tokens return their raw amount so callers don't
        accidentally multiply an ARB/PENDLE profit by the ETH price.
        """
        if dec_in == 6:
            return amount_tokens                        # USDC / USDC.e
        weth = cfg("network", "weth").lower()
        if token_addr.lower() == weth:
            return amount_tokens * self._get_eth_price_usd()
        # Unknown token — treat 1 token = $1 as a floor so USD checks
        # are not inflated.  Paths denominated in non-WETH tokens will
        # need to exceed min_profit_usd in raw-token units.
        return amount_tokens

    def _check_viable(
        self,
        opportunity: dict,
        amount_flash: int,
        dec_in: int,
    ) -> tuple:
        """
        Pre-flight checks before building or sending any transaction.

        Returns (ok: bool, profit_usd: float, gas_cost_usd: float).
        Logs the reason and returns False when any check fails.
        """
        token0         = opportunity.get("tokens", [""])[0]
        gap            = opportunity.get("gap", 0)
        profit_tokens  = gap * (amount_flash / 10 ** dec_in)
        profit_usd     = self._to_usd(profit_tokens, dec_in, token0)

        # 1a. Flash loan USD cap — rejects ghost opportunities from pools with
        #     inflated reserves (e.g. 14 000 ETH flash = clearly bogus).
        flash_usd = self._to_usd(amount_flash / 10 ** dec_in, dec_in, token0)
        if flash_usd > _MAX_FLASH_USD:
            logger.warning(
                f"Skip {opportunity['symbol']}: flash ${flash_usd:,.0f} > "
                f"max ${_MAX_FLASH_USD:,.0f}"
            )
            return False, profit_usd, 0.0

        # 1b. Profit sanity cap — real Arbitrum arb does not return $50 000+.
        if profit_usd > _MAX_PROFIT_USD:
            logger.warning(
                f"Skip {opportunity['symbol']}: profit ${profit_usd:,.0f} > "
                f"sanity cap ${_MAX_PROFIT_USD:,.0f}"
            )
            return False, profit_usd, 0.0

        # 1c. Gap sanity cap — a gap above 5% in a multi-hop path almost always
        #     means a scam/honeypot token is creating a fake price signal.
        #     Legitimate liquid-market arb rarely exceeds 2-3%.
        if gap > 0.05:
            logger.warning(
                f"Skip {opportunity['symbol']}: gap {gap:.2%} > 5% sanity cap"
            )
            return False, profit_usd, 0.0

        # 2. Minimum profit threshold (from config)
        min_profit_usd = cfg("strategy", "min_profit_usd")
        if profit_usd < min_profit_usd:
            logger.debug(
                f"Skip {opportunity['symbol']}: "
                f"profit ${profit_usd:.2f} < min ${min_profit_usd:.2f}"
            )
            return False, profit_usd, 0.0

        # 3. Gas cost in USD using current on-chain gas price
        try:
            gas_price_wei = self._w3.eth.gas_price          # current base + priority
        except Exception:
            gas_price_wei = self._w3.to_wei(
                cfg("gas", "max_fee_per_gas_gwei"), "gwei"
            )

        gas_limit    = cfg("gas", "gas_limit")
        gas_cost_eth = (gas_price_wei * gas_limit) / 10 ** 18
        gas_cost_usd = gas_cost_eth * self._get_eth_price_usd()

        # 3. Hard cap on gas spend (config: strategy.max_gas_usd)
        max_gas_usd = cfg("strategy", "max_gas_usd")
        if gas_cost_usd > max_gas_usd:
            logger.warning(
                f"Skip {opportunity['symbol']}: "
                f"gas ${gas_cost_usd:.4f} > max_gas_usd ${max_gas_usd:.2f}"
            )
            return False, profit_usd, gas_cost_usd

        # 4. Gas must be less than expected profit (net positive trade)
        if gas_cost_usd >= profit_usd:
            logger.info(
                f"Skip {opportunity['symbol']}: "
                f"gas ${gas_cost_usd:.4f} >= profit ${profit_usd:.4f}"
            )
            return False, profit_usd, gas_cost_usd

        return True, profit_usd, gas_cost_usd

    # ── Flash amount sizing ───────────────────────────────────────────────────

    def _get_flash_amount(self, opportunity: dict) -> tuple:
        """
        Size the flash loan using calculate_optimal_multihop exclusively.
        Back-propagates the 3% impact cap through every pool in the path.
        Returns (0, dec_in) when reserve data is missing — caller must skip.
        """
        gap    = opportunity.get("gap", 0)
        dec_in = opportunity.get("dec_in", 18)
        hops   = opportunity.get("hops", [])

        amount = calculate_optimal_multihop(hops, gap)
        if amount > 0:
            logger.debug(
                f"Flash size: {amount / 10**dec_in:.6f} "
                f"across {len(hops)} pools (gap={gap:.3%})"
            )
        return amount, dec_in

    # ── Transaction builder ───────────────────────────────────────────────────

    def _build_tx(self, opportunity: dict, amount_flash: int, min_profit: int) -> dict:
        steps = [
            {
                "dex":      DEX_TYPE_MAP.get(opportunity["versions"][i], 1),
                "tokenIn":  checksum(opportunity["tokens"][i]),
                "tokenOut": checksum(opportunity["tokens"][i + 1]),
                "fee":      opportunity["fees"][i] or 0,
            }
            for i in range(len(opportunity["pools"]))
        ]

        with self._nonce_lock:
            on_chain = self._w3.eth.get_transaction_count(self._account.address, "pending")
            # Use max(on_chain, local+1) so concurrent calls each get a unique nonce
            # even if the node hasn't seen the previous tx yet.
            if self._nonce < on_chain:
                self._nonce = on_chain
            else:
                self._nonce += 1
            nonce = self._nonce

        dec_in         = opportunity.get("dec_in", 18)
        max_fee_gwei   = cfg("gas", "max_fee_per_gas_gwei")
        prio_fee_gwei  = cfg("gas", "max_priority_fee_gwei")
        gas_limit      = cfg("gas", "gas_limit")
        chain_id       = cfg("network", "chain_id")
        eth_price      = self._get_eth_price_usd()
        max_fee_wei    = self._w3.to_wei(max_fee_gwei,  "gwei")
        prio_fee_wei   = self._w3.to_wei(prio_fee_gwei, "gwei")
        gas_cost_eth   = (max_fee_wei * gas_limit) / 10 ** 18
        gas_cost_usd   = gas_cost_eth * eth_price

        step_summary = " -> ".join(
            f"{s['tokenIn'][:6]}../{s['tokenOut'][:6]}.. "
            f"[dex={s['dex']} fee={s['fee']}]"
            for s in steps
        )

        logger.info(
            f"[TX BUILD] {opportunity['symbol']} | "
            f"nonce={nonce} | chainId={chain_id}\n"
            f"  Flash : {amount_flash / 10**dec_in:.6f} tokens "
            f"(raw={amount_flash})\n"
            f"  minProfit: {min_profit / 10**dec_in:.6f} tokens "
            f"(raw={min_profit})\n"
            f"  Steps : {step_summary}\n"
            f"  Gas   : limit={gas_limit:,} | "
            f"maxFee={max_fee_gwei} gwei | "
            f"priority={prio_fee_gwei} gwei\n"
            f"  Gas $ : {gas_cost_eth:.6f} ETH "
            f"(~${gas_cost_usd:.4f} @ ETH=${eth_price:,.0f})"
        )

        return self._contract.functions.executeArb(
            amount_flash,
            checksum(opportunity["tokens"][0]),
            steps,
            min_profit,
        ).build_transaction({
            "from":                 self._account.address,
            "nonce":                nonce,
            "chainId":              chain_id,
            "gas":                  gas_limit,
            "maxFeePerGas":         max_fee_wei,
            "maxPriorityFeePerGas": prio_fee_wei,
        })

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute(self, opportunity: dict):
        mode = get_mode()

        if not self._contract or not self._account:
            logger.warning(
                f"[{mode.upper()}] Cannot execute {opportunity['symbol']}: "
                "arb_contract or private_key not configured."
            )
            return None

        symbol = opportunity["symbol"]

        # Cooldown — suppress the same path for 60s after execution so a
        # reverting tx doesn't cause the bot to loop on the same opportunity.
        elapsed = time.time() - self._cooldown.get(symbol, 0)
        if elapsed < self._cooldown_sec:
            logger.debug(
                f"Skip {symbol}: cooldown ({elapsed:.0f}s < {self._cooldown_sec}s)"
            )
            return None

        amount_flash, dec_in = self._get_flash_amount(opportunity)

        if amount_flash == 0:
            logger.debug(f"Skip {symbol}: no reserve data for sizing")
            return None

        # ── Pre-flight: profit & gas checks ───────────────────────────────────
        ok, profit_usd, gas_cost_usd = self._check_viable(opportunity, amount_flash, dec_in)
        if not ok:
            return None

        # On-chain minimum profit (0.1% of flash amount, in token native units)
        min_profit = max(1, int(amount_flash * 0.001))

        try:
            tx = self._build_tx(opportunity, amount_flash, min_profit)

            record_payload = {
                "token":            opportunity["tokens"][0],
                "symbol":           opportunity["symbol"],
                "type":             opportunity.get("type", "dual"),
                "gap":              opportunity["gap"],
                "amount_flash":     amount_flash / 10 ** dec_in,
                "estimated_profit": profit_usd,
                "gas_cost_usd":     gas_cost_usd,
                "timestamp":        int(time.time()),
            }

            if mode == "simulate":
                self._w3.eth.call(tx)
                logger.info(
                    f"[SIMULATE] [OK] {symbol} | "
                    f"Gap={opportunity['gap']:.3%} | "
                    f"Flash={amount_flash / 10**dec_in:.4f} | "
                    f"Profit≈${profit_usd:.2f} | "
                    f"Gas≈${gas_cost_usd:.4f}"
                )
                record_payload["tx_hash"] = f"sim-{int(time.time())}-{symbol}"
                record_execution(record_payload)
                self._cooldown[symbol] = time.time()
                return "sim-success"

            else:
                signed  = self._account.sign_transaction(tx)
                # web3.py v5 uses rawTransaction (camelCase); v6 uses raw_transaction
                raw_tx  = getattr(signed, 'raw_transaction', None) or getattr(signed, 'rawTransaction', b'')
                tx_hash = self._w3.eth.send_raw_transaction(raw_tx)
                logger.info(
                    f"[LIVE] Sent: {tx_hash.hex()} | {symbol} | "
                    f"Flash={amount_flash / 10**dec_in:.4f} | "
                    f"Profit≈${profit_usd:.2f} | Gas≈${gas_cost_usd:.4f}"
                )
                record_payload["tx_hash"] = tx_hash.hex()
                record_execution(record_payload)
                self._cooldown[symbol] = time.time()
                return tx_hash.hex()

        except Exception as e:
            logger.info(
                f"[{mode.upper()}] [FAILED] {opportunity['symbol']}: {str(e)[:120]}"
            )
            # Apply cooldown even on failure — prevents hammering the same path
            # when txs revert or wallet is underfunded (gap stays open, bot loops).
            self._cooldown[symbol] = time.time()
            return None
