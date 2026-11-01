# -*- coding: utf-8 -*-
"""「港股全景」频道：港股情绪、宽度、沽空、南向与估值快照（8 个模块）。

与 A 股「短线精灵」、美股「短线精灵 · 美股」并列，但口径来源完全不同：
  A 股频道自建温度（上涨占比 + 涨停 + 跌停）
  美股频道用 CNN 恐惧贪婪指数
  港股频道没有权威情绪指数可用，改用「沽空比例」——这是港股独有且每日披露的数据

八个模块：
  ① 三指数温度      恒生 / 恒生科技 / 国企，位置分位 + 动能分位 + 量能扩张（不合成跨页可比的总分）
  ② 涨跌家数        成分股并集，涨/平/跌 + ±5% 分档（港股无涨跌停）
  ③ 涨跌榜          并集池 Top10 / Bottom10，按成交额 >= 1000 万港币滤仙股
  ④ 宽度            站上 MA20 / MA60 比例、距 52 周高点回撤分布
  ⑤ 沽空压力        并集池金额加权沽空率 + 分布 + 20 日均值对比
  ⑥ 南向活跃        港股通成交活跃榜（当日 Top10 + 合计成交额）
  ⑦ 结构强弱        恒生科技 ÷ 恒生指数 的 60 日比值曲线
  ⑧ 估值分布        并集池 PE / PB 中位数与四分位、亏损股占比（**无历史分位**）

数据源与坑：
  westock quote       批量 150 只约 1~2 秒；港股 quote 带 pe_ratio / pb_ratio，指数恒为 0
  westock fund short  港股卖空数据。**单次最多 10 码**；当日快照稳，区间请求限频严格
                       （连发第 3 批只剩 2/10 成功），故只取当日，历史靠自建归档攒
  westock index constituent  恒指 95 / 恒生科技 30 / 国企 50，去重后约 150 只
  westock kline       长区间会静默截断，必须走 wsfeed 的限频 + 行数校验
  南向资金「当日净买入」  东财 kamt 收盘后返回值等于当日额度（420 亿），不是净买入额，
                       不可直接展示；故本页只用「成交活跃榜」这一可核实字段

产物：data/hk_senti/hk_senti.json（构建期注入首屏）
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_core as core  # noqa: E402
import wsfeed  # noqa: E402
from sentiment import num, parse_tables  # noqa: E402  复用 markdown 表格解析

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "data", "hk_senti")
OUT_JSON = os.path.join(OUTDIR, "hk_senti.json")
SHORT_ARCHIVE = os.path.join(OUTDIR, "short_archive.json")

# ---------------------------------------------------------------- 常量
IDX = [("恒生指数", "hkHSI", "恒指"),
       ("恒生科技", "hkHSTECH", "恒生科技"),
       ("国企指数", "hkHSCEI", "国企")]

MIN_AMT = 10_000_000         # 涨跌榜成交额门槛（港币）。港股有大量成交额为 0 的仙股，
                             # 不过滤的话涨幅榜前排全是 0.01 元涨到 0.02 元的品种。
SHORT_HI = 30.0              # 沽空率高于此值视为「空头压力显著」。
                             # 取 30 而不是 10：实测池内中位数就在 23% 左右，
                             # 阈值设 10 会让 95% 的成分股都"偏高"，指标失去区分度。
SHORT_BATCH = 10             # fund short 单次最多 10 个代码，硬限制
SHORT_GAP = 2.0              # 批间隔。实测 3 秒更稳，但 12 批会拖到 40 秒以上
SHORT_TRY = 3
COVER_MIN = 0.90             # 覆盖低于此值即判该模块降级，页面不展示结论
INDEX_DAYS = 780             # 取 3.2 年：250 日分位窗口 + 回撤窗口各留一倍余量

PHASE = {1: "盘前", 2: "早盘", 3: "午盘", 4: "收盘"}


def _bin():
    return wsfeed.bin_path()


def _env():
    return wsfeed.env()


def _run(args, timeout=240):
    r = subprocess.run([_bin()] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=_env(), shell=False)
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "错误:" in text[:200]:
        raise RuntimeError("%s 失败：%s" % (" ".join(args[:2]), text.strip()[:160]))
    return text


def _block(txt, key):
    for b in parse_tables(txt):
        if key in b["header"]:
            return b
    return None


def _records(block):
    """列数不齐时补空——批量 quote 会裁掉尾部空列。"""
    h = block["header"]
    n = len(h)
    out = []
    for r in block["rows"]:
        if len(r) < n:
            r = r + [""] * (n - len(r))
        out.append({h[i]: r[i] for i in range(n)})
    return out


def _f(x):
    v = num(x)
    return v if v is not None else 0.0


def _amt(s):
    """'25.27亿' / '1,234万' -> 港元数值。westock 榜单单列会带中文单位。"""
    s = str(s or "").strip().replace(",", "")
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    v = num(s)
    return (v or 0.0) * mult


# ---------------------------------------------------------------- 取数
def constituents(idx):
    """指数成分股。返回 [(code, name)]，保持接口输出顺序。"""
    txt = _run(["index", "constituent", idx, "--limit", "0"])
    out = []
    for b in parse_tables(txt):
        if "code" in b["header"] and "name" in b["header"]:
            for r in _records(b):
                c = r.get("code", "")
                if c.startswith("hk"):
                    out.append((c, r.get("name", "")))
    return out


def quotes(codes):
    """批量行情。港股 150 只一次调用，实测 1~2 秒。

    注意：批量返回的列会被裁掉尾部空列（如 pe_ratio 全空时整列消失），
    所以取字段必须用 .get()，不能下标。
    """
    out = {}
    for i in range(0, len(codes), 400):
        txt = _run(["quote", ",".join(codes[i:i + 400])])
        b = _block(txt, "change_percent")
        if b:
            for r in _records(b):
                out[r.get("code", "")] = r
    return out


def index_series(code, days=INDEX_DAYS):
    """指数日线。走 wsfeed：它带限频重试与行数完整性校验，能挡住静默截断。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(days * 1.45))
    df = core.parse_westock(wsfeed.kline(code, start.isoformat(), end.isoformat()))
    return df.tail(days).reset_index(drop=True)


