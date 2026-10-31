# -*- coding: utf-8 -*-
"""「美股精灵」频道：美股情绪、宽度与结构快照（8 个模块）。

与 A 股「短线精灵」并列，但口径完全不同，页面上必须分开看：
  A 股频道自建温度（上涨占比 + 涨停 + 跌停），美股频道直接用 CNN 恐惧贪婪指数。

八个模块：
  ① 恐惧贪婪总表      CNN 总分 + 7 个分项 + 1 年走势（只取最新读数，不做分位）
  ② 涨跌榜            四池合并 Top10 / Bottom10
  ③ 板块热力          11 只 SPDR 行业 ETF，1/5/20/60 日与年初至今
  ④ 涨跌家数          四池分别统计 + ±5% 极端家数
  ⑤ 防御 vs 进攻      XLU+XLP+XLV 对 XLK+XLY+XLC 的比值曲线
  ⑥ Mag7 贡献度       七巨头当日涨跌与市值加权贡献
  ⑦ 半导体相对强弱    SOXX/SPY 比值曲线
  ⑧ 52 周新高新低     主池成分股里创新高/新低的只数

数据源与坑：
  CNN graphdata        必须带完整浏览器请求头，缺一项返回反爬 HTML
  westock quote        批量 590 只约 1.4 秒；东方财富接口需绕代理，故不用
  中概池 466 只        含大量粉单仙股（实测涨幅第一 SEII 涨 100%、成交额 0），
                       按成交额 >= 500 万美元过滤后约 70 只
  美股交易时段         09:30-16:00 ET，对应北京时间 21:30-04:00（夏令时）。

产物：data/us_senti/us_senti.json（构建期注入首屏）、fng_archive.json（每日存档）
"""
import datetime
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sentiment import num, parse_tables  # noqa: E402  复用 markdown 表格解析

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "data", "us_senti")
OUT_JSON = os.path.join(OUTDIR, "us_senti.json")
ARCHIVE = os.path.join(OUTDIR, "fng_archive.json")

# ---------------------------------------------------------------- 常量
POOLS = [("道指", "us.DJI"), ("标普500", "us.SPX"), ("纳指100", "us.NDX")]
CN_MIN_AMT = 5_000_000          # 中概池成交额门槛（美元）

SECTORS = [("usXLK", "科技"), ("usXLF", "金融"), ("usXLE", "能源"),
           ("usXLV", "医疗保健"), ("usXLI", "工业"), ("usXLY", "非必需消费"),
           ("usXLP", "必需消费"), ("usXLU", "公共事业"), ("usXLB", "基础材料"),
           ("usXLRE", "房地产"), ("usXLC", "通讯服务")]

DEFENSIVE = ["usXLU", "usXLP", "usXLV"]
OFFENSIVE = ["usXLK", "usXLY", "usXLC"]

MAG7 = [("usAAPL", "苹果"), ("usMSFT", "微软"), ("usNVDA", "英伟达"),
        ("usGOOGL", "谷歌"), ("usAMZN", "亚马逊"), ("usMETA", "Meta"),
        ("usTSLA", "特斯拉")]

# CNN 分项：(返回字段, 中文名, 说明)
FNG_PARTS = [
    ("market_momentum_sp500", "市场动能", "标普500 相对 125 日均线的位置"),
    ("stock_price_strength", "股价强度", "创 52 周新高与新低的家数对比"),
    ("stock_price_breadth", "市场宽度", "上涨股成交量占比（McClellan 量能）"),
    ("put_call_options", "看跌看涨比", "5 日平均 put/call 期权比，越高越恐慌"),
    ("market_volatility_vix", "市场波动", "VIX 相对自身 50 日均线的偏离"),
    ("junk_bond_demand", "垃圾债需求", "高收益债与国债利差，越窄越贪婪"),
    ("safe_haven_demand", "避险需求", "股票与国债的 20 日收益差，越低越避险"),
]

CN_HINT = {"道指": "30 只", "标普500": "503 只", "纳指100": "101 只"}


def _bin():
    for c in (os.path.expanduser("~/.local/bin/westock.exe"),
              os.path.expanduser("~/.local/bin/westock")):
        if os.path.exists(c):
            return c
    return "westock"


