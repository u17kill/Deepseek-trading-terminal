# -*- coding: utf-8 -*-
"""Regime 模块评分。

口径见 SPEC.md §4。这里只做三件事：把读数变成档位、把档位合成模块分、
把模块分经弹性矩阵摊到六类资产。不生成任何判断文字。
"""
import datetime
import json
import os
import statistics as st

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
TZ = datetime.timezone(datetime.timedelta(hours=8))

BAND = [(55, "dg", "深绿"), (20, "lg", "浅绿"), (-20, "y", "黄"),
        (-55, "lr", "浅红"), (-999, "dr", "深红")]

WEIGHTS = {"policy": 0.20, "macro": 0.30, "industry": 0.30, "tech": 0.20}

ASSETS = [
    # 顺序 = 页面显示顺序，也 = GROUPS 展开后的顺序，三处只此一份。
    # 全部美元计价。亚太与商品用美股上市 ETF，不用本地指数：本币序列里
    # 汇率与本地价格混在一起，跨资产比较时会失真（日元贬、日经本币涨，
    # 美元回报其实是负的）。ETF 跟踪 MSCI 系列，不是原指数，页面要说清。
    ("us_large", "标普500",  "SPY"),
    ("us_eq",    "纳指100",  "QQQ"),
    ("semi",     "半导体",   "SOXX"),
    ("cn_eq",    "中国",     "FXI"),
    ("jp_eq",    "日本",     "EWJ"),
    ("kr_eq",    "韩国",     "EWY"),
    ("tw_eq",    "台湾",     "EWT"),
    ("in_eq",    "印度",     "INDA"),
    ("us_bond",  "美债",     "TLT"),
    ("usd",      "美元",     "DX-Y.NYB"),
    ("gold",     "黄金",     "GLD"),
    ("silver",   "白银",     "SLV"),
    ("copper",   "铜",       "CPER"),
    ("oil",      "原油",     "USO"),
]

# 矩阵按组排，组内按上面的顺序。列太多，不分组就只剩一串代码。
GROUPS = [
    ("eq_us",   "美股",  ["us_large", "us_eq", "semi"]),
    ("eq_apac", "亚太",  ["cn_eq", "jp_eq", "kr_eq", "tw_eq", "in_eq"]),
    ("rate",    "利率",  ["us_bond"]),
    ("fx",      "外汇",  ["usd"]),
    ("metal",   "贵金属与工业金属", ["gold", "silver", "copper"]),
    ("energy",  "能源",  ["oil"]),
]

# 先验弹性矩阵，固定不优化。见 SPEC §4.5。
#
# 符号约定（必须统一，这里踩过坑）：s_m 是「该模块对 risk-on 的支持度」（正=支持），
# 所以 e 的含义是「支持度上升 1 单位时，该资产往哪走」——正=跟涨，负=反跌。
# 曾经把政策与宏观两行按「恶化→资产往哪走」填，两种约定混用，结果宏观鹰派
# （s=-23）反而给美股贡献了 +5.6 分，方向全反。
#
# 美债一列是唯一必须跨行为正负相反的地方：
#   政策行 e=-0.3：地缘缓和=支持度升=避险需求退=债券跌 → 反跌，故负
#   宏观行 e=+0.5：货币转松=支持度升=收益率下行=债券涨 → 跟涨，故正
#
# 新增八类（v0.2.0）的填法，逐条写依据，别只留数字：
#   SPY 比 QQQ 每档低 0.05~0.1：宽基里少了久期型成长股权重。
#   SOXX 政策 1.0 / 产业 1.1：出口管制与资本开支两头直接命中，全表最高。
#   白银政策 -0.5（弱于黄金 -0.7）：避险属性不如黄金，但工业需求托底；
#   产业 +0.5（远高于黄金 +0.1）：光伏与电子的真实用量在这里。
#   原油政策 -0.5：地缘紧张 = 支持度降 = 油价涨，符号必须为负。
#   亚太四地按「对管制的暴露度」排：台湾最敏感（1.0），韩国次之（0.9，
#   半导体链），日本（0.8，盟友且是受益方），印度最低（0.6，关联弱）。
#   产业行亚太分化同理：韩国/台湾 0.9（半导体链），日本 0.6，印度 0.4。
ELASTIC = {
    "policy":   {"us_large": +0.65, "us_eq": +0.7, "semi": +1.0,
                 "us_bond": -0.3, "usd": -0.5,
                 "gold": -0.7, "silver": -0.5, "copper": +0.5, "oil": -0.5,
                 "cn_eq": +1.0, "jp_eq": +0.8, "kr_eq": +0.9,
                 "tw_eq": +1.0, "in_eq": +0.6},
    "macro":    {"us_large": +0.75, "us_eq": +0.8, "semi": +0.9,
                 "us_bond": +0.5, "usd": -0.6,
                 "gold": +0.6, "silver": +0.5, "copper": +0.4, "oil": +0.3,
                 # 亚太这一行 v0.2.1 按历史 beta 重排，原来五个全是 0.4，
                 # 等于假设「美国利率对日韩台印的传导完全一样」。实测（近 250 日，
                 # 2Y 每 100bp 的资产涨跌）差 8 倍：韩国 −14.8、印度 −6.1、
                 # 中国 −5.0、日本 −4.9、台湾 −1.8。
                 # 注意 e 是「折现率渠道」与「汇率渠道」轧差后的净值，不是单一机制：
                 # 美债利率升 → 折现率升（压估值）+ 美元升（未对冲 ETF 的美元回报被抬）。
                 # 台湾最低是因为它由半导体周期驱动，对利率不敏感（它的高暴露在产业行）。
                 "cn_eq": +0.3, "jp_eq": +0.5, "kr_eq": +0.7,
                 "tw_eq": +0.35, "in_eq": +0.35},
    "industry": {"us_large": +0.85, "us_eq": +0.9, "semi": +1.1,
                 "us_bond": -0.2, "usd": +0.2,
                 "gold": +0.1, "silver": +0.5, "copper": +0.8, "oil": +0.7,
                 "cn_eq": +0.5, "jp_eq": +0.6, "kr_eq": +0.9,
                 "tw_eq": +0.9, "in_eq": +0.4},
    # 技术行（v0.3.0 新增）：对「技术因子」的暴露，先验分档，不用历史 β。
    # 依据是对市场动量的 β 大小：高动量 β（半导体/纳指/白银）放大趋势，
    # 债券/黄金/美元是避险资产、趋势因子上升时反跌（负暴露）。
    "tech":     {"us_large": +1.0, "us_eq": +1.2, "semi": +1.3,
                 "us_bond": -0.3, "usd": -0.2,
                 "gold": -0.3, "silver": +1.0, "copper": +0.9, "oil": +0.8,
                 "cn_eq": +1.0, "jp_eq": +1.0, "kr_eq": +1.1,
                 "tw_eq": +1.1, "in_eq": +0.8},
}


