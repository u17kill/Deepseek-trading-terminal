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

# 默认资产清单：14 股指 + 4 商品 + 3 外汇 + 3 债券收益率 + 2 加密 = 26
DEFAULT_ASSETS = [
    ("eq", "sh000001", "上证指数", "股指"),
    ("eq", "sz399001", "深证成指", "股指"),
    ("eq", "sh000300", "沪深300", "股指"),
    ("eq", "hkHSI", "恒生指数", "股指"),
    ("eq", "hkHSTECH", "恒生科技", "股指"),
    ("eq", "usIVV", "标普500", "股指"),
    ("eq", "usDJI", "道琼斯", "股指"),
    ("eq", "usSOXX", "费城半导体", "股指"),
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
    ("fx", "fxUSDCNY", "美元/人民币", "外汇"),
    ("fx", "fxUSDJPY", "美元/日元", "外汇"),
    ("bd", "us10y", "10年期美债", "利率"),
    ("bd", "cn10y", "中国10年国债", "利率"),
    ("bd", "jp10y", "日本10年国债", "利率"),
    ("cr", "BTC-USDT", "比特币", "加密"),
    ("cr", "ETH-USDT", "以太坊", "加密"),
]

# 代码里带口径标注的，卡片右下角显示这个而不是代码
SRC_LABEL = {"usIVV": "IVV", "usDJI": "DIA", "fuNQ": "NQ", "fuES": "ES",
             "usSOXX": "SOXX", "fxUSDCNY": "USD/CNY", "fxUSDJPY": "USD/JPY"}


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
BOND_FAIL = []            # 本次刷新里取不到收益率的品种，随页面注明


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


# ---------------------------------------------------------------- 债券收益率
# 收益率不是「价格」，套不进 quote 的口径：
#   ① 没有「昨收」，涨跌只能用**基点**（bp = 0.01 个百分点），不是百分比；
#   ② 三个市场三个源，westock 只有美债与中债的宏观指标，日债在财务省 CSV；
#   ③ 中债指标滞后（常见 T-2），所以卡片右下角必须写各自的日期，不能只靠页首那天。
# 因此单开一个 bd 品种，价格存百分数（4.859 表示 4.859%），chg 存 bp。
BOND_LABEL = {"us10y": "UST", "cn10y": "中债", "jp10y": "MOF"}

MOF_CUR = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
           "jgbcme.csv")
MOF_ALL = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
           "historical/jgbcme_all.csv")
MOF_CACHE = os.path.join(OUTDIR, "jgb_hist.csv")
MOF_TTL = 86400           # 财务省按月更新，一天抓一次足够；all.csv 有 1.2MB


def _yield_series_us_cn(code):
    """美债 / 中债 10Y，走 westock 宏观指标。

    接口返回**无序**（同一年内日期乱序），必须自己排序后再取最后两根，
    否则「昨日」可能是半年前的某一天，bp 变化会离谱。
    """
    short = "us_yield_curve" if code == "us10y" else "cn_yield_curve"
    dcol = "EndDate" if code == "us10y" else "YTM_END_DATE"
    vcol = "YIELD_10Y" if code == "us10y" else "YTM_YIELD_10Y"
    rows = _table(ws(["macro", "indicator", short, "--limit", "40"]))
    pts = {}
    for r in rows:
        d = str(_pick(r, [dcol]) or "").strip()
        v = _num(_pick(r, [vcol]))
        if len(d) == 8 and d.isdigit() and v is not None:
            pts["%s-%s-%s" % (d[:4], d[4:6], d[6:])] = v
    return sorted(pts.items())


def _mof_parse(text):
    """解析财务省 CSV。表头在第一列是 Date 的那行，10Y 靠表头定位而不是数第几列。"""
    out, head = [], None
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if head is None:
            if cells and cells[0] == "Date":
                head = cells
            continue
        if len(cells) < len(head) or "10Y" not in head:
            continue
        d = cells[0]
        if d.count("/") != 2:
            continue
        v = _num(cells[head.index("10Y")])
        if v is None:          # 早年没有 10Y 品种，留空，不猜
            continue
        y, m, dd = d.split("/")
        out.append(("%s-%02d-%02d" % (y, int(m), int(dd)), v))
    return out


