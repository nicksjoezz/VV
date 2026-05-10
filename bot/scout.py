import json
import asyncio
import time
import urllib.request
from typing import List
from collections import defaultdict
from .utils import ROOT_DIR, logger, checksum, get_web3, MULTICALL3_ADDR, MULTICALL3_ABI

WATCHLIST_PATH  = ROOT_DIR / "logs" / "watchlist.json"
POOL_CACHE_PATH = ROOT_DIR / "logs" / "pool_cache.json"

# ── DexScreener pool discovery ────────────────────────────────────────────────

# Minimum pool liquidity in USD — excludes dead / near-empty pools.
MIN_POOL_LIQUIDITY = 10_000    # $10k

# Anchor tokens used to seed DexScreener queries. Every Arbitrum pair
# involving any of these tokens is fetched and filtered by liquidity.
ANCHOR_TOKENS = [
    "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
    "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC (native)
    "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT
    "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",  # WBTC
    "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",  # USDC.e (bridged)
    "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",  # DAI
    "0x912CE59144191C1204E64559FE8253a0e49E6548",  # ARB
    "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",  # LINK
    "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f2",  # UNI
    "0x0c880f6761F1af8d9Aa9C466984b80DAb9a8c9e8",  # PENDLE
    "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",  # GMX
    "0x11cDb42B0EB46D95f990BeDD4695A6e3fA034978",  # CRV
    "0xba5DdD1f9d7F570dc94a51479a000E3BCE967196",  # AAVE
    "0x5979D7b546E38E414F7E9822514be443A4800529",  # wstETH
    "0x35751007a407ca6FEFfE80b3cB397736D2cf4dbe",  # weETH
    "0x2416092f143378750bb29b79eD961ab195CcEea5",  # ezETH
    "0xEC70Dcb4A1EFa46b8F2D97C310C9c4790ba5ffA8",  # rETH
    "0x498Bf2B1E120FeD3ad3D42EA2165E9b73f99C1e5",  # crvUSD
    "0x7dfF72693f6A4149b17e7C6314655f6a9F7c8B33",  # GHO
    "0x6c84a8f1c29108F47a79964b5Fe888D4f4D0dE40",  # tBTC
    "0xcbB7C0000ab88B473b1f5aFd9ef808440eed33Bf",  # cbBTC
    "0x3082CC23568ea640225c2467653dB90e9250aaa0",  # RDNT
    "0x18c11FD286C5EC11c3b683Caa813b77f5163A122",  # GNS
    "0x93b346b6BC2548dA6A1E7d98E9a421B42541425b",  # LUSD
    "0x6985884C4392D348587B19cb9eAAf157F13271cd",  # ZRO
    "0x13Ad51ed4F1B7e9Dc168d8a00cB3f4dDD85EfA60",  # LDO
    "0x9d2F299715D94d8A7E6F5eaa8E654E8c74a988A7",  # FXS
    "0x4e352cf164e64adcbad318c3a1e222e9eba4ce42",  # MCB
    "0x539bdE0d7Dbd336b79148AA742883198BBF60342",  # MAGIC
    "0x6694340fc020c5E6B96567843da2df01b2CE1eb6",  # STG
]

# Maps DexScreener dexId to (version string, default fee in ppm).
# Version strings must match executor.py's DEX_TYPE_MAP keys.
#
# "camelot" = Camelot V2 AMM (UniV2-style with referrer) — routes to CAMELOT_V2_ROUTER.
#   NOTE: DexScreener may return "camelot" for BOTH V2 and V3 pools.
#   classify_pools() corrects any V2→V3 misclassification via on-chain globalState() call.
#
# "sushiswap-v3" is intentionally excluded: SushiSwap V3 uses its own factory
#   (0x1af415a1EbA07a4986a52B6f2e7dE7003D82231e). Routing via UNIV3_ROUTER computes
#   the wrong pool address (Uniswap's factory 0x1F98431c... is hardcoded there),
#   causing the pool to call safeTransfer on wrong tokens → 'TF' revert.
DEXSCREENER_DEX_MAP = {
    "uniswap":        ("univ3",     3000),
    "uniswap-v3":     ("univ3",     3000),
    "uniswap-v2":     ("univ2",     3000),
    "camelot":        ("camelotv2", 3000),  # V2 AMM by default; upgraded to "algebra" if needed
    "camelot-v3":     ("algebra",   0),     # Camelot V3 (Algebra) — confirmed V3
    "sushiswap":      ("sushiv2",   3000),
    "sushiswap-v3":   ("sushiv3",  3000),  # SushiSwap V3 — routes via SUSHI_V3_ROUTER
}