def band_of(score):
    for lo, code, cn in BAND:
        if score >= lo:
            return code, cn
    return "dr", "深红"


def _mean(xs):
    return sum(xs) / float(len(xs)) if xs else None


def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ---------- 时间工具 ----------

def _shift(d, years=0, days=0):
    y = int(d[:4]) - years
    try:
        return "%04d%s" % (y, d[4:])
    except Exception:  # noqa: BLE001
        return d


def window(dates, values, years=5):
    """取最近 N 年的点。分位窗口统一用 5 年。"""
    if not dates:
        return [], []
    cutoff = _shift(dates[-1], years=years)
    idx = [i for i, d in enumerate(dates) if d >= cutoff]
    return [dates[i] for i in idx], [values[i] for i in idx]


def pct_rank(v, win):
    """v 在 win 里的分位（0~1，含并列取中）。"""
    w = [x for x in win if x is not None]
    if not w or v is None:
        return None
    le = sum(1 for x in w if x < v)
    eq = sum(1 for x in w if x == v)
    return (le + eq * 0.5) / float(len(w))


def change_over(dates, values, days):
    """相对 N 天前的变化（绝对值）。用于收益率类，单位与原始一致。"""
    if not dates or not values:
        return None
    cutoff = (datetime.date.fromisoformat(dates[-1])
              - datetime.timedelta(days=days)).isoformat()
    prev = None
    for d, v in zip(dates, values):
        if d <= cutoff:
            prev = v
    if prev is None:
        return None
    return values[-1] - prev


def chg_n(values, n):
    """最近 n 个点的变化。"""
    if len(values) <= n:
        return None
    return values[-1] - values[-1 - n]


def rolling_changes(values, n):
    """历史上每个时点的同期变化序列 —— 变化项分位的分母。

    这里曾经写成「在同一终点上换回看天数」（range(n, n+k) 去算 change_over），
    那是把不同窗口的累计变化拿来比大小，不是滚动分布，结果 M8 把
    +19bp 的紧缩重新定价判成了 +2 有利。变化项的分位必须用真正的滚动序列。
    """
    return [values[i] - values[i - n] for i in range(n, len(values))]


def rolling_returns(values, n):
    """历史上每个时点的 n 期收益率序列。比值类指标（铜金比、相对强弱）用它做分母。"""
    out = []
    for i in range(n, len(values)):
        if values[i - n]:
            out.append(values[i] / values[i - n] - 1.0)
    return out


def ret_n(values, n):
    if len(values) <= n or not values[-1 - n]:
        return None
    return values[-1] / values[-1 - n] - 1.0


def yoy(dates, values):
    """同比（%）。月度序列取 12 个点前。"""
    if len(values) < 13 or not values[-13]:
        return None
    return (values[-1] / values[-13] - 1.0) * 100.0


def ann3m(dates, values):
    """3 个月年化（%）。月度指数序列取 3 个点前。"""
    if len(values) < 4 or not values[-4]:
        return None
    return ((values[-1] / values[-4]) ** 4 - 1.0) * 100.0


def band_dual(level_p, chg_p, dir_):
    """水平 0.6 + 变化 0.4，合成 -2~+2。dir_ 为 +1/-1。"""
    def to_s(p, d):
        if p is None:
            return None
        if d < 0:
            p = 1.0 - p
        if p >= 0.90:
            return 2.0
        if p >= 0.70:
            return 1.0
        if p >= 0.30:
            return 0.0
        if p >= 0.10:
            return -1.0
        return -2.0
    s1 = to_s(level_p, dir_)
    s2 = to_s(chg_p, dir_) if chg_p is not None else None
    if s1 is None and s2 is None:
        return None
    if s2 is None:
        return s1
    return 0.6 * s1 + 0.4 * s2


def band_abs(p, dirneg=True):
    """纯分位映射，不走变化项。dirneg=True 表示分位越高越不利。"""
    if p is None:
        return None
    q = 1.0 - p if dirneg else p
    if q >= 0.90:
        return 2.0
    if q >= 0.70:
        return 1.0
    if q >= 0.30:
        return 0.0
    if q >= 0.10:
        return -1.0
    return -2.0


# ---------- 指标 ----------

