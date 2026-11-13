"""Rank CSI 300 closing signals for a short-term watchlist."""

import math
from statistics import median

from stock_screen import MIN_AMOUNT, MIN_COVERAGE, dated_bars, number, validate_snapshot


VERSION = "1.0"
MAX_DAILY_MOVE = 8
WEIGHTS = {"excess5": 0.45, "excess20": 0.35, "amount_ratio": 0.20}


def features(stock, days, benchmark5, benchmark20):
    code = stock["code"]
    bars = dated_bars(stock["bars"], code, set(days[-21:]))
    today = bars.get(days[-1], {})
    if today.get("tradestatus") == "0":
        return True, None
    close, amount, move = (number(today.get(field)) for field in ("close", "amount", "pctChg"))
    status, st = today.get("tradestatus"), today.get("isST")
    if status != "1" or st not in {"0", "1"} or any(v is None for v in (close, amount, move)):
        return False, None
    if close <= 0 or amount <= 0 or move <= -100:
        return False, None
    if st == "1" or amount < MIN_AMOUNT or abs(move) >= MAX_DAILY_MOVE:
        return True, None

    changes, prior_amounts = [], []
    for day in days[-21:-1]:
        bar = bars.get(day, {})
        change, turnover = number(bar.get("pctChg")), number(bar.get("amount"))
        if bar.get("tradestatus") != "1" or change is None or change <= -100 or turnover is None or turnover <= 0:
            return True, None
        changes.append(1 + change / 100)
        prior_amounts.append(turnover)
    changes.append(1 + move / 100)
    excess5 = (math.prod(changes[-5:]) - 1) * 100 - benchmark5
    excess20 = (math.prod(changes[-20:]) - 1) * 100 - benchmark20
    if excess5 <= 0 or excess20 <= 0:
        return True, None
    return True, {"code": code, "name": stock["name"], "close": close, "move": move,
                  "excess5": excess5, "excess20": excess20, "amount_ratio": amount / median(prior_amounts)}


def rank_short_term(snapshot):
    days, stocks, closes = validate_snapshot(snapshot)
    benchmark5 = (closes[-1] / closes[-6] - 1) * 100
    benchmark20 = (closes[-1] / closes[-21] - 1) * 100
    eligible, covered = [], 0
    for stock in stocks:
        try:
            valid, row = features(stock, days, benchmark5, benchmark20)
        except ValueError:
            continue
        covered += valid
        if row:
            eligible.append(row)
    complete = covered >= MIN_COVERAGE * len(stocks)
    board = []
    if complete and eligible:
        for row in eligible:
            score = 0
            for factor, weight in WEIGHTS.items():
                below = sum(peer[factor] < row[factor] for peer in eligible)
                tied = sum(peer[factor] == row[factor] for peer in eligible)
                percentile = (below + (tied - 1) / 2) / (len(eligible) - 1) if len(eligible) > 1 else 0.5
                score += weight * percentile
            board.append({**row, "score": round(100 * score, 8)})
        board.sort(key=lambda row: (-row["score"], -row["excess5"], row["code"]))
    return {"complete": complete, "covered": covered, "eligible": len(eligible),
            "benchmark5": benchmark5, "benchmark20": benchmark20, "board": board[:10]}
