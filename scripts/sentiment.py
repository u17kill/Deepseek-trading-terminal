# -*- coding: utf-8 -*-
"""「A股雷达」频道数据管道：A 股情绪周期复盘取数 + 计算。

数据源统一为腾讯自选股 CLI（westock），口径：
  market-overview  指数 / 成交额 / 宽度 / 技术 / 估值 / 风格   —— 支持 --date 历史
  screen ranking   连续涨停天数榜                              —— 支持 --date
  quote            个股与指数指定日行情（换手/成交额/20日涨幅）—— 支持 --date
  lhb              全市场与机构专用席位榜单                    —— 支持 --date
  sector ranking   板块实时榜                                  —— **实时榜忽略 --date**
  trade-calendar   交易日历（含月末/周末标记）

因此本频道的口径分两层，前端须分别标注：
  · 复盘层（温度 / 宽度 / 梯队 / 龙虎榜）= 最近一个已收盘交易日 T
  · 实时层（板块强弱）= 抓取时刻的最新快照，与 T 可能不同日

产物：data/senti/sentiment.json（供构建期注入首屏）
"""
import datetime
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTI = os.path.join(BASE, "data", "senti")
OUT_JSON = os.path.join(SENTI, "sentiment.json")
PULSE_CACHE = os.path.join(SENTI, "pulse_cache.json")

# 温度分档（与页面文案、历史复盘一致）
BANDS = [(20.0, "冰点", "#1f6feb"), (40.0, "低温", "#6aa9f0"),
         (60.0, "中性", "#b4b2a9"), (80.0, "升温", "#e08b3a"),
         (101.0, "亢奋", "#d64545")]

# 复盘标的指数（顺序即展示顺序）
INDEX_LIST = [("sh000001", "上证指数"), ("sh000016", "上证 50"),
              ("sz399001", "深证成指"), ("sh000852", "中证 1000"),
              ("sz399006", "创业板指"), ("sh000688", "科创 50")]


# ---------------------------------------------------------------- 取数基础
def westock_bin():
    """Windows 下必须用绝对路径，subprocess 不会自动补 .exe。"""
    for cand in (os.path.expanduser("~/.local/bin/westock.exe"),
                 os.path.expanduser("~/.local/bin/westock")):
        if os.path.exists(cand):
            return cand
    return "westock"


def _env():
    env = os.environ.copy()
    extra = os.path.expanduser("~/.local/bin")
    if extra not in env.get("PATH", ""):
        env["PATH"] = env.get("PATH", "") + os.pathsep + extra
    return env


def _run(args, timeout=150):
    cmd = [westock_bin()] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=_env(), shell=False)
    if r.returncode != 0:
        raise RuntimeError("westock 取数失败(%s)：%s" % (" ".join(args[:3]),
                                                        (r.stderr or r.stdout)[:200]))
    return r.stdout


# ---------------------------------------------------------------- markdown 解析
def _is_sep(row):
    return bool(row) and all(c.strip("-: ") == "" for c in row)


def parse_tables(text):
    """把 westock 的 markdown 输出切成 [{title, header, rows}]。"""
    blocks, title, lines, i = [], "", text.splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("### "):
            title = ln[4:].strip()
        if ln.strip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            body = rows[2:] if len(rows) > 2 and _is_sep(rows[1]) else rows[1:]
            if rows:
                blocks.append({"title": title, "header": rows[0], "rows": body})
            continue
        i += 1
    return blocks


def num(x):
    try:
        s = str(x).replace(",", "").replace("%", "").strip()
        if s in ("", "-", "--", "nan", "None"):
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _under(blocks, key):
    return [b for b in blocks if b["title"] and key in b["title"]]


def _kv(blocks, key):
    """合并某标题下所有两列表格成 dict。"""
    out = {}
    for b in _under(blocks, key):
        if len(b["header"]) == 2:
            for r in b["rows"]:
                if len(r) >= 2:
                    out[r[0]] = r[1]
    return out


def _grid(blocks, key):
    """取某标题下第一个多列表，返回 (列名, {行名: {列名: 值}})。"""
    for b in _under(blocks, key):
        if len(b["header"]) > 2:
            cols = b["header"][1:]
            rows = {}
            for r in b["rows"]:
                if len(r) >= len(cols) + 1:
                    rows[r[0]] = dict(zip(cols, r[1:]))
            return cols, rows
    return [], {}


