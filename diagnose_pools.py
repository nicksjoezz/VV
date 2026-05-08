#!/usr/bin/env python3
"""
diagnose_pools.py

Connects to Arbitrum via Alchemy and replicates exactly what the monitor does:
  - fetches prices and virtual reserves for every DEX pool
  - computes cross-DEX gaps
  - identifies low-liquidity / suspicious pools (E280 etc.)

Run from the ARB BOT directory:
    python diagnose_pools.py
"""
import sys, json, time, urllib.request
from collections import defaultdict
from itertools import combinations
from web3 import Web3

# ── Connection ────────────────────────────────────────────────────────────────

with open("config.json") as f:
    _cfg = json.load(f)

ALCHEMY_KEY = _cfg["network"]["alchemy_keys"][0]
w3 = Web3(Web3.HTTPProvider(
    f"https://arb-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}",
    request_kwargs={"timeout": 30}
))
if not w3.is_connected():
    print("[ERROR] Cannot connect to Alchemy")
    sys.exit(1)

block = w3.eth.block_number
print(f"Connected to Arbitrum One  |  block={block:,}")

# ── ETH price ─────────────────────────────────────────────────────────────────

def fetch_eth_price():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"
        with urllib.request.urlopen(url, timeout=5) as r:
            return float(json.loads(r.read())["ethereum"]["usd"])
    except Exception:
        return 2300.0

ETH_PRICE = fetch_eth_price()
print(f"ETH price:   ${ETH_PRICE:,.0f}")
MIN_RESERVE = _cfg.get("strategy", {}).get("min_reserve_tokens", 100)
print(f"Liquidity floor:  {MIN_RESERVE} whole tokens per pool side\n")

# ── Helpers ───────────────────────────────────────────────────────────────────

ZERO_ADDR = "0x" + "0" * 40

def cs(a): return Web3.to_checksum_address(a)

