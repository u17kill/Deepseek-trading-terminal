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
import csv
import datetime
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import zipfile
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
    # mod="local"：不进任何模块分，只用于「本地政策利率 vs 美国」的对照表。
    # 这三条是亚太资产各自央行的口径，用来说明「美国模块分代表不了本地政策」。
    # 台湾与印度在 FRED 没有可用序列（台湾无、印度 INTDSRINM193N 停在 2022-07），
    # 所以表里只有三个地区——凑不齐就空着，不拿别的口径顶。
    ("IR3TIB01JPM156N", "日本 3个月银行间利率",   0,  "%",   "local"),
    ("IR3TIB01KRM156N", "韩国 3个月银行间利率",   0,  "%",   "local"),
    ("IR3TIB01CNM156N", "中国 3个月银行间利率",   0,  "%",   "local"),
    ("IRSTCI01INM156N", "印度 隔夜拆借利率",      0,  "%",   "local"),
    ("DTB3",          "美国 3个月国库券",         0,  "%",   "local"),
    ("DFEDTARL",      "联储目标区间下限",         0,  "%",   "local"),
    ("DFEDTARU",      "联储目标区间上限",         0,  "%",   "local"),
]

# asset 非空表示它是矩阵里的资产之一，参与技术打分；"ref" / "cn_eq_aux" 只取数不进分。
# 全部走美元计价 ETF（或指数本身）。亚太用美股 ETF 而不是 ^N225 / ^KS11 这类本币指数：
# 本币序列把汇率和本地价格混在一起，横比会失真。ETF 跟踪 MSCI 系列，不是原指数。
PRICES = [
    ("SPY",      "标普500 ETF",      "us_large"),
    ("QQQ",      "纳指100 ETF",      "us_eq"),
    ("SOXX",     "半导体 ETF",       "semi"),
    ("TLT",      "20年+ 美债 ETF",   "us_bond"),
    ("DX-Y.NYB", "美元指数",         "usd"),
    ("GLD",      "黄金 ETF",         "gold"),
    ("SLV",      "白银 ETF",         "silver"),
    ("CPER",     "铜 ETF",           "copper"),
    ("USO",      "WTI 原油 ETF",     "oil"),
    ("FXI",      "中国大盘 ETF",     "cn_eq"),
    ("EWJ",      "日本 ETF",         "jp_eq"),
    ("EWY",      "韩国 ETF",         "kr_eq"),
    ("EWT",      "台湾 ETF",         "tw_eq"),
    ("INDA",     "印度 ETF",         "in_eq"),
    ("^HSI",     "恒生指数",         "cn_eq_aux"),
    ("SMH",      "半导体 ETF（对照）", "ref"),
    ("HG=F",     "COMEX 铜",         "ref"),
    ("GC=F",     "COMEX 黄金",       "ref"),
    ("CL=F",     "WTI 原油",         "ref"),
]

# 资产清单只放在 score.py 的 ASSETS 一处。这里原本也有一份，八个月没被引用过，
# 却会和 score.py 慢慢漂开——同一份清单写两遍，改一边忘另一边是这类模块的常见死法。


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


