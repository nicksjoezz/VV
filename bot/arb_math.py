import math

MAX_IMPACT = 0.03   # 3% maximum price impact per pool


def _inverse_amm(amount_out: float, reserve_in: float, reserve_out: float, fee_pct: float) -> float:
    """
    Inverse constant-product AMM: given desired output, compute required input.
    Returns inf when the output would drain more than the available reserve,
    or when fee_pct is invalid.
    """
    if reserve_out <= amount_out or reserve_in <= 0 or fee_pct >= 1.0:
        return math.inf
    return (reserve_in * amount_out) / ((reserve_out - amount_out) * (1.0 - fee_pct))


def calculate_optimal_multihop(hops: list, gap: float = 0.0) -> int:
    """
    Compute the largest flash loan amount that keeps price impact ≤ 3%
    on EVERY pool in the arb path — dual, triangular, or any N-hop route.

    Algorithm (back-propagation):
      For each pool k in the path, enforce that the tokens flowing *into* it
      do not exceed 3% of its input reserve.  Back-solve through pools k-1 → 0
      to express that constraint as a max amount for the *first* pool's input.
      The final answer is the minimum across all N back-propagated candidates,
      also bounded by the sqrt-of-price-ratio theoretical optimum.

    Each hop dict:
        reserve_in  (int)   – input-token reserve in native units (wei / raw)
        reserve_out (int)   – output-token reserve in native units
        fee_pct     (float) – pool fee as a decimal, e.g. 0.003 for 0.3%
        dec_in      (int)   – input-token decimals (only needed for hop 0)

    Returns optimal flash loan amount in native units of hop[0].token_in,
    or 0 if not enough data / result is dust.
    """
    if not hops:
        return 0

    candidates = []

    for k in range(len(hops)):
        # Skip this candidate if any hop from 0..k is missing reserve data.
        # A missing hop would force the constraint to 0 and hide real limits.
        if any(
            hops[j]["reserve_in"] <= 0 or hops[j]["reserve_out"] <= 0
            for j in range(k + 1)
        ):
            continue

        # Constraint: input to hop k ≤ 3% of its input-token reserve
        cap = hops[k]["reserve_in"] * MAX_IMPACT

        # Back-propagate cap through hops k-1 → 0
        max_in_0 = cap
        ok = True
        for j in range(k - 1, -1, -1):
            hop = hops[j]
            # max_in_0 is currently "max input to hop j+1" = "max output from hop j"
            max_in_0 = _inverse_amm(
                max_in_0,
                hop["reserve_in"],
                hop["reserve_out"],
                hop["fee_pct"],
            )
            if not math.isfinite(max_in_0) or max_in_0 <= 0:
                ok = False
                break

        if ok and max_in_0 > 0:
            candidates.append(max_in_0)

    # Bound by the theoretical sqrt-of-price-ratio optimum.
    # Only added when ALL hops have complete reserve data — otherwise we
    # could bypass per-pool constraints for hops with missing data.
    all_hops_valid = all(
        h["reserve_in"] > 0 and h["reserve_out"] > 0 for h in hops
    )
    if gap > 0 and hops and all_hops_valid:
        sqrt_opt = (math.sqrt(1.0 + gap) - 1.0) * hops[0]["reserve_in"]
        if sqrt_opt > 0:
            candidates.append(sqrt_opt)

    if not candidates:
        return 0

    optimal = min(candidates)
    if optimal <= 0:
        return 0

    dec_in = hops[0].get("dec_in", 18) if hops else 18
    dust   = 10 ** max(0, dec_in - 4)   # ignore anything < 0.0001 tokens
    if optimal < dust:
        return 0

    return int(optimal)
