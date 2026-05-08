import asyncio
import json
import time
from websockets import connect
from .utils import (
    get_web3, cfg, checksum,
    MULTICALL3_ADDR, MULTICALL3_ABI, logger, ROOT_DIR
)

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"

# ABIs
POOL_INFO_ABI = json.loads('[{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]')
ERC20_ABI        = json.loads('[{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"}]')

# Function selectors
SEL_SLOT0       = "0x3850c7bd"  # slot0()
SEL_GLOBALSTATE = "0x1ad57897"  # globalState()
SEL_GETRESERVES = "0x0902f1ac"  # getReserves()
SEL_LIQUIDITY   = "0x1a686502"  # liquidity()  — V3 only


class ArbMonitor:
    def __init__(self, on_opportunity=None):
        self.on_opportunity  = on_opportunity
        self.watchlist       = []
        self.metadata        = {}   # token_addr -> decimals
        self.w3              = get_web3()
        self.pool_tokens     = {}   # pool_addr  -> (t0, t1)
        self.watched_pools   = set()
        self._last_backrun   = {}   # pool_addr  -> last trigger timestamp (WSS dedup)
        self._load_watchlist()

    def _load_watchlist(self):
        if WATCHLIST_PATH.exists():
            try:
                with open(WATCHLIST_PATH, "r") as f:
                    new_watchlist = json.load(f)
                    if new_watchlist != self.watchlist:
                        self.watchlist = new_watchlist
                        self._update_watched_pools()
                        self._fetch_metadata()
                        self._fetch_pool_tokens()
            except Exception as e:
                logger.error(f"Error loading watchlist: {e}")

    def _update_watched_pools(self):
        self.watched_pools = set()
        for item in self.watchlist:
            for p in item["pools"]:
                self.watched_pools.add(p.lower())

    def _fetch_metadata(self):
        tokens = set()
        for item in self.watchlist:
            for t in item["tokens"]:
                tokens.add(t.lower())

        needed = [t for t in tokens if t not in self.metadata]
        if not needed:
            return

        logger.info(f"Fetching metadata for {len(needed)} tokens...")
        try:
            mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MULTICALL3_ABI)
            calls = []
            for t in needed:
                erc20 = self.w3.eth.contract(address=checksum(t), abi=ERC20_ABI)
                calls.append((checksum(t), erc20.encodeABI("decimals")))

            _, return_data = mc_contract.functions.aggregate(calls).call()
            for i, t in enumerate(needed):
                try:
                    self.metadata[t] = self.w3.codec.decode(["uint8"], return_data[i])[0]
                except:
                    self.metadata[t] = 18
        except Exception as e:
            logger.error(f"Metadata fetch failed: {e}")

    def _fetch_pool_tokens(self):
        pools = set()
        for item in self.watchlist:
            for p in item["pools"]:
                pools.add(p.lower())

        needed = [p for p in pools if p not in self.pool_tokens]
        if not needed:
            return

        logger.info(f"Fetching token info for {len(needed)} pools...")
        try:
            mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MULTICALL3_ABI)
            calls = []
            for p in needed:
                p_contract = self.w3.eth.contract(address=checksum(p), abi=POOL_INFO_ABI)
                calls.append((checksum(p), p_contract.encodeABI("token0")))
                calls.append((checksum(p), p_contract.encodeABI("token1")))

            _, return_data = mc_contract.functions.aggregate(calls).call()
            for i, p in enumerate(needed):
                try:
                    t0 = self.w3.codec.decode(["address"], return_data[i * 2])[0].lower()
                    t1 = self.w3.codec.decode(["address"], return_data[i * 2 + 1])[0].lower()
                    self.pool_tokens[p] = (t0, t1)
                except:
                    pass
        except Exception as e:
            logger.error(f"Pool token fetch failed: {e}")

    def get_price_from_res(self, res, ptype, meta):
        if ptype in ("univ3", "algebra"):
            sqrtP = self.w3.codec.decode(["uint160"], res[:32])[0]
            return (sqrtP / (2 ** 96)) ** 2 * (10 ** meta["dec0"] / 10 ** meta["dec1"])
        else:
            # UniV2 / CamelotV2 / SushiV2
            dec = self.w3.codec.decode(["uint112", "uint112", "uint32"], res)
            res0, res1 = dec[0], dec[1]
            return (res1 / 10 ** meta["dec1"]) / (res0 / 10 ** meta["dec0"]) if res0 > 0 else 0

    def check_all_prices_multicall(self):
        if not self.watchlist:
            return []

        MC3_RESILIENT_ABI = json.loads('[{"inputs":[{"internalType":"bool","name":"requireSuccess","type":"bool"},{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"tryAggregate","outputs":[{"components":[{"internalType":"bool","name":"success","type":"bool"},{"internalType":"bytes","name":"returnData","type":"bytes"}],"internalType":"struct Multicall3.Result[]","name":"returnData","type":"tuple[]"}],"stateMutability":"payable","type":"function"}]')
        mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MC3_RESILIENT_ABI)

        # Two separate call lists so we can split results cleanly
        price_calls  = []  # one per pool
        liq_calls    = []  # one per V3/Algebra pool (liquidity())
        pool_meta    = []  # parallel to price_calls
        v3_liq_addrs = []  # parallel to liq_calls
        added_pools  = set()

        for item in self.watchlist:
            for i, p_addr in enumerate(item["pools"]):
                p_addr = p_addr.lower()
                if p_addr not in self.pool_tokens or p_addr in added_pools:
                    continue
                added_pools.add(p_addr)

                ptype = item["versions"][i]
                t0, t1 = self.pool_tokens[p_addr]

                if ptype in ("univ3", "algebra"):
                    sel = SEL_SLOT0 if ptype == "univ3" else SEL_GLOBALSTATE
                    price_calls.append({"target": checksum(p_addr), "callData": sel})
                    # Extra call: liquidity() to size the flash loan
                    liq_calls.append({"target": checksum(p_addr), "callData": SEL_LIQUIDITY})
                    v3_liq_addrs.append(p_addr)
                else:
                    # getReserves() gives both price AND reserve amounts in one call
                    price_calls.append({"target": checksum(p_addr), "callData": SEL_GETRESERVES})

                pool_meta.append({
                    "addr":   p_addr,
                    "type":   ptype,
                    "token0": t0,
                    "token1": t1,
                    "dec0":   self.metadata.get(t0, 18),
                    "dec1":   self.metadata.get(t1, 18),
                })

        all_calls = price_calls + liq_calls
        if not all_calls:
            return []

        opportunities = []
        try:
            results = mc_contract.functions.tryAggregate(False, all_calls).call()

            prices       = {}   # pool_addr -> price_t0_in_t1
            pool_reserves = {}  # pool_addr -> {"reserve0": int, "reserve1": int}
            sqrt_prices  = {}   # pool_addr -> sqrtPriceX96 (for V3 virtual reserves)

            # ── Phase 1: parse price results ─────────────────────────────────
            for i, (success, res) in enumerate(results[:len(price_calls)]):
                if not success or not res:
                    continue
                meta = pool_meta[i]
                try:
                    price = self.get_price_from_res(res, meta["type"], meta)
                    prices[meta["addr"]] = price

                    if meta["type"] in ("univ3", "algebra"):
                        # Store sqrtPriceX96 for virtual reserve computation below
                        sqrt_prices[meta["addr"]] = self.w3.codec.decode(["uint160"], res[:32])[0]
                    else:
                        # V2: reserves are already in this same call result
                        dec = self.w3.codec.decode(["uint112", "uint112", "uint32"], res)
                        pool_reserves[meta["addr"]] = {"reserve0": dec[0], "reserve1": dec[1]}
                except Exception:
                    continue

            # ── Phase 2: parse liquidity() results for V3 pools ──────────────
            for i, (success, res) in enumerate(results[len(price_calls):]):
                if not success or not res:
                    continue
                p_addr = v3_liq_addrs[i]
                try:
                    L      = self.w3.codec.decode(["uint128"], res)[0]
                    sqrt_p = sqrt_prices.get(p_addr, 0)
                    if L > 0 and sqrt_p > 0:
                        # Virtual reserves at the current tick (proportional, not exact TVL)
                        # x ≈ L * 2^96 / sqrtP  (token0 units)
                        # y ≈ L * sqrtP / 2^96  (token1 units)
                        virt_x = int(L * (2 ** 96) / sqrt_p)
                        virt_y = int(L * sqrt_p / (2 ** 96))
                        pool_reserves[p_addr] = {"reserve0": virt_x, "reserve1": virt_y}
                except Exception:
                    continue

            # ── Phase 3: evaluate arb opportunities ───────────────────────────
            for item in self.watchlist:
                amount = 1.0
                valid  = True

                for i in range(len(item["pools"])):
                    p_addr = item["pools"][i].lower()
                    if p_addr not in prices:
                        valid = False
                        break

                    t_in    = item["tokens"][i].lower()
                    t0, _   = self.pool_tokens[p_addr]
                    p_t0_t1 = prices[p_addr]

                    if t_in == t0:
                        amount *= p_t0_t1
                    else:
                        amount /= p_t0_t1 if p_t0_t1 > 0 else 1

                # require >0.2% gap and <50% gap
                # gaps above 50% are garbage from zero-liquidity / rugged tokens
                if not valid or amount <= 1.002 or amount >= 1.50:
                    continue

                # ── Build per-hop reserve/fee data for multi-hop sizing ────────
                # Each hop describes one pool crossing with both reserves so
                # arb_math can back-propagate the 3% cap through every pool.
                hops = []
                for i, p_addr in enumerate(item["pools"]):
                    p_l   = p_addr.lower()
                    t_in  = item["tokens"][i].lower()
                    t_out = item["tokens"][i + 1].lower()
                    t0, t1 = self.pool_tokens.get(p_l, (None, None))

                    pr = pool_reserves.get(p_l, {})
                    if t0 and t_in == t0:
                        r_in  = pr.get("reserve0", 0)
                        r_out = pr.get("reserve1", 0)
                    else:
                        r_in  = pr.get("reserve1", 0)
                        r_out = pr.get("reserve0", 0)

                    # Convert pool fee to decimal fraction for AMM math
                    version = item["versions"][i]
                    raw_fee = item["fees"][i] or 0
                    if version in ("algebra", "camelotv3"):
                        fee_pct = 0.0025            # Algebra default ~0.25%
                    elif version in ("univ2", "sushiv2", "camelotv2"):
                        fee_pct = 0.003             # standard V2 0.30%
                    else:
                        fee_pct = (raw_fee or 3000) / 1_000_000  # UniV3 ppm → decimal

                    hops.append({
                        "reserve_in":  r_in,
                        "reserve_out": r_out,
                        "fee_pct":     fee_pct,
                        "dec_in":      self.metadata.get(t_in, 18),
                        "dec_out":     self.metadata.get(t_out, 18),
                    })

                # Minimum liquidity guard — reject low-liquidity, dead, and
                # scam token pools (e.g. E280 which has near-zero real depth).
                # Each side of every hop must hold at least min_reserve_tokens
                # whole tokens. Config key: strategy.min_reserve_tokens (default 100).
                # For dec=18: 100 tokens (e.g. 100 WETH / 100 PENDLE).
                # For dec=6:  100 USDC  = $100 — keeps the check token-agnostic.
                _min_r = (cfg("strategy", "min_reserve_tokens") or 100)
                if any(
                    h["reserve_in"]  < _min_r * 10 ** h["dec_in"] or
                    h["reserve_out"] < _min_r * 10 ** h["dec_out"]
                    for h in hops
                ):
                    continue

                # Top-level reserve_in / dec_in kept for single-pool fallback
                first_pool  = item["pools"][0].lower()
                t_in_first  = item["tokens"][0].lower()
                t0_first, _ = self.pool_tokens.get(first_pool, (None, None))
                pr0         = pool_reserves.get(first_pool, {})
                reserve_in  = (
                    pr0.get("reserve0", 0) if t_in_first == t0_first
                    else pr0.get("reserve1", 0)
                )

                opportunities.append({
                    "symbol":          item["symbol"],
                    "type":            item["type"],
                    "tokens":          item["tokens"],
                    "pools":           item["pools"],
                    "versions":        item["versions"],
                    "fees":            item["fees"],
                    "gap":             amount - 1.0,
                    "profit_pct":      amount - 1.0,
                    "expected_output": amount,
                    "hops":            hops,          # per-pool reserve + fee data
                    "reserve_in":      reserve_in,    # first-pool fallback
                    "dec_in":          self.metadata.get(t_in_first, 18),
                })

        except Exception as e:
            logger.error(f"Multicall check failed: {e}")

        return opportunities

    async def static_scanner_loop(self):
        logger.info("Sentinel Static Scanner started (12s interval)")
        while True:
            self._load_watchlist()
            opps = self.check_all_prices_multicall()
            for opp in opps:
                if self.on_opportunity:
                    await self.on_opportunity(opp)
            await asyncio.sleep(12)

    async def event_listener(self):
        """WSS listener for real-time backrunning."""
        TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
        TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

        keys = cfg("network", "alchemy_keys")
        if not keys:
            return

        key_idx = 0
        while True:
            key     = keys[key_idx % len(keys)]
            wss_url = f"wss://arb-mainnet.g.alchemy.com/v2/{key}"
            try:
                async with connect(wss_url) as ws:
                    sub = {
                        "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                        "params":  ["logs", {"topics": [[TOPIC_V3, TOPIC_V2]]}]
                    }
                    await ws.send(json.dumps(sub))
                    await ws.recv()
                    logger.info(f"Sentinel WSS active on key index {key_idx % len(keys)}")

                    while True:
                        msg     = await ws.recv()
                        data    = json.loads(msg)
                        res     = data.get("params", {}).get("result", {})
                        emitter = res.get("address", "").lower()

                        if emitter in self.watched_pools:
                            now = time.time()
                            if now - self._last_backrun.get(emitter, 0) < 1.0:
                                continue  # same pool fired again within 1s — skip
                            self._last_backrun[emitter] = now
                            logger.info(f"BACKRUN TRIGGER on {emitter}")
                            opps = self.check_all_prices_multicall()
                            for opp in opps:
                                if emitter in [p.lower() for p in opp["pools"]]:
                                    if self.on_opportunity:
                                        await self.on_opportunity(opp)
            except Exception as e:
                wait = min(60, 5 * (2 ** (key_idx % 3)))
                logger.warning(f"WSS Error: {e}. Reconnecting in {wait}s...")
                key_idx += 1
                await asyncio.sleep(wait)