def local_rates():
    """央行政策利率：BIS 月度库 + 台湾央行官网。

    存在的理由：政策与宏观两个模块用的全是美国数据，把它们摊到日韩台印时，
    隐含假设「本地政策跟着美联储走」。这个假设现在不成立——日本一年加了 75bp、
    韩国 +50bp、印度 −25bp，美国按兵不动。所以需要一张本地的利率表，
    让人看到「美国模块分代表不了的那部分」有多大。

    两个源分工：
      BIS CBPOL 月度库覆盖 US/JP/KR/CN/IN，但**不含台湾**（试过，zip 里没有 TW 行）。
      台湾改抓央行官网的「重貼現率」，是一手来源，带生效日。
    """
    out = {"fetched_at": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
           "items": [], "errors": [], "note": ""}
    want = {"US": "美国", "JP": "日本", "KR": "韩国", "CN": "中国", "IN": "印度"}

    try:
        url = "https://data.bis.org/static/bulk/WS_CBPOL_csv_col.zip"
        # BIS CBPOL 是月度库，重抓一次要 20 秒上下。7 天内直接用本地副本——
        # 政策利率不会日内变，为了一个不动的数每天等 20 秒不划算。
        zp = os.path.join(RAW, "bis_cbpol.zip")
        if os.path.exists(zp) and time.time() - os.path.getmtime(zp) < 7 * 86400:
            with open(zp, "rb") as f:
                body = f.read()
        else:
            body = _get(url, timeout=60)
            _cache("bis_cbpol.zip", body)
        zf = zipfile.ZipFile(io.BytesIO(body))
        name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
        rows = list(csv.reader(io.StringIO(zf.read(name).decode("utf-8", "ignore"))))
        hdr = rows[0]
        dcols = [i for i, h in enumerate(hdr)
                 if len(h) == 7 and h[4] == "-" and h[:4].isdigit()]
        seen = set()
        for r in rows[1:]:
            if len(r) < 13:
                continue
            code = r[2]
            if code not in want or code in seen:
                continue
            vals = [(hdr[i], r[i]) for i in dcols if i < len(r) and r[i] not in ("", None)]
            if not vals:
                continue
            seen.add(code)
            ld, lv = vals[-1]
            # 12 个月前：取「不晚于一年前」的最后一条，避免月份缺失时取空
            cut = str(int(ld[:4]) - 1) + ld[4:]
            pd_, pv = next(((d, v) for d, v in reversed(vals) if d <= cut), (None, None))
            # 12 个月区间：只看窗口内（> cut 到最新）走过的最高/最低。
            # 光看「期末−期初」会把「降了又加回来」这种往返抹成 0，
            # 区间让中间波动藏不住。
            win = [(float(v), d) for d, v in vals if d > cut]
            hi = max(win) if win else None
            lo = min(win) if win else None
            out["items"].append({
                "code": code, "name": want[code], "rate": float(lv), "date": ld,
                "prev": float(pv) if pv not in (None, "") else None,
                "prev_date": pd_, "src": "BIS CBPOL",
                "hi": hi[0] if hi else None, "hi_date": hi[1] if hi else None,
                "lo": lo[0] if lo else None, "lo_date": lo[1] if lo else None})
    except Exception as e:  # noqa: BLE001
        out["errors"].append("BIS: %s: %s" % (type(e).__name__, str(e)[:90]))

    # 台湾：央行官网首页摘要块。SSL 证书链在本机校验不过，这只读一个数字，
    # 单独放行；抓不到就缺席，不拿别的口径顶。
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=ctx))
        req = urllib.request.Request("https://www.cbc.gov.tw/tw/lp-461-1.html",
                                     headers={"User-Agent": UA})
        body = op.open(req, timeout=25).read().decode("utf-8", "ignore")
        _cache("cbc_index.html", body.encode("utf-8"))
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
        m = re.search(r"重貼現率\s*(\d{4}-\d{2}-\d{2})\s*([\d.]+)\s*%", txt)
        if m:
            out["items"].append({
                "code": "TW", "name": "台湾", "rate": float(m.group(2)),
                "date": m.group(1), "prev": None, "prev_date": None,
                "src": "台湾央行", "note": "重貼現率，生效日"})
        else:
            out["errors"].append("台湾央行：未匹配到重貼現率")
    except Exception as e:  # noqa: BLE001
        out["errors"].append("台湾央行: %s: %s" % (type(e).__name__, str(e)[:90]))

    order = ["US", "JP", "KR", "CN", "TW", "IN"]
    out["items"].sort(key=lambda x: order.index(x["code"]) if x["code"] in order else 99)
    out["note"] = ("政策利率口径，不是国债收益率。美国一行的用处是当分母："
                   "别处看到的 4.x% 是 2Y 国债，两个数不是一回事。")
    return out


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

    # 本地政策利率：BIS 那个 zip 284KB，--only 时不重抓，沿用上一份。
    if only in ("", "local"):
        try:
            out["local_rates"] = local_rates()
        except Exception as e:  # noqa: BLE001
            out["local_rates"] = prev.get("local_rates") or {}
            errors.append({"kind": "local", "id": "policy_rates",
                           "error": "%s: %s" % (type(e).__name__, str(e)[:90])})
    else:
        out["local_rates"] = prev.get("local_rates") or {}
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