def build_indicators(S, P):
    """返回指标明细列表。每项 {id, name, mod, date, value, disp, p, band}"""
    out = []

    def add(iid, name, mod, date, disp, p, band, note=""):
        # 滞后天数是必须显示的：核心 PCE 与新订单都是两个月前的事实，
        # 不加这个数，读者会把它当成「今天的基本面」。
        lag = None
        try:
            lag = (datetime.date.today() - datetime.date.fromisoformat(date)).days
        except Exception:  # noqa: BLE001
            pass
        out.append({"id": iid, "name": name, "mod": mod, "date": date,
                    "disp": disp, "p": p, "band": band, "note": note, "lag": lag})

    def ser(sid):
        s = S.get(sid)
        if not s or not s.get("dates"):
            return None
        return s

    # ---- 宏观 ----
    s = ser("T10Y2Y")
    if s:
        d, v = window(s["dates"], s["values"])
        p1 = pct_rank(v[-1], v)
        ch = chg_n(v, 63)
        p2 = pct_rank(ch, rolling_changes(v, 63))
        add("M1", "10Y-2Y 期限差", "macro", s["dates"][-1],
            "%.2f pp" % v[-1], p1, band_dual(p1, p2, +1))

    s = ser("DFII10")
    if s:
        d, v = window(s["dates"], s["values"])
        p1 = pct_rank(v[-1], v)
        ch = chg_n(v, 63)
        p2 = pct_rank(ch, rolling_changes(v, 63))
        add("M2", "10Y 实际利率", "macro", s["dates"][-1],
            "%.2f%%" % v[-1], p1, band_dual(p1, p2, -1))

    s = ser("T10YIE")
    if s:
        _, v = window(s["dates"], s["values"])
        dev = abs(v[-1] - 2.3)
        hist = [abs(x - 2.3) for x in v]
        p1 = pct_rank(dev, hist)
        add("M3", "10Y 通胀预期", "macro", s["dates"][-1],
            "%.2f%%（偏离中枢 %.2f）" % (v[-1], dev), p1, band_abs(p1, dirneg=True),
            note="倒 U：偏离 2.3% 越远越不利")

    s = ser("PCEPILFE")
    s3 = ann3m(s["dates"], s["values"]) if s else None
    if s and s3 is not None:
        # 分位会骗人：5 年窗口含 2022 高通胀，2.9% 在窗口里只排 34 分位，
        # 可它离 2% 目标还差 0.9pp。这一项改用相对目标的绝对档位，
        # 与「联储看的是离 2% 多远」这个事实对齐。SPEC §4.1 第 4 条的例外，
        # 理由是它有明确政策目标、而分位窗口已被极端期污染。
        if s3 <= 2.0:
            b = 2.0
        elif s3 <= 2.5:
            b = 1.0
        elif s3 <= 3.0:
            b = 0.0
        elif s3 <= 3.5:
            b = -1.0
        else:
            b = -2.0
        add("M4", "核心 PCE 3M 年化", "macro", s["dates"][-1],
            "%.2f%%（目标 2%%）" % s3, None, b,
            note="按相对 2% 目标的绝对档位，不用分位")

    s = ser("UNRATE")
    if s:
        v = s["values"]
        sahm = _mean(v[-3:]) - min(v[-15:-3]) if len(v) >= 15 else None
        hist = []
        for i in range(14, len(v)):
            m3 = _mean(v[i - 2:i + 1])
            lo = min(v[max(0, i - 14):i - 2])
            hist.append(m3 - lo)
        p1 = pct_rank(sahm, hist) if sahm is not None else None
        add("M5", "失业率 Sahm 缺口", "macro", s["dates"][-1],
            "%+.2f pp（失业率 %.1f%%）" % (sahm, v[-1]) if sahm is not None else "-",
            p1, band_abs(p1, dirneg=True),
            note="≥0.5 触发衰退判定")

    s = ser("NFCI")
    if s:
        _, v = window(s["dates"], s["values"])
        p1 = pct_rank(v[-1], v)
        ch = chg_n(v, 63)
        p2 = pct_rank(ch, rolling_changes(v, 63))
        add("M6", "芝加哥联储金融条件", "macro", s["dates"][-1],
            "%.3f（负=宽松）" % v[-1], p1, band_dual(p1, p2, -1))

    s = ser("DTWEXBGS")
    if s:
        _, v = window(s["dates"], s["values"])
        p1 = pct_rank(v[-1], v)
        ch = chg_n(v, 63)
        p2 = pct_rank(ch, rolling_changes(v, 63))
        add("M7", "广义美元指数", "macro", s["dates"][-1],
            "%.2f" % v[-1], p1, band_dual(p1, p2, -1))

    s = ser("DGS2")
    if s:
        _, v = window(s["dates"], s["values"])
        ch = chg_n(v, 20)
        p = pct_rank(ch, rolling_changes(v, 20))
        add("M8", "2Y 收益率 20 日变化", "macro", s["dates"][-1],
            "%+.0f bp（现 %.2f%%）" % ((ch or 0) * 100, v[-1]), p,
            band_abs(p, dirneg=True), note="利率路径代理变量，非 FedWatch")

    # ---- 产业 ----
    s = ser("NEWORDER")
    if s:
        y = yoy(s["dates"], s["values"])
        hist = []
        for i in range(12, len(s["values"])):
            h = (s["values"][i] / s["values"][i - 12] - 1) * 100 if s["values"][i - 12] else None
            if h is not None:
                hist.append(h)
        p = pct_rank(y, hist)
        add("I1", "核心资本品新订单同比", "industry", s["dates"][-1],
            "%+.1f%%" % y if y is not None else "-", p, band_abs(p, dirneg=False),
            note="capex 最直接代理")

    s = ser("AMTMNO")
    if s:
        y = yoy(s["dates"], s["values"])
        hist = []
        for i in range(12, len(s["values"])):
            h = (s["values"][i] / s["values"][i - 12] - 1) * 100 if s["values"][i - 12] else None
            if h is not None:
                hist.append(h)
        p = pct_rank(y, hist)
        add("I2", "制造业新订单同比", "industry", s["dates"][-1],
            "%+.1f%%" % y if y is not None else "-", p, band_abs(p, dirneg=False))

    s = ser("TCU")
    if s:
        _, v = window(s["dates"], s["values"])
        dev = abs(v[-1] - 78.0)
        hist = [abs(x - 78.0) for x in v]
        p = pct_rank(dev, hist)
        add("I3", "工业产能利用率", "industry", s["dates"][-1],
            "%.1f%%（偏离 78%% 中枢 %.1f）" % (v[-1], dev), p,
            band_abs(p, dirneg=True), note="倒 U：过热与过剩都不利")

    s = ser("ISRATIO")
    if s:
        _, v = window(s["dates"], s["values"])
        p = pct_rank(v[-1], v)
        add("I4", "库存/销售比", "industry", s["dates"][-1],
            "%.2f x" % v[-1], p, band_abs(p, dirneg=True))

    # 铜金比
    cu, au = P.get("HG=F"), P.get("GC=F")
    if cu and au and cu.get("close") and au.get("close"):
        cd = dict(zip(cu["dates"], cu["close"]))
        ad = dict(zip(au["dates"], au["close"]))
        common = sorted(set(cd) & set(ad))
        if len(common) > 260:
            ratio = [cd[d] / ad[d] for d in common]
            _, w = window(common, ratio)
            p1 = pct_rank(ratio[-1], w)
            rl = ret_n(w, 21)
            p2 = pct_rank(rl, rolling_returns(w, 21))
            add("I5", "铜金比", "industry", common[-1],
                "%.4f（20 日 %+.1f%%）" % (ratio[-1], (rl or 0) * 100), p1,
                band_dual(p1, p2, +1),
                note="需求 vs 避险，全球制造业温度计")

    # 半导体相对强弱
    sm, sp = P.get("SMH"), P.get("SPY")
    if sm and sp and sm.get("close") and sp.get("close"):
        sd = dict(zip(sm["dates"], sm["close"]))
        pd_ = dict(zip(sp["dates"], sp["close"]))
        common = sorted(set(sd) & set(pd_))
        if len(common) > 260:
            rs = [sd[d] / pd_[d] for d in common]
            _, w = window(common, rs)
            p1 = pct_rank(rs[-1], w)
            rl = ret_n(w, 21)
            p2 = pct_rank(rl, rolling_returns(w, 21))
            add("I6", "半导体/标普 相对强弱", "industry", common[-1],
                "%.4f（20 日 %+.1f%%）" % (rs[-1], (rl or 0) * 100), p1,
                band_dual(p1, p2, +1))

    s = ser("BAMLH0A0HYM2")
    if s:
        _, v = window(s["dates"], s["values"])
        p = pct_rank(v[-1], v)
        ch = change_over(s["dates"], s["values"], 20)
        add("I7", "高收益债 OAS", "industry", s["dates"][-1],
            "%.2f%%（20 日 %+.0f bp）" % (v[-1], (ch or 0) * 100), p,
            band_abs(p, dirneg=True))

    return out