def _records(block):
    h = block["header"]
    return [{h[i]: (r[i] if i < len(r) else "") for i in range(len(h))} for r in block["rows"]]


# ---------------------------------------------------------------- 交易日
def calendar(start, end):
    txt = _run(["trade-calendar", "--market", "hs", "--start", start, "--end", end])
    days = []
    for b in parse_tables(txt):
        if "date" in [c.lower() for c in b["header"]]:
            for r in b["rows"]:
                if len(r) >= 6:
                    days.append({"date": r[0], "trading": r[5] == "是",
                                 "month_end": r[1] == "是", "week_end": r[3] == "是"})
    return days


def resolve_target(now=None):
    """最近一个已收盘交易日。

    盘中（交易日 15:05 前）沿用上一交易日：连板梯队与龙虎榜当日尚未定稿，
    用未完成的数据做复盘会给出错误的温度。
    """
    now = now or datetime.datetime.now()
    start = (now.date() - datetime.timedelta(days=20)).isoformat()
    end = now.date().isoformat()
    try:
        days = calendar(start, end)
    except Exception:  # noqa: BLE001  日历接口异常时退化为「工作日近似」
        d = now.date()
        if now.hour < 15 or (now.hour == 15 and now.minute < 5):
            d -= datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d -= datetime.timedelta(days=1)
        return d.isoformat(), False, False

    closed = now.hour > 15 or (now.hour == 15 and now.minute >= 5)
    cands = [d for d in days if d["trading"] and (d["date"] < end or closed)]
    if not cands:
        raise RuntimeError("交易日历未取得可用日期")
    t = cands[-1]
    return t["date"], t["month_end"], t["week_end"]


def prev_trading_days(date, n=6):
    d0 = datetime.date.fromisoformat(date)
    start = (d0 - datetime.timedelta(days=n * 3 + 15)).isoformat()
    days = [x["date"] for x in calendar(start, date) if x["trading"]]
    return days[-n:]


# ---------------------------------------------------------------- 各数据块
def market_overview(date):
    blocks = parse_tables(_run(["market-overview", "--type", "all", "--date", date]))
    return blocks


def updown_of(date):
    kv = _kv(parse_tables(_run(["market-overview", "--type", "updown", "--date", date])), "updown")
    return {"up": int(num(kv.get("A股上涨股票数")) or 0),
            "lu": int(num(kv.get("A股涨停股票数")) or 0),
            "ld": int(num(kv.get("A股跌停股票数")) or 0),
            "up_pct": num(kv.get("A股上涨股票比例(%)"))}


def temperature(up_pct, lu, ld):
    """情绪温度：50% 上涨占比 + 30% 涨停强度 + 20% 跌停惩罚。"""
    return round(0.5 * up_pct + 0.3 * min(lu, 100) + 0.2 * max(0, 100 - ld * 10), 1)


def band_of(t):
    for hi, name, color in BANDS:
        if t < hi:
            return name, color
    return BANDS[-1][1], BANDS[-1][2]