_meta = {}
ERC20_ABI = [
    {"inputs":[],"name":"symbol",      "outputs":[{"type":"string"}], "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"name",        "outputs":[{"type":"string"}], "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"decimals",    "outputs":[{"type":"uint8"}],  "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"totalSupply", "outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
]

def token_meta(addr):
    a = addr.lower()
    if a in _meta:
        return _meta[a]
    try:
        c = w3.eth.contract(address=cs(a), abi=ERC20_ABI)
        sym  = c.functions.symbol().call()
        name = c.functions.name().call()
        decs = c.functions.decimals().call()
        ts   = c.functions.totalSupply().call()
    except Exception:
        sym, name, decs, ts = a[:8], "?", 18, 0
    _meta[a] = {"symbol": sym, "name": name, "decimals": decs, "total_supply": ts}
    return _meta[a]

# ── Known tokens ──────────────────────────────────────────────────────────────

KNOWN = {
    "WETH":   "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "USDC.e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
    "ARB":    "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "LINK":   "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "UNI":    "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0",
    "PENDLE": "0x0c880f6761F1af8d9Aa9C466984b80DAb9a8c9e8",
}
ADDR_SYM = {v.lower(): k for k, v in KNOWN.items()}

# ── Factory ABIs ──────────────────────────────────────────────────────────────

V3_FAC_ABI = [{"inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],
               "name":"getPool","outputs":[{"type":"address"}],
               "stateMutability":"view","type":"function"}]
V2_FAC_ABI = [{"inputs":[{"type":"address"},{"type":"address"}],
               "name":"getPair","outputs":[{"type":"address"}],
               "stateMutability":"view","type":"function"}]
ALG_FAC_ABI = [{"inputs":[{"type":"address"},{"type":"address"}],
                "name":"poolByPair","outputs":[{"type":"address"}],
                "stateMutability":"view","type":"function"}]

# Pool ABIs
V3_POOL_ABI = [
    {"inputs":[],"name":"slot0","outputs":[
        {"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"},
        {"name":"observationIndex","type":"uint16"},{"name":"observationCardinality","type":"uint16"},
        {"name":"observationCardinalityNext","type":"uint16"},{"name":"feeProtocol","type":"uint8"},
        {"name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"liquidity","outputs":[{"name":"","type":"uint128"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"fee","outputs":[{"name":"","type":"uint24"}],"stateMutability":"view","type":"function"},
]
V2_POOL_ABI = [
    {"inputs":[],"name":"getReserves","outputs":[
        {"name":"r0","type":"uint112"},{"name":"r1","type":"uint112"},{"name":"ts","type":"uint32"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"},
]

FACTORIES = {
    "UniV3":     (w3.eth.contract(address=cs("0x1F98431c8aD98523631AE4a59f267346ea31F984"), abi=V3_FAC_ABI),  "univ3"),
    "CamelotV3": (w3.eth.contract(address=cs("0x1a3c9B1d2F0529D97f2afC5136Cc23e58f1FD35B"), abi=ALG_FAC_ABI),"algebra"),
    "CamelotV2": (w3.eth.contract(address=cs("0x6EcCab422D763aC031210895C81787E87B43A652"), abi=V2_FAC_ABI),  "camelotv2"),
    "UniV2":     (w3.eth.contract(address=cs("0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9"), abi=V2_FAC_ABI),  "univ2"),
    "SushiV2":   (w3.eth.contract(address=cs("0xc35DADB65012eC5796536bD9864eD8773aBc74C4"), abi=V2_FAC_ABI),  "sushiv2"),
}
V3_FEES = [100, 500, 3000, 10000]

# ── Step 1: discover pools for known token pairs ──────────────────────────────

print("=" * 80)
print("STEP 1 — Discover all pools for known token pairs")
print("=" * 80)

discovered = []   # {"dex", "ptype", "pool", "fee"}

pairs = list(combinations(KNOWN.values(), 2))
print(f"Querying {len(pairs)} pairs × 5 DEXes ...\n")

for t0, t1 in pairs:
    # UniV3 — one call per fee tier
    fac, ptype = FACTORIES["UniV3"]
    for fee in V3_FEES:
        try:
            pool = fac.functions.getPool(cs(t0), cs(t1), fee).call()
            if pool.lower() != ZERO_ADDR:
                discovered.append({"dex":"UniV3","ptype":ptype,"pool":pool,"fee":fee,"t_req":(t0.lower(),t1.lower())})
        except Exception:
            pass

    # CamelotV3 (Algebra) — one pool per pair
    fac, ptype = FACTORIES["CamelotV3"]
    try:
        pool = fac.functions.poolByPair(cs(t0), cs(t1)).call()
        if pool.lower() != ZERO_ADDR:
            discovered.append({"dex":"CamelotV3","ptype":ptype,"pool":pool,"fee":0,"t_req":(t0.lower(),t1.lower())})
    except Exception:
        pass

    # V2 DEXes
    for dex_name in ("CamelotV2", "UniV2", "SushiV2"):
        fac, ptype = FACTORIES[dex_name]
        try:
            pool = fac.functions.getPair(cs(t0), cs(t1)).call()
            if pool.lower() != ZERO_ADDR:
                discovered.append({"dex":dex_name,"ptype":ptype,"pool":pool,"fee":3000,"t_req":(t0.lower(),t1.lower())})
        except Exception:
            pass

print(f"  Found {len(discovered)} pools\n")

# ── Step 2: scan for UNKNOWN tokens connected to PENDLE and UNI ──────────────
# The bot's watchlist included PENDLE→E280→UNI triangular paths.
# E280 address starts with 0x058E. Find it via factory PairCreated events.

print("=" * 80)
print("STEP 2 — Scan for unknown tokens in PENDLE / UNI pools (finds E280, TT1, TT2 etc.)")
print("=" * 80)

PAIR_CREATED_TOPICS = {
    "CamelotV2": Web3.keccak(text="PairCreated(address,address,address,uint256)").hex(),
    "UniV2":     Web3.keccak(text="PairCreated(address,address,address,uint256)").hex(),
    "SushiV2":   Web3.keccak(text="PairCreated(address,address,address,uint256)").hex(),
}
FACTORY_ADDRS = {
    "CamelotV2": "0x6EcCab422D763aC031210895C81787E87B43A652",
    "UniV2":     "0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9",
    "SushiV2":   "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
}

SCAN_TOKENS = [KNOWN["PENDLE"], KNOWN["UNI"], KNOWN["ARB"], KNOWN["LINK"]]
SCAN_RANGE  = 5_000_000   # last ~5M blocks on Arbitrum ≈ ~3 months

extra_pools = []

for tok in SCAN_TOKENS:
    tok_topic = "0x" + "0" * 24 + tok[2:].lower()   # left-pad to 32 bytes
    tok_sym = ADDR_SYM.get(tok.lower(), tok[:8])
    for dex_name, fac_addr in FACTORY_ADDRS.items():
        try:
            logs = w3.eth.get_logs({
                "address":   cs(fac_addr),
                "fromBlock": block - SCAN_RANGE,
                "toBlock":   block,
                "topics":    [PAIR_CREATED_TOPICS[dex_name], tok_topic],
            })
            for log in logs:
                t0_raw = log["topics"][1].hex()
                t1_raw = log["topics"][2].hex()
                t0 = cs("0x" + t0_raw[-40:]).lower()
                t1 = cs("0x" + t1_raw[-40:]).lower()
                data = log["data"]
                if isinstance(data, bytes): data = data.hex()
                if data.startswith("0x"): data = data[2:]
                pool = cs("0x" + data[24:64])
                other = t1 if t0.lower() == tok.lower() else t0
                if other.lower() not in ADDR_SYM:
                    extra_pools.append({
                        "dex": dex_name, "ptype": "univ2" if dex_name != "CamelotV2" else "camelotv2",
                        "pool": pool, "fee": 3000,
                        "t_req": (tok.lower(), other.lower()),
                        "via_token": tok_sym,
                    })
        except Exception as e:
            if "limit" in str(e).lower() or "range" in str(e).lower():
                # Try smaller range
                try:
                    logs = w3.eth.get_logs({
                        "address":   cs(fac_addr),
                        "fromBlock": block - 500_000,
                        "toBlock":   block,
                        "topics":    [PAIR_CREATED_TOPICS[dex_name], tok_topic],
                    })
                    for log in logs:
                        t0_raw = log["topics"][1].hex()
                        t1_raw = log["topics"][2].hex()
                        t0 = cs("0x" + t0_raw[-40:]).lower()
                        t1 = cs("0x" + t1_raw[-40:]).lower()
                        data = log["data"]
                        if isinstance(data, bytes): data = data.hex()
                        if data.startswith("0x"): data = data[2:]
                        pool = cs("0x" + data[24:64])
                        other = t1 if t0.lower() == tok.lower() else t0
                        if other.lower() not in ADDR_SYM:
                            extra_pools.append({
                                "dex": dex_name, "ptype": "univ2" if dex_name != "CamelotV2" else "camelotv2",
                                "pool": pool, "fee": 3000,
                                "t_req": (tok.lower(), other.lower()),
                                "via_token": tok_sym,
                            })
                except Exception:
                    pass

# Deduplicate by pool address
seen_pools = {p["pool"].lower() for p in discovered}
for p in extra_pools:
    if p["pool"].lower() not in seen_pools:
        discovered.append(p)
        seen_pools.add(p["pool"].lower())

print(f"  Found {len(extra_pools)} extra pools with unknown tokens\n")

# ── Step 3: fetch on-chain data for every pool ────────────────────────────────

print("=" * 80)
print("STEP 3 — Fetch price, reserves, liquidity for every pool")
print("=" * 80)

def fetch_v3(pool_addr):
    try:
        c = w3.eth.contract(address=cs(pool_addr), abi=V3_POOL_ABI)
        slot0 = c.functions.slot0().call()
        liq   = c.functions.liquidity().call()
        t0    = c.functions.token0().call().lower()
        t1    = c.functions.token1().call().lower()
        sqrt_p = slot0[0]
        d0 = token_meta(t0)["decimals"]
        d1 = token_meta(t1)["decimals"]
        if sqrt_p > 0:
            price = (sqrt_p / 2**96)**2 * (10**d0 / 10**d1)
            virt_x = int(liq * 2**96 / sqrt_p)
            virt_y = int(liq * sqrt_p / 2**96)
        else:
            price = 0
            virt_x = virt_y = 0
        return {"ok":True,"t0":t0,"t1":t1,"price":price,"r0":virt_x,"r1":virt_y,"liq":liq,"sqrt_p":sqrt_p}
    except Exception as e:
        return {"ok":False,"err":str(e)}

def fetch_v2(pool_addr):
    try:
        c = w3.eth.contract(address=cs(pool_addr), abi=V2_POOL_ABI)
        res = c.functions.getReserves().call()
        t0  = c.functions.token0().call().lower()
        t1  = c.functions.token1().call().lower()
        r0, r1 = res[0], res[1]
        d0 = token_meta(t0)["decimals"]
        d1 = token_meta(t1)["decimals"]
        price = (r1 / 10**d1) / (r0 / 10**d0) if r0 > 0 else 0
        return {"ok":True,"t0":t0,"t1":t1,"price":price,"r0":r0,"r1":r1,"liq":min(r0,r1),"sqrt_p":0}
    except Exception as e:
        return {"ok":False,"err":str(e)}

enriched = []
for i, p in enumerate(discovered):
    ptype = p["ptype"]
    if ptype in ("univ3", "algebra"):
        data = fetch_v3(p["pool"])
    else:
        data = fetch_v2(p["pool"])

    if not data["ok"]:
        continue

    t0, t1 = data["t0"], data["t1"]
    m0, m1 = token_meta(t0), token_meta(t1)
    d0, d1 = m0["decimals"], m1["decimals"]
    r0_tok = data["r0"] / 10**d0
    r1_tok = data["r1"] / 10**d1
    passes = r0_tok >= MIN_RESERVE and r1_tok >= MIN_RESERVE
    sym0 = ADDR_SYM.get(t0, m0["symbol"])
    sym1 = ADDR_SYM.get(t1, m1["symbol"])

    enriched.append({
        **p,
        "t0": t0, "t1": t1,
        "sym0": sym0, "sym1": sym1,
        "d0": d0, "d1": d1,
        "price": data["price"],
        "r0": data["r0"], "r1": data["r1"],
        "r0_tok": r0_tok, "r1_tok": r1_tok,
        "liq": data["liq"],
        "liq_pass": passes,
        "sqrt_p": data["sqrt_p"],
        "is_unknown": t0 not in ADDR_SYM or t1 not in ADDR_SYM,
    })
    print(f"  [{i+1:>3}/{len(discovered)}] {sym0}/{sym1} on {p['dex']:<12} "
          f"r0={r0_tok:>12.2f}  r1={r1_tok:>12.2f}  "
          f"{'[PASS]' if passes else '[FAIL liq]'}")

print(f"\nFetched {len(enriched)} pools\n")

# ── Step 4: cross-DEX price comparison ───────────────────────────────────────

print("=" * 80)
print("STEP 4 — Cross-DEX price comparison (what the monitor sees per pair)")
print("=" * 80)

by_pair = defaultdict(list)
for p in enriched:
    key = tuple(sorted([p["sym0"], p["sym1"]]))
    by_pair[key].append(p)

issues = []

for pair_key in sorted(by_pair):
    pools = by_pair[pair_key]
    A, B  = pair_key

    # Normalise all prices to A/B direction
    normed = []
    for p in pools:
        if p["sym0"] == A:
            normed.append((p["price"], p))
        else:
            normed.append((1/p["price"] if p["price"] > 0 else 0, p))

    prices   = [pr for pr, _ in normed if pr > 0]
    min_p    = min(prices) if prices else 0
    max_p    = max(prices) if prices else 0
    gap_pct  = (max_p / min_p - 1) if min_p > 0 else 0
    any_fail = any(not p["liq_pass"] for p in pools)
    any_unk  = any(p["is_unknown"]   for p in pools)

    # Flags
    flags = []
    if any_fail: flags.append("LOW LIQ")
    if any_unk:  flags.append("UNKNOWN TOKEN")
    if gap_pct > 0.05:  flags.append(f"GAP {gap_pct:.1%} !!!")
    elif gap_pct > 0.002: flags.append(f"gap {gap_pct:.2%}")

    flag_str = "  [" + " | ".join(flags) + "]" if flags else ""
    print(f"\n{A}/{B}{flag_str}")
    print(f"  {'DEX':<12} {'TYPE':<10} {'FEE':<8} {'PRICE (A/B)':<20} "
          f"{'RESERVE A':<16} {'RESERVE B':<16} {'LIQ?':<6} {'NOTE'}")
    print(f"  {'-'*98}")

    for pr, p in sorted(normed, key=lambda x: x[0]):
        fee_str = f"{p['fee']/10000:.2f}%" if p["ptype"] == "univ3" else ("alg" if p["ptype"]=="algebra" else "0.30%")
        liq_str = "PASS" if p["liq_pass"] else "FAIL"
        note    = ""
        if p["is_unknown"]:
            unk = p["sym1"] if p["sym0"] == A else p["sym0"]
            addr = p["t1"] if p["sym0"] == A else p["t0"]
            note = f"UNKNOWN: {unk} ({addr})"
        print(f"  {p['dex']:<12} {p['ptype']:<10} {fee_str:<8} {pr:<20.8f} "
              f"{p['r0_tok'] if p['sym0']==A else p['r1_tok']:<16.4f} "
              f"{p['r1_tok'] if p['sym0']==A else p['r0_tok']:<16.4f} "
              f"{liq_str:<6} {note}")

    if gap_pct > 0.002:
        issues.append({
            "pair": f"{A}/{B}",
            "gap": gap_pct,
            "min_price": min_p,
            "max_price": max_p,
            "n_pools": len(pools),
            "any_unknown": any_unk,
            "any_low_liq": any_fail,
        })

# ── Step 5: unknown token deep-dive ──────────────────────────────────────────

print("\n")
print("=" * 80)
print("STEP 5 — Unknown token investigation")
print("=" * 80)

unknown_addrs = set()
for p in enriched:
    if p["t0"] not in ADDR_SYM: unknown_addrs.add(p["t0"])
    if p["t1"] not in ADDR_SYM: unknown_addrs.add(p["t1"])

if not unknown_addrs:
    print("  No unknown tokens found in any pool.")
else:
    print(f"  {len(unknown_addrs)} unknown token(s) found:\n")
    for addr in sorted(unknown_addrs):
        m = token_meta(addr)
        ts_whole = m["total_supply"] / 10**m["decimals"] if m["decimals"] <= 30 else 0
        # Find which pools it appears in
        in_pools = [p for p in enriched if p["t0"] == addr or p["t1"] == addr]
        pair_strs = [f"{p['sym0']}/{p['sym1']} on {p['dex']}" for p in in_pools]
        print(f"  Address : {addr}")
        print(f"  Symbol  : {m['symbol']}")
        print(f"  Name    : {m['name']}")
        print(f"  Decimals: {m['decimals']}")
        print(f"  Supply  : {ts_whole:,.0f} tokens")
        print(f"  In pools: {', '.join(pair_strs)}")
        # Check liquidity in each pool
        for p in in_pools:
            r = p["r0_tok"] if p["t0"]==addr else p["r1_tok"]
            status = "PASS" if r >= MIN_RESERVE else f"FAIL (only {r:.4f} tokens)"
            print(f"           -> {p['dex']}: {r:.4f} {m['symbol']} in pool  [{status}]")
        print()

# ── Step 6: monitor simulation ───────────────────────────────────────────────

print("=" * 80)
print("STEP 6 — Simulate what the monitor's gap filter would detect (and what it should block)")
print("=" * 80)
print(f"  Gap filter:  1.002 < amount < 1.50  (0.2% – 50%)")
print(f"  NEW gap cap: amount < 1.05           (5% cap added in executor)")
print(f"  Liq filter:  each side >= {MIN_RESERVE} whole tokens\n")

print(f"  {'PATH':<40} {'GAP':<10} {'LIQ?':<6} {'GAP<5%?':<10} {'VERDICT'}")
print(f"  {'-'*85}")

# Enumerate every possible triangular path through found pools
# Only consider pools that pass the liquidity filter for this simulation
liq_ok_pools = [p for p in enriched if p["liq_pass"]]

# Build adjacency: token -> list of pools
adj = defaultdict(list)
for p in liq_ok_pools:
    adj[p["t0"]].append(p)
    adj[p["t1"]].append(p)

triggered = []
checked   = set()

for base_sym, base_addr in KNOWN.items():
    base = base_addr.lower()
    for p1 in adj.get(base, []):
        mid = p1["t1"] if p1["t0"] == base else p1["t0"]
        for p2 in adj.get(mid, []):
            end = p2["t1"] if p2["t0"] == mid else p2["t0"]
            if end == base or end == mid: continue
            for p3 in adj.get(end, []):
                final = p3["t1"] if p3["t0"] == end else p3["t0"]
                if final != base: continue
                path_key = tuple(sorted([p1["pool"].lower(), p2["pool"].lower(), p3["pool"].lower()]))
                if path_key in checked: continue
                checked.add(path_key)

                # Compute product of prices (same as monitor's amount computation)
                def price_along(pool, t_in):
                    if pool["t0"] == t_in:
                        return pool["price"]
                    else:
                        return 1/pool["price"] if pool["price"] > 0 else 0

                amount = 1.0
                amount *= price_along(p1, base)
                amount *= price_along(p2, mid)
                amount *= price_along(p3, end)

                gap = amount - 1.0
                s0 = ADDR_SYM.get(base, base[:8])
                s1 = ADDR_SYM.get(mid,  token_meta(mid)["symbol"])
                s2 = ADDR_SYM.get(end,  token_meta(end)["symbol"])
                path_str = f"{s0}->{s1}->{s2}->{s0}"

                passes_monitor = 1.002 < amount < 1.50
                passes_gap_cap = gap <= 0.05
                passes_liq     = True   # already filtered above

                if passes_monitor:
                    verdict = "FIRE" if passes_gap_cap else "BLOCKED by gap cap"
                    triggered.append({
                        "path": path_str,
                        "gap": gap,
                        "passes_gap_cap": passes_gap_cap,
                        "pools": [p1["pool"], p2["pool"], p3["pool"]],
                        "dexes": [p1["dex"], p2["dex"], p3["dex"]],
                    })
                    print(f"  {path_str:<40} {gap:>+8.3%}   {'PASS':<6} {'YES' if passes_gap_cap else 'NO':<10} {verdict}")

if not triggered:
    print("  No triangular opportunities found in the liq-filtered pool set.")

# Also simulate with ALL pools (including low-liq) to show what bot was seeing before
print(f"\n  -- Simulation with low-liquidity pools INCLUDED (what old bot saw) --\n")
adj_all = defaultdict(list)
for p in enriched:
    adj_all[p["t0"]].append(p)
    adj_all[p["t1"]].append(p)

ghost_count = 0
for base_sym, base_addr in KNOWN.items():
    base = base_addr.lower()
    for p1 in adj_all.get(base, []):
        mid = p1["t1"] if p1["t0"] == base else p1["t0"]
        for p2 in adj_all.get(mid, []):
            end = p2["t1"] if p2["t0"] == mid else p2["t0"]
            if end == base or end == mid: continue
            for p3 in adj_all.get(end, []):
                final = p3["t1"] if p3["t0"] == end else p3["t0"]
                if final != base: continue
                path_key = tuple(sorted([p1["pool"].lower(), p2["pool"].lower(), p3["pool"].lower()]))
                amount = 1.0
                def price_along2(pool, t_in):
                    if pool["t0"] == t_in: return pool["price"]
                    return 1/pool["price"] if pool["price"] > 0 else 0
                amount *= price_along2(p1, base)
                amount *= price_along2(p2, mid)
                amount *= price_along2(p3, end)
                gap = amount - 1.0
                passes_monitor = 1.002 < amount < 1.50
                if passes_monitor:
                    s0 = ADDR_SYM.get(base, base[:8])
                    s1 = ADDR_SYM.get(mid,  token_meta(mid)["symbol"])
                    s2 = ADDR_SYM.get(end,  token_meta(end)["symbol"])
                    path_str = f"{s0}->{s1}->{s2}->{s0}"
                    liq_ok_path = p1["liq_pass"] and p2["liq_pass"] and p3["liq_pass"]
                    gap_ok = gap <= 0.05
                    if not liq_ok_path or not gap_ok:
                        ghost_count += 1
                        print(f"  {path_str:<40} {gap:>+8.3%}   "
                              f"{'PASS' if liq_ok_path else 'FAIL':<6} "
                              f"{'YES' if gap_ok else 'NO':<10} "
                              f"GHOST (would have fired before fixes)")

if ghost_count == 0:
    print("  No ghost opportunities found.")

# ── Summary ───────────────────────────────────────────────────────────────────

print("\n")
print("=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"  Total pools fetched:       {len(enriched)}")
print(f"  Pools passing liq filter:  {sum(1 for p in enriched if p['liq_pass'])}")
print(f"  Pools failing liq filter:  {sum(1 for p in enriched if not p['liq_pass'])}")
print(f"  Unknown tokens found:      {len(unknown_addrs)}")
print(f"  Price-gap issues:          {len(issues)}")
for iss in issues:
    print(f"    {iss['pair']}: gap={iss['gap']:.2%}  unknown={iss['any_unknown']}  low_liq={iss['any_low_liq']}")
print()