# ---------- 技术面 ----------

def _ma(c, n):
    return _mean(c[-n:]) if len(c) >= n else None


def _atr(h, l, c, n=14):
    trs = []
    for i in range(len(c) - n, len(c)):
        if i <= 0:
            continue
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return _mean(trs)


def tech_score(px):
    """单个资产的技术分与明细。返回 (score -100~100, items, note)"""
    if not px or not px.get("close") or len(px["close"]) < 210:
        return None, [], "样本不足"
    c, h, l, v, d = px["close"], px["high"], px["low"], px["volume"], px["dates"]
    parts, items = [], []

    ma200, ma50 = _ma(c, 200), _ma(c, 50)
    dev = (c[-1] / ma200 - 1) * 100 if ma200 else None
    if dev is not None:
        s = _clip(dev / 10.0)
        parts.append(s)
        items.append({"id": "T1", "name": "距 200DMA", "val": "%+.1f%%" % dev, "s": round(s, 2)})

    if ma50 and ma200:
        gap = (ma50 / ma200 - 1) * 100
        s = 1.0 if gap > 0.5 else (-1.0 if gap < -0.5 else 0.0)
        parts.append(s)
        items.append({"id": "T2", "name": "50/200 均线", "val": "%+.2f%%" % gap, "s": s})

    hi = max(c[-252:]) if len(c) >= 252 else max(c)
    dd = (c[-1] / hi - 1) * 100
    # 连续映射：回撤 0 → +1，-10% → 0，-20% → -1。
    # 原来写成 dd>-15 时线性、否则直接 -1，在 -15% 处从 -0.007 跳到 -1.0，
    # 一个百分点之差给出完全不同的档位。
    s = _clip(1.0 + dd / 10.0)
    parts.append(s)
    items.append({"id": "T3", "name": "距 52 周高", "val": "%+.1f%%" % dd, "s": round(s, 2)})

    if len(c) > 21:
        mom = (c[-1] / c[-21] - 1) * 100
        s = _clip(mom / 8.0)
        parts.append(s)
        items.append({"id": "T4", "name": "20 日动量", "val": "%+.1f%%" % mom, "s": round(s, 2)})

    brk = 0.0
    note = ""
    if len(c) > 22:
        lo20 = min(l[-21:-1])
        atr = _atr(h, l, c) or 0.0
        vol_ma = _mean(v[-21:-1]) or 0.0
        if c[-1] < lo20:
            depth = lo20 - c[-1]
            strong = atr and depth > atr and v[-1] and vol_ma and v[-1] > vol_ma
            brk = -1.0 if not strong else -1.5
            note = "破 20 日低点" + ("（放量且超 ATR，强破位）" if strong else "")
    parts.append(brk)
    items.append({"id": "T5", "name": "破位信号", "val": note or "未破位", "s": brk})

    score = _mean(parts) * 100
    return round(score, 1), items, note


