"""市场总览（第一页，v1.8.0）。

职责
----
把「今天发生了什么」压缩成一个可直接阅读的页面，分三段：
  ① 今日市场      —— 判断文字（brief.json，由收盘后任务生成）
  ② 指数与大类资产 —— 用户可增删的资产清单 + 当日走势
  ③ 自选股        —— 纵轴市场 × 横轴板块

本文件只负责 ② ③ 的取数与组装；① 的文字由 brief.py 单独维护。

三条硬约束（都会写进 SPEC 红线）
--------------------------------
1. **westock quote 不能混品种批量。** 期货、外汇与股票放进同一批时，只有股票会返回，
   期货和外汇被**静默丢弃**——退出码仍是 0，只在末尾给一行提示。所以这里按
   eq / fu / fx 分三次调用，并且每次都核对返回条数，少一条就报错而不是少画一张卡。
2. **分时的「当天」是三种口径。** 股市 4~6.5 小时（244~393 行），期货与外汇约 23 小时
   （1300~1450 行），加密 24 小时。所以迷你图只能各自看形状，不能横比波动幅度。
3. **板块分类是本终端维护的，不是交易所口径。** westock 的板块只覆盖 A 股（申万），
   恒生分类指数取不到行情。认不出的标的落「未分类」，不猜。

配置与缓存分离
--------------
`config.json` 是**用户资产**（加了哪些资产、自选股清单、板块归类），必须入库；
`market.json` 是**行情缓存**，随时可重抓，不入库。这样换机器能恢复清单，
而不会被几十 MB 的分时数据污染 diff。
"""
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wsfeed  # noqa: E402
import apac  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "data", "market")
CFG = os.path.join(OUTDIR, "config.json")
CACHE = os.path.join(OUTDIR, "market.json")
BRIEF = os.path.join(OUTDIR, "brief.json")

SECTORS = ["科技", "半导体", "金融", "能源材料", "医药", "消费", "工业",
           "公用事业", "未分类"]

OKX = "https://www.okx.com/api/v5"
UA = {"User-Agent": "Mozilla/5.0"}

MIN_GAP = 0.12            # minute 接口实测 0.78s/次，节流到 0.12 只是为了不让连发太密
MIN_POOL = 5              # 分时并发数。20 个资产 × 0.78s ÷ 5 ≈ 3.2s
GI_TAIL = 20              # 新浪指数没有分时，用最近 20 个交易日代替（页面标注 20D）

# 默认资产清单：13 股指 + 4 商品 + 1 外汇 + 2 加密 = 20，刚好 5 列 4 行
DEFAULT_ASSETS = [
    ("eq", "sh000001", "上证指数", "股指"),
    ("eq", "sz399001", "深证成指", "股指"),
    ("eq", "sh000300", "沪深300", "股指"),
    ("eq", "hkHSI", "恒生指数", "股指"),
    ("eq", "hkHSTECH", "恒生科技", "股指"),
    ("eq", "usIVV", "标普500", "股指"),
    ("eq", "usDJI", "道琼斯", "股指"),
    ("fu", "fuNQ", "纳斯达克100", "股指"),
    ("gi", "NKY", "日经225", "股指"),
    ("gi", "KOSPI", "韩国 KOSPI", "股指"),
    ("gi", "TWJQ", "台湾加权", "股指"),
    ("gi", "SENSEX", "印度 SENSEX", "股指"),
    ("gi", "STI", "新加坡 STI", "股指"),
    ("fu", "fuGC", "COMEX 黄金", "商品"),
    ("fu", "fuSI", "COMEX 白银", "商品"),
    ("fu", "fuCL", "WTI 原油", "商品"),
    ("fu", "fuHG", "COMEX 铜", "商品"),
    ("fx", "fxDINIW", "美元指数", "外汇"),
    ("cr", "BTC-USDT", "比特币", "加密"),
    ("cr", "ETH-USDT", "以太坊", "加密"),
]

# 代码里带口径标注的，卡片右下角显示这个而不是代码
SRC_LABEL = {"usIVV": "IVV", "usDJI": "DIA", "fuNQ": "NQ", "fuES": "ES"}


# ---------------------------------------------------------------- 配置
def default_config():
    return {
        "version": 1,
        "assets": [{"kind": k, "code": c, "name": n, "cls": cl}
                   for k, c, n, cl in DEFAULT_ASSETS],
        "watchlist": [],          # 不预置个股：那是用户的持仓，不该由程序凭空决定
        "sectors": SECTORS,
        "sector_note": "行业归类由本终端维护，非交易所官方口径；认不出的落「未分类」",
    }


