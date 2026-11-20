# -*- coding: utf-8 -*-
"""Regime 模块取数。

只用公开、无需 key 的源：
  FRED CSV 直连（fredgraph.csv）—— 宏观与产业
  CBOE CDN —— VIX / VIX3M
  Yahoo chart —— 六类资产与辅助标的

设计原则：
  1. 抓不到不报错中断，落进 errors，由 score.py 标「未获取」并从分里剔除。
  2. 只保留最近 KEEP_DAYS 天：分位窗口是 5 年，多留一年做缓冲。
  3. 原始响应落在 data/raw/，出问题能直接看原文。

用法：
    python fetch.py               全抓
    python fetch.py --only=fred   只抓 FRED
    python fetch.py --only=price  只抓行情
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
RAW = os.path.join(DATA, "raw")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
TZ = datetime.timezone(datetime.timedelta(hours=8))
KEEP_DAYS = 2200          # 约 6 年

FRED = [
    # id           名称                       dir  单位  所属模块
    ("T10Y2Y",      "10Y-2Y 期限差",            +1,  "pp",  "macro"),
    ("DFII10",      "10Y 实际利率",             -1,  "%",   "macro"),
    ("T10YIE",      "10Y 通胀预期",              0,  "%",   "macro"),
    ("PCEPILFE",    "核心 PCE 物价指数",        -1,  "idx", "macro"),
    ("UNRATE",      "失业率",                   -1,  "%",   "macro"),
    ("NFCI",        "芝加哥联储金融条件",       -1,  "idx", "macro"),
    ("DTWEXBGS",    "广义美元指数",             -1,  "idx", "macro"),
    ("DGS2",        "2Y 国债收益率",            -1,  "%",   "macro"),
    ("NEWORDER",    "核心资本品新订单",         +1,  "$M",  "industry"),
    ("AMTMNO",      "制造业新订单",             +1,  "$M",  "industry"),
    ("TCU",         "工业产能利用率",            0,  "%",   "industry"),
    ("ISRATIO",     "库存/销售比",              -1,  "x",   "industry"),
    ("BAMLH0A0HYM2", "高收益债 OAS",            -1,  "pp",  "industry"),
]

# asset 非空表示它是六类资产之一，参与矩阵与技术打分
PRICES = [
    ("QQQ",      "纳指100 ETF",      "us_eq"),
    ("TLT",      "20年+ 美债 ETF",   "us_bond"),
    ("GLD",      "黄金 ETF",         "gold"),
    ("DX-Y.NYB", "美元指数",         "usd"),
    ("CPER",     "铜 ETF",           "ind_metal"),
    ("FXI",      "中国大盘 ETF",     "cn_eq"),
    ("^HSI",     "恒生指数",         "cn_eq_aux"),
    ("SPY",      "标普500 ETF",      "ref"),
    ("SMH",      "半导体 ETF",       "ref"),
    ("HG=F",     "COMEX 铜",         "ref"),
    ("GC=F",     "COMEX 黄金",       "ref"),
]

ASSETS = [
    ("us_eq",     "美股",     "QQQ"),
    ("us_bond",   "美债",     "TLT"),
    ("gold",      "黄金",     "GLD"),
    ("usd",       "美元",     "DX-Y.NYB"),
    ("ind_metal", "工业金属", "CPER"),
    ("cn_eq",     "A股/港股", "FXI"),
]


# 必须绕过系统代理：本机代理对 FRED 不通，urllib 默认读环境代理会全部超时，
# 而 curl --noproxy '*' 是通的。所有源统一走这个 opener。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, timeout=30, browser=True):
    """browser=False 用于 FRED。

    FRED 的 WAF 对「完整 Chrome UA + Python 的 TLS 指纹」这个组合会挂起不响应
    （实测：带 Chrome UA 12.7 秒超时，不带 UA 1.2 秒返回；同一 UA 访问百度正常）。
    这不是代理问题——已绕过系统代理仍复现。所以 FRED 走最小请求头，
    让 urllib 用默认 Python-urllib UA，雅虎与 CBOE 则相反，必须带浏览器 UA。
    """
    headers = {"Accept": "text/csv,application/json,*/*"}
    if browser:
        headers["User-Agent"] = UA
        headers["Accept-Language"] = "en-US,en;q=0.9"
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read()


def _cut(dates, *cols):
    """只保留最近 KEEP_DAYS 天。返回 (dates, [cols])。"""
    if not dates:
        return dates, cols
    lo = (datetime.date.fromisoformat(dates[-1])
          - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    idx = [i for i, d in enumerate(dates) if d >= lo]
    return [dates[i] for i in idx], [[c[i] for i in idx] for c in cols]


def _cache(name, body):
    os.makedirs(RAW, exist_ok=True)
    with open(os.path.join(RAW, name), "wb") as f:
        f.write(body)


def fred_one(sid):
    """FRED CSV：observation_date,<id>；缺失值写作 '.'"""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % sid
    body = _get(url, browser=False)
    _cache("fred_%s.csv" % sid, body)
    text = body.decode("utf-8-sig", "replace")
    dates, vals = [], []
    for ln in text.splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if not d or v in ("", "."):
            continue
        try:
            vals.append(float(v))
        except ValueError:
            continue
        dates.append(d)
    dates, (vals,) = _cut(dates, vals)
    return {"dates": dates, "values": vals}


def cboe_one(name):
    """CBOE CSV：DATE,OPEN,HIGH,LOW,CLOSE；日期是 MM/DD/YYYY，且要跟随重定向。"""
    url = ("https://cdn.cboe.com/api/global/us_indices/daily_prices/"
           "%s_History.csv" % name)
    body = _get(url)
    _cache("cboe_%s.csv" % name, body)
    text = body.decode("utf-8-sig", "replace")
    dates, vals = [], []
    for ln in text.splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 5:
            continue
        try:
            m, d, y = parts[0].strip().split("/")
            iso = "%04d-%02d-%02d" % (int(y), int(m), int(d))
            vals.append(float(parts[4]))
        except (ValueError, IndexError):
            continue
        dates.append(iso)
    dates, (vals,) = _cut(dates, vals)
    return {"dates": dates, "values": vals}


def yahoo_one(ticker):
    """Yahoo chart：日线 OHLCV。注意 chartPreviousClose 是窗口起点，不是昨收。"""
    t = urllib.parse.quote(ticker, safe="")
    url = ("https://query2.finance.yahoo.com/v8/finance/chart/%s"
           "?range=3y&interval=1d&includePrePost=false" % t)
    body = _get(url, timeout=35)
    _cache("yahoo_%s.json" % ticker.replace("=", "_").replace("^", "idx_"), body)
    j = json.loads(body.decode("utf-8", "replace"))
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("no result: %s" % ((j.get("chart") or {}).get("error")))
    r0 = res[0]
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    out = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for i, t0 in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        d = datetime.datetime.fromtimestamp(t0, TZ).date().isoformat()
        out["dates"].append(d)
        out["open"].append(_f(q.get("open"), i))
        out["high"].append(_f(q.get("high"), i))
        out["low"].append(_f(q.get("low"), i))
        out["close"].append(float(c))
        out["volume"].append(_f(q.get("volume"), i))
    if not out["dates"]:
        raise ValueError("empty series")
    # 价格序列不裁：3 年已够 200DMA 与 52 周高，且方便判断长趋势
    out["meta"] = {"currency": (r0.get("meta") or {}).get("currency"),
                   "last": out["close"][-1], "last_date": out["dates"][-1]}
    return out


def _f(arr, i):
    if not arr or i >= len(arr) or arr[i] is None:
        return None
    return float(arr[i])


def main():
    only = ""
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]

    t0 = time.time()
    os.makedirs(DATA, exist_ok=True)

    # --only 只更新该源，其余原样保留：否则单抓 FRED 会把行情清空，
    # 单抓行情会把 FRED 清空，改完一处反而弄丢另一处。
    prev = {}
    pj = os.path.join(DATA, "series.json")
    if only and os.path.exists(pj):
        try:
            with open(pj, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:  # noqa: BLE001
            prev = {}
    series = dict(prev.get("series") or {})
    prices = dict(prev.get("prices") or {})
    errors = []

    jobs = []
    if only in ("", "fred"):
        jobs += [("fred", sid) for sid, *_ in FRED]
    if only in ("", "cboe"):
        jobs += [("cboe", n) for n in ("VIX", "VIX3M")]
    if only in ("", "price"):
        jobs += [("price", tk) for tk, *_ in PRICES]

    def run(job):
        kind, key = job
        try:
            if kind == "fred":
                return job, fred_one(key), None
            if kind == "cboe":
                return job, cboe_one(key), None
            return job, yahoo_one(key), None
        except Exception as e:  # noqa: BLE001
            return job, None, "%s: %s" % (type(e).__name__, str(e)[:110])

    with ThreadPoolExecutor(max_workers=6) as ex:
        for (kind, key), res, err in ex.map(run, jobs):
            if err:
                errors.append({"kind": kind, "id": key, "error": err})
                continue
            if kind == "fred":
                meta = next(x for x in FRED if x[0] == key)
                series[key] = {"name": meta[1], "dir": meta[2], "unit": meta[3],
                               "mod": meta[4], "src": "FRED",
                               "n": len(res["dates"]), **res}
            elif kind == "cboe":
                series[key] = {"name": key, "dir": 0, "unit": "idx", "mod": "internal",
                               "src": "CBOE", "n": len(res["dates"]), **res}
            else:
                meta = next(x for x in PRICES if x[0] == key)
                prices[key] = {"name": meta[1], "asset": meta[2], "src": "Yahoo", **res}

    out = {
        "fetched_at": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "keep_days": KEEP_DAYS,
        "series": series,
        "prices": prices,
        "errors": errors,
    }
    with open(os.path.join(DATA, "series.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")

    tail = {}
    for k, v in series.items():
        tail[k] = (v["dates"][-1] if v["dates"] else "-", v["values"][-1] if v["values"] else None)
    for k, v in prices.items():
        tail[k] = (v["dates"][-1], round(v["close"][-1], 3))

    print("fetched %d series / %d prices in %.1fs" % (len(series), len(prices), time.time() - t0))
    for e in errors:
        print("  [未获取] %-6s %-14s %s" % (e["kind"], e["id"], e["error"]))
    if "-v" in sys.argv:
        for k in sorted(tail):
            print("  %-14s %s %s" % (k, tail[k][0], tail[k][1]))
    return 0 if not errors else 0


if __name__ == "__main__":
    sys.exit(main())
