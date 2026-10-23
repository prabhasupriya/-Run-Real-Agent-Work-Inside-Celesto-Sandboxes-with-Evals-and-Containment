from __future__ import annotations

import math


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def p90(values: list[float]) -> float:
    s = sorted(values)
    return s[math.ceil(0.9 * len(s)) - 1]


def summarize(runs: list[dict]) -> dict:
    n = len(runs)
    return {
        "run_count": n,
        "success_rate": round(sum(1 for r in runs if r["success"]) / n, 6),
        "median_wall_clock_s": round(median([r["wall_clock_s"] for r in runs]), 6),
        "p90_wall_clock_s": round(p90([r["wall_clock_s"] for r in runs]), 6),
        "median_boot_to_ready_s": round(median([r["boot_to_ready_s"] for r in runs]), 6),
        "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 6),
    }