# ---------- 技术因子（v0.3.0 重构）----------
#
# 原来的技术层是「14 个资产各自 tech_score → 取算术平均当模块分」，本质是
# 资产价格的滞后画像：股票类 8 个资产占了大多数票，商品与汇率的独立信息被稀释。
# 而且「股票跌没跌」这件事宏观/产业层已经在回答，技术层没有提供增量信息。
#
# 重构后技术层 = 4 个横跨全市场的技术因子，与政策/宏观/产业同构：
#   T1 趋势斜率  风险资产篮子（SPY/QQQ/SOXX/FXI/EWJ/EWY）平均 50/200 偏离 + 20 日动量
#   T2 波动率结构 VIX 绝对水平分位 + VIX/VIX3M 期限结构
#   T3 商品-股票动量差  铜/油 20 日动量 − 股票 20 日动量（再通胀 vs 衰退）
#   T4 市场宽度  （数据待补，暂 None）
#
# 因子档位经截尾均值合成模块分，再走 ELASTIC["tech"] 摊到 14 类资产。
# 逐资产的 tech_score 不再参与模块分，降级为资产卡上的「已破位」徽标。

RISK_BASKET = ["SPY", "QQQ", "SOXX", "FXI", "EWJ", "EWY"]


def _trend_component(c, ma_fast_n=20, ma_slow_n=200):
    """单个价格序列的趋势斜率档位（-2~+2）。

    用「价格相对 20 日均线」而非 50/200 关系：50/200 是慢信号，急速下跌时
    均线仍在高位、关系还是正的（SOXX 现价 123 远低于 ma20 150，50/200 却 +4.4%），
    会把 -22% 的暴跌读成「趋势未破」。价格 vs 20DMA 才能及时反映斜率转向。
    """
    fast, slow = _ma(c, ma_fast_n), _ma(c, ma_slow_n)
    if not fast:
        return None, None
    gap = (c[-1] / fast - 1) * 100  # 现价相对 20 日均线的偏离
    if gap > 2.0:
        s = 2.0
    elif gap > 0.5:
        s = 1.0
    elif gap > -0.5:
        s = 0.0
    elif gap > -2.0:
        s = -1.0
    else:
        s = -2.0
    return s, gap


def _mom_component(c, n=20):
    """20 日动量的档位（-2~+2）。用分位映射，不用绝对百分比（各资产波动率不同）。"""
    if len(c) <= n or not c[-1 - n]:
        return None, None
    mom = (c[-1] / c[-1 - n] - 1) * 100
    # 直接映射：动量本身越强越 risk-on
    s = _clip(mom / 8.0) * 2.0  # 8% 动量 → +2，-8% → -2
    return round(s, 2), mom


def tech_factors(P, S):
    """返回技术因子明细列表，格式与 build_indicators 的 add 一致
    （{id,name,mod,date,disp,p,band}）。band 为 -2~+2 档位。"""
    out = []

    # ---- T1 趋势斜率：风险资产篮子的价格相对 20DMA 偏离 + 动量 ----
    gaps, moms = [], []
    for tk in RISK_BASKET:
        p = P.get(tk)
        if not p or not p.get("close") or len(p["close"]) < 210:
            continue
        c = p["close"]
        s, gap = _trend_component(c)
        if gap is not None:
            gaps.append(gap)
        _, mom = _mom_component(c)
        if mom is not None:
            moms.append(mom)
    if gaps:
        avg_gap = _mean(gaps)
        # 篮子价格相对 20DMA：分档同 _trend_component
        if avg_gap > 2.0:
            gs = 2.0
        elif avg_gap > 0.5:
            gs = 1.0
        elif avg_gap > -0.5:
            gs = 0.0
        elif avg_gap > -2.0:
            gs = -1.0
        else:
            gs = -2.0
        ms = _clip(_mean(moms) / 8.0) * 2.0 if moms else 0.0
        # 动量权重 0.5：趋势斜率里「最近发生了什么」比「均线在哪」更该占主导，
        # 否则暴跌会被慢均线对冲成中性（见 _trend_component 注释）。
        band = 0.5 * gs + 0.5 * ms
        out.append({"id": "T1", "name": "趋势斜率（风险篮子）", "mod": "tech",
                    "date": P.get(RISK_BASKET[0], {}).get("dates", [None])[-1],
                    "disp": "价格距20DMA %+.1f%% · 20日动量 %+.1f%%" % (avg_gap, _mean(moms) if moms else 0),
                    "p": None, "band": round(band, 2),
                    "note": "SPY/QQQ/SOXX/FXI/EWJ/EWY 六资产平均"})

    # ---- T2 波动率结构：VIX 绝对水平 + 期限结构 ----
    vix, vix3 = S.get("VIX"), S.get("VIX3M")
    if vix and vix3 and vix["values"] and vix3["values"]:
        _, vv = window(vix["dates"], vix["values"])
        p_abs = pct_rank(vv[-1], vv)
        # VIX 越高越 risk-off
        vabs = band_abs(p_abs, dirneg=True)
        ratio = vv[-1] / vix3["values"][-1]
        # 期限结构：倒挂(>=1.0)=真恐慌=-2，深度 contango(<0.92)=常态=+1
        if ratio >= 1.0:
            vterm = -2.0
        elif ratio >= 0.95:
            vterm = -1.0
        elif ratio >= 0.92:
            vterm = 0.0
        else:
            vterm = 1.0
        band = 0.6 * vabs + 0.4 * vterm
        out.append({"id": "T2", "name": "波动率结构", "mod": "tech",
                    "date": vix["dates"][-1],
                    "disp": "VIX %.1f（期限比 %.3f）" % (vv[-1], ratio),
                    "p": p_abs, "band": round(band, 2),
                    "note": "VIX 水平 0.6 + 期限结构 0.4"})

    # ---- T3 商品-股票动量差：再通胀 vs 衰退 ----
    comm_tks = ["HG=F", "CL=F"]  # 铜、油
    eq_tks = ["SPY", "QQQ"]
    cmom, emom = [], []
    for tk in comm_tks:
        p = P.get(tk)
        if p and p.get("close") and len(p["close"]) > 21:
            _, mom = _mom_component(p["close"])
            if mom is not None:
                cmom.append(mom)
    for tk in eq_tks:
        p = P.get(tk)
        if p and p.get("close") and len(p["close"]) > 21:
            _, mom = _mom_component(p["close"])
            if mom is not None:
                emom.append(mom)
    if cmom and emom:
        cmean, emean = _mean(cmom), _mean(emom)
        diff = cmean - emean
        # 关键：这个因子的语义是「相对切换」，只在商品与股票**反向**时才给强信号。
        # 两者同向普跌（当前正是：铜 -15%、油 -19%、股票 -9~13%）是流动性冲击/去杠杆，
        # 已由 T1/T2 覆盖，这里不该重复计成「衰退」。同向时给弱 risk-off（-0.5 封顶），
        # 反向时按差值放大（再通胀=正，衰退=负）。
        same_direction = (cmean >= 0 and emean >= 0) or (cmean < 0 and emean < 0)
        if same_direction:
            s = -0.5 if cmean < 0 else 0.5
        else:
            s = _clip(diff / 6.0) * 2.0
        out.append({"id": "T3", "name": "商品-股票动量差", "mod": "tech",
                    "date": P.get(comm_tks[0], {}).get("dates", [None])[-1],
                    "disp": "商品 %+.1f%% vs 股票 %+.1f%%（差 %+.1fpp%s）" % (
                        cmean, emean, diff, "· 同向普跌" if same_direction and cmean < 0
                        else ("· 同向普涨" if same_direction else "")),
                    "p": None, "band": round(s, 2),
                    "note": "反向=再通胀/衰退切换；同向=普涨/普跌，弱信号"})

    # ---- T4 市场宽度：数据待补 ----
    out.append({"id": "T4", "name": "市场宽度", "mod": "tech",
                "date": None, "disp": "未获取", "p": None, "band": None,
                "note": "标普等权 vs 市值加权的偏离，数据源待补"})

    return out