def _batch_status(txt):
    """解析批次状态行。返回 (是否全部成功, 成功数, 总数)。

    `fund short` 的失败**不体现在退出码上**——它打印 `[Batch] 状态: error`
    和一张失败列表，退出码仍是 0。不显式检查就会拿到一张"部分股票缺失"的表，
    算出来的沽空率看起来完全正常，但样本只剩三成。这类静默降级必须挡在入口。
    """
    m = re.search(r"状态:\s*(\w+)\s*\|\s*总数:\s*(\d+)\s*\|\s*成功:\s*(\d+)", txt)
    if not m:
        return (False, 0, 0) if "数据为空" in txt else (True, -1, -1)
    return m.group(1) == "success", int(m.group(3)), int(m.group(2))


def short_rows(codes, date):
    """港股**当日**卖空快照。返回 ({code: ShortRatio}, 成功批数, 总批数)。

    接口限制与实测节奏（v1.6.0 记录）：
      - 单次最多 10 个代码，超过直接报错；
      - 当日快照 `--date` 稳定，3 秒间隔连发 3 批全成功；
      - 区间 `--start/--end` 限频严格得多，连发第 3 批就只剩 2/10 成功。
    所以本页只取当日快照，20 日均值改由**自建归档**累积（见 archive_short）。
    用区间硬拉 30 天需要 12 批 × 10 秒以上，代价与收益不成比例。
    """
    out = {}
    codes = list(codes)
    batches = [codes[i:i + SHORT_BATCH] for i in range(0, len(codes), SHORT_BATCH)]
    ok_batches = 0
    for bi, chunk in enumerate(batches):
        txt = ""
        for attempt in range(SHORT_TRY):
            try:
                txt = _run(["fund", "short", ",".join(chunk), "--date", date], timeout=180)
            except Exception as e:  # noqa: BLE001
                txt = str(e)
            ok, succ, total = _batch_status(txt)
            if ok:
                ok_batches += 1
                break
            wait = 3 * (attempt + 1)
            print("[重试] 沽空第 %d/%d 批（%s/%s），%d 秒后重试（%d/%d）"
                  % (bi + 1, len(batches), succ, total, wait, attempt + 1, SHORT_TRY))
            time.sleep(wait)
        for b in parse_tables(txt):
            if "ShortRatio" not in b["header"]:
                continue
            for r in _records(b):
                c = r.get("code") or r.get("symbol") or ""
                v = num(r.get("ShortRatio"))
                if c and v is not None:
                    out[c] = v
        if bi < len(batches) - 1:
            time.sleep(SHORT_GAP)
    return out, ok_batches, len(batches)