def _yield_series_jp():
    """日本 10Y，走财务省。当前月文件只有几天，历史文件按月更新，
    两个拼起来才够画 20D 迷你图。历史文件缓存一天，避免每次都下 1.2MB。"""
    texts = []
    fresh = (os.path.exists(MOF_CACHE)
             and time.time() - os.path.getmtime(MOF_CACHE) < MOF_TTL)
    if fresh:
        texts.append(open(MOF_CACHE, encoding="utf-8", errors="replace").read())
    else:
        r = requests.get(MOF_ALL, headers=UA, timeout=40)
        r.raise_for_status()
        body = r.content.decode("utf-8-sig", errors="replace")
        open(MOF_CACHE, "w", encoding="utf-8").write(body)
        texts.append(body)
    r = requests.get(MOF_CUR, headers=UA, timeout=40)
    r.raise_for_status()
    texts.append(r.content.decode("utf-8-sig", errors="replace"))
    pts = {}
    for t in texts:
        for d, v in _mof_parse(t):
            pts[d] = v
    return sorted(pts.items())


def bond_card(code):
    """一张债券收益率卡。取不到就抛，由上层决定是跳过还是用缓存。"""
    if code == "jp10y":
        pts = _yield_series_jp()
    elif code in ("us10y", "cn10y"):
        pts = _yield_series_us_cn(code)
    else:
        raise RuntimeError("未知的债券代码：%s" % code)
    if len(pts) < 2:
        raise RuntimeError("%s 只取到 %d 个收益率点，不足以算变化" % (code, len(pts)))
    last_d, last_v = pts[-1]
    prev_v = pts[-2][1]
    return {
        "price": round(last_v, 3),
        "chg": round((last_v - prev_v) * 100, 1),      # bp
        "spark": {"type": "daily", "pts": thin([round(v, 3) for _, v in pts[-GI_TAIL:]])},
        # 日期写进 src：中债滞后两三天，只标页首那天会把旧读数当成今天的
        "src": "%s %s" % (BOND_LABEL.get(code, ""), last_d[5:]),
        "asof": last_d,
    }


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

# 收益率不是行情品种，westock 的 search 里根本搜不到——只加名字不改搜索，
# 用户就永远加不上这张卡（v1.32.0 之前搜「费城半导体」也栽在同一处）。
BOND_ALIAS = {
    "美债": "us10y", "美国国债": "us10y", "十年期美债": "us10y", "10年美债": "us10y",
    "美债收益率": "us10y", "us10y": "us10y", "ust": "us10y", "tnx": "us10y",
    "中债": "cn10y", "中国国债": "cn10y", "国债收益率": "cn10y", "cn10y": "cn10y",
    "日债": "jp10y", "日本国债": "jp10y", "jgb": "jp10y", "jp10y": "jp10y",
}
BOND_NAME = {"us10y": "10年期美债", "cn10y": "中国10年国债", "jp10y": "日本10年国债"}
# 搜索下拉里的口径说明要说人话：UST / MOF 是卡片角标的缩写，不是给用户看的
BOND_HINT = {"us10y": "美国国债", "cn10y": "中债", "jp10y": "日本财务省"}

# 指数型标的在 westock 里的缺口：费城半导体只有 ETF 没有指数，搜类型搜不到，
# 这里给常见叫法留一条路，落到跟踪它的 ETF 上（口径写在 hint 里，不假装是指数）
ASSET_ALIAS = {
    "sox": "usSOXX", "费半": "usSOXX", "费城半导体": "usSOXX",
    "半导体指数": "usSOXX", "费城半导体指数": "usSOXX",
}
ALIAS_ETF_NAME = {"usSOXX": "费城半导体ETF（跟踪 PHLX 半导体指数）"}


