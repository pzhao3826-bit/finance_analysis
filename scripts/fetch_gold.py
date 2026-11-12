"""Save daily gold and macro observations in a timestamped snapshot."""

import argparse
import csv
import io
import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


FRED_SERIES = {
    "DTWEXBGS": "美联储广义美元指数（指数点）",
    "DFII10": "美国10年期实际利率（%）",
    "DEXCHUS": "美元兑人民币（人民币/美元）",
}
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "gold"


def download(url):
    headers = ["--user-agent", "Mozilla/5.0"] if "finance.yahoo.com" in url else []
    response = subprocess.run(
        ["curl", "--http1.1", "--fail", "--silent", "--show-error", "--location",
         "--max-time", "15", *headers, url],
        capture_output=True,
    )
    if response.returncode:
        raise OSError(response.stderr.decode("utf-8", errors="replace").strip())
    return response.stdout.decode("utf-8-sig")


def fred_observations(body, code, start_date):
    rows = csv.DictReader(io.StringIO(body))
    observations = []
    for row in rows:
        value = row[code]
        date = row[rows.fieldnames[0]]
        if value in ("", ".") or date < start_date:
            continue
        observations.append({"date": date, "value": float(value)})
    return observations


def gold_observations(body):
    chart = json.loads(body)["chart"]
    if chart.get("error"):
        raise ValueError(str(chart["error"]))
    result = chart["result"][0]
    market_timezone = ZoneInfo(result["meta"]["exchangeTimezoneName"])
    closes = result["indicators"]["quote"][0]["close"]
    return [
        {"date": datetime.fromtimestamp(stamp, market_timezone).date().isoformat(),
         "value": close}
        for stamp, close in zip(result["timestamp"], closes)
        if close is not None
    ]


def collect(name, source, url, parse):
    series = {"name": name, "source": source, "request_url": url}
    try:
        observations = parse(download(url))
        if not observations or any(not math.isfinite(row["value"]) for row in observations):
            raise ValueError("empty or invalid observations")
        series["observations"] = observations
        series["latest"] = max(observations, key=lambda row: row["date"])
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        series["error"] = f"{type(error).__name__}: {error}"
    return series


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="calendar days to request")
    args = parser.parse_args()
    if not 7 <= args.days <= 365:
        parser.error("--days must be between 7 and 365")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    series = {}
    for code, name in FRED_SERIES.items():
        query = urlencode({"id": code, "cosd": start.date().replace(day=1).isoformat()})
        series[code] = collect(
            name, f"https://fred.stlouisfed.org/series/{code}",
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?{query}",
            lambda body: fred_observations(body, code, start.date().isoformat()),
        )
    query = urlencode({"period1": int(start.timestamp()),
                       "period2": int(now.timestamp()), "interval": "1d"})
    series["GC=F"] = collect(
        "黄金期货（美元/盎司）", "https://finance.yahoo.com/quote/GC=F/",
        f"https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF?{query}",
        gold_observations,
    )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    snapshot = {"requested_at_utc": now.isoformat(), "lookback_days": args.days,
                "series": series}
    path = OUTPUT / f"{now.strftime('%Y-%m-%dT%H%M%S.%fZ')}.json"
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=OUTPUT, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            json.dump(snapshot, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(path)
    for code, result in series.items():
        if "error" in result:
            print(f"{code}: {result['error']}", file=sys.stderr)
        else:
            latest = result["latest"]
            print(f"{code}: {latest['date']} {latest['value']}")
    return int(any("error" in result for result in series.values()))


if __name__ == "__main__":
    sys.exit(main())
