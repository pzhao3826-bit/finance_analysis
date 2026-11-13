"""BaoStock research inputs and CNINFO disclosure links."""

import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


CHINA = ZoneInfo("Asia/Shanghai")
BENCHMARK = "sh.000300"
STOCK_FIELDS = "date,code,close,amount,tradestatus,pctChg,peTTM,pbMRQ,isST"
CODE = re.compile(r"(?:sh|sz)\.\d{6}")
CNINFO = "https://www.cninfo.com.cn"
CNINFO_SEARCH = CNINFO + "/new/disclosure"
SOURCE_ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError)


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def fetch_rows(query, *args, **kwargs):
    result = query(*args, **kwargs)
    rows = []
    while result.error_code == "0" and result.next():
        values = result.get_row_data()
        if len(values) != len(result.fields):
            raise ValueError(f"{query.__name__} 返回了不完整的数据行")
        rows.append(dict(zip(result.fields, values)))
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock {query.__name__} [{result.error_code}] {result.error_msg}")
    return rows


def recent_quarters(day):
    year, quarter = day.year, (day.month - 1) // 3 + 1
    for _ in range(6):
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
        yield year, quarter


def financial_rows(fetch, query, code, day, count):
    rows = []
    for year, quarter in recent_quarters(day):
        rows.extend(fetch(query, code, year, quarter))
        periods = {
            row["statDate"] for row in rows
            if row.get("statDate") and row.get("pubDate")
            and row["statDate"] <= row["pubDate"] < day.isoformat()
        }
        if len(periods) >= count:
            break
    return rows


def collect(bao, requested_day, cache_dir, *, refresh=False, now=None, progress=None):
    now = now or datetime.now(CHINA)
    end = requested_day or now.date()
    start = end - timedelta(days=130)
    calendar = fetch_rows(bao.query_trade_dates, start.isoformat(), end.isoformat())
    if not calendar or not any(row.get("calendar_date") == end.isoformat() for row in calendar):
        raise ValueError(f"BaoStock 交易日历未覆盖 {end}")
    trading_days = sorted(row["calendar_date"] for row in calendar if row["is_trading_day"] == "1")
    if len(trading_days) < 60:
        raise ValueError("交易日历不足 60 个交易日")
    day = trading_days[-1]
    if requested_day and day != requested_day.isoformat():
        raise ValueError(f"{requested_day} 非交易日，最近交易日为 {day}")
    if day == now.date().isoformat() and now.hour < 18:
        raise ValueError("北京时间 18:00 后再采集当日日线，或用 --as-of 指定已收盘交易日")

    benchmark = fetch_rows(
        bao.query_history_k_data_plus, BENCHMARK, "date,code,close",
        trading_days[-60], day, frequency="d", adjustflag="3",
    )
    if {row["date"] for row in benchmark if row["code"] == BENCHMARK} != set(trading_days[-60:]):
        raise ValueError(f"沪深 300 指数日线未覆盖截至 {day} 的 60 个交易日")

    members = fetch_rows(bao.query_hs300_stocks, date=day)
    codes = {row["code"] for row in members}
    if len(members) != 300 or len(codes) != 300 or any(not CODE.fullmatch(code) for code in codes):
        raise ValueError(f"沪深 300 成分未通过校验，返回 {len(codes)} 个不同代码")
    dates = {row.get("updateDate", "") for row in members}
    cutoff = date.fromisoformat(day)
    if len(dates) != 1 or not 0 <= (cutoff - date.fromisoformat(next(iter(dates)))).days <= 14:
        raise ValueError(f"沪深 300 成分更新日期不可用于 {day}：{', '.join(sorted(dates))}")

    try:
        industries = fetch_rows(bao.query_stock_industry, date=day)
        industry_error = None
    except SOURCE_ERRORS as exc:
        industries, industry_error = [], str(exc)
    industry_by_code = {
        row["code"]: row for row in industries
        if row.get("code") in codes and row.get("industry") and row.get("industryClassification")
        and row.get("updateDate") and row["updateDate"] <= day
    }
    stocks = []
    for index, member in enumerate(sorted(members, key=lambda row: row["code"]), 1):
        code = member["code"]
        path = cache_dir / day / f"{code}.json"
        cache = {}
        if path.exists() and not refresh:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    cache = saved
            except (OSError, ValueError):
                pass
        errors = {}

        def fetch(query, *args, **kwargs):
            key = json.dumps([query.__name__, args, kwargs], sort_keys=True)
            if key not in cache:
                rows = fetch_rows(query, *args, **kwargs)
                usable = bool(rows)
                if query.__name__ == "query_history_k_data_plus":
                    usable = any(row.get("date") == day for row in rows)
                if usable:
                    cache[key] = rows
                    save_json(path, cache)
                return rows
            return cache[key]

        def stage(name, load):
            try:
                return load()
            except SOURCE_ERRORS as exc:
                errors[name] = str(exc)
                return []

        bars = stage("行情", lambda: fetch(
            bao.query_history_k_data_plus, code, STOCK_FIELDS, trading_days[-21], day,
            frequency="d", adjustflag="3",
        ))
        active = any(row.get("date") == day and row.get("tradestatus") == "1" and row.get("isST") == "0" for row in bars)
        dividends = []
        if active:
            for year in sorted({cutoff.year, (cutoff - timedelta(days=365)).year}):
                dividends.extend(stage(f"分红 {year}", lambda: fetch(
                    bao.query_dividend_data, code, str(year), yearType="operate",
                )))
        stocks.append({
            "code": code, "name": member["code_name"], "industry": industry_by_code.get(code),
            "bars": bars, "profit": stage("盈利", lambda: financial_rows(fetch, bao.query_profit_data, code, cutoff, 1)) if active else [],
            "growth": stage("成长", lambda: financial_rows(fetch, bao.query_growth_data, code, cutoff, 2)) if active else [],
            "dividends": dividends, "errors": errors,
        })
        if progress and (index % 25 == 0 or index == len(members)):
            progress(f"已检查 {index}/{len(members)} 只成分股")

    return {
        "schema": 1, "as_of": day, "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "BaoStock", "constituent_date": next(iter(dates)),
        "trading_days": trading_days[-60:], "benchmark": benchmark, "stocks": stocks,
        "industry_error": industry_error,
    }