def archive_short(day, rec):
    """每日沽空读数存档。

    为什么必须自己攒：接口能拿区间，但限频到「连发 3 批就掉」，硬拉 20 天不现实。
    归档每天一条，攒满 20 个交易日之后 20 日均值自然就有了。
    这也是本页唯一一个「今天不做、以后补不回来」的模块——和 CNN F&G 同理。
    """
    d = {}
    if os.path.exists(SHORT_ARCHIVE):
        try:
            d = json.loads(open(SHORT_ARCHIVE, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            d = {}
    if day and rec.get("ratio") is not None:
        d[day] = rec
    os.makedirs(OUTDIR, exist_ok=True)
    with open(SHORT_ARCHIVE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return d


def short_base(arch, day, win=20):
    """从归档取「今天之前」最近 win 个交易日的均值。返回 (均值, 样本数)。"""
    keys = sorted(k for k in arch if k < day and arch[k].get("ratio") is not None)
    vals = [arch[k]["ratio"] for k in keys[-win:]]
    return (round(sum(vals) / len(vals), 2) if vals else None), len(vals)


def south_active():
    """南下成交活跃股（当日 Top10）。返回 (日期, [{code,name,amt}])。

    两个坑：
    1. 日期不在表格列里，而在表前的 `**2026-10-30**` 标记行上，只能从原文抓；
    2. 榜单实际返回「近 1 周按日」多天数据，不切分的话同一只票会出现多次
       （中芯国际连占两行），必须只取最新日期那一段并去重。
    """
    txt = _run(["screen", "ranking", "--market", "hk", "--type", "south_active_d"])
    marks = list(re.finditer(r"\*\*(\d{4}-\d{2}-\d{2})\*\*", txt))
    date, tail = "", txt
    if marks:
        date = max(m.group(1) for m in marks)
        pos = max(i for i, m in enumerate(marks) if m.group(1) == date)
        tail = txt[marks[pos].end():]
    rows, seen = [], set()
    for b in parse_tables(tail):
        if "tradeAmount" not in b["header"]:
            continue
        for r in _records(b):
            c = r.get("code", "")
            if not c or c in seen:
                continue
            seen.add(c)
            rows.append({"code": c, "name": r.get("name", ""),
                         "amt": _amt(r.get("tradeAmount"))})
    rows.sort(key=lambda x: -x["amt"])
    return date, rows[:10]


# ---------------------------------------------------------------- 计算
def index_card(name, code, slug, df):
    """三指数温度卡。

    只给三个分项，不合成「总分」：A 股总分的 35% 来自「成交额占两市比重」，
    港股没有可比的「两市」分母，强行合成会让两页的 60 分含义不同。
    位置与动能两项完全复用 v4 公式，这两项跨页可比；量能扩张是港股本地口径。
    """
    o = core.build(df)
    volx = df["amount"] / df["amount"].rolling(20).mean()
    s_volx = core.pr(volx)
    pos = _last(o["s_pos"])
    mom = _last(o["s_mom"])
    vz = _last(s_volx)
    heat = None
    if pos is not None and mom is not None and vz is not None:
        heat = 0.50 * pos + 0.30 * mom + 0.20 * vz
    return {
        "name": name, "code": code, "slug": slug,
        "close": float(df["close"].iloc[-1]),
        "chg": float(df["chg"].iloc[-1]) if "chg" in df else None,
        "pos": pos, "mom": mom, "volx": _last(volx), "volx_pr": vz,
        "heat": heat,
        "dd250": _last(o["dd250"]),
        "rsi6": _last(o["rsi6"]),
        "n": int(len(df)),
        "start": str(df["date"].iloc[0]), "end": str(df["date"].iloc[-1]),
    }


def _last(s):
    s = s.dropna()
    return float(s.iloc[-1]) if len(s) else None


def ratio_curve(a, b, n=60):
    """两只标的的比值曲线，以窗口首日为 100。"""
    da = a.set_index("date")["close"]
    db = b.set_index("date")["close"]
    j = pd.concat([da, db], axis=1, keys=["a", "b"]).dropna()
    if len(j) < 10:
        return {"curve": [], "last": None, "chg": None}
    r = (j["a"] / j["b"]).tail(n)
    base = r.iloc[0]
    return {"curve": [{"d": str(d), "v": round(float(v / base * 100), 2)}
                      for d, v in r.items()],
            "last": round(float(r.iloc[-1] / base * 100), 2),
            "chg": round(float((r.iloc[-1] / base - 1) * 100), 2)}


def breadth(label, rows):
    """涨跌家数 + 动能宽度 + 距高点分布。

    这里不报「站上 MA20 比例」——那需要 150 只成分股各取一次日线，
    在本项目的限频环境下每天多 150 次请求，代价远超收益。
    改用 quote 现成的 20/60 日涨跌幅：口径是「N 日收益为正的比例」，
    含义接近（都在描述趋势扩散程度）但不是同一个数，页面必须按原名标注。
    """
    up = sum(1 for r in rows if (r["chg"] or 0) > 0)
    dn = sum(1 for r in rows if (r["chg"] or 0) < 0)
    fl = len(rows) - up - dn

    def _pos(key):
        v = [1 if (r.get(key) or 0) > 0 else 0 for r in rows if r.get(key) is not None]
        return round(sum(v) / len(v) * 100, 1) if v else None

    return {
        "name": label, "n": len(rows), "up": up, "down": dn, "flat": fl,
        "p5": sum(1 for r in rows if (r["chg"] or 0) >= 5),
        "m5": sum(1 for r in rows if (r["chg"] or 0) <= -5),
        "up20": _pos("chg20"),
        "up60": _pos("chg60"),
        "near_hi": sum(1 for r in rows
                       if (r.get("dd52") is not None and r["dd52"] >= -3)),
        "far_hi": sum(1 for r in rows
                      if (r.get("dd52") is not None and r["dd52"] <= -30)),
    }


def _fmt(v, n=1):
    return "—" if v is None else ("%.*f" % (n, v))


def rank_list(rows, n=10, reverse=True):
    s = sorted([r for r in rows if r["amt"] >= MIN_AMT and r["chg"] is not None],
               key=lambda r: r["chg"], reverse=reverse)[:n]
    return [{"code": r["code"], "name": r["name"], "chg": round(r["chg"], 2),
             "px": r["px"], "amt": r["amt"], "pools": r["pools"]} for r in s]


def valuation(rows):
    """成分股估值分布。只有当前快照——个股历史 PE 拿不到，故无分位。

    这一点必须在页面上写明：它回答「现在贵不贵」，不回答「比过去贵还是便宜」。
    """
    pe = [r["pe"] for r in rows if r["pe"] and r["pe"] > 0]
    pb = [r["pb"] for r in rows if r["pb"] and r["pb"] > 0]
    if not pe:
        return None
    s = pd.Series(pe)
    sp = pd.Series(pb) if pb else None
    return {
        "n": len(rows), "pe_n": len(pe),
        "pe_med": round(float(s.median()), 2),
        "pe_q1": round(float(s.quantile(0.25)), 2),
        "pe_q3": round(float(s.quantile(0.75)), 2),
        "pb_med": round(float(sp.median()), 2) if sp is not None else None,
        "loss": len(rows) - len(pe),
        "loss_pct": round((len(rows) - len(pe)) / len(rows) * 100, 1),
    }


def hk_phase(now=None, date=None):
    """快照所处时段。

    时段必须跟着**数据日期**走，不能只看当前时钟：周末或节假日点刷新，
    接口返回的仍是上一个交易日的收盘序列，若按当下时钟报「午盘」，
    页面就会出现「10-30　午盘」这种自相矛盾的标注——10-30 早收盘了。
    故数据日期不是今天时，一律记作收盘。
    """
    now = now or datetime.datetime.now()
    if date and date != now.strftime("%Y-%m-%d"):
        return "收盘"
    t = now.hour * 60 + now.minute
    if t < 9 * 60 + 30:
        return "盘前"
    if t < 12 * 60:
        return "早盘"
    if t < 13 * 60:
        return "午间休市"
    if t < 16 * 60 + 10:
        return "午盘"
    return "收盘"


# ---------------------------------------------------------------- 组装
def build():
    now = datetime.datetime.now()

    # ---- 宇宙：三指数成分股去重
    seen = {}
    for lname, idx, _slug in IDX:
        try:
            for c, n in constituents(idx):
                r = seen.setdefault(c, {"code": c, "name": n, "pools": []})
                if lname not in r["pools"]:
                    r["pools"].append(lname)
        except Exception as e:  # noqa: BLE001  单个指数失败不该拖垮整页
            print("[警告] %s 成分股取数失败：%s" % (lname, e))
    codes = list(seen.keys())
    if not codes:
        raise RuntimeError("三个指数成分股均未取到")

    q = quotes(codes)

    # ---- 指数日线（串行，wsfeed 内部已有 1.2s 节流）
    series = {}
    for name, code, slug in IDX:
        try:
            series[slug] = index_series(code)
        except Exception as e:  # noqa: BLE001
            print("[警告] %s 日线取数失败：%s" % (name, e))
    if not series:
        raise RuntimeError("三个指数日线均未取到")

    date = max((str(df["date"].iloc[-1]) for df in series.values()), default="")

    # ---- 成分股记录
    rows = []
    for c, meta in seen.items():
        r = q.get(c)
        if not r:
            continue
        px = num(r.get("price"))
        hi, lo = num(r.get("high_52week")), num(r.get("low_52week"))
        rows.append({
            "code": c, "name": meta["name"], "pools": meta["pools"],
            "chg": num(r.get("change_percent")), "px": px,
            "amt": _f(r.get("amount")),
            "pe": num(r.get("pe_ratio")), "pb": num(r.get("pb_ratio")),
            "ytd": num(r.get("chg_ytd")),
            "chg20": num(r.get("chg_20d")), "chg60": num(r.get("chg_60d")),
            "dd52": round((px / hi - 1) * 100, 2) if (px and hi) else None,
        })

    # ---- ⑤ 沽空（当日全池 + 归档基线）
    short = {"date": date, "ratio": None, "ratio20": None, "med": None,
             "amt": 0.0, "top": [], "hi_cnt": 0, "n": 0,
             "cover": 0.0, "base_n": 0, "degraded": True}
    try:
        smap, ok_b, all_b = short_rows(codes, date or datetime.date.today().isoformat())
        cover = ok_b / all_b if all_b else 0.0
        # 权重用当日成交额：它同时是 ShortRatio 的分母，用它聚合才与个股口径一致。
        w = {c: _f(q[c].get("amount")) for c in smap
             if c in q and _f(q[c].get("amount")) > 0}
        den = sum(w.values())
        amt = sum(w[c] * v / 100.0 for c, v in smap.items() if c in w)
        ratio = amt / den * 100 if den else None
        vals = sorted(smap.items(), key=lambda x: -x[1])
        med = round(float(pd.Series([v for _, v in vals]).median()), 2) if vals else None

        arch = archive_short(date or "", {"ratio": round(ratio, 2) if ratio else None,
                                          "med": med, "n": len(vals)})
        base, base_n = short_base(arch, date or "")
        short = {
            "date": date,
            "ratio": round(ratio, 2) if ratio is not None else None,
            "ratio20": base, "base_n": base_n,
            "med": med,
            "amt": round(amt, 0),
            "n": len(vals),
            "hi_cnt": sum(1 for _, v in vals if v >= SHORT_HI),
            "bins": {"hi": sum(1 for _, v in vals if v >= SHORT_HI),
                     "mid": sum(1 for _, v in vals if 10 <= v < SHORT_HI),
                     "lo": sum(1 for _, v in vals if v < 10)},
            "cover": round(cover * 100, 1),
            "degraded": cover < COVER_MIN,
            "top": [{"code": c, "name": seen.get(c, {}).get("name", c), "ratio": round(v, 2)}
                    for c, v in vals[:5]],
        }
        print("  沽空：%d/%d 批成功，池内 %d 只有读数" % (ok_b, all_b, len(vals)))
    except Exception as e:  # noqa: BLE001
        print("[警告] 沽空取数失败：%s" % e)

    # ---- ⑥ 南向活跃
    south = {"date": "", "top": [], "total": 0.0}
    try:
        sd, st = south_active()
        south = {"date": sd, "top": st, "total": round(sum(x["amt"] for x in st), 0)}
    except Exception as e:  # noqa: BLE001
        print("[警告] 南向活跃榜取数失败：%s" % e)

    # ---- ⑦ 结构强弱
    coef = {"curve": [], "last": None, "chg": None}
    if "恒生科技" in series and "恒指" in series:
        coef = ratio_curve(series["恒生科技"], series["恒指"], n=60)

    # ---- 汇总
    pools = []
    for lname, _idx, _slug in IDX:
        pools.append(breadth(lname, [r for r in rows if lname in r["pools"]]))

    return {
        "date": date,
        "phase": hk_phase(now, date),
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "universe": {"total": len(rows),
                     "pools": {lname: sum(1 for r in rows if lname in r["pools"])
                               for lname, _i, _s in IDX}},
        "indices": [index_card(n, c, s, series[s]) for n, c, s in IDX if s in series],
        "breadth": {"all": breadth("三指数并集", rows), "pools": pools},
        "gainers": rank_list(rows, 10, True),
        "losers": rank_list(rows, 10, False),
        "short": short,
        "south": south,
        "coef": coef,
        "valuation": valuation(rows),
        "min_amt": MIN_AMT,
    }


def save(p):
    os.makedirs(OUTDIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, separators=(",", ":"))
    return OUT_JSON


def load():
    if os.path.exists(OUT_JSON):
        try:
            return json.loads(open(OUT_JSON, encoding="utf-8").read())
        except Exception:  # noqa: BLE001
            return None
    return None


if __name__ == "__main__":
    p = build()
    save(p)
    print("港股 %s（%s）｜并集 %d 只 %s"
          % (p["date"], p["phase"], p["universe"]["total"], p["universe"]["pools"]))
    for d in p["indices"]:
        print("  %-6s %10.2f %+6.2f%%  位置 %s 动能 %s 量能 %s 热度 %s（%d 日）"
              % (d["name"], d["close"], d["chg"] or 0,
                 _fmt(d["pos"]), _fmt(d["mom"]), _fmt(d["volx_pr"]), _fmt(d["heat"]), d["n"]))
    b = p["breadth"]["all"]
    print("宽度：涨 %d / 跌 %d / 平 %d｜±5%% %d/%d" % (b["up"], b["down"], b["flat"], b["p5"], b["m5"]))
    s = p["short"]
    print("沽空：%s%%（中位 %s%%，基线 %s%% / %d 日）｜高于 %.0f%% 的 %d 只｜批成功 %s%%%s"
          % (s["ratio"], s["med"], s["ratio20"], s["base_n"], SHORT_HI, s["hi_cnt"],
             s["cover"], "  [降级]" if s["degraded"] else ""))
    print("南向活跃 %s：%d 只，合计 %.1f 亿" % (p["south"]["date"], len(p["south"]["top"]),
                                          p["south"]["total"] / 1e8))
    v = p["valuation"]
    if v:
        print("估值：PE 中位 %.2f（%.2f~%.2f）｜PB 中位 %s｜亏损 %.1f%%"
              % (v["pe_med"], v["pe_q1"], v["pe_q3"], v["pb_med"], v["loss_pct"]))
    print("已写入 %s" % OUT_JSON)
