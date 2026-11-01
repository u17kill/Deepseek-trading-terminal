"""亚太五地指数温度（第六页，v1.7.0）。

设计边界（与 A 股页 / 港股页刻意不同）
--------------------------------------
本页只做**位置、动能、相对强弱**三件事，不做量能、拥挤、估值、宽度。

原因是数据本身不允许：
  - 日经、新加坡 STI 的成交量恒为 0（指数不发布成交量）；
  - KOSPI 有量无成交额；台湾加权 1000 天里有 402 天成交量为 0；
  - 五地都没有成分股清单接口，更没有行业分类。
勉强凑出「量能」只会得到五套互不通用的特例口径，所以整块砍掉。

指标口径
--------
位置（s_pos）与动能（s_mom）**完全复用 indicator_core.build()**，与 A 股页逐字相同，
因此「位置 62」在两页是同一个含义。不合成总分：A 股总分的 35% 来自成交额占比，
本页拿不到该分母，合成出来会是另一种刻度，跨页一读就错。

样本长度不一致（重要）
----------------------
五地日线各 1000 根，起点 2022-09~11；港股 780 根，起点 2023-09；基准更长。
所以卡片横比「当前分位」是安全的，**横比历史极端天数、停留时长则无效**，
页面每张卡都标注自己的样本区间。
"""
import datetime
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_core as core  # noqa: E402
import wsfeed  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "data", "apac")
OUT_JSON = os.path.join(OUTDIR, "apac.json")
HSI_CACHE = os.path.join(OUTDIR, "hkHSI.csv")

# (地区, 指数名, 新浪代码, 短名)
MKTS = [("日本", "日经225", "NKY", "日经"),
        ("韩国", "KOSPI", "KOSPI", "韩国"),
        ("中国台湾", "台湾加权", "TWJQ", "台湾"),
        ("印度", "SENSEX", "SENSEX", "印度"),
        ("新加坡", "STI", "STI", "新加坡")]

# 基准：读已有缓存，不重复取数。IVV 为标普500 的 ETF 代理，与估值页同源。
BENCH = [("SPX", "标普500", os.path.join(BASE, "data", "us", "usIVV.csv")),
         ("CSI300", "沪深300", os.path.join(BASE, "data", "oos", "hs300.csv"))]

HORIZONS = [5, 20, 60, 120]
RS_H = [20, 60, 120]
CURVE_N = 120                 # 相对强弱曲线长度（交易日）
CORR_N = 60                   # 相关性矩阵窗口
HSI_DAYS = 900                # 港股参照取 900 根，与五地量级接近
SINA_URL = "https://gi.finance.sina.com.cn/hq/daily?symbol=%s&num=10000"
SINA_TRY = 3

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://finance.sina.com.cn"}


def _f(x):
    v = pd.to_numeric(x, errors="coerce")
    return None if pd.isna(v) else float(v)


# ---------------------------------------------------------------- 取数
def sina_daily(sym):
    """新浪全球指数日线。返回 DataFrame[date, open, high, low, close, volume]。

    这台机器上东财 push2his 完全不通（连 A 股指数都返回空），日韩台印新只能走
    新浪 gi 接口。该接口不给成交额，也不给涨跌幅，都自己算。
    """
    last = ""
    for i in range(SINA_TRY):
        try:
            r = requests.get(SINA_URL % sym, headers=UA, timeout=25)
            j = r.json()
            data = (j.get("result") or {}).get("data")
            if isinstance(data, dict):
                data = list(data.values())
            if not data:
                last = str(j.get("message") or "空返回")[:60]
            else:
                df = pd.DataFrame(data)
                for a, b in (("o", "open"), ("h", "high"),
                             ("l", "low"), ("c", "close"), ("v", "volume")):
                    df[b] = pd.to_numeric(df[a], errors="coerce")
                df["date"] = df["d"].astype(str)
                df = (df[["date", "open", "high", "low", "close", "volume"]]
                      .sort_values("date").reset_index(drop=True))
                df["date"] = df["date"].astype(str)
                # 新浪对部分休市日/异常日会返回 close=0（SENSEX 实测 4 天）。
                # 不清掉的话 pct_change 直接变 inf，20 日波动率整段 NaN，
                # 动能分位会退化成「上一次有效值」——看起来有数字，其实是过期的。
                bad = (df["close"] <= 0) | df["close"].isna()
                if bad.any():
                    print("  [清洗] %s 剔除 %d 个零价/空值行" % (sym, int(bad.sum())))
                    df = df[~bad].reset_index(drop=True)
                df = df.drop_duplicates("date", keep="last").reset_index(drop=True)
                return df
        except Exception as e:  # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, str(e)[:60])
        if i < SINA_TRY - 1:
            time.sleep(2 * (i + 1))
    raise RuntimeError("%s 取数失败（%s）" % (sym, last))