# ---------- 模块合成 ----------

def trim_mean(bands):
    """截尾均值：去掉一个最高一个最低。指标数 ≤3 时不去尾。"""
    xs = [b for b in bands if b is not None]
    if not xs:
        return None, None, 0
    if len(xs) >= 4:
        xs = sorted(xs)[1:-1]
    return _mean(xs), (st.pstdev(xs) if len(xs) > 1 else 0.0), len(xs)


def module_from(indicators, mod):
    items = [i for i in indicators if i["mod"] == mod]
    bands = [i["band"] for i in items]
    m, disp, n = trim_mean(bands)
    if m is None:
        return {"score": None, "band": "na", "label": "未获取", "disp": None,
                "n": 0, "weak": True, "downgraded": False}
    score = round(m / 2.0 * 100, 1)
    code, cn = band_of(score)
    downgraded = False
    if disp is not None and disp > 1.0:
        # 指标互相打架：强制降一级，不确定就显示成不确定
        order = ["dg", "lg", "y", "lr", "dr"]
        i = order.index(code)
        if i < len(order) - 1 and code != "y":
            code = order[i + 1] if i < 2 else order[i - 1]
            cn = dict((c, n2) for _, c, n2 in BAND)[code]
        downgraded = True
    return {"score": score, "band": code, "label": cn,
            "disp": round(disp, 2) if disp is not None else None,
            "n": n, "weak": n <= 1, "downgraded": downgraded}


def policy_module():
    """政策分来自人工 rubric。state=confirmed 的才计入。"""
    p = os.path.join(DATA, "policy_answers.json")
    if not os.path.exists(p):
        return {"score": None, "band": "na", "label": "未获取", "disp": None,
                "n": 0, "weak": True, "downgraded": False}, [], []
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    dims = {x["id"]: x for x in d.get("dims", [])}
    rows, conf, pend = [], [], []
    for a in d.get("answers", []):
        dim = dims.get(a["dim"])
        if not dim:
            continue
        row = {"dim": a["dim"], "name": dim["name"], "weight": dim["weight"],
               "score": a["score"], "fact": a.get("fact", ""), "date": a.get("date", ""),
               "source": a.get("source", ""), "state": a.get("state", "pending")}
        rows.append(row)
        (conf if row["state"] == "confirmed" else pend).append(row)

    def agg(items):
        if not items:
            return None
        tw = sum(i["weight"] for i in items)
        return sum(i["score"] * i["weight"] for i in items) / tw if tw else None

    def to_mod(avg):
        if avg is None:
            return {"score": None, "band": "na", "label": "未获取", "disp": None,
                    "n": 0, "weak": True, "downgraded": False}
        score = round(avg / 2.0 * 100, 1)
        code, cn = band_of(score)
        disp = st.pstdev([i["score"] for i in conf]) if len(conf) > 1 else 0.0
        return {"score": score, "band": code, "label": cn,
                "disp": round(disp, 2), "n": len(conf), "weak": len(conf) <= 1,
                "downgraded": False}

    conf_mod = to_mod(agg(conf))
    prev = to_mod(agg(conf + pend)) if pend else None
    return conf_mod, rows, (prev["score"] if prev else None)


# ---------- 主流程 ----------