ERC20_ABI = json.loads('[{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

# Function selectors for on-chain pool type detection
_SEL_FEE         = "0xddca3f43"   # fee()         — UniV3 pools only
_SEL_GLOBALSTATE = "0x1ad57897"   # globalState() — Algebra / Camelot V3 pools only
_SEL_GETRESERVES = "0x0902f1ac"   # getReserves() — UniV2 / SushiV2 / CamelotV2 pools only


async def _discover_pools_dexscreener() -> list:
    """
    Query DexScreener for all Arbitrum pairs of every anchor token.
    Filters to supported DEXes and MIN_POOL_LIQUIDITY.
    Returns pool dicts: {dex, pool, token0, token1, fee, type, liq_usd}.
    """
    seen  = set()
    pools = []

    for i, addr in enumerate(ANCHOR_TOKENS):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "arbitrum"]
        except Exception as e:
            logger.warning(f"DexScreener [{addr[:10]}]: {e}")
            pairs = []

        kept = 0
        for p in pairs:
            liq    = float((p.get("liquidity") or {}).get("usd") or 0)
            dex_id = (p.get("dexId") or "").lower()

            if liq < MIN_POOL_LIQUIDITY or dex_id not in DEXSCREENER_DEX_MAP:
                continue

            # Require non-zero 24h volume AND at least 1 trade — filters dead pools
            # that have liquidity sitting in them but no actual activity.
            txns   = p.get("txns", {}).get("h24", {})
            vol24  = float((p.get("volume") or {}).get("h24") or 0)
            trades = int(txns.get("buys", 0)) + int(txns.get("sells", 0))
            if vol24 == 0 or trades == 0:
                continue

            pool_addr  = (p.get("pairAddress") or "").lower()
            base_addr  = (p.get("baseToken")   or {}).get("address", "").lower()
            quote_addr = (p.get("quoteToken")  or {}).get("address", "").lower()

            if not pool_addr or not base_addr or not quote_addr:
                continue
            if pool_addr in seen:
                continue
            seen.add(pool_addr)

            version, default_fee = DEXSCREENER_DEX_MAP[dex_id]
            pools.append({
                "dex":      dex_id,
                "pool":     pool_addr,
                "token0":   base_addr,
                "token1":   quote_addr,
                "fee":      default_fee,
                "type":     version,
                "liq_usd":  liq,
                "vol24":    vol24,
                "trades24": trades,
            })
            kept += 1

        logger.info(
            f"DexScreener [{i+1}/{len(ANCHOR_TOKENS)}] "
            f"{addr[:10]}... +{kept} pools ({len(pools)} total)"
        )
        await asyncio.sleep(0.4)   # ~150 req/min — well inside free tier

    logger.info(f"DexScreener discovery: {len(pools)} pools across {len(ANCHOR_TOKENS)} anchor tokens")
    return pools


