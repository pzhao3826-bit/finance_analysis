"""Deterministic weekly rankings from a saved research snapshot."""

import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from stock_sources import BENCHMARK, CODE


RULES_VERSION = "1.0"
MIN_AMOUNT = 100_000_000
MIN_COVERAGE = 0.9
MIN_FACTOR_COVERAGE = 0.7
STRATEGIES = {
    "价值＋质量": (("earnings_yield", "盈利收益率 %"), ("roe", "ROE %")),
    "经营改善": (("profit_yoy", "归母净利润同比 %"), ("yoy_change", "同比变化百分点")),
    "红利": (("dividend_yield", "近 365 天股息率 %"),),
}


def number(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def datestr(value):
    return date.fromisoformat(value).isoformat()


def dated_bars(rows, code, days):
    result = {}
    for row in rows:
        day = row.get("date")
        if row.get("code") != code or day not in days or day in result:
            raise ValueError(f"{code} 日线含重复日期、越界日期或错误代码")
        result[day] = row
    return result


def latest_reports(rows, code, cutoff, field):
    by_period = defaultdict(dict)
    for row in rows:
        if row.get("code") != code or not row.get("statDate") or not row.get("pubDate"):
            continue
        period, published = datestr(row["statDate"]), datestr(row["pubDate"])
        period_date = date.fromisoformat(period)
        quarter_end = (period_date.month, period_date.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}
        if quarter_end and period <= published < cutoff:
            by_period[period].setdefault(published, set()).add(number(row.get(field)))
    reports = []
    for period, versions in sorted(by_period.items(), reverse=True):
        published = max(versions)
        values = versions[published]
        value = next(iter(values)) if len(values) == 1 else None
        reports.append((period, published, value))
    return reports


def dividend_yield(rows, code, day, close):
    cutoff = date.fromisoformat(day)
    start = (cutoff - timedelta(days=365)).isoformat()
    cash_by_date = {}
    for row in rows:
        ex_day = row.get("dividOperateDate")
        if row.get("code") != code:
            return None
        if not ex_day or not start < datestr(ex_day) <= day:
            continue
        cash = number(row.get("dividCashPsBeforeTax"))
        if cash is None or cash < 0 or cash_by_date.get(ex_day, cash) != cash:
            return None
        cash_by_date[ex_day] = cash
    return 100 * sum(cash_by_date.values()) / close


def stock_metrics(stock, days):
    code = stock["code"]
    row = {"code": code, "name": stock["name"], "industry": None, "industry_date": None,
           "industry_classification": None, "reasons": [], "errors": stock["errors"],
           "return20": None, "quote_ok": False}
    day = days[-1]
    industry = stock.get("industry")
    if industry and industry.get("code") == code and industry.get("updateDate") and datestr(industry["updateDate"]) <= day:
        row["industry"], row["industry_date"] = industry["industry"], industry["updateDate"]
        row["industry_classification"] = industry["industryClassification"]
    try:
        bars = dated_bars(stock["bars"], code, set(days[-21:]))
    except ValueError as exc:
        row["reasons"].append(str(exc))
        return row
    quote = bars.get(day, {})
    close, amount = number(quote.get("close")), number(quote.get("amount"))
    row.update(close=close, amount=amount)
    status, st = quote.get("tradestatus"), quote.get("isST")
    if status == "0":
        row["quote_ok"] = True
        row["reasons"].append("当日停牌")
    elif status != "1" or st not in {"0", "1"} or close is None or close <= 0 or amount is None or amount <= 0:
        row["reasons"].append("缺少当日有效行情、交易状态或 ST 标记")
    else:
        row["quote_ok"] = True
        if st == "1":
            row["reasons"].append("ST")
        if amount < MIN_AMOUNT:
            row["reasons"].append("当日成交额低于 1 亿元")
    changes = []
    for trading_day in days[-20:]:
        bar = bars.get(trading_day, {})
        status = bar.get("tradestatus")
        change = 0 if status == "0" else number(bar.get("pctChg"))
        if status not in {"0", "1"} or change is None or change <= -100:
            break
        changes.append(1 + change / 100)
    if len(changes) == 20 and number(bars.get(days[-21], {}).get("close")):
        row["return20"] = (math.prod(changes) - 1) * 100
    if not row["industry"]:
        row["reasons"].append("缺少截至当日的行业归属")
    if row["reasons"]:
        return row

    pe, pb = number(quote.get("peTTM")), number(quote.get("pbMRQ"))
    row.update(pe=pe, pb=pb, earnings_yield=100 / pe if pe and pe > 0 else None)
    profit = latest_reports(stock["profit"], code, day, "roeAvg") if "盈利" not in stock["errors"] else []
    growth = latest_reports(stock["growth"], code, day, "YOYPNI") if "成长" not in stock["errors"] else []
    if profit:
        period, published, roe = profit[0]
        if (date.fromisoformat(day) - date.fromisoformat(period)).days <= 210:
            row.update(roe=roe * 100 if roe is not None else None, profit_period=period, profit_published=published)
    if growth:
        period, published, yoy = growth[0]
        current_period = date.fromisoformat(period)
        if (date.fromisoformat(day) - current_period).days <= 210:
            row.update(profit_yoy=yoy * 100 if yoy is not None else None, growth_period=period, growth_published=published)
        if len(growth) > 1 and yoy is not None and growth[1][2] is not None and row.get("growth_period"):
            previous_period, previous_published, previous_yoy = growth[1]
            quarter_start = current_period.replace(month=current_period.month - 2, day=1)
            previous_quarter = (quarter_start - timedelta(days=1)).isoformat()
            if previous_period == previous_quarter:
                row.update(yoy_change=(yoy - previous_yoy) * 100, previous_growth_published=previous_published)
    if not any(stage.startswith("分红 ") for stage in stock["errors"]):
        row["dividend_yield"] = dividend_yield(stock["dividends"], code, day, close)
    return row


def rank_strategy(rows, fields):
    by_industry = defaultdict(list)
    for row in rows:
        if all(row.get(field) is not None for field in fields) and not direction_exclusion(row, fields):
            by_industry[row["industry_classification"], row["industry"]].append(row)
    scored = []
    for peers in by_industry.values():
        if len(peers) < 2:
            continue
        for row in peers:
            percentiles = []
            for field in fields:
                below = sum(peer[field] < row[field] for peer in peers)
                tied = sum(peer[field] == row[field] for peer in peers)
                percentiles.append((below + (tied - 1) / 2) / (len(peers) - 1))
            scored.append({**row, "score": round(100 * mean(percentiles), 8), "peer_count": len(peers)})
    ranked = sorted(scored, key=lambda row: (-row["score"], -row["peer_count"], row["code"]))
    industry_scores = defaultdict(list)
    for row in ranked:
        industry_scores[row["industry_classification"], row["industry"]].append(row["score"])
    for row in ranked:
        peers = industry_scores[row["industry_classification"], row["industry"]]
        row["industry_rank"] = 1 + sum(score > row["score"] for score in peers)
    return ranked


def direction_exclusion(row, fields):
    if fields == ["dividend_yield"] and row.get("dividend_yield") == 0:
        return "窗口内无现金分红"
    if "yoy_change" in fields and row.get("yoy_change") is not None and row["yoy_change"] <= 0:
        return "归母净利润同比未较上一报告期改善"
    return None


def validate_snapshot(snapshot):
    if snapshot.get("schema") != 1 or snapshot.get("source") != "BaoStock":
        raise ValueError("不支持的快照版本或数据源")
    days = snapshot["trading_days"]
    day = datestr(snapshot["as_of"])
    if len(days) != 60 or days != sorted(set(map(datestr, days))) or days[-1] != day:
        raise ValueError("快照交易日历与研究日期不符")
    age = (date.fromisoformat(day) - date.fromisoformat(snapshot["constituent_date"])).days
    if not 0 <= age <= 14:
        raise ValueError("成分快照日期超出 14 天有效范围或晚于研究日期")
    stocks = snapshot["stocks"]
    codes = {row["code"] for row in stocks}
    if len(stocks) != 300 or len(codes) != 300 or any(not CODE.fullmatch(code) for code in codes):
        raise ValueError("快照必须包含 300 只不同的沪深 300 成分股")
    benchmark = dated_bars(snapshot["benchmark"], BENCHMARK, set(days))
    closes = [number(benchmark.get(d, {}).get("close")) for d in days]
    if any(close is None or close <= 0 for close in closes):
        raise ValueError("基准指数缺少 60 日有效收盘价")
    return days, stocks, closes


def screen(snapshot):
    days, stocks, closes = validate_snapshot(snapshot)
    rows = [stock_metrics(stock, days) for stock in sorted(stocks, key=lambda stock: stock["code"])]
    quote_count = sum(row["quote_ok"] for row in rows)
    ranked = {}
    coverage = {}
    eligible = [row for row in rows if not row["reasons"]]
    for label, factors in STRATEGIES.items():
        fields = [field for field, _ in factors]
        coverage[label] = sum(all(row.get(field) is not None for field in fields) for row in eligible)
        ranked[label] = rank_strategy(eligible, fields)
    returns = [row["return20"] for row in rows if row["return20"] is not None]
    benchmark_return = (closes[-1] / closes[-21] - 1) * 100
    industries = defaultdict(list)
    for row in rows:
        if row["industry"] and row["return20"] is not None:
            industries[row["industry_classification"], row["industry"]].append(row["return20"])
    relative = [(f"{name}（{classification}）", mean(values) - benchmark_return, len(values))
                for (classification, name), values in industries.items() if len(values) >= 2]
    complete = (
        quote_count >= len(rows) * MIN_COVERAGE
        and sum(row["industry"] is not None for row in rows) >= len(rows) * MIN_COVERAGE
        and bool(eligible) and all(ranked.values())
        and all(count >= len(eligible) * MIN_FACTOR_COVERAGE for count in coverage.values())
    )
    return {"rows": rows, "ranked": ranked, "quote_count": quote_count, "complete": complete,
            "eligible_count": len(eligible), "factor_coverage": coverage,
            "benchmark_return": benchmark_return, "close": closes[-1], "ma60": mean(closes),
            "advancers": sum(value > 0 for value in returns), "breadth_count": len(returns),
            "industries": sorted(relative, key=lambda item: (-item[1], item[0]))}


def shortlist(ranked):
    # 先给每个方向一个名额。同一只股票命中多个方向时保留全部入围依据。
    selected = {}
    for board in ranked.values():
        best = next((row for row in board if row["code"] not in selected), None)
        if best:
            selected[best["code"]] = best
    return [(row, [(label, index, entry) for label, board in ranked.items()
                   for index, entry in enumerate(board, 1) if entry["code"] == code])
            for code, row in selected.items()]