def build_local_rates(lr, S=None):
    """把 BIS / 央行的原始条目加工成页面能直接用的表。

    两件事这里做、页面不做：算与美国政策利率的利差（负=本地比美国低）、
    算 12 个月变化（bp）。台湾那条没有 prev，变化留空而不是填 0——
    「没数据」和「没变化」在页面上必须分开，混起来就变成假事实。

    v0.3.2 起多了两件：①「12 个月区间」直接把 fetch 算好的 hi/lo 透传，
    让「降了又加回来」这种往返不被期末−期初抹平；②「市场押注」——用各经济体
    短端市场利率（3M 银行间/国库券/隔夜拆借）减去政策利率，正=市场押加息、
    负=押降息。这是前瞻，政策利率是「现在」，短端市场利率是「市场对未来的定价」。
    """
    items = lr.get("items") or []
    us = next((x for x in items if x["code"] == "US"), None)

    # 市场短端利率映射：code -> (FRED 序列 id, 口径名)
    MM = {
        "US": ("DTB3", "3M 国库券"),
        "JP": ("IR3TIB01JPM156N", "3M 银行间"),
        "KR": ("IR3TIB01KRM156N", "3M 银行间"),
        "CN": ("IR3TIB01CNM156N", "3M 银行间"),
        "IN": ("IRSTCI01INM156N", "隔夜拆借"),
        # TW 无 FRED 短端序列，留空
    }

    def _latest(sid):
        if not S:
            return None, None
        s = S.get(sid)
        if not s or not s.get("values"):
            return None, None
        return s["values"][-1], s["dates"][-1]

    out = []
    for it in items:
        d = dict(it)
        d["spread"] = (round(it["rate"] - us["rate"], 3)
                       if us and it["code"] != "US" else None)
        d["chg"] = (round((it["rate"] - it["prev"]) * 100, 0)
                    if it.get("prev") is not None else None)
        # 美国目标区间（联储口径，比 BIS 单一政策利率更准）
        d["tgt_lo"] = None
        d["tgt_hi"] = None
        if it["code"] == "US":
            lo_v, _ = _latest("DFEDTARL")
            hi_v, _ = _latest("DFEDTARU")
            if lo_v is not None and hi_v is not None:
                d["tgt_lo"] = lo_v
                d["tgt_hi"] = hi_v
        # 市场押注：短端市场利率 − 政策利率
        d["mm"] = None
        d["mm_date"] = None
        d["mm_label"] = None
        if it["code"] in MM:
            sid, lbl = MM[it["code"]]
            mv, md = _latest(sid)
            if mv is not None:
                d["mm"] = round(mv - it["rate"], 3)
                d["mm_date"] = md
                d["mm_label"] = lbl
        out.append(d)
    return {"as_of": lr.get("fetched_at"), "items": out,
            "errors": lr.get("errors") or [], "note": lr.get("note", "")}