def _env():
    env = os.environ.copy()
    extra = os.path.expanduser("~/.local/bin")
    if extra not in env.get("PATH", ""):
        env["PATH"] = env.get("PATH", "") + os.pathsep + extra
    return env


def _run(args, timeout=240):
    r = subprocess.run([_bin()] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=_env(), shell=False)
    if r.returncode != 0:
        raise RuntimeError("westock 失败(%s)：%s" % (" ".join(args[:2]),
                                                    (r.stderr or r.stdout)[:200]))
    return r.stdout


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


def _block(txt, key):
    for b in parse_tables(txt):
        if key in b["header"]:
            return b
    return None


def _f(x):
    v = num(x)
    return v if v is not None else 0.0


# ---------------------------------------------------------------- 取数
def constituents(idx):
    txt = _run(["index", "constituent", idx])
    out = []
    for b in parse_tables(txt):
        if "code" in b["header"] and "name" in b["header"]:
            for r in _records(b):
                if r["code"].startswith("us"):
                    out.append((r["code"], r["name"]))
    return out


def china_pool():
    txt = _run(["universe", "--market", "us", "--type", "china_stock"])
    out = []
    for b in parse_tables(txt):
        if "code" in b["header"] and "name" in b["header"]:
            for r in _records(b):
                if r["code"].startswith("us"):
                    out.append((r["code"], r["name"]))
    return out


def quotes(codes):
    """批量行情。实测 503 只一次 1.4 秒，650 只一批不会超命令行长度。"""
    out = {}
    for i in range(0, len(codes), 650):
        txt = _run(["quote", ",".join(codes[i:i + 650])])
        b = _block(txt, "change_percent")
        if b:
            for r in _records(b):
                out[r["code"]] = r
    return out