def pulse_series(dates):
    """六日情绪脉搏。历史交易日不会变，缓存到 pulse_cache.json 减少取数。"""
    cache = {}
    if os.path.exists(PULSE_CACHE):
        try:
            cache = json.loads(open(PULSE_CACHE, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            cache = {}
    out = []
    for d in dates:
        if d not in cache:
            try:
                cache[d] = updown_of(d)
            except Exception:  # noqa: BLE001
                continue
        rec = dict(cache[d])
        rec["d"] = d[5:]
        rec["temp"] = temperature(rec["up_pct"], rec["lu"], rec["ld"])
        out.append(rec)
    try:
        os.makedirs(SENTI, exist_ok=True)
        json.dump(cache, open(PULSE_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return out


def ladder_of(date):
    txt = _run(["screen", "ranking", "--type", "limitup_days", "--date", date, "--limit", "120"])
    blocks = parse_tables(txt)
    block = next((b for b in blocks if "LimitUpDays" in b["header"]), None)
    if not block:
        return {"count": 0, "dist": [], "samples": [], "max": 0, "ge2": 0}
    rows = []
    for r in _records(block):
        days = int(num(r.get("LimitUpDays")) or 0)
        if days <= 0:
            continue
        rows.append({"code": r.get("code", ""), "name": r.get("name", ""), "days": days})
    rows.sort(key=lambda x: (-x["days"], x["code"]))
    dist = {}
    for r in rows:
        dist[r["days"]] = dist.get(r["days"], 0) + 1
    return {"count": len(rows), "dist": dist, "max": rows[0]["days"] if rows else 0,
            "ge2": sum(1 for r in rows if r["days"] >= 2), "rows": rows}


def quotes_of(codes, date):
    if not codes:
        return {}
    txt = _run(["quote", ",".join(codes), "--date", date])
    blocks = parse_tables(txt)
    block = next((b for b in blocks if "change_percent" in b["header"]), None)
    if not block:
        return {}
    return {r["code"]: r for r in _records(block)}


def lhb_of(date):
    res = {"all_count": 0, "rows": [], "inst": []}
    try:
        blocks = parse_tables(_run(["lhb", "--type", "all", "--date", date]))
        block = next((b for b in blocks if "netBuyAmount" in b["header"]), None)
        if block:
            res["rows"] = _records(block)
            res["all_count"] = len(res["rows"])
    except Exception:  # noqa: BLE001
        pass
    try:
        blocks = parse_tables(_run(["lhb", "--type", "institution", "--date", date]))
        block = next((b for b in blocks if "instBuyAmt" in b["header"]), None)
        if block:
            best = {}
            for r in _records(block):
                code = r.get("code", "")
                amt = num(r.get("instBuyAmt")) or 0.0
                same_day = str(r.get("tdDays", "")).strip() == "1"
                cur = best.get(code)
                # 同一只票可能同时出现在「1 日榜」与「3 日榜」，优先保留 1 日口径
                if cur is None or (same_day and not cur["_d1"]):
                    best[code] = {"code": code, "name": r.get("name", ""), "amt": amt,
                                  "branches": int(num(r.get("instBuyBranchCount")) or 0),
                                  "chg": num(r.get("instBuyRate")), "_d1": same_day}
            res["inst"] = list(best.values())
    except Exception:  # noqa: BLE001
        pass
    return res


def sectors_of(limit=6):
    """板块实时榜。接口的实时榜忽略 --date，这里只能取抓取时刻的最新快照。"""
    out = {}
    for kind in ("industry", "concept"):
        for side, order in (("up", "desc"), ("down", "asc")):
            try:
                txt = _run(["sector", "ranking", "--kind", kind, "--order", order,
                            "--limit", str(limit)])
                block = next((b for b in parse_tables(txt) if "changePct" in b["header"]), None)
                if not block:
                    continue
                recs = []
                for r in _records(block):
                    up, tot = _ratio(r.get("upCount"))
                    recs.append({"name": r.get("name", ""), "chg": num(r.get("changePct")),
                                 "net": round((num(r.get("mainNetInflow")) or 0) / 1e4, 2),
                                 "net5": round((num(r.get("mainNetInflow5d")) or 0) / 1e4, 2),
                                 "up": up, "total": tot,
                                 "leader": r.get("leader", "")})
                out.setdefault(kind, {})[side] = recs
            except Exception:  # noqa: BLE001
                out.setdefault(kind, {})[side] = []
    return out


def _ratio(s):
    try:
        a, b = str(s).split("/")
        return int(a), int(b)
    except Exception:  # noqa: BLE001
        return 0, 0


# ---------------------------------------------------------------- 文字判断
def verdict_text(t, prev, up_pct, lu, ld, amt_pct120, max_boards):
    gap = round(t - prev, 1) if prev is not None else None
    if t < 20:
        mood = "冰点"
    elif t < 40:
        mood = "低温退潮"
    elif t < 60:
        mood = "中性拉锯"
    elif t < 80:
        mood = "升温"
    else:
        mood = "亢奋"

    if gap is not None and gap <= -20:
        trend = "单日大幅回落"
    elif gap is not None and gap < 0:
        trend = "继续走弱"
    elif gap is not None and gap > 0:
        trend = "小幅回暖"
    else:
        trend = "与前一日持平"

    # 是否属于「恐慌出清」：跌停潮 + 放量，两者缺一不可
    if ld >= 20 and amt_pct120 >= 90:
        flush = "跌停家数已达 %d 家、成交额放大到 120 日均值的 %.2f%%，出现恐慌出清特征。" % (ld, amt_pct120)
    elif ld < 10 and amt_pct120 < 85:
        flush = ("跌停仅 %d 家、成交额为 120 日均值的 %.2f%%——既没有跌停潮也没有地量，"
                 "缺了「恐慌出清」这一环。" % (ld, amt_pct120))
    else:
        flush = "跌停 %d 家、成交额为 120 日均值的 %.2f%%，出清程度中等，尚未形成极端。" % (ld, amt_pct120)

    if t < 40 and ld < 10:
        concl = "低温但未出清，更像退潮中继而非可以抄底的冰点反转。"
    elif t < 40:
        concl = "低温伴随跌停扩散，属于正在出清的过程，需等跌停潮收敛。"
    elif t >= 80:
        concl = "情绪高位，注意高度股断板引发的负反馈。"
    else:
        concl = "情绪处在常态区间，方向由结构而非温度决定。"

    title = "%s：涨停 %d 家，最高 %d 板" % (mood, lu, max_boards)
    body = "情绪温度 %.1f（%s），较前一日 %s。" % (
        t, mood, ("%+.1f" % gap) if gap is not None else "—（无前值）")
    body += "上涨占比 %.2f%%，涨停 %d / 跌停 %d。%s%s" % (up_pct, lu, ld, flush, concl)
    return {"title": title, "mood": mood, "trend": trend, "body": body}


def gap_check(br, sectors):
    """空仓检验：当日是否存在「看得懂、买得到、拿得住」的买点。

    tone 表示该项对多头是否有利，不是「数值高低」。
    """
    down_pct = br["down_pct"]
    lu, ld, up_pct = br["lu"], br["ld"], br["up_pct"]
    strong = []
    for r in (sectors.get("industry", {}).get("up") or [])[:6]:
        if (r["chg"] or 0) >= 2 and r["total"] and r["up"] / r["total"] >= 0.75 and (r["net5"] or 0) > 0:
            strong.append(r)
    items = [
        {"k": "全市场下跌占比", "v": "%.2f%%" % down_pct,
         "tone": "bad" if down_pct >= 60 else "flat",
         "note": "随机选股的期望收益为负" if down_pct >= 60 else "个股层面仍有结构性机会"},
        {"k": "涨停家数", "v": "%d 家" % lu, "tone": "bad" if lu < 40 else "good",
         "note": "低于 40 家属低温区间，接力成功率低" if lu < 40 else "接力环境尚可"},
        {"k": "跌停家数", "v": "%d 家" % ld, "tone": "flat",
         "note": ("不足 10 家，没有恐慌盘出清，但也没有强制平仓的连锁风险" if ld < 10
                  else "已出现跌停扩散，正在出清")},
        {"k": "板块级买点", "v": ("%d 个" % len(strong)) if strong else "无",
         "tone": "good" if strong else "bad",
         "note": ("、".join(r["name"] for r in strong[:3]) + " 涨幅与资金双正")
                 if strong else "没有一个板块同时满足涨幅 ≥2%、宽度 ≥75%、5 日均流入为正"},
    ]
    right = down_pct >= 60 and lu < 40
    return {"items": items,
            "concl": ("空仓不亏，就是当日的正确操作" if right else "当日并非全面退潮，空仓会错过结构性机会"),
            "rule": ("涨停 < 40 且上涨占比 < 20% 同时成立时，短线仓位上限压到 3 成以内；"
                     + ("本日两项同时触发，属系统性降仓信号。" if (lu < 40 and up_pct < 20)
                        else "本日未同时触发，可维持常态仓位。"))}


def risk_list(tech, val, inst, amt_pct120, ladder, sectors):
    risks = []
    top = (ladder.get("samples") or [{}])[0]
    if top and top.get("days", 0) >= 3:
        hot = (top.get("chg60") or 0) > 60 or (top.get("turn") or 0) > 20
        risks.append({
            "lv": "高" if hot else "中",
            "t": "高位股断板风险",
            "b": "%d 板 %s 换手 %.1f%%、60 日 %+.1f%%。当前 2 板以上仅 %d 只，任何一只崩塌都会直接击穿梯队结构。"
                 % (top["days"], top["name"], top.get("turn") or 0, top.get("chg60") or 0, ladder.get("ge2", 0)),
        })
    if ladder.get("max", 0) <= 4:
        risks.append({
            "lv": "中",
            "t": "空间高度受压制",
            "b": "最高板仅 %d 板、2 板以上 %d 只，占涨停总数 %.0f%%。梯队越薄，情绪越难自我强化。"
                 % (ladder.get("max", 0), ladder.get("ge2", 0),
                    100.0 * ladder.get("ge2", 0) / max(ladder.get("count", 1), 1)),
        })
    outs = [x for x in inst if (x["amt"] or 0) < 0]
    outs.sort(key=lambda x: x["amt"])
    if outs:
        top3 = outs[:3]
        s = sum(x["amt"] for x in top3) / 1e8
        if s < -8:
            risks.append({
                "lv": "高",
                "t": "机构单边减仓",
                "b": "%s 三只合计机构净卖出 %.2f 亿，集中、单边、无对冲，属于撤退而非调仓。"
                     % ("、".join(x["name"] for x in top3), s),
            })
    if amt_pct120 and amt_pct120 < 75:
        risks.append({
            "lv": "中",
            "t": "缩量阴跌，底部不牢",
            "b": "成交额仅为 120 日均值的 %.2f%%。缩量下跌缺少恐慌盘出清，下跌可以在低成交量下持续更久。" % amt_pct120,
        })
    ma = [tech.get("ma5"), tech.get("ma10"), tech.get("ma20"), tech.get("ma60")]
    if all(ma) and ma[0] < ma[1] < ma[2] < ma[3]:
        risks.append({
            "lv": "中",
            "t": "均线空头排列",
            "b": "MA5 %.2f < MA10 %.2f < MA20 %.2f < MA60 %.2f，短中期趋势一致向下；RSI6 %.2f 尚未进入 20 以下的超卖区。"
                 % (ma[0], ma[1], ma[2], ma[3], tech.get("rsi6") or 0),
        })
    if val.get("pe_pct5") and val["pe_pct5"] > 50:
        risks.append({
            "lv": "低",
            "t": "估值缺乏安全垫",
            "b": "中证全指 PE(TTM) %.2f，近 5 年 %.2f%% 分位、近 10 年 %.2f%% 分位——不便宜，跌下来没有估值托底。"
                 % (val.get("pe") or 0, val["pe_pct5"], val.get("pe_pct10") or 0),
        })
    return risks[:5]


def next_day(lu, amt_yi, ladder, month_end):
    scen = [
        {"tag": "情景 A · 修复确认",
         "cond": "涨停回升至 %d 家以上，且主线出现 2 板以上个股" % max(50, int(lu * 1.4)),
         "b": "资金开始在主线内部形成高度，修复才有第二次机会。关注主线内部的接力结构，而非追高最高板。"},
        {"tag": "情景 B · 二次杀跌",
         "cond": "最高板断板 + 权重赛道继续放量下跌",
         "b": "连板高度回落、梯队瓦解，情绪进入「无高度、无主线」的真空期。此时任何抄底都属于逆势。"},
        {"tag": "情景 C · 缩量磨底",
         "cond": "成交额降至 1.3 万亿以下，涨停 30–45 家，跌停 ≤ 5 家",
         "b": "最可能出现的路径。指数窄幅震荡、个股继续分化，策略上是降低频率而非降低标准。"},
    ]
    watch = [
        {"k": "涨停家数阈值", "v": "≥ %d 家视为修复，< 40 家维持低温" % max(50, int(lu * 1.4)),
         "s": "当前 %d 家" % lu},
        {"k": "最高板开板反应", "v": ("%s（%d 板）" % (ladder["samples"][0]["name"], ladder["samples"][0]["days"]))
         if ladder.get("samples") else "—", "s": "一字板开板即负反馈，看承接量而非价格"},
        {"k": "量能配合", "v": "成交额回到 %.2f 万亿以上" % max(1.6, amt_yi * 1.03 / 1.0),
         "s": "当前 %.2f 万亿" % amt_yi},
        {"k": "主线延续性", "v": "领涨板块能否出现 2 板以上个股", "s": "当前梯队里的最高板不在主线板块内"},
    ]
    if month_end:
        watch.append({"k": "月末效应", "v": "本月最后一个交易日", "s": "机构调仓与隔周持仓意愿会放大尾盘波动"})
    return {"scenarios": scen, "watch": watch}


# ---------------------------------------------------------------- 组装
def build(date=None, now=None):
    now = now or datetime.datetime.now()
    month_end = week_end = False
    if date is None:
        date, month_end, week_end = resolve_target(now)

    ov = market_overview(date)
    up_kv = _kv(ov, "updown")
    te_kv = _kv(ov, "technical")
    va_kv = _kv(ov, "valuation")
    tr_kv = _kv(ov, "trade")
    tr_cols, tr_rows = _grid(ov, "trade")

    amt_yi = (num(tr_kv.get("两市成交额(亿)")) or 0) / 1e4        # 万亿
    amt_pct120 = num(tr_kv.get("当日两市成交额/120日均值(%)")) or 0

    up = int(num(up_kv.get("A股上涨股票数")) or 0)
    dn = int(num(up_kv.get("A股下跌股票数")) or 0)
    fl = int(num(up_kv.get("A股平盘股票数")) or 0)
    tot = int(num(up_kv.get("A股股票数")) or 0)
    lu = int(num(up_kv.get("A股涨停股票数")) or 0)
    ld = int(num(up_kv.get("A股跌停股票数")) or 0)
    up_pct = num(up_kv.get("A股上涨股票比例(%)")) or 0

    t_now = temperature(up_pct, lu, ld)

    # 六日脉搏（含 T）
    try:
        days = prev_trading_days(date, 6)
    except Exception:  # noqa: BLE001
        days = [date]
    pulse = pulse_series(days)
    prev_temp = None
    for p in pulse:
        if p["d"] != date[5:]:
            prev_temp = p["temp"]
    if len(pulse) >= 2:
        prev_temp = pulse[-2]["temp"]

    # 连板梯队 + 个股明细
    lad = ladder_of(date)
    samples = []
    for r in lad["rows"]:
        if r["days"] >= 2:
            samples.append(r)
    samples = samples[:14]
    q = quotes_of([s["code"] for s in samples], date) if samples else {}
    rows = []
    for s in samples:
        d = q.get(s["code"], {})
        turn = num(d.get("turnover_rate")) or 0
        rng = num(d.get("range_pct"))
        rows.append({"days": s["days"], "code": s["code"], "name": s["name"],
                     "chg": num(d.get("change_percent")),
                     "turn": turn, "amt": round((num(d.get("amount")) or 0) / 1e8, 2),
                     "chg20": num(d.get("chg_20d")), "chg60": num(d.get("chg_60d")),
                     "yizi": (rng is not None and rng <= 1.0) or turn < 2})
    avg_turn = round(sum(x["turn"] for x in rows) / len(rows), 2) if rows else 0
    # 同高度内按换手率降序：换手最高的那只是真正的风向标，放在最前面
    rows.sort(key=lambda x: (-x["days"], -(x["turn"] or 0)))
    lad["samples"] = rows
    lad["yizi"] = sum(1 for x in rows if x["yizi"])
    lad["huan"] = len(rows) - lad["yizi"]
    lad["avg_turn"] = avg_turn
    lad["dist_list"] = [{"label": "%d 板" % k, "n": v}
                        for k, v in sorted(lad["dist"].items())]

    # 龙虎榜
    lhb = lhb_of(date)
    inst = sorted([x for x in lhb["inst"] if x["amt"]], key=lambda x: -x["amt"])
    ins = [{"name": x["name"], "amt": round(x["amt"] / 1e8, 2), "branch": x["branches"],
            "chg": x["chg"]} for x in inst if x["amt"] > 0][:6]
    outs = [{"name": x["name"], "amt": round(x["amt"] / 1e8, 2), "branch": x["branches"],
             "chg": x["chg"]} for x in inst if x["amt"] < 0]
    outs.sort(key=lambda x: x["amt"])
    outs = outs[:6]

    # 指数：三只来自 trade 表，其余补 quote
    idx = []
    for code, nm in INDEX_LIST:
        if nm in tr_cols:
            idx.append({"name": nm, "close": num(tr_rows.get("收盘", {}).get(nm)),
                        "chg": num(tr_rows.get("涨跌幅", {}).get(nm))})
    try:
        extra = quotes_of(["sh000016", "sh000852", "sh000688"], date)
        m = {"sh000016": "上证 50", "sh000852": "中证 1000", "sh000688": "科创 50"}
        done = {x["name"] for x in idx}
        for code, nm in m.items():
            if nm in done or code not in extra:
                continue
            idx.append({"name": nm, "close": num(extra[code].get("price")),
                        "chg": num(extra[code].get("change_percent"))})
        order = [n for _, n in INDEX_LIST]
        idx.sort(key=lambda x: order.index(x["name"]) if x["name"] in order else 99)
    except Exception:  # noqa: BLE001
        pass

    sectors = sectors_of()

    tech = {"ma5": num(te_kv.get("基于5个交易日的收盘价移动平均值")),
            "ma10": num(te_kv.get("基于10个交易日的收盘价移动平均值")),
            "ma20": num(te_kv.get("基于20个交易日的收盘价移动平均值")),
            "ma60": num(te_kv.get("基于60个交易日的收盘价移动平均值")),
            "ma250": num(te_kv.get("基于250个交易日的收盘价移动平均值")),
            "macd": num(te_kv.get("MACD值")), "rsi6": num(te_kv.get("基于6个交易日的RSI指标值"))}
    val = {"pe": num(va_kv.get("中证全指pettm")), "pb": num(va_kv.get("中证全指pblf")),
           "pe_pct5": num(va_kv.get("中证全指pettm百分位(5年)")),
           "pe_pct10": num(va_kv.get("中证全指pettm百分位(10年)")),
           "pe_pct3": num(va_kv.get("中证全指pettm百分位(3年)"))}

    basis = band_of(t_now)[0]
    br = {"up": up, "down": dn, "flat": fl, "total": tot, "up_pct": round(up_pct, 2),
          "lu": lu, "ld": ld,
          "down_pct": round(dn / tot * 100, 2) if tot else round(100 - up_pct, 2),
          "ratio": num(up_kv.get("A股涨跌比")),
          "nh20": int(num(up_kv.get("A股创20日新高个股数")) or 0),
          "nl20": int(num(up_kv.get("A股创20日新低个股数")) or 0),
          "nl250": int(num(up_kv.get("A股创250日新低个股数")) or 0),
          "nl120": int(num(up_kv.get("A股创120日新低个股数")) or 0)}
    payload = {
        "date": date,
        "weekday": "周" + "一二三四五六日"[datetime.date.fromisoformat(date).weekday()],
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "month_end": month_end, "week_end": week_end,
        "live_at": now.strftime("%m-%d %H:%M"),
        "temp": t_now, "temp_prev": prev_temp,
        "temp_delta": round(t_now - prev_temp, 1) if prev_temp is not None else None,
        "band": basis,
        "pulse": pulse,
        "breadth": br,
        "volume": {"amt": round(amt_yi, 3), "pct120": amt_pct120,
                   "ma10": round((num(tr_kv.get("两市成交额(亿)10日均值")) or 0) / 1e4, 3),
                   "ma20": round((num(tr_kv.get("两市成交额(亿)20日均值")) or 0) / 1e4, 3)},
        "index": idx,
        "tech": tech, "val": val,
        "ladder": {k: lad[k] for k in ("count", "max", "ge2", "samples", "yizi", "huan",
                                       "avg_turn", "dist_list")},
        "sector": sectors,
        "inst": {"lhb_count": lhb["all_count"], "inst_count": len(lhb["inst"]),
                 "in": ins, "out": outs,
                 "sum_in": round(sum(x["amt"] for x in inst if x["amt"] > 0) / 1e8, 2),
                 "sum_out": round(sum(x["amt"] for x in inst if x["amt"] < 0) / 1e8, 2)},
    }
    payload["verdict"] = verdict_text(t_now, prev_temp, up_pct, lu, ld, amt_pct120,
                                      lad.get("max", 0))
    payload["gap"] = gap_check(br, sectors)
    payload["risks"] = risk_list(tech, val, inst, amt_pct120, lad, sectors)
    payload["next"] = next_day(lu, amt_yi, lad, month_end)
    return payload


def save(payload):
    os.makedirs(SENTI, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    return OUT_JSON


def load():
    if os.path.exists(OUT_JSON):
        try:
            return json.loads(open(OUT_JSON, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            return None
    return None


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    p = build(arg)
    save(p)
    print("交易日 %s（%s）｜温度 %.1f [%s]｜涨停 %d / 跌停 %d｜连板 %d 只 / 最高 %d 板"
          % (p["date"], p["weekday"], p["temp"], p["band"], p["breadth"]["lu"],
             p["breadth"]["ld"], p["ladder"]["count"], p["ladder"]["max"]))
    print("已写入 %s" % OUT_JSON)