def main():
    with open(os.path.join(DATA, "series.json"), encoding="utf-8") as f:
        raw = json.load(f)
    S, P = raw["series"], raw["prices"]

    ind = build_indicators(S, P)
    mods = {m: module_from(ind, m) for m in ("macro", "industry")}
    mods["policy"], pol_rows, pol_preview = policy_module()
    # 政策没确认时也要让页面能画出「若确认会怎样」，所以单独挂一份参考档，
    # 不参与任何合计。build.py 用它渲染斜纹格。
    if pol_preview is not None:
        pb, pl = band_of(pol_preview)
        mods["policy"]["preview"] = {"score": pol_preview, "band": pb, "label": pl}

    # 技术层重构（v0.3.0）：不再「14 资产 tech_score 取均值」，改走 4 个市场因子
    # （tech_factors），经截尾均值合成模块分，再走 ELASTIC["tech"] 摊到资产。
    # 逐资产 tech_score 保留，但只用于资产卡上的「已破位」徽标，不进模块分。
    tech_ind = tech_factors(P, S)
    mods["tech"] = module_from(tech_ind, "tech")

    # 逐资产破位检测：只提取「是否放量破位」，供资产卡徽标用
    tech_flags = {}
    for aid, aname, tk in ASSETS:
        _, items, note = tech_score(P.get(tk))
        # T5 破位信号：s < 0 说明已破位（-1 或 -1.5）
        broken = False
        strong = False
        for it in items:
            if it["id"] == "T5" and it["s"] < 0:
                broken = True
                strong = it["s"] <= -1.5
        tech_flags[aid] = {"broken": broken, "strong": strong,
                           "note": note if broken else ""}

    # 风险偏好总分：可用模块按权重重新归一
    avail = [(m, WEIGHTS[m]) for m in WEIGHTS
             if mods.get(m) and mods[m]["score"] is not None
             and not mods[m]["weak"]]
    if avail:
        tw = sum(w for _, w in avail)
        risk = round(sum(mods[m]["score"] * w for m, w in avail) / tw, 1)
    else:
        risk = None
    rcode, rcn = band_of(risk) if risk is not None else ("na", "未获取")

    # 资产矩阵
    assets = []
    for aid, aname, tk in ASSETS:
        num, den, contrib = 0.0, 0.0, []
        pv = (mods.get("policy") or {}).get("preview") or {}
        for m in ("policy", "macro", "industry", "tech"):
            mo = mods.get(m)
            if not mo:
                continue
            e = ELASTIC[m][aid]
            w = WEIGHTS[m]
            if mo["score"] is None:
                # 政策未 confirmed：补一条参考贡献，只为让「四层」在每张资产卡里都露出来，
                # 不进 num/den，合计行与资产分不受影响
                if m == "policy" and pv.get("score") is not None:
                    contrib.append({"m": m, "e": e, "pending": True,
                                    "v": round(w * (pv["score"] / 100.0) * e * 100, 1)})
                continue
            if mo["weak"]:
                continue
            num += w * (mo["score"] / 100.0) * e
            den += w * abs(e)
            contrib.append({"m": m, "e": e, "v": round(w * (mo["score"] / 100.0) * e * 100, 1)})
        sc = round(num / den * 100, 1) if den else None
        code, cn = band_of(sc) if sc is not None else ("na", "未获取")
        fl = tech_flags.get(aid, {})
        assets.append({"id": aid, "name": aname, "ticker": tk, "score": sc,
                       "band": code, "label": cn, "tech": None,
                       "broken": fl.get("broken", False),
                       "broken_strong": fl.get("strong", False),
                       "broken_note": fl.get("note", ""),
                       "px": (P.get(tk) or {}).get("close", [None])[-1],
                       "px_date": (P.get(tk) or {}).get("dates", [None])[-1],
                       "contrib": contrib,
                       "tech_items": [],
                       "tech_note": ""})

    # 确认层
    conf = {}
    vix, vix3 = S.get("VIX"), S.get("VIX3M")
    if vix and vix3 and vix["values"] and vix3["values"]:
        r = vix["values"][-1] / vix3["values"][-1]
        conf["vix_ratio"] = {"v": round(r, 3), "date": vix["dates"][-1],
                             "state": "backwardation" if r >= 1.0 else "contango",
                             "note": "期限结构倒挂=真恐慌" if r >= 1.0 else "常态"}
    hy = S.get("BAMLH0A0HYM2")
    if hy:
        ch = change_over(hy["dates"], hy["values"], 20)
        conf["hy_chg"] = {"v": round(ch * 100, 1) if ch is not None else None,
                          "date": hy["dates"][-1],
                          "state": "widen" if (ch or 0) * 100 > 30 else "flat",
                          "note": "走阔>30bp 视为信用事件" if (ch or 0) * 100 > 30 else "信用端尚未确认"}
    qq, tl = P.get("QQQ"), P.get("TLT")
    if qq and tl:
        qd = dict(zip(qq["dates"], qq["close"]))
        td = dict(zip(tl["dates"], tl["close"]))
        common = sorted(set(qd) & set(td))[-62:]
        if len(common) > 40:
            qr = [qd[common[i]] / qd[common[i - 1]] - 1 for i in range(1, len(common))]
            tr = [td[common[i]] / td[common[i - 1]] - 1 for i in range(1, len(common))]
            try:
                corr = st.correlation(qr, tr)
            except Exception:  # noqa: BLE001
                corr = None
            if corr is not None:
                conf["corr"] = {"v": round(corr, 3), "date": common[-1],
                                "state": "flip" if corr > 0.2 else "normal",
                                "note": "正相关=避险失效，债券对冲项作废" if corr > 0.2 else "负相关，避险仍有效"}
    gl, r10 = P.get("GLD"), S.get("DFII10")
    if gl and r10 and gl.get("close") and len(gl["close"]) > 21:
        gch = (gl["close"][-1] / gl["close"][-21] - 1) * 100
        dch = change_over(r10["dates"], r10["values"], 30)
        credit = (dch or 0) * 100 > 5 and gch > 0
        conf["gold_real"] = {
            "v": round(gch, 1), "date": gl["dates"][-1],
            "state": "credit" if credit else "rate",
            # 「金价跌、实际利率升」是反向关系，说的正是交易利率；
            # 只有两者背离（利率升、金还涨）才说明市场在交易信用。
            "note": ("实际利率上行而金价仍涨 → 交易的是信用，不是利率" if credit
                     else "实际利率上行、金价下跌 → 交易的是利率")}

    # 供给不足提示
    warnings = []
    if mods["policy"]["score"] is None:
        warnings.append("政策模块无 confirmed 条目：20% 权重由其他模块按比例分摊，政策参考分仅示意")
    elif mods["policy"]["weak"]:
        warnings.append("政策模块仅 1 条 confirmed，权重减半")
    for e in raw.get("errors", []):
        warnings.append("未获取：%s %s" % (e["id"], e["error"]))

    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    out = {
        "as_of": today,
        "fetched_at": raw.get("fetched_at"),
        "horizon": "medium",
        "horizon_label": "中期 1~6 个月",
        "weights": WEIGHTS,
        "elastic": ELASTIC,
        "modules": mods,
        "risk_appetite": {"score": risk, "band": rcode, "label": rcn,
                          "modules_used": [m for m, _ in avail]},
        "assets": assets,
        "groups": [{"id": g, "name": n, "ids": ids} for g, n, ids in GROUPS],
        "local_rates": build_local_rates(raw.get("local_rates") or {}, S),
        "confirm": conf,
        "indicators": ind + tech_ind,
        "policy": {"rows": pol_rows, "preview_score": pol_preview},
        "warnings": warnings,
    }
    with open(os.path.join(DATA, "score.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.makedirs(os.path.join(DATA, "snapshot"), exist_ok=True)
    with open(os.path.join(DATA, "snapshot", today + ".json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump({"as_of": today, "modules": {k: v["score"] for k, v in mods.items()},
                   "risk": risk,
                   "assets": {a["id"]: a["score"] for a in assets},
                   "indicators": {i["id"]: i["band"] for i in ind}}, f,
                  ensure_ascii=False, indent=1)
        f.write("\n")

    print("as_of %s  风险偏好 %s (%s)" % (today, risk, rcn))
    for m in ("policy", "macro", "industry", "tech"):
        mo = mods[m]
        print("  %-9s %7s  %-4s  disp=%-5s n=%d%s" % (
            m, mo["score"], mo["label"], mo["disp"], mo["n"],
            "  [强制降级]" if mo["downgraded"] else ""))
    for a in assets:
        flag = "  [已破位]" if a["broken"] else ""
        flag = "  [强破位]" if a["broken_strong"] else flag
        print("  %-8s %7s  %-4s%s" % (a["name"], a["score"], a["label"], flag))
    for w in warnings:
        print("  [提示] " + w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