def _kind_of(code):
    c = code.lower()
    if c.startswith("fu"):
        return "fu"
    if c.startswith("fx"):
        return "fx"
    if code in BOND_NAME:
        return "bd"
    if "-USDT" in code.upper():
        return "cr"
    return "eq"


def search(q, kind="stock"):
    """搜索可添加的标的。

    kind=stock 只搜股票（自选股用）；kind=asset 搜指数 / ETF / 期货 / 外汇 / 加密 /
    债券收益率（资产卡用）。
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

    key = q.lower()
    # 别名先出：用户搜的往往就是「中文叫法」，让它在列表最前面
    if key in ASSET_ALIAS:
        c = ASSET_ALIAS[key]
        add(c, ALIAS_ETF_NAME.get(c, c), "ETF · 跟踪 PHLX 半导体指数")
    for k, c in BOND_ALIAS.items():
        if key == k or (len(key) >= 2 and key in k):
            add(c, BOND_NAME[c], "收益率 · " + BOND_HINT[c])

    # 四个类型串行要 3 秒（每次 westock 调用都有进程启动开销），下拉框会像卡住。
    # 并发后约 1 秒；quote 的分钟线本来就在 5 线程里跑，搜索并发没有新增风险。
    kinds = (("index", "指数"), ("futures", "期货"),
             ("forex", "外汇"), ("etf", "ETF"))
    with ThreadPoolExecutor(max_workers=4) as ex:
        hits = list(ex.map(
            lambda kh: [(r, kh[1]) for r in _table(
                ws(["search", q, "--type", kh[0], "--limit", "6"]))], kinds))
    for group in hits:
        for r, hint in group:
            add(_pick(r, ["code"]), _pick(r, ["name"]),
                _pick(r, ["categoryLabel"]) or hint)
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

    # 收益率：三个源各自成败，缺一个不能连坐整页。取不到就退回上一份缓存里
    # 同一张卡（价格与日期照旧写着，不冒充今天），连缓存都没有才跳过这张。
    BOND_FAIL[:] = []
    if groups.get("bd"):
        cached = {}
        if os.path.exists(CACHE):
            try:
                for c in load().get("assets") or []:
                    if c.get("kind") == "bd":
                        cached[c["code"]] = c
            except Exception:
                cached = {}
        for a in groups["bd"]:
            try:
                quotes[a["code"]] = bond_card(a["code"])
            except Exception as e:
                BOND_FAIL.append("%s(%s)" % (a["name"], e))
                print("[警告] %s 收益率取数失败：%s" % (a["code"], e))
                if a["code"] in cached:
                    c = dict(cached[a["code"]])
                    c["src"] = (c.get("src") or "") + " 缓存"
                    quotes[a["code"]] = c

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
        elif a["kind"] == "bd":
            # 收益率自带 mini 序列与日期来源（见 bond_card），不走分钟接口
            card["spark"] = q.get("spark") or {"type": "daily", "pts": []}
            card["src"] = q.get("src") or ""
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
    BOND_FAIL[:] = []
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
                   "利率": sum(1 for c in cards if c["cls"] == "利率"),
                   "加密": sum(1 for c in cards if c["cls"] == "加密")},
        "watchlist": wl,
        "brief": load_brief(date),
        # 收益率缺哪一个要说出来：三张卡里少一张，页首日期不会替它解释
        "bond_fail": BOND_FAIL,
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
        # 收益率的变化是基点，不是百分比——同一列里混两种单位最容易看错
        chg = ("%6sbp" % c["chg"]) if c["kind"] == "bd" else ("%6s%%" % c["chg"])
        print("  %-14s %-10s %10s  %s  分时 %d 点  %s"
              % (c["name"][:12], c["code"], c["price"], chg,
                 len(c["spark"]["pts"]), c.get("src") or ""))
    if d.get("bond_fail"):
        print("[收益率缺数] %s" % "；".join(d["bond_fail"]))