def kline_closes(code, days=260):
    """返回 [(date, close)] 正序。kline 输出为倒序，需翻转。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(days * 1.65))
    txt = _run(["kline", code, "--period", "day",
                "--start", start.isoformat(), "--end", end.isoformat()])
    b = _block(txt, "date")
    if not b:
        return []
    rows = []
    for r in _records(b):
        c = num(r.get("last"))
        d = r.get("date", "")
        if c and d:
            rows.append((d, c))
    rows.sort(key=lambda x: x[0])
    return rows[-days:]


def kline_many(codes, days=260):
    with ThreadPoolExecutor(max_workers=5) as ex:
        fut = {c: ex.submit(kline_closes, c, days) for c in codes}
        return {c: f.result() for c, f in fut.items()}


# ---------------------------------------------------------------- ① CNN 恐惧贪婪
FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FNG_HEAD = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cnn.com/",
    "Origin": "https://www.cnn.com",
}


def fng_label(s):
    if s is None:
        return "—"
    if s < 25:
        return "极度恐惧"
    if s < 45:
        return "恐惧"
    if s < 55:
        return "中性"
    if s < 75:
        return "贪婪"
    return "极度贪婪"


def fetch_fng():
    import requests
    s = requests.Session()
    s.trust_env = False        # 系统代理会拦掉 CNN，必须绕开
    last = None
    for _ in range(3):
        try:
            r = s.get(FNG_URL, headers=FNG_HEAD, timeout=25)
            if r.status_code == 200 and r.content[:1] == b"{":
                return r.json()
            last = "http %d / 非 JSON（可能被反爬）" % r.status_code
        except Exception as e:  # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
    raise RuntimeError("CNN 恐惧贪婪取数失败：%s" % last)


def fng_pack(raw):
    fg = raw.get("fear_and_greed") or {}
    score = num(fg.get("score"))
    ts = fg.get("timestamp") or ""
    parts = []
    for key, cn, desc in FNG_PARTS:
        v = raw.get(key) or {}
        parts.append({"k": cn, "desc": desc, "score": num(v.get("score")),
                      "rating": v.get("rating") or ""})
    hist = []
    for p in (raw.get("fear_and_greed_historical") or {}).get("data") or []:
        try:
            d = datetime.datetime.fromtimestamp(p["x"] / 1000, datetime.timezone.utc)
            d = d + datetime.timedelta(hours=-4)      # 近似美东时间，只用于取日期
            hist.append({"d": d.strftime("%Y-%m-%d"), "v": round(float(p["y"]), 1)})
        except Exception:  # noqa: BLE001
            continue
    # 同一交易日可能多条，取最后一条
    uniq = {}
    for h in hist:
        uniq[h["d"]] = h["v"]
    hist = [{"d": k, "v": v} for k, v in sorted(uniq.items())]

    return {
        "score": round(score, 1) if score is not None else None,
        "label": fng_label(score),
        "raw_rating": (fg.get("rating") or "").title(),
        "ts": ts,
        "date": ts[:10] if ts else "",
        "prev_close": num(fg.get("previous_close")),
        "prev_week": num(fg.get("previous_1_week")),
        "prev_month": num(fg.get("previous_1_month")),
        "prev_year": num(fg.get("previous_1_year")),
        "parts": parts,
        "history": hist,
    }


def archive_fng(f):
    """每日存档。页面不做分位，但读数留档后可以随时补算。"""
    try:
        os.makedirs(OUTDIR, exist_ok=True)
        all_ = {}
        if os.path.exists(ARCHIVE):
            all_ = json.loads(open(ARCHIVE, encoding="utf-8").read())
        d = f.get("date") or datetime.date.today().isoformat()
        all_[d] = {"score": f["score"], "label": f["label"],
                   "parts": {p["k"]: p["score"] for p in f["parts"]}}
        with open(ARCHIVE, "w", encoding="utf-8") as fh:
            json.dump(all_, fh, ensure_ascii=False, indent=0, sort_keys=True)
        return len(all_)
    except Exception:  # noqa: BLE001  存档失败不影响主流程
        return 0


# ---------------------------------------------------------------- ② 涨跌榜
def rank_list(rows, key, n=10, reverse=True):
    """rows: [{code,name,chg,amt,mcap,pools}]，剔除无成交额与 ETF。"""
    pool = [r for r in rows
            if r["chg"] is not None and r["amt"] > 0
            and "ETF" not in r["name"].upper() and "ETN" not in r["name"].upper()]
    pool.sort(key=lambda r: r[key], reverse=reverse)
    return pool[:n]


# ---------------------------------------------------------------- ③ 板块
def sector_rows(q):
    out = []
    for code, cn in SECTORS:
        r = q.get(code) or {}
        hi, lo, px = num(r.get("high_52week")), num(r.get("low_52week")), num(r.get("price"))
        pos = None
        if hi and lo and px and hi > lo:
            pos = round((px - lo) / (hi - lo) * 100, 1)
        out.append({"code": code, "name": cn, "px": px,
                    "chg": num(r.get("change_percent")),
                    "chg5": num(r.get("chg_5d")), "chg20": num(r.get("chg_20d")),
                    "chg60": num(r.get("chg_60d")), "ytd": num(r.get("chg_ytd")),
                    "pos52": pos, "amt": _f(r.get("amount"))})
    out.sort(key=lambda x: -(x["chg"] if x["chg"] is not None else -99))
    return out


# ---------------------------------------------------------------- ⑤⑦ 相对强弱
def ratio_curve(hi_codes, lo_codes, closes, n=120):
    """两组合成净值之比。每组内等权，起点归一为 100。"""
    codes = list(hi_codes) + list(lo_codes)
    dates = None
    for c in codes:
        ds = {d for d, _ in closes.get(c) or []}
        dates = ds if dates is None else (dates & ds)
    if not dates:
        return []
    ds = sorted(dates)[-n:]
    base = {}
    for c in codes:
        m = dict(closes.get(c) or [])
        try:
            b = m[ds[0]]
        except KeyError:
            return []
        if not b:
            return []
        base[c] = b
    out = []
    for d in ds:
        try:
            hv = sum(dict(closes[c])[d] / base[c] for c in hi_codes) / len(hi_codes)
            lv = sum(dict(closes[c])[d] / base[c] for c in lo_codes) / len(lo_codes)
        except KeyError:
            continue
        if lv:
            out.append({"d": d, "v": round(hv / lv * 100, 2)})
    return out


def rel_perf(hi_codes, lo_codes, closes):
    """区间相对收益：慢组涨幅减快组涨幅（百分点）。"""
    res = {}
    for k, days in (("r5", 5), ("r20", 20), ("r60", 60)):
        vals = []
        for c in list(hi_codes) + list(lo_codes):
            s = closes.get(c) or []
            if len(s) <= days:
                vals = []
                break
            vals.append((s[-1][1] / s[-1 - days][1] - 1) * 100)
        if len(vals) == len(list(hi_codes) + list(lo_codes)):
            nh = len(hi_codes)
            res[k] = round(sum(vals[:nh]) / nh - sum(vals[nh:]) / len(lo_codes), 2)
        else:
            res[k] = None
    return res


# ---------------------------------------------------------------- 组装
def us_session(now=None):
    """美东时间与交易时段判断。"""
    try:
        from zoneinfo import ZoneInfo
        et = datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        et = (now or datetime.datetime.utcnow()) - datetime.timedelta(hours=4)
    mins = et.hour * 60 + et.minute
    open_ = et.weekday() < 5 and 570 <= mins < 960
    return et, open_


def build():
    now = datetime.datetime.now()
    et, in_session = us_session(now)

    # ---- 宇宙：三个指数去重 + 中概过滤
    seen = {}
    for pname, idx in POOLS:
        try:
            for c, n in constituents(idx):
                r = seen.setdefault(c, {"code": c, "name": n, "pools": []})
                if pname not in r["pools"]:
                    r["pools"].append(pname)
        except Exception:  # noqa: BLE001  单个指数失败不该拖垮整页
            continue
    main_codes = list(seen.keys())
    if not main_codes:
        raise RuntimeError("三个指数成分股均未取到")

    q = quotes(main_codes)

    cn_raw = []
    try:
        cn_raw = china_pool()
    except Exception:  # noqa: BLE001
        pass
    cn_filtered = 0
    if cn_raw:
        cn_q = quotes([c for c, _ in cn_raw])
        for c, n in cn_raw:
            r = cn_q.get(c)
            if not r or _f(r.get("amount")) < CN_MIN_AMT:
                continue
            cn_filtered += 1
            if c in seen:
                seen[c]["pools"].append("中概")
            else:
                seen[c] = {"code": c, "name": n, "pools": ["中概"]}
            q[c] = r

    # ---- 每只标的整理成扁平记录
    rows = []
    for c, meta in seen.items():
        r = q.get(c)
        if not r:
            continue
        rows.append({
            "code": c, "name": meta["name"], "pools": meta["pools"],
            "chg": num(r.get("change_percent")),
            "px": num(r.get("price")),
            "amt": _f(r.get("amount")),
            "mcap": _f(r.get("total_market_cap")),
            "chg5": num(r.get("chg_5d")), "chg20": num(r.get("chg_20d")),
            "chg60": num(r.get("chg_60d")), "ytd": num(r.get("chg_ytd")),
            "hi52": num(r.get("high_52week")), "lo52": num(r.get("low_52week")),
            "time": r.get("time") or "",
        })

    dates = [r["time"] for r in rows if r["time"]]
    date = max(dates) if dates else et.strftime("%Y-%m-%d")

    # ---- ④ 宽度（分池）
    pools = []
    for pname, _ in POOLS:
        sub = [r for r in rows if pname in r["pools"]]
        pools.append(_breadth(pname, sub, hint=CN_HINT.get(pname)))
    cn_sub = [r for r in rows if "中概" in r["pools"]]
    pools.append(_breadth("中概", cn_sub,
                          hint="成交额≥%.0f万，%d/%d 只" % (CN_MIN_AMT / 1e4, cn_filtered,
                                                          len(cn_raw) or 0)))
    overall = _breadth("四池合计", rows)

    # ---- ② 涨跌榜
    gainers = rank_list(rows, "chg", 10, True)
    losers = rank_list(rows, "chg", 10, False)

    # ---- ETF / Mag7 / 宽基
    etf_codes = [c for c, _ in SECTORS] + ["usSPY", "usQQQ", "usDIA", "usSOXX"] \
        + [c for c, _ in MAG7] + DEFENSIVE + OFFENSIVE
    eq = quotes(sorted(set(etf_codes)))

    sectors = sector_rows(eq)

    mag7 = []
    for code, cn in MAG7:
        r = eq.get(code) or {}
        mag7.append({"code": code, "name": cn, "chg": num(r.get("change_percent")),
                     "px": num(r.get("price")), "mcap": _f(r.get("total_market_cap")),
                     "ytd": num(r.get("chg_ytd")),
                     "hi52": num(r.get("high_52week"))})
    tot_mcap = sum(m["mcap"] for m in mag7) or 1
    for m in mag7:
        m["w"] = round(m["mcap"] / tot_mcap * 100, 1)
        m["contrib"] = round((m["chg"] or 0) * m["mcap"] / tot_mcap, 3)
    mag7.sort(key=lambda m: -(m["chg"] if m["chg"] is not None else -99))
    mag_avg = round(sum((m["chg"] or 0) * m["mcap"] for m in mag7) / tot_mcap, 2)

    spy = eq.get("usSPY") or {}
    qqq = eq.get("usQQQ") or {}
    soxx = eq.get("usSOXX") or {}
    dia = eq.get("usDIA") or {}
    index = [{"name": "标普500", "etf": "SPY", "px": num(spy.get("price")),
              "chg": num(spy.get("change_percent")), "amt": _f(spy.get("amount")) / 1e8},
             {"name": "纳斯达克100", "etf": "QQQ", "px": num(qqq.get("price")),
              "chg": num(qqq.get("change_percent")), "amt": _f(qqq.get("amount")) / 1e8},
             {"name": "道琼斯", "etf": "DIA", "px": num(dia.get("price")),
              "chg": num(dia.get("change_percent")), "amt": _f(dia.get("amount")) / 1e8},
             {"name": "费城半导体", "etf": "SOXX", "px": num(soxx.get("price")),
              "chg": num(soxx.get("change_percent")), "amt": _f(soxx.get("amount")) / 1e8}]

    # ---- ⑤⑥⑦ 曲线
    need = sorted(set(DEFENSIVE + OFFENSIVE + ["usSPY", "usSOXX"]))
    closes = {}
    try:
        closes = kline_many(need)
    except Exception:  # noqa: BLE001
        pass
    def_off = ratio_curve(DEFENSIVE, OFFENSIVE, closes)
    semi = ratio_curve(["usSOXX"], ["usSPY"], closes)

    defensive = {
        "curve": def_off,
        "rel": rel_perf(DEFENSIVE, OFFENSIVE, closes),
        "def_now": _avg_chg(eq, DEFENSIVE),
        "off_now": _avg_chg(eq, OFFENSIVE),
        "def20": _avg_key(eq, DEFENSIVE, "chg_20d"),
        "off20": _avg_key(eq, OFFENSIVE, "chg_20d"),
        "def5": _avg_key(eq, DEFENSIVE, "chg_5d"),
        "off5": _avg_key(eq, OFFENSIVE, "chg_5d"),
    }
    semi_block = {
        "curve": semi,
        "rel": rel_perf(["usSOXX"], ["usSPY"], closes),
        "soxx": {"chg": num(soxx.get("change_percent")),
                 "chg20": num(soxx.get("chg_20d")), "chg60": num(soxx.get("chg_60d")),
                 "ytd": num(soxx.get("chg_ytd"))},
        "spy": {"chg": num(spy.get("change_percent")),
                "chg20": num(spy.get("chg_20d")), "chg60": num(spy.get("chg_60d")),
                "ytd": num(spy.get("chg_ytd"))},
    }

    # ---- ⑧ 52 周新高新低（只统计美股主池，中概 ADR 的 52 周口径不齐）
    main_set = set(main_codes)
    hi_list, lo_list, pos_list = [], [], []
    for r in rows:
        if r["code"] not in main_set or not r["px"] or not r["hi52"] or not r["lo52"]:
            continue
        if r["px"] >= r["hi52"] * 0.995:
            hi_list.append(r)
        if r["px"] <= r["lo52"] * 1.005:
            lo_list.append(r)
        if r["hi52"] > r["lo52"]:
            pos_list.append((r["px"] - r["lo52"]) / (r["hi52"] - r["lo52"]) * 100)
    pos_list.sort()
    highlow = {
        "nh": len(hi_list), "nl": len(lo_list), "total": len(pos_list),
        "med_pos": round(pos_list[len(pos_list) // 2], 1) if pos_list else None,
        "hi_sample": [{"name": x["name"], "code": x["code"], "chg": x["chg"],
                       "ytd": x["ytd"]} for x in sorted(
                           hi_list, key=lambda x: -(x["mcap"] or 0))[:8]],
        "lo_sample": [{"name": x["name"], "code": x["code"], "chg": x["chg"],
                       "ytd": x["ytd"]} for x in sorted(
                           lo_list, key=lambda x: -(x["mcap"] or 0))[:8]],
    }

    # ---- ① F&G
    fng = None
    archive_days = 0
    try:
        fng = fng_pack(fetch_fng())
        archive_days = archive_fng(fng)
    except Exception as e:  # noqa: BLE001
        print("[警告] CNN 恐惧贪婪不可用：%s" % e)
    if fng:
        fng["archive_days"] = archive_days

    payload = {
        "date": date,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "et": et.strftime("%Y-%m-%d %H:%M"),
        "in_session": in_session,
        "phase": "盘中快照" if in_session else "已收盘",
        "universe": {"main": len(main_codes), "cn_raw": len(cn_raw),
                     "cn_kept": cn_filtered, "total": len(rows)},
        "index": index,
        "fng": fng,
        "gainers": gainers, "losers": losers,
        "sectors": sectors,
        "breadth": {"pools": pools, "all": overall},
        "defensive": defensive,
        "mag": {"rows": mag7, "avg": mag_avg,
                "qqq": num(qqq.get("change_percent"))},
        "semi": semi_block,
        "highlow": highlow,
    }
    return payload


def _avg_chg(q, codes):
    v = [num((q.get(c) or {}).get("change_percent")) for c in codes]
    v = [x for x in v if x is not None]
    return round(sum(v) / len(v), 2) if v else None


def _avg_key(q, codes, key):
    v = [num((q.get(c) or {}).get(key)) for c in codes]
    v = [x for x in v if x is not None]
    return round(sum(v) / len(v), 2) if v else None


def _breadth(name, sub, hint=None):
    up = sum(1 for r in sub if (r["chg"] or 0) > 0)
    dn = sum(1 for r in sub if (r["chg"] or 0) < 0)
    fl = len(sub) - up - dn
    p5 = sum(1 for r in sub if (r["chg"] or 0) >= 5)
    m5 = sum(1 for r in sub if (r["chg"] or 0) <= -5)
    chgs = [r["chg"] for r in sub if r["chg"] is not None]
    return {"name": name, "up": up, "down": dn, "flat": fl, "total": len(sub),
            "p5": p5, "m5": m5,
            "avg": round(sum(chgs) / len(chgs), 2) if chgs else None,
            "up_pct": round(up / len(sub) * 100, 1) if sub else None,
            "hint": hint}


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
    f = p["fng"] or {}
    print("美股 %s（%s）｜标的 %d 只（主池 %d + 中概 %d/%d）"
          % (p["date"], p["phase"], p["universe"]["total"],
             p["universe"]["main"], p["universe"]["cn_kept"], p["universe"]["cn_raw"]))
    print("F&G %.1f [%s]  前收 %.1f  一周前 %.1f  存档 %d 天"
          % (f.get("score") or 0, f.get("label"), f.get("prev_close") or 0,
             f.get("prev_week") or 0, f.get("archive_days") or 0))
    b = p["breadth"]["all"]
    print("宽度：涨 %d / 跌 %d / 平 %d｜涨超5%% %d、跌超5%% %d"
          % (b["up"], b["down"], b["flat"], b["p5"], b["m5"]))
    print("新高 %d / 新低 %d｜Mag7 加权 %+.2f%% vs QQQ %+.2f%%"
          % (p["highlow"]["nh"], p["highlow"]["nl"], p["mag"]["avg"],
             p["mag"]["qqq"] or 0))
    print("已写入 %s" % OUT_JSON)
