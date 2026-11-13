"""Collect or replay a weekly CSI 300 screen and prepare its review packet."""

import argparse
import hashlib
import json
import os
import socket
import sys
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_screen import (
    MIN_COVERAGE, MIN_FACTOR_COVERAGE, RULES_VERSION, STRATEGIES, direction_exclusion, screen, shortlist,
)
from stock_sources import CHINA, CNINFO_SEARCH, SOURCE_ERRORS, announcement_index, announcements, collect, save_json


ROOT = Path(__file__).resolve().parents[1]


def markdown(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", " ")


def metric(value):
    return f"{value:.2f}" if value is not None else "—"


def render_screen(snapshot, result, digest):
    day = snapshot["as_of"]
    rows = result["rows"]
    stock_count = len(rows)
    industry_count = sum(row["industry"] is not None for row in rows)
    state = "可进入复核" if result["complete"] else "数据不完整，仅供诊断"
    lines = [
        f"# 沪深 300 周度筛选 · {day}", "",
        f"- **状态**｜{state}",
        f"- **研究截止**｜{day} 18:00（北京时间）。财务仅使用此日期之前披露的版本。",
        f"- **来源**｜BaoStock。成分快照日期 {snapshot['constituent_date']}，采集时间 {snapshot['captured_at']}。",
        f"- **规则版本**｜{RULES_VERSION}。输入快照 SHA-256 `{digest}`。",
        f"- **覆盖**｜有效行情或已确认停牌 {result['quote_count']}/{stock_count}，行业 {industry_count}/{stock_count}，通过通用过滤 {result['eligible_count']}。",
        f"- **复核门槛**｜行情与行业覆盖各至少 {MIN_COVERAGE:.0%}，各方向因子覆盖至少 {MIN_FACTOR_COVERAGE:.0%}，三套榜单均有结果。",
        "", "## 市场环境", "",
        f"- 沪深 300 近 20 个交易日 {result['benchmark_return']:+.2f}%。收盘 {result['close']:.2f}，60 日均线 {result['ma60']:.2f}。",
    ]
    breadth = result["breadth_count"]
    if breadth:
        up = result["advancers"]
        lines.append(f"- 成分股近 20 日上涨 {up}/{breadth}（{up / breadth:.1%}），全池共 {stock_count} 只。")
    else:
        lines.append("- 成分股近 20 日涨幅数据缺失。")
    relative = result["industries"]
    for heading, groups in (("相对较强", relative[:3]), ("相对较弱", list(reversed(relative[-3:])))):
        if groups:
            values = "、".join(f"{markdown(name)} {excess:+.2f} 个百分点（{count} 只）" for name, excess, count in groups)
            lines.append(f"- {heading}｜{values}。行业为可计算成分股等权涨幅相对指数的差值。")
    if snapshot.get("industry_error"):
        lines.append(f"- 行业接口失败｜{markdown(snapshot['industry_error'])}")
    lines.extend([
        "", "## 三套原始榜单", "",
        "停牌、ST、当日成交额低于 1 亿元，以及缺行情或行业的股票先排除。每个方向在同一行业分类的同行内取因子百分位，因子等权，至少两只有效同行。总分并列时先看同行样本数，再按代码排序，行业并列共用排名。",
    ])
    ranked_codes = {}
    for label, factors in STRATEGIES.items():
        board = result["ranked"][label]
        ranked_codes[label] = {row["code"] for row in board}
        columns = " | ".join(title for _, title in factors)
        lines.extend(["", f"### {label}", "",
                      f"因子齐全 {result['factor_coverage'][label]}/{result['eligible_count']}，进入榜单 {len(board)}。", ""])
        if not board:
            continue
        lines.extend([
            f"| 排名 | 代码 | 名称 | 行业 | 行业排名/样本 | {columns} | 分数 |",
            "| " + " | ".join(["---"] * (6 + len(factors))) + " |",
        ])
        for index, row in enumerate(board, 1):
            values = " | ".join(metric(row[key]) for key, _ in factors)
            lines.append(f"| {index} | {row['code']} | {markdown(row['name'])} | {markdown(row['industry'])} | {row['industry_rank']}/{row['peer_count']} | {values} | {row['score']:.2f} |")
    lines.extend(["", "## 排除与缺失", "", "| 代码 | 原因 |", "| --- | --- |"])
    exclusions = []
    for row in rows:
        reasons = list(row["reasons"])
        if not reasons:
            for label, factors in STRATEGIES.items():
                if row["code"] in ranked_codes[label]:
                    continue
                missing = [title for key, title in factors if row.get(key) is None]
                reason = f"缺少 {'、'.join(missing)}" if missing else direction_exclusion(row, [key for key, _ in factors]) or "同行样本不足"
                reasons.append(f"{label}：{reason}")
        reasons.extend(f"{stage}取数失败：{error}" for stage, error in sorted(row["errors"].items()))
        if reasons:
            exclusions.append(f"| {row['code']} | {markdown('；'.join(reasons))} |")
    lines.extend(exclusions or ["| — | 无 |"])
    return "\n".join(lines) + "\n"


def collect_evidence(candidates, cutoff, digest, *, online):
    entries = {}
    if candidates and online:
        try:
            organizations = announcement_index()
        except SOURCE_ERRORS as exc:
            organizations, source_error = {}, str(exc)
        else:
            source_error = None
        for row, _ in candidates:
            code = row["code"]
            try:
                if source_error:
                    raise ValueError(source_error)
                found = announcements(code, organizations[code.split(".")[1]], cutoff)
                entries[code] = {"items": found, "error": None}
            except SOURCE_ERRORS as exc:
                entries[code] = {"items": [], "error": str(exc)}
    return {"schema": 1, "snapshot_sha256": digest, "source": "巨潮资讯", "entries": entries}


def render_review(day, candidates, evidence, complete):
    lines = [f"# 沪深 300 候选复核 · {day}", "", "- **本期研究推荐**｜暂无。以下为量化候选，待完成原文复核。",
             f"- **依据**｜[本期筛选](screen.md)。输入快照 SHA-256 `{evidence['snapshot_sha256']}`。"]
    if not complete:
        lines.append("- **状态**｜本期数据覆盖未达到发布门槛，暂缓生成复核名单。")
    for row, hits in candidates:
        code = row["code"]
        lines.extend(["", f"## {code} {markdown(row['name'])}", "",
                      f"- **参考价**｜{row['close']:.2f} 元，{day} 收盘。PE TTM {metric(row['pe'])}，PB {metric(row['pb'])}。"])
        for label, rank, entry in hits:
            values = "、".join(f"{title} {metric(entry[key])}" for key, title in STRATEGIES[label])
            lines.append(f"- **入围依据**｜{label}第 {rank}，行业第 {entry['industry_rank']}/{entry['peer_count']}，{values}。")
        for period_key, published_key in (("profit_period", "profit_published"), ("growth_period", "growth_published")):
            if row.get(period_key):
                line = f"- **财务披露**｜报告期 {row[period_key]}，BaoStock 记录披露日 {row[published_key]}。"
                if line not in lines:
                    lines.append(line)
        record = evidence["entries"].get(code)
        if record and record.get("items"):
            lines.extend(["- **巨潮原文链接**｜已抓取标题和发布时间，待阅读原文核实。", ""])
            reports = [item for item in record["items"] if item.get("periodic_report")][:4]
            other = [item for item in record["items"] if not item.get("periodic_report")][:8]
            for item in reports + other:
                title = markdown(item["title"]).replace("[", "&#91;").replace("]", "&#93;")
                lines.append(f"  - {item['published_at'][:10]} [{title}]({item['url']})")
            lines.append("")
        else:
            detail = "本次未请求公告接口"
            if record:
                detail = record.get("error") or "检索窗口无匹配"
            lines.append(f"- **公告证据待补**｜{markdown(detail)}。 [巨潮资讯]({CNINFO_SEARCH})")
        lines.extend(["- **待复核**｜核心判断、估值区间及下行情景、催化剂、失效条件、下次复查日。",
                      "- **复盘**｜待填写可成交起止价格、含分红扣费收益、同期基准、最大回撤和判断结果。"])
    return "\n".join(lines) + "\n"


def arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--as-of", type=date.fromisoformat, help="指定研究交易日 YYYY-MM-DD")
    source.add_argument("--snapshot", type=Path, help="离线重跑已保存的 snapshot.json")
    parser.add_argument("--refresh", action="store_true", help="重新取数，跳过同日取数缓存")
    parser.add_argument("--evidence", type=Path, help="离线重跑时同时复用 evidence.json")
    parser.add_argument("--no-announcements", action="store_true", help="在线采集时跳过公告请求")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "records" / "stocks", help="每期目录的父目录")
    args = parser.parse_args(argv)
    if args.snapshot and args.refresh:
        parser.error("--refresh 仅用于在线采集")
    if args.evidence and not args.snapshot:
        parser.error("--evidence 需要同时使用 --snapshot")
    return args