def _to_core_df(df, mkt="APAC"):
    """补齐 indicator_core 需要的列。amount / turnover 仅供 build() 的内部降级
    分支使用，本页不读拥挤分项，所以留 0 不影响位置与动能。"""
    d = df.copy()
    d["amount"] = 0.0
    d["turnover"] = 0.0
    d["chg"] = d["close"].pct_change() * 100
    d["market"] = mkt
    return d[["date", "open", "close", "high", "low", "volume", "amount",
              "turnover", "chg", "market"]]


def hsi_ref():
    """恒生指数参照序列。只用于相对强弱对比与象限参考点，**不占卡片**。

    走 wsfeed（限频重试 + 行数校验），成功后落盘缓存；取数失败时退回缓存，
    再失败就让参照点整体缺席——不能让港股一个参照把整页拖挂。
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(HSI_DAYS * 1.5))
    try:
        txt = wsfeed.kline("hkHSI", start.isoformat(), end.isoformat(), strict=False)
        df = core.parse_westock(txt).tail(HSI_DAYS).reset_index(drop=True)
        if len(df) > 60:
            os.makedirs(OUTDIR, exist_ok=True)
            df[["date", "close"]].to_csv(HSI_CACHE, index=False)
            return df, "live"
    except Exception as e:  # noqa: BLE001
        print("[警告] 恒指参照取数失败：%s" % e)
    if os.path.exists(HSI_CACHE):
        df = pd.read_csv(HSI_CACHE)
        df["date"] = df["date"].astype(str)
        return df, "cache"
    return None, "missing"


def bench(path):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, usecols=["date", "close"])
    df["date"] = df["date"].astype(str)
    return df.dropna().drop_duplicates("date").reset_index(drop=True)


# ---------------------------------------------------------------- 计算
def _last(s, strict=True):
    """取序列末值。

    strict=True 时**只认最后一行**，NaN 就返回 None。旧写法 dropna() 后取末值，
    会把「末行无效」悄悄替换成「上一次有效值」——页面上照样显示一个数，但那个数
    是几天前的，且没有任何标记。宁可显示「—」。
    """
    if not len(s):
        return None
    v = s.iloc[-1]
    if pd.isna(v):
        return None
    return float(v)


def label(p):
    """分位标签。注意这是**分位本身的档位**，不是 A 股那种总分档位——
    两者数值区间相同但含义不同，不能混着读。"""
    if p is None:
        return "—"
    if p >= 85:
        return "极热"
    if p >= 70:
        return "偏热"
    if p > 30:
        return "中性"
    if p > 15:
        return "偏冷"
    return "极冷"


def card(region, name, sym, short, df):
    o = core.build(_to_core_df(df))
    c = df["close"]
    r = {}
    for n in HORIZONS:
        r["r%d" % n] = round(float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None
    pos, mom = _last(o["s_pos"]), _last(o["s_mom"])
    return {
        "region": region, "name": name, "sym": sym, "short": short,
        "close": round(float(c.iloc[-1]), 2),
        "chg": round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2) if len(c) > 1 else None,
        "pos": round(pos, 1) if pos is not None else None,
        "mom": round(mom, 1) if mom is not None else None,
        "label": label(pos),
        "rsi6": round(_last(o["rsi6"]), 1) if _last(o["rsi6"]) is not None else None,
        "dd250": round(_last(o["dd250"]), 1) if _last(o["dd250"]) is not None else None,
        "n": int(len(df)), "start": str(df["date"].iloc[0]), "end": str(df["date"].iloc[-1]),
        **r,
    }


def _asof(series_df, dates):
    """把基准收盘对齐到目标市场的交易日上（取当日或之前最近一个收盘）。

    为什么必须对齐：各市场交易日数不同，同样「最近 60 根」跨的日历长度不一样。
    实测 2026-10-30 这天，台湾 61 根回溯到 07-31，而标普 61 根回溯到 08-06——
    两边根本不是同一个时间窗。直接相减得到的「超额」里，有一半是窗口错位造成的。

    对齐方式：按**同一日历窗口**取各自收盘（同一天用当天收盘），碰到对方休市就往
    前找最近一个有效收盘。这不是「隔夜效应」——它是「同期表现差」，即谁在这个
    时间窗里跑得更好，与谁先开市无关。
    """
    b = series_df.set_index("date")["close"]
    b.index = b.index.astype(str)
    b = b[~b.index.duplicated(keep="last")].sort_index()
    return b.reindex(list(dates), method="ffill")


def rs_table(mkt_df, bench_df, n_list=RS_H):
    """区间表现分解：本地收益 / 同期基准收益 / 超额（均为百分点）。

    三项都给出来，是为了让页面上的「超额」可校验——否则读者拿卡片的本地涨幅
    去减基准涨幅，会发现对不上，而原因只是窗口对齐方式不同。
    """
    d = list(mkt_df["date"])
    b = _asof(bench_df, d)
    c = mkt_df["close"].reset_index(drop=True)
    out = {}
    for n in n_list:
        key = "r%d" % n
        if len(d) <= n:
            out[key] = None
            continue
        loc = float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100
        bv0, bv1 = b.iloc[-1 - n], b.iloc[-1]
        if pd.isna(bv0) or pd.isna(bv1) or not bv0:
            out[key] = {"loc": round(loc, 2), "bench": None, "ex": None}
            continue
        bch = float(bv1 / bv0 - 1) * 100
        out[key] = {"loc": round(loc, 2), "bench": round(bch, 2),
                    "ex": round(loc - bch, 2)}
    return out


def rs_curve(mkt_df, bench_df, n=CURVE_N):
    """累计相对收益曲线（百分点），以窗口首日为 0。

    与 rs_table 同一套 asof 对齐，且窗口取 n+1 个点（n 个收益），
    保证曲线末值 = 表格里的「120 日超额」，两个模块不会互相打脸。
    """
    d = list(mkt_df["date"])[-(n + 1):]
    b = _asof(bench_df, d)
    m = mkt_df["close"].tail(n + 1).reset_index(drop=True)
    b = b.reset_index(drop=True)
    if len(m) < 20 or b.isna().sum() > len(b) * 0.2:
        return None
    m0, b0 = m.iloc[0], b.iloc[0]
    if not m0 or not b0 or pd.isna(b0):
        return None
    rel = ((m / m0 - 1) - (b / b0 - 1)) * 100
    # 每 2 个交易日取一点：120 天折线在页面上像素不够，抽稀后形状不变
    step = 2
    idx = list(range(0, len(rel), step))
    if idx[-1] != len(rel) - 1:
        idx.append(len(rel) - 1)
    return {"dates": [d[i] for i in idx],
            "pts": [round(float(rel.iloc[i]), 2) for i in idx],
            "last": round(float(rel.iloc[-1]), 2)}


def corr_matrix(frames):
    """日收益相关性。各市场节假日不同，只能 inner join，样本会少于 60 天。"""
    ser = {}
    for k, df in frames.items():
        if df is None or len(df) < 30:
            continue
        s = df.set_index("date")["close"].astype(float)
        ser[k] = s.pct_change()
    if len(ser) < 2:
        return None
    j = pd.concat(ser, axis=1, join="inner").dropna().tail(CORR_N)
    if len(j) < 20:
        return None
    c = j.corr()
    names = list(c.columns)
    return {"names": names,
            "m": [[round(float(c.iloc[i, jj]), 2) for jj in range(len(names))]
                  for i in range(len(names))],
            "n": int(len(j)),
            "from": str(j.index[0]), "to": str(j.index[-1])}


# ---------------------------------------------------------------- 组装
def build():
    t0 = time.time()
    markets, frames, rs, curves, bench_stat = [], {}, {}, {}, {}
    src = {}

    for region, name, sym, short in MKTS:
        df = sina_daily(sym)
        frames[short] = df
        markets.append(card(region, name, sym, short, df))
        print("  %-4s %-9s %d 根  %s ~ %s  位置 %s / 动能 %s"
              % (short, name, len(df), df["date"].iloc[0], df["date"].iloc[-1],
                 markets[-1]["pos"], markets[-1]["mom"]))
        src[short] = {"source": "新浪 gi", "rows": int(len(df))}

    hsi, hsi_src = hsi_ref()
    ref = None
    if hsi is not None and len(hsi) > 60:
        hsi_c = card("中国香港", "恒生指数", "hkHSI", "恒指", hsi)
        hsi_c["is_ref"] = True
        ref = hsi_c
        frames["恒指"] = hsi
        print("  恒指（参照）%d 根  位置 %s / 动能 %s  [%s]"
              % (len(hsi), ref["pos"], ref["mom"], hsi_src))

    bl = {}
    for key, label_, path in BENCH:
        b = bench(path)
        if b is None or len(b) < 150:
            print("[警告] 基准 %s 缺失或过短：%s" % (key, path))
            continue
        bl[key] = b
        bench_stat[key] = {
            "name": label_,
            "r20": round(float(b["close"].iloc[-1] / b["close"].iloc[-21] - 1) * 100, 2),
            "r60": round(float(b["close"].iloc[-1] / b["close"].iloc[-61] - 1) * 100, 2),
            "r120": round(float(b["close"].iloc[-1] / b["close"].iloc[-121] - 1) * 100, 2),
            "n": int(len(b)), "start": str(b["date"].iloc[0]), "end": str(b["date"].iloc[-1]),
        }

    # 相对强弱：每个基准一张表 + 一条曲线
    for key, b in bl.items():
        rows = []
        for m in markets:
            rows.append({"short": m["short"], "region": m["region"],
                         **rs_table(frames[m["short"]], b)})
        if ref is not None:
            rows.append({"short": "恒指", "region": "中国香港",
                         "is_ref": True, **rs_table(hsi, b)})
        rs[key] = rows

        cs = []
        for m in markets:
            cv = rs_curve(frames[m["short"]], b)
            if cv:
                cs.append({"short": m["short"], "region": m["region"], **cv})
        if ref is not None:
            cv = rs_curve(hsi, b)
            if cv:
                cs.append({"short": "恒指", "region": "中国香港", "is_ref": True, **cv})
        curves[key] = {"dates": cs[0]["dates"] if cs else [],
                       "series": [{"short": c["short"], "region": c["region"],
                                   "is_ref": c.get("is_ref", False),
                                   "pts": c["pts"], "last": c["last"]} for c in cs]}

    cm = corr_matrix(frames)

    p = {
        "date": max(m["end"] for m in markets),
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t0, 1),
        "markets": markets,
        "ref": ref,
        "ref_src": hsi_src,
        "bench": bench_stat,
        "rs": rs,
        "curves": curves,
        "corr": cm,
        "sources": src,
        "horizons": HORIZONS,
    }
    p["notes"] = _notes(p)
    return p


def _notes(p):
    """自动生成的边界说明。数字都是从本次结果里取的，不写死。"""
    n = []
    short = [m for m in p["markets"] if m["n"] < 1000]
    if short:
        n.append("样本不足 1000 根的标的：%s。分位只在各自样本内可比，横比历史极端天数无效。"
                 % "、".join("%s（%d 根）" % (m["short"], m["n"]) for m in short))
    n.append("位置与动能复用 A 股页同一套公式（位置=乖离/布林/回撤三合一，"
             "动能=RSI6 与 10 日风险调整动量各半），因此「位置 62」两页同义；"
             "但本页不合成总分——缺成交额占比这一项，合成出来是另一种刻度。")
    n.append("相对强弱按同一日历窗口对齐：各市场取同一天的收盘，对方休市则往前取最近"
             "有效收盘。不能直接比「最近 N 根」——各市场交易日数不同，同样 60 根跨的"
             "日历长度会差好几天。所以卡片上的本地涨幅与超额里的本地收益口径一致，"
             "但基准那一列只能用对齐后的值。")
    if p["corr"]:
        n.append("相关性矩阵受节假日影响，只取各市场共同交易日，本次样本 %d 天。"
                 % p["corr"]["n"])
    n.append("本页无成交量/额模块：日经与新加坡 STI 成交量恒为 0，KOSPI 有量无额，"
             "台湾加权 40% 的交易日成交量为 0。五地凑不出统一口径，故整块不做。")
    return n


def save(p):
    os.makedirs(OUTDIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, separators=(",", ":"))
    return OUT_JSON


def load():
    if not os.path.exists(OUT_JSON):
        return None
    with open(OUT_JSON, encoding="utf-8") as f:
        return json.load(f)


def main():
    p = build()
    save(p)
    print("\n完成 %.1fs -> %s（%.1f KB）"
          % (p["elapsed"], OUT_JSON, os.path.getsize(OUT_JSON) / 1024))
    print("\n%-6s %10s %8s %7s %7s %7s" % ("地区", "收盘", "当日%", "位置", "动能", "标签"))
    for m in p["markets"]:
        print("%-6s %10.2f %8.2f %7s %7s %7s"
              % (m["short"], m["close"], m["chg"] or 0, m["pos"], m["mom"], m["label"]))
    if p["ref"]:
        r = p["ref"]
        print("%-6s %10.2f %8.2f %7s %7s %7s  ← 参照"
              % ("恒指", r["close"], r["chg"] or 0, r["pos"], r["mom"], r["label"]))
    for k, rows in p["rs"].items():
        print("\n相对 %s 超额（百分点，括号内为 本地 / 基准 分解）：" % p["bench"][k]["name"])
        for r in rows:
            cells = []
            for h in RS_H:
                v = r["r%d" % h]
                if not v or v["ex"] is None:
                    cells.append("%d日 %s" % (h, "—"))
                else:
                    cells.append("%d日 %+6.2f (%+.2f/%+.2f)"
                                 % (h, v["ex"], v["loc"], v["bench"]))
            print("  %-6s %s%s" % (r["short"], " | ".join(cells),
                                   "  ← 参照" if r.get("is_ref") else ""))


if __name__ == "__main__":
    main()