class OnChainScout:
    def __init__(self):
        self.w3         = get_web3()
        self.pool_cache = self._load_cache()

    # ── Cache ─────────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        empty = {"pools": [], "timestamp": 0}
        if POOL_CACHE_PATH.exists():
            try:
                with open(POOL_CACHE_PATH) as f:
                    data = json.load(f)
                # Accept both old format (list) and new format (dict with timestamp)
                if isinstance(data, list):
                    return {"pools": data, "timestamp": 0}
                return data
            except Exception:
                pass
        return empty

    def _save_cache(self, pools: list):
        entry = {"pools": pools, "timestamp": time.time()}
        with open(POOL_CACHE_PATH, "w") as f:
            json.dump(entry, f, indent=2)
        self.pool_cache = entry

    # ── V3 fee resolution ─────────────────────────────────────────────────────

    def classify_pools(self, pools: list) -> list:
        """
        Single Multicall3 batch that verifies every pool's type on-chain and
        reclassifies or drops pools that don't match their DexScreener label.

        DexScreener uses the SAME dexId for multiple protocol versions:
          "uniswap" / "uniswap-v3" = UniV3         (default → "univ3")
          "camelot"                 = V2 OR V3/Algebra (default → "camelotv2")
          "sushiswap"               = V2 OR V3        (default → "sushiv2")

        Per-type checks performed:
          univ3    : fee()         succeeds → confirmed UniV3, update fee value
                                   fails   → not a real UniV3 pool → DROP
          camelotv2: globalState() succeeds → actually Algebra/V3 → upgrade to "algebra"
                     getReserves() succeeds → confirmed V2 → keep as "camelotv2"
                     both fail             → unknown/unsupported → DROP
          sushiv2  : getReserves() succeeds → confirmed SushiSwap V2 → keep
                                   fails   → SushiSwap V3 (its own factory, unsupported
                                             router) → DROP to prevent 'TF' errors
        """
        MC3_ABI = json.loads('[{"inputs":[{"internalType":"bool","name":"requireSuccess","type":"bool"},{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call[]","name":"calls","type":"tuple[]"}],"name":"tryAggregate","outputs":[{"components":[{"internalType":"bool","name":"success","type":"bool"},{"internalType":"bytes","name":"returnData","type":"bytes"}],"internalType":"struct Multicall3.Result[]","name":"returnData","type":"tuple[]"}],"stateMutability":"payable","type":"function"}]')
        mc = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MC3_ABI)

        calls = []
        meta  = []   # (pool_index, check_label)

        for i, p in enumerate(pools):
            ptype = p["type"]
            if ptype in ("univ3", "sushiv3"):
                # Both use fee() to confirm they're real V3 pools and get actual fee tier
                calls.append({"target": checksum(p["pool"]), "callData": _SEL_FEE})
                meta.append((i, "fee"))
            elif ptype == "camelotv2":
                # Two probes: globalState (Algebra V3) and getReserves (V2 AMM)
                calls.append({"target": checksum(p["pool"]), "callData": _SEL_GLOBALSTATE})
                meta.append((i, "camelot_gs"))
                calls.append({"target": checksum(p["pool"]), "callData": _SEL_GETRESERVES})
                meta.append((i, "camelot_rv"))
            elif ptype == "sushiv2":
                calls.append({"target": checksum(p["pool"]), "callData": _SEL_GETRESERVES})
                meta.append((i, "reserves"))

        if not calls:
            return pools

        try:
            results = mc.functions.tryAggregate(False, calls).call()
        except Exception as e:
            logger.warning(f"Pool classification failed (keeping defaults): {e}")
            return pools

        # Accumulate per-pool results: pool_index -> {label: success}
        pool_checks: dict = {}
        pool_data:   dict = {}
        for (success, data), (pool_i, label) in zip(results, meta):
            pool_checks.setdefault(pool_i, {})[label] = success
            pool_data.setdefault(pool_i, {})[label]   = data

        fee_resolved = upgraded = dropped = 0

        for pool_i, checks in pool_checks.items():
            p = pools[pool_i]

            if "fee" in checks:
                if checks["fee"] and pool_data[pool_i].get("fee"):
                    try:
                        fee = self.w3.codec.decode(["uint24"], pool_data[pool_i]["fee"])[0]
                        p["fee"] = fee
                        fee_resolved += 1
                    except Exception:
                        p["_drop"] = True
                        dropped += 1
                else:
                    p["_drop"] = True   # not a real UniV3 pool
                    dropped += 1

            elif "camelot_gs" in checks:
                if checks["camelot_gs"]:
                    p["type"] = "algebra"
                    p["fee"]  = 0
                    upgraded += 1
                elif checks.get("camelot_rv"):
                    pass   # confirmed Camelot V2, keep
                else:
                    p["_drop"] = True   # neither V2 nor V3 → unknown
                    dropped += 1

            elif "reserves" in checks:
                if not checks["reserves"]:
                    p["_drop"] = True   # SushiSwap V3 — no supported router
                    dropped += 1

        pools = [p for p in pools if not p.get("_drop")]

        logger.info(
            f"Pool classification: {fee_resolved} UniV3 fees resolved, "
            f"{upgraded} Camelot pools upgraded V2->Algebra, "
            f"{dropped} pools dropped (unsupported/fake)"
        )
        return pools

    # ── Pool refresh ──────────────────────────────────────────────────────────

    async def refresh_pools(self):
        pools = await _discover_pools_dexscreener()
        pools = self.classify_pools(pools)
        self._save_cache(pools)

    # ── Token metadata ────────────────────────────────────────────────────────

    async def get_token_metadata(self, token_addresses: List[str]):
        token_addresses = list(set(token_addresses))
        results = {}
        batch_size = 50

        mc_contract = self.w3.eth.contract(address=checksum(MULTICALL3_ADDR), abi=MULTICALL3_ABI)

        for i in range(0, len(token_addresses), batch_size):
            batch = token_addresses[i:i+batch_size]
            calls = []
            for addr in batch:
                contract = self.w3.eth.contract(address=checksum(addr), abi=ERC20_ABI)
                calls.append((checksum(addr), contract.encodeABI("symbol")))
                calls.append((checksum(addr), contract.encodeABI("decimals")))

            try:
                _, return_data = mc_contract.functions.aggregate(calls).call()
                for j, addr in enumerate(batch):
                    try:
                        try:
                            sym = self.w3.codec.decode(["string"], return_data[j*2])[0]
                        except Exception:
                            sym = self.w3.codec.decode(["bytes32"], return_data[j*2])[0].decode("utf-8").strip("\x00")
                        dec = self.w3.codec.decode(["uint8"], return_data[j*2+1])[0]
                        results[addr.lower()] = {"symbol": sym, "decimals": dec}
                    except Exception:
                        results[addr.lower()] = {"symbol": addr[:6], "decimals": 18}
            except Exception as e:
                logger.warning(f"Multicall metadata failed: {e}")
                for addr in batch:
                    results[addr.lower()] = {"symbol": addr[:6], "decimals": 18}

        return results

    # ── Watchlist builder ─────────────────────────────────────────────────────

    async def filter_and_build_watchlist(self):
        all_pools = self.pool_cache.get("pools", [])
        logger.info(f"Building watchlist from {len(all_pools)} verified pools")

        # ── Adjacency graph sorted by liquidity (best pools first) ──────────
        adj    = defaultdict(list)
        by_pair = defaultdict(list)
        for p in all_pools:
            t0 = p["token0"].lower()
            t1 = p["token1"].lower()
            adj[t0].append(p)
            adj[t1].append(p)
            by_pair[tuple(sorted([t0, t1]))].append(p)

        for t in adj:
            adj[t].sort(key=lambda p: p.get("liq_usd", 0), reverse=True)

        # Top-150 most-connected tokens as triangular bases
        token_counts = defaultdict(int)
        for p in all_pools:
            token_counts[p["token0"].lower()] += 1
            token_counts[p["token1"].lower()] += 1
        base_tokens = [
            t for t, _ in sorted(token_counts.items(), key=lambda x: x[1], reverse=True)[:150]
        ]
        logger.info(f"Selected {len(base_tokens)} base tokens for triangular paths")

        watchlist     = []
        seen_paths    = set()
        needed_tokens = set()

        # Max pools per leg in triangular search — sorted by liq so top-30
        # always means highest-liquidity pools first, not arbitrary ordering.
        MAX_LEG = 30

        # ── 1. Dual-DEX: same pair on different exchanges ────────────────────
        for pair, pair_pools in by_pair.items():
            # Best pool per DEX for this pair (by liquidity)
            best_by_dex = {}
            for p in pair_pools:
                prev = best_by_dex.get(p["dex"])
                if prev is None or p.get("liq_usd", 0) > prev.get("liq_usd", 0):
                    best_by_dex[p["dex"]] = p

            dex_names = list(best_by_dex.keys())
            if len(dex_names) < 2:
                continue

            needed_tokens.add(pair[0])
            needed_tokens.add(pair[1])

            for i in range(len(dex_names)):
                for j in range(i + 1, len(dex_names)):
                    d1, d2 = dex_names[i], dex_names[j]
                    p1, p2 = best_by_dex[d1], best_by_dex[d2]
                    key = (p1["pool"], p2["pool"])
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)

                    min_liq = min(p1.get("liq_usd", 0), p2.get("liq_usd", 0))
                    watchlist.append({
                        "type":     "dual",
                        "tokens":   [pair[0], pair[1], pair[0]],
                        "dexes":    [d1, d2],
                        "pools":    [p1["pool"], p2["pool"]],
                        "versions": [p1["type"], p2["type"]],
                        "fees":     [p1["fee"], p2["fee"]],
                        "min_liq":  min_liq,
                        "n_dexes":  2,
                    })

        # ── 2. Triangular + cross-DEX: A -> B -> C -> A ──────────────────────
        # For every (A,B,C) triple reachable within the top-liq adjacency lists,
        # ALL pool combinations are generated — including paths that use the same
        # token triple but route through different DEXes on each leg.
        # This captures cross-DEX triangular arb (e.g. buy on Camelot, sell on Uni).
        for base in base_tokens:
            for p1 in adj.get(base, [])[:MAX_LEG]:
                mid = p1["token1"].lower() if p1["token0"].lower() == base else p1["token0"].lower()
                if mid == base:
                    continue

                for p2 in adj.get(mid, [])[:MAX_LEG]:
                    end = p2["token1"].lower() if p2["token0"].lower() == mid else p2["token0"].lower()
                    if end in (base, mid):
                        continue

                    for p3 in adj.get(end, [])[:MAX_LEG]:
                        final = p3["token1"].lower() if p3["token0"].lower() == end else p3["token0"].lower()
                        if final != base:
                            continue

                        key = (p1["pool"], p2["pool"], p3["pool"])
                        if key in seen_paths:
                            continue
                        seen_paths.add(key)

                        needed_tokens.update([base, mid, end])
                        dexes   = [p1["dex"], p2["dex"], p3["dex"]]
                        min_liq = min(
                            p1.get("liq_usd", 0),
                            p2.get("liq_usd", 0),
                            p3.get("liq_usd", 0),
                        )
                        watchlist.append({
                            "type":     "triangular",
                            "tokens":   [base, mid, end, base],
                            "dexes":    dexes,
                            "pools":    [p1["pool"], p2["pool"], p3["pool"]],
                            "versions": [p1["type"], p2["type"], p3["type"]],
                            "fees":     [p1["fee"], p2["fee"], p3["fee"]],
                            "min_liq":  min_liq,
                            "n_dexes":  len(set(dexes)),
                        })

        logger.info(
            f"Discovered {len(watchlist)} raw paths "
            f"({sum(1 for w in watchlist if w['type']=='dual')} dual, "
            f"{sum(1 for w in watchlist if w['type']=='triangular')} triangular)"
        )

        # ── Rank: cross-DEX paths first, then by minimum pool liquidity ──────
        # Paths using more unique DEXes have higher cross-exchange price divergence
        # potential. Within the same n_dexes tier, higher min liquidity = more
        # reliable execution (less slippage, harder to drain in one flash loan).
        watchlist.sort(key=lambda x: (-x["n_dexes"], -x["min_liq"]))
        watchlist = watchlist[:2000]

        # ── Attach token metadata ─────────────────────────────────────────────
        token_meta = await self.get_token_metadata(list(needed_tokens))
        for item in watchlist:
            syms = [token_meta.get(t.lower(), {}).get("symbol", t[:6]) for t in item["tokens"]]
            item["symbol"] = (
                f"{syms[0]}/{syms[1]}" if item["type"] == "dual"
                else " -> ".join(syms)
            )
            item["dec_in"] = token_meta.get(item["tokens"][0].lower(), {}).get("decimals", 18)

        # ── Validate structure ────────────────────────────────────────────────
        valid = [
            item for item in watchlist
            if len(item["tokens"]) == len(item["pools"]) + 1
        ]
        n_dropped = len(watchlist) - len(valid)
        if n_dropped:
            logger.warning(f"Dropped {n_dropped} malformed watchlist entries")

        with open(WATCHLIST_PATH, "w") as f:
            json.dump(valid, f, indent=2)

        n_dual  = sum(1 for w in valid if w["type"] == "dual")
        n_tri   = sum(1 for w in valid if w["type"] == "triangular")
        n_xdex  = sum(1 for w in valid if w.get("n_dexes", 1) > 1)
        n_xdex3 = sum(1 for w in valid if w.get("n_dexes", 1) == 3)
        logger.info(
            f"Watchlist: {len(valid)} entries | "
            f"{n_dual} dual | {n_tri} triangular | "
            f"{n_xdex} cross-DEX ({n_xdex3} use 3 different exchanges)"
        )


async def update_watchlist():
    scout = OnChainScout()
    await scout.refresh_pools()
    await scout.filter_and_build_watchlist()


if __name__ == "__main__":
    asyncio.run(update_watchlist())