def cninfo_json(path, form=None):
    command = ["curl", "--fail", "--silent", "--show-error", "--max-time", "12",
               "--user-agent", "Mozilla/5.0", "--referer", CNINFO_SEARCH,
               "--header", f"Origin: {CNINFO}"]
    if form:
        command.extend(["--data", urlencode(form)])
    for attempt in range(2):
        try:
            response = subprocess.run([*command, CNINFO + path], capture_output=True, timeout=15)
            if response.returncode:
                raise OSError(response.stderr.decode("utf-8", errors="replace").strip())
            return json.loads(response.stdout)
        except subprocess.TimeoutExpired as exc:
            if attempt:
                raise OSError("巨潮请求超时") from exc
        except (OSError, ValueError):
            if attempt:
                raise
        time.sleep(1)


def announcement_index():
    payload = cninfo_json("/new/data/szse_stock.json")
    rows = payload if isinstance(payload, list) else payload["stockList"]
    if not rows:
        raise ValueError("巨潮证券索引为空")
    return {str(row["code"]): row["orgId"] for row in rows if row.get("orgId")}


def announcements(code, organization, cutoff):
    symbol = code.split(".")[1]
    results = {}
    # 先单独取定期报告，避免近期的例行公告把财报挤出证据包。
    report_category = "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh;"
    report_ids = set()
    categories = (report_category, "")
    for category in categories:
        start = cutoff.date() - timedelta(days=365 if category else 90)
        payload = cninfo_json("/new/hisAnnouncement/query", {
            "stock": f"{symbol},{organization}", "tabName": "fulltext", "pageSize": 30,
            "pageNum": 1, "column": "", "category": category,
            "seDate": f"{start}~{cutoff.date()}", "isHLtitle": "false",
            "plate": "", "searchkey": "", "secid": "", "sortName": "", "sortType": "",
        })
        for row in payload["announcements"] or []:
            identity = str(row.get("announcementId", ""))
            if str(row.get("secCode")) != symbol or not identity.isdigit():
                continue
            published = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, CHINA)
            # 巨潮常用零点表示仅有日期。研究当天的日期不能证明收盘前已发布。
            date_only = published.time() == datetime.min.time()
            if (not start <= published.date() <= cutoff.date() or published > cutoff
                    or (date_only and published.date() == cutoff.date())):
                continue
            title = unescape(re.sub(r"<[^>]*>", "", row["announcementTitle"]))
            if category:
                report_ids.add(identity)
            results[identity] = {"title": title, "published_at": published.date().isoformat() if date_only else published.isoformat(),
                                 "periodic_report": identity in report_ids,
                                 "url": f"{CNINFO}/new/disclosure/detail?annoId={identity}"}
    return sorted(results.values(), key=lambda row: (row["published_at"], row["url"]), reverse=True)