def load_config():
    if not os.path.exists(CFG):
        os.makedirs(OUTDIR, exist_ok=True)
        cfg = default_config()
        json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return cfg
    return json.load(open(CFG, encoding="utf-8"))


def save_config(cfg):
    os.makedirs(OUTDIR, exist_ok=True)
    json.dump(cfg, open(CFG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 底层
def _table(text):
    """把 westock 的 markdown 表格解析成 [{列名: 值}]。

    quote 对不同品种返回**完全不同的表头**（A股 40 列、期货 24 列、外汇 12 列），
    所以不能用列序号，只能按列名取。
    """
    # 外汇表的 marketStatus 单元格自带竖线（"2026-11-01 18:01:55|USDCNY_close_已收盘"），
    # 直接按 | 切会把一行切成 13 格、对不上 12 个表头，整行被丢掉。
    # 这里把「时间串后面的那个竖线」先替换成空格，再切。
    rows = [re.sub(r"(?<=\d{2}:\d{2}:\d{2})\|", " ", l)
            for l in (text or "").splitlines() if l.strip().startswith("|")]
    if len(rows) < 3:
        return []
    def cells(l):
        return [c.strip() for c in l.strip().strip("|").split("|")]
    head = cells(rows[0])
    out = []
    for l in rows[2:]:
        c = cells(l)
        if len(c) != len(head):
            continue
        out.append(dict(zip(head, c)))
    return out


def _num(x):
    try:
        v = float(str(x).replace(",", "").replace("%", ""))
        return v
    except Exception:
        return None


def _pick(row, names):
    for n in names:
        if n in row and row[n] not in ("", "-", "—"):
            return row[n]
    return None


_last_call = [0.0]


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_call[0] = time.time()


def ws(cmd, timeout=60):
    _throttle()
    r = subprocess.run([wsfeed.bin_path()] + cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=wsfeed.env())
    return r.stdout or ""


_QUOTE_DATES = []         # A 股 quote 自带的行情日期，用于判定快照属于哪个交易日


def _as_date(v):
    """把 quote 的 time 规整成 YYYY-MM-DD。认不出就返回 None，不猜。"""
    s = str(v or "").strip()
    if not s:
        return None
    s = s.replace("/", "-").split(" ")[0].split("T")[0]
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def snapshot_date():
    """本次快照的行情日期。取 A 股 quote 回传 time 的众数。

    A 股行情自带交易日，这是唯一权威口径；此前读 data/oos/result_latest.json
    的 last.date，那份文件由研究侧脚本产出、不随看板每日刷新，会把今天的
    快照错标成上一交易日。期货与外汇的 quote 不带 time，故不参与。
    """
    ds = [d for d in _QUOTE_DATES if d]
    if not ds:
        return None
    tally = {}
    for d in ds:
        tally[d] = tally.get(d, 0) + 1
    return sorted(tally.items(), key=lambda kv: (kv[1], kv[0]))[-1][0]


def quote(codes):
    """一次 quote。调用方必须已经保证 codes 属于同一品种大类。"""
    out = {}
    for i in range(0, len(codes), 150):
        chunk = codes[i:i + 150]
        text = ws(["quote", ",".join(chunk)])
        for row in _table(text):
            code = _pick(row, ["code"]) or ""
            price = _num(_pick(row, ["price", "lastPrice", "last"]))
            chg = _num(_pick(row, ["change_percent", "changePct", "change_pct"]))
            prev = _num(_pick(row, ["prev_close", "prevClose", "prevSettlement"]))
            if price is None:
                continue
            # 有些品种只给涨跌额不给涨跌幅，自己算
            if chg is None and prev:
                chg = (price / prev - 1) * 100
            out[code] = {"price": price, "chg": round(chg, 2) if chg is not None else None,
                         "name": _pick(row, ["name"]) or ""}
            if code.startswith(("sh", "sz")):
                d = _as_date(_pick(row, ["time"]))
                if d:
                    _QUOTE_DATES.append(d)
    return out


SPARK_MAX = 140           # 迷你图宽 150px、高 26px，140 个点已经看不出差别


def thin(pts, n=SPARK_MAX):
    """抽稀。期货分时 1381 个点，全量下发会让注入数据涨到 190KB，
    而这些点最终只画在 150×26 的框里。等距取样并保留最后一个点，
    保证曲线末端与卡片上的涨跌幅对齐。"""
    if len(pts) <= n:
        return pts
    step = len(pts) / (n - 1)
    out = [pts[int(i * step)] for i in range(n - 1)]
    out.append(pts[-1])
    return out


def minute(code):
    """当日分时。返回 [[HHMM, price]]，已抽稀。"""
    rows = _table(ws(["minute", code], timeout=40))
    pts = []
    for r in rows:
        t = _pick(r, ["time"])
        p = _num(_pick(r, ["price"]))
        if t and p is not None:
            pts.append([str(t), p])
    return thin(pts)


def okx_tickers(ids):
    out = {}
    try:
        r = requests.get(OKX + "/market/tickers", params={"instType": "SPOT"},
                         headers=UA, timeout=15)
        for t in (r.json().get("data") or []):
            if t.get("instId") in ids:
                last = float(t["last"])
                op = float(t.get("open24h") or last)
                out[t["instId"]] = {
                    "price": last,
                    "chg": round((last / op - 1) * 100, 2) if op else None,
                    "name": "",
                }
    except Exception as e:
        print("[警告] OKX tickers 失败：%s" % e)
    return out


def okx_candles(inst, n=24):
    try:
        r = requests.get(OKX + "/market/candles",
                         params={"instId": inst, "bar": "1H", "limit": n},
                         headers=UA, timeout=15)
        data = r.json().get("data") or []
        # 返回是倒序（最新在前），转成正序
        pts = [[time.strftime("%H%M", time.localtime(int(c[0]) / 1000)), float(c[4])]
               for c in reversed(data)]
        return thin(pts)
    except Exception as e:
        print("[警告] OKX 分时失败 %s：%s" % (inst, e))
        return []


# ---------------------------------------------------------------- 搜索
# 加密不在 westock 里，只能自己维护别名表；认不出的不猜
CRYPTO_ALIAS = {
    "btc": "BTC-USDT", "比特币": "BTC-USDT", "bitcoin": "BTC-USDT",
    "eth": "ETH-USDT", "以太坊": "ETH-USDT", "ethereum": "ETH-USDT",
    "sol": "SOL-USDT", "solana": "SOL-USDT",
    "doge": "DOGE-USDT", "狗狗币": "DOGE-USDT",
    "xrp": "XRP-USDT", "瑞波": "XRP-USDT",
}
CRYPTO_NAME = {"BTC-USDT": "比特币", "ETH-USDT": "以太坊", "SOL-USDT": "Solana",
               "DOGE-USDT": "狗狗币", "XRP-USDT": "瑞波"}


def _kind_of(code):
    c = code.lower()
    if c.startswith("fu"):
        return "fu"
    if c.startswith("fx"):
        return "fx"
    if "-USDT" in code.upper():
        return "cr"
    return "eq"


def search(q, kind="stock"):
    """搜索可添加的标的。

    kind=stock 只搜股票（自选股用）；kind=asset 搜指数 / 期货 / 外汇 / 加密（资产卡用）。
    返回 [{kind, code, name, hint}]，hint 是给用户看的口径说明（交易所 / 品种类别）。
    """
    q = (q or "").strip()
    if len(q) < 1:
        return []
    out, seen = [], set()

    def add(c, n, hint):
        if not c or c in seen:
            return
        seen.add(c)
        out.append({"kind": _kind_of(c), "code": c, "name": n or c, "hint": hint})

    if kind == "stock":
        for r in _table(ws(["search", q, "--type", "stock", "--limit", "12"])):
            add(_pick(r, ["code"]), _pick(r, ["name"]), _pick(r, ["type"]) or "")
        return out[:12]

    for t, hint in (("index", "指数"), ("futures", "期货"), ("forex", "外汇")):
        for r in _table(ws(["search", q, "--type", t, "--limit", "6"])):
            add(_pick(r, ["code"]), _pick(r, ["name"]),
                _pick(r, ["categoryLabel"]) or hint)
    key = q.lower()
    if key in CRYPTO_ALIAS:
        inst = CRYPTO_ALIAS[key]
        add(inst, CRYPTO_NAME.get(inst, inst), "加密 · OKX")
    return out[:14]


# ---------------------------------------------------------------- 组装
def build_assets(cfg, brief_assets=None):
    """按品种大类分批取数，再并发补分时。"""
    groups = {"eq": [], "fu": [], "fx": [], "cr": [], "gi": []}
    for a in cfg["assets"]:
        groups.setdefault(a["kind"], []).append(a)

    quotes = {}
    for kind in ("eq", "fu", "fx"):
        if not groups.get(kind):
            continue
        codes = [a["code"] for a in groups[kind]]
        got = quote(codes)
        # 硬约束 1：少一条就是被静默丢弃了，宁可报错也不画一张空卡
        missing = [c for c in codes if c not in got]
        if missing:
            raise RuntimeError("%s 类行情缺失 %d 个：%s —— 混批会被静默丢弃，"
                               "请检查是否混入了其他品种"
                               % (kind, len(missing), ",".join(missing[:5])))
        quotes.update(got)

    if groups.get("cr"):
        quotes.update(okx_tickers({a["code"] for a in groups["cr"]}))

    if groups.get("gi"):
        for a in groups["gi"]:
            try:
                df = apac.sina_daily(a["code"])
                row = df.iloc[-1]
                prev = float(df["close"].iloc[-2])
                quotes[a["code"]] = {
                    "price": round(float(row["close"]), 2),
                    "chg": round((float(row["close"]) / prev - 1) * 100, 2),
                    "name": a["name"],
                    "_daily": thin([round(float(x), 2) for x in df["close"].tail(GI_TAIL)]),
                }
            except Exception as e:
                print("[警告] %s 日线失败：%s" % (a["code"], e))

    # 分时并发
    todo = [a for a in cfg["assets"] if a["kind"] in ("eq", "fu", "fx")]
    with ThreadPoolExecutor(max_workers=MIN_POOL) as ex:
        mins = dict(zip([a["code"] for a in todo],
                        ex.map(lambda a: minute(a["code"]), todo)))

    cards = []
    for a in cfg["assets"]:
        q = quotes.get(a["code"])
        if not q:
            continue
        card = {
            "kind": a["kind"], "code": a["code"],
            # 配置里的名字优先：westock 回的是「标普500 ETF-iShares」「纳斯达克100指数」，
            # 卡片上放不下，也不是我们想显示的称呼
            "name": a["name"] or q.get("name"), "cls": a["cls"],
            "price": q["price"], "chg": q["chg"],
            "src": SRC_LABEL.get(a["code"], ""),
        }
        if a["kind"] == "cr":
            card["spark"] = {"type": "minute", "pts": okx_candles(a["code"])}
            card["src"] = "OKX"
        elif a["kind"] == "gi":
            card["spark"] = {"type": "daily", "pts": q.get("_daily") or []}
            card["src"] = "20D"
        else:
            card["spark"] = {"type": "minute", "pts": mins.get(a["code"]) or []}
        cards.append(card)
    return cards


def build_watchlist(cfg):
    """按市场分批 quote；行业来自配置，认不出落「未分类」。"""
    wl = cfg.get("watchlist") or []
    if not wl:
        return {"rows": [], "by_market": {}, "sectors": cfg["sectors"],
                "count": 0, "note": "尚无自选股，可用「批量导入」一次粘贴代码清单"}

    def mkt(code):
        c = code.lower()
        if c.startswith(("sh", "sz", "bj")):
            return "A股"
        if c.startswith("hk"):
            return "港股"
        if c.startswith("us"):
            return "美股"
        return "其他"

    buckets = {}
    for s in wl:
        buckets.setdefault(mkt(s["code"]), []).append(s)

    rows = []
    for m, items in buckets.items():
        got = quote([s["code"] for s in items])
        for s in items:
            q = got.get(s["code"])
            if not q:
                continue
            sector = s.get("sector") or "未分类"
            if sector not in cfg["sectors"]:
                sector = "未分类"
            rows.append({"code": s["code"], "name": s.get("name") or q["name"],
                         "market": m, "sector": sector,
                         "price": q["price"], "chg": q["chg"]})

    by_market, by_sector = {}, {}
    for r in rows:
        by_market.setdefault(r["market"], []).append(r)
        by_sector.setdefault(r["sector"], []).append(r)

    up = sum(1 for r in rows if (r["chg"] or 0) > 0)
    return {"rows": rows, "by_market": {"ORDER": sorted(by_market.keys()), **by_market},
            "by_sector": {"ORDER": [s for s in cfg["sectors"] if s in by_sector], **by_sector},
            "sectors": [s for s in cfg["sectors"] if s in by_sector],
            "all_sectors": cfg["sectors"],
            "markets": sorted(by_market.keys()),
            "count": len(rows), "up": up, "down": len(rows) - up}


def load_brief(now_date):
    """判断文字。跨日即失效：日期不一致时页面必须降级标注，不能装作是今天的。"""
    if not os.path.exists(BRIEF):
        return {"as_of": None, "stale": True, "text": None}
    try:
        b = json.load(open(BRIEF, encoding="utf-8"))
    except Exception:
        return {"as_of": None, "stale": True, "text": None}
    b["stale"] = b.get("as_of") != now_date
    return b


def build(save=True):
    t0 = time.time()
    cfg = load_config()
    _QUOTE_DATES.clear()
    cards = build_assets(cfg)
    wl = build_watchlist(cfg)
    # 数据日期以 A 股 quote 自带交易日为准，取不到才退回研究缓存
    date = snapshot_date() or _last_date()
    data = {
        "date": date,
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "elapsed": round(time.time() - t0, 1),
        "assets": cards,
        "counts": {"all": len(cards),
                   "股指": sum(1 for c in cards if c["cls"] == "股指"),
                   "商品": sum(1 for c in cards if c["cls"] == "商品"),
                   "外汇": sum(1 for c in cards if c["cls"] == "外汇"),
                   "加密": sum(1 for c in cards if c["cls"] == "加密")},
        "watchlist": wl,
        "brief": load_brief(date),
    }
    if save:
        save_state(data)
    return data


def save_state(data):
    os.makedirs(OUTDIR, exist_ok=True)
    json.dump(data, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)


def apply_order(state, cfg):
    """只按配置重排顺序，不重新取数。

    拖动卡片改的是「排列」，价格与分时一个字节都没变。为此重跑 12 秒的全量取数
    既是浪费，也会把用户刚摆好的画面再抖一次。这里就地重排，并让板块列序跟着
    config["sectors"] 走——列序同样由配置决定。
    """
    if not state:
        return state

    aorder = {a["code"]: i for i, a in enumerate(cfg.get("assets") or [])}
    state["assets"] = sorted(state.get("assets") or [],
                             key=lambda a: aorder.get(a.get("code"), 10 ** 6))

    wl = state.get("watchlist") or {}
    worder = {s["code"]: i for i, s in enumerate(cfg.get("watchlist") or [])}
    wmap = {s["code"]: s for s in (cfg.get("watchlist") or [])}
    if wl.get("rows"):
        # 板块归类改了也要跟着走：行的 sector 是构建时写进缓存的，不刷新就会在
        # 拖动后弹回原来那一格。所以这里以配置为准，顺带补上导入时留空的名字。
        for r in wl["rows"]:
            w = wmap.get(r.get("code"))
            if not w:
                continue
            sec = w.get("sector") or "未分类"
            r["sector"] = sec if sec in (cfg.get("sectors") or []) else "未分类"
            if w.get("name"):
                r["name"] = w["name"]
        rows = sorted(wl["rows"], key=lambda r: worder.get(r.get("code"), 10 ** 6))
        wl["rows"] = rows
        by_market = {}
        for r in rows:
            by_market.setdefault(r["market"], []).append(r)
        wl["by_market"] = {"ORDER": sorted(by_market), **by_market}
        wl["markets"] = sorted(by_market)
        present = {r["sector"] for r in rows}
        wl["sectors"] = [s for s in (cfg.get("sectors") or []) if s in present]
    wl["all_sectors"] = cfg.get("sectors") or wl.get("all_sectors")
    state["watchlist"] = wl
    return state


def _last_date():
    """行情日期。优先读 A 股主缓存，取不到就退回港股缓存，再不行用今天。"""
    for p in (os.path.join(BASE, "data", "oos", "result_latest.json"),
              os.path.join(BASE, "data", "hk_senti", "hk_senti.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
            for k in ("date", "as_of"):
                if d.get(k):
                    return d[k]
            if d.get("meta", {}).get("date"):
                return d["meta"]["date"]
        except Exception:
            continue
    return time.strftime("%Y-%m-%d")


def load():
    if os.path.exists(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    return None


if __name__ == "__main__":
    d = build()
    print("市场总览  %s  资产 %d 个  自选 %d 只  耗时 %.1fs"
          % (d["date"], len(d["assets"]), d["watchlist"]["count"], d["elapsed"]))
    for c in d["assets"]:
        print("  %-14s %-10s %10s  %6s%%  分时 %d 点"
              % (c["name"][:12], c["code"], c["price"], c["chg"],
                 len(c["spark"]["pts"])))
