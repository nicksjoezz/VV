import json
import asyncio
from typing import List
from collections import defaultdict
from web3 import Web3
from .utils import ROOT_DIR, logger, checksum, cfg, MULTICALL3_ADDR, MULTICALL3_ABI

WATCHLIST_PATH = ROOT_DIR / "logs" / "watchlist.json"
POOL_CACHE_PATH = ROOT_DIR / "logs" / "pool_cache.json"

# FREE public Arbitrum RPCs from research
DISCOVERY_RPCS = [
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum.llamarpc.com",
    "https://rpc.ankr.com/arbitrum",
    "https://arbitrum-one.public.blastapi.io"
]

def get_discovery_w3():
    for rpc in DISCOVERY_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return Web3(Web3.HTTPProvider(cfg("network", "rpc_http")))

FACTORIES = {
    # Uniswap V3 — dominant on Arbitrum (~70% spot DEX volume). Verified factory + router.
    "uniswap_v3": {
        "address": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
        "deploy_block": 165,
        "type": "univ3",
        "topic": Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
    },
    # Camelot V3 (Algebra) — #2 DEX on Arbitrum, ~$76M/day. Verified factory + router.
    "camelot_v3": {
        "address": "0x1a3c9B1d2F0529D97f2afC5136Cc23e58f1FD35B",
        "deploy_block": 71408700,
        "type": "algebra",
        "topic": Web3.keccak(text="Pool(address,address,address)").hex()
    },
    # Camelot V2 — active classic AMM pools (xGRAIL pairs, legacy). Verified.
    "camelot_v2": {
        "address": "0x6EcCab422D763aC031210895C81787E87B43A652",
        "deploy_block": 40000000,
        "type": "univ2",
        "topic": Web3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    },
    # Uniswap V2 — officially deployed on Arbitrum in 2024. Thin liquidity but valid.
    "uniswap_v2": {
        "address": "0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9",
        "deploy_block": 178000000,
        "type": "univ2",
        "topic": Web3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    },
    # SushiSwap V2 — active, cross-chain factory. Verified factory + router.
    "sushiswap_v2": {
        "address": "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
        "deploy_block": 100000000,
        "type": "univ2",
        "topic": Web3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    },
    # NOTE: sushiswap_v3 removed — its SwapRouter uses Route Processor architecture
    # (incompatible with ISwapRouter.exactInputSingle used by our contract).
    # NOTE: ramses_v2 removed — factory address was the LP NFT contract (not pool deployer),
    # and Ramses V2 volume is ~$25K/day (too low for viable arb after gas costs).
}

ERC20_ABI = json.loads('[{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}]')