def run(args):
    now = datetime.now(CHINA)
    if args.as_of and args.as_of > now.date():
        raise ValueError("研究日期不能晚于今天")
    if args.as_of and (args.output_dir / args.as_of.isoformat()).exists():
        raise FileExistsError("已有该研究日期的记录。请用 --output-dir 指定另一目录重跑。")
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        try:
            import baostock as bao
        except ImportError as exc:
            raise RuntimeError("请先运行 python3 -m pip install -r requirements-stocks.txt") from exc
        # BaoStock 的 TCP 客户端使用默认 socket 设置。
        socket.setdefaulttimeout(15)
        login = bao.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock 登录失败 [{login.error_code}] {login.error_msg}")
        try:
            snapshot = collect(bao, args.as_of, ROOT / "data" / "stocks" / "cache", refresh=args.refresh, now=now,
                               progress=lambda message: print(message, flush=True))
        finally:
            bao.logout()

    result = screen(snapshot)
    day = snapshot["as_of"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / day
    if destination.exists():
        raise FileExistsError(f"已有研究记录 {destination}。请用 --output-dir 指定另一目录重跑。")
    candidates = shortlist(result["ranked"]) if result["complete"] else []
    cutoff = datetime.combine(date.fromisoformat(day), time(18), CHINA)
    with TemporaryDirectory(prefix=".stock-research-", dir=args.output_dir) as temporary:
        staging = Path(temporary) / day
        save_json(staging / "snapshot.json", snapshot)
        digest = hashlib.sha256((staging / "snapshot.json").read_bytes()).hexdigest()
        if args.evidence:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            if evidence.get("schema") != 1 or evidence.get("snapshot_sha256") != digest:
                raise ValueError("证据文件与快照不匹配")
        else:
            evidence = collect_evidence(candidates, cutoff, digest, online=not (args.snapshot or args.no_announcements))
        save_json(staging / "evidence.json", evidence)
        (staging / "screen.md").write_text(render_screen(snapshot, result, digest), encoding="utf-8")
        (staging / "review.md").write_text(render_review(day, candidates, evidence, result["complete"]), encoding="utf-8")
        # 同一目录内原子发布。并发执行时，已有非空记录目录会让 rename 失败。
        os.rename(staging, destination)
    print(destination.resolve())
    if not result["complete"]:
        print("数据覆盖不足，已保存诊断记录。", file=sys.stderr)
    return 0 if result["complete"] else 2


def main():
    try:
        return run(arguments(None))
    except SOURCE_ERRORS as exc:
        print(f"A 股研究运行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