class OnChainScout:
    def __init__(self):
        self.w3 = get_discovery_w3()
        self.pool_cache = self._load_cache()

    def _load_cache(self):
        empty = {"last_blocks": {}, "pools": []}
        if POOL_CACHE_PATH.exists():
            try:
                with open(POOL_CACHE_PATH, "r") as f:
                    data = json.load(f)
                # Drop pools from DEXes that are no longer in FACTORIES
                # (e.g. sushiswap_v3 had wrong address, ramses_v2 was an NFT contract)
                active_dexes = set(FACTORIES.keys())
                before = len(data.get("pools", []))
                data["pools"] = [p for p in data.get("pools", []) if p.get("dex") in active_dexes]
                pruned = before - len(data["pools"])
                if pruned:
                    logger.info(f"Pruned {pruned} stale pools from removed DEXes")
                # Also drop last_block entries for removed DEXes
                data["last_blocks"] = {k: v for k, v in data.get("last_blocks", {}).items() if k in active_dexes}
                return data
            except Exception:
                return empty
        return empty

    def _save_cache(self):
        with open(POOL_CACHE_PATH, "w") as f:
            json.dump(self.pool_cache, f, indent=2)

    async def fetch_logs(self, factory_name, config, to_block):
        # Scan last 50M blocks (~6 months) for broad coverage
        default_start = max(config["deploy_block"], to_block - 50000000)
        from_block = self.pool_cache["last_blocks"].get(factory_name, default_start)

        if from_block >= to_block: return []

        all_logs = []
        batch_size = 50000 # Research suggests 50k
        current = from_block

        logger.info(f"Scanning {factory_name} from {current} to {to_block}...")

        # Limit per cycle to 50M blocks to get more pairs quickly
        cycle_limit = 50000000
        target_to = min(to_block, current + cycle_limit)

        while current < target_to:
            end = min(current + batch_size, target_to)
            try:
                logs = self.w3.eth.get_logs({
                    "address": checksum(config["address"]),
                    "fromBlock": current,
                    "toBlock": end,
                    "topics": [config["topic"]]
                })
                all_logs.extend(logs)
                current = end + 1
                if logs: logger.info(f"Found {len(logs)} logs in {factory_name} (Total: {len(all_logs)})")
                await asyncio.sleep(0.1) # Be nice to public RPCs
            except Exception as e:
                if "limit" in str(e).lower() or "range" in str(e).lower() or "too many" in str(e).lower():
                    batch_size //= 2
                    logger.warning(f"Reducing batch size to {batch_size} for {factory_name}")
                    if batch_size < 100: break
                else:
                    logger.error(f"Error fetching logs for {factory_name}: {e}")
                    break

        self.pool_cache["last_blocks"][factory_name] = current
        return all_logs

    def parse_log(self, log, factory_name):
        config = FACTORIES[factory_name]
        try:
            topics = log["topics"]
            data = log["data"]
            if isinstance(data, bytes): data = data.hex()
            if data.startswith("0x"): data = data[2:]

            token0 = checksum("0x" + topics[1].hex()[-40:])
            token1 = checksum("0x" + topics[2].hex()[-40:])

            pool_address = ""
            fee = 0
            ptype = "univ2"  # safe default; overwritten below

            if config["type"] == "univ3":
                fee = int(topics[3].hex(), 16)
                pool_address = checksum("0x" + data[-40:])
                ptype = "univ3"
            elif config["type"] == "algebra":
                pool_address = checksum("0x" + data[-40:])
                ptype = "algebra"
            elif config["type"] == "univ2":
                pool_address = checksum("0x" + data[24:64])
                fee = 3000
                if factory_name == "camelot_v2":
                    ptype = "camelotv2"
                elif "sushi" in factory_name:
                    ptype = "sushiv2"
                else:
                    ptype = "univ2"

            return {
                "dex": factory_name,
                "pool": pool_address,
                "token0": token0,
                "token1": token1,
                "fee": fee,
                "type": ptype
            }
        except Exception:
            return None

    async def scan_factories(self):
        try:
            latest_block = self.w3.eth.block_number
        except Exception:
            return

        new_pools = []
        for name, config in FACTORIES.items():
            logs = await self.fetch_logs(name, config, latest_block)
            for log in logs:
                p = self.parse_log(log, name)
                if p: new_pools.append(p)

        existing_pools = {p["pool"].lower() for p in self.pool_cache["pools"]}
        for p in new_pools:
            if p["pool"].lower() not in existing_pools:
                self.pool_cache["pools"].append(p)
                existing_pools.add(p["pool"].lower())

        self._save_cache()
        logger.info(f"Total pools in cache: {len(self.pool_cache['pools'])}")

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
                # multicall3 returns [blockNumber, returnData[]]
                _, return_data = mc_contract.functions.aggregate(calls).call()

                for j, addr in enumerate(batch):
                    try:
                        try:
                            sym = self.w3.codec.decode(["string"], return_data[j*2])[0]
                        except Exception:
                            sym = self.w3.codec.decode(["bytes32"], return_data[j*2])[0].decode('utf-8').strip('\x00')

                        dec = self.w3.codec.decode(["uint8"], return_data[j*2+1])[0]
                        results[addr.lower()] = {"symbol": sym, "decimals": dec}
                    except Exception as e:
                        logger.debug(f"Metadata fail for {addr}: {e}")
                        results[addr.lower()] = {"symbol": addr[:6], "decimals": 18}
            except Exception as e:
                logger.warning(f"Multicall metadata failed: {e}")
                for addr in batch:
                    results[addr.lower()] = {"symbol": addr[:6], "decimals": 18}

        return results

    async def filter_and_build_watchlist(self):
        pools = self.pool_cache["pools"]
        by_pair = defaultdict(list)
        for p in pools:
            pair = tuple(sorted([p["token0"].lower(), p["token1"].lower()]))
            by_pair[pair].append(p)

        # Dynamic base token selection (Top 100 most connected tokens)
        token_counts = defaultdict(int)
        for p in pools:
            token_counts[p["token0"].lower()] += 1
            token_counts[p["token1"].lower()] += 1

        # Sort by pool count and take top 100
        sorted_tokens = sorted(token_counts.items(), key=lambda x: x[1], reverse=True)
        base_tokens = [t[0] for t in sorted_tokens[:100]]
        logger.info(f"Dynamically selected {len(base_tokens)} base tokens for triangular paths.")

        watchlist = []
        needed_tokens = set()

        # 1. Dual-DEX Pairs
        for pair, pair_pools in by_pair.items():
            dexes = {p["dex"] for p in pair_pools}
            if len(dexes) >= 2:
                needed_tokens.add(pair[0]); needed_tokens.add(pair[1])
                # Pick the best pool for each DEX (often there's only one, or multiple fee tiers)
                best_by_dex = {}
                for p in pair_pools:
                    prev = best_by_dex.get(p["dex"])
                    if prev is None:
                        best_by_dex[p["dex"]] = p
                    elif p["type"] in ("univ3", "algebra"):
                        # For V3, prefer the lowest fee tier — those pools
                        # attract the deepest liquidity (e.g. USDC/WETH 0.05%)
                        if p["fee"] < prev["fee"]:
                            best_by_dex[p["dex"]] = p
                    else:
                        # For V2 there's only one pool per pair; first entry wins
                        pass

                dex_names = list(best_by_dex.keys())
                for i in range(len(dex_names)):
                    for j in range(i+1, len(dex_names)):
                        d1, d2 = dex_names[i], dex_names[j]
                        p1, p2 = best_by_dex[d1], best_by_dex[d2]
                        watchlist.append({
                            "type": "dual",
                            "tokens": [p1["token0"], p1["token1"], p1["token0"]],
                            "dexes": [d1, d2],
                            "pools": [p1["pool"], p2["pool"]],
                            "versions": [p1["type"], p2["type"]],
                            "fees": [p1["fee"], p2["fee"]]
                        })

        # 2. Triangular Paths
        # Build adjacency list
        adj = defaultdict(list)
        for p in pools:
            adj[p["token0"].lower()].append(p)
            adj[p["token1"].lower()].append(p)

        # Cap adjacency per token to avoid O(n^3) explosion.
        # Major tokens (WETH, USDC) can appear in 500+ pools — uncapped this
        # produces billions of iterations and hangs indefinitely.
        MAX_ADJ = 20

        for base in base_tokens:
            for p1 in adj.get(base, [])[:MAX_ADJ]:
                mid = p1["token1"].lower() if p1["token0"].lower() == base else p1["token0"].lower()
                if mid == base: continue
                for p2 in adj.get(mid, [])[:MAX_ADJ]:
                    end = p2["token1"].lower() if p2["token0"].lower() == mid else p2["token0"].lower()
                    if end == base or end == mid: continue
                    # Check if there is a pool between end and base
                    for p3 in adj.get(end, [])[:MAX_ADJ]:
                        final = p3["token1"].lower() if p3["token0"].lower() == end else p3["token0"].lower()
                        if final == base:
                            needed_tokens.add(base); needed_tokens.add(mid); needed_tokens.add(end)
                            watchlist.append({
                                "type": "triangular",
                                "tokens": [base, mid, end, base],
                                "dexes": [p1["dex"], p2["dex"], p3["dex"]],
                                "pools": [p1["pool"], p2["pool"], p3["pool"]],
                                "versions": [p1["type"], p2["type"], p3["type"]],
                                "fees": [p1["fee"], p2["fee"], p3["fee"]]
                            })

        token_meta = await self.get_token_metadata(list(needed_tokens))
        for item in watchlist:
            syms = [token_meta.get(t.lower(), {}).get("symbol", t[:6]) for t in item["tokens"]]
            if item["type"] == "dual":
                item["symbol"] = f"{syms[0]}/{syms[1]}"
            else:
                item["symbol"] = " -> ".join(syms)

        watchlist.sort(key=lambda x: (x["type"], x["symbol"]))

        # Limit watchlist size for stability
        watchlist = watchlist[:500]

        # Verify tokens vs pools length for all items
        valid_watchlist = []
        for item in watchlist:
            if len(item["tokens"]) == len(item["pools"]) + 1:
                valid_watchlist.append(item)
            else:
                logger.warning(f"Invalid watchlist item: {item['symbol']} tokens={len(item['tokens'])} pools={len(item['pools'])}")

        with open(WATCHLIST_PATH, "w") as f:
            json.dump(valid_watchlist, f, indent=2)
        logger.info(f"Watchlist updated: {len(valid_watchlist)} entries.")

async def update_watchlist():
    scout = OnChainScout()
    await scout.scan_factories()
    await scout.filter_and_build_watchlist()

if __name__ == "__main__":
    asyncio.run(update_watchlist())
