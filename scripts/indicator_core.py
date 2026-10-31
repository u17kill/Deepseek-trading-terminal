"""指标计算核心：与 indicator_v3_1.py 算法完全一致，供脚本与本地服务共用。

改动仅限于把 run() 的入参从「文件名」改为「DataFrame」，新增 westock 输出解析，
计算逻辑一行未动。
"""
import re

import numpy as np
import pandas as pd

WIN = 250
W = {"pos": 0.40, "crowd": 0.35, "mom": 0.25}
COLS = ["date", "open", "close", "high", "low", "volume", "amount", "turnover", "chg"]


def rsi(close, n=6):
    d = close.diff()
    ag = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    al = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ag / al.replace(0, 1e-12))


def pr(s, win=WIN):
    return s.rolling(win, min_periods=int(win * 0.7)).rank(pct=True) * 100


def build(df):
    c = df["close"]
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    bias20 = c / ma20 - 1
    bollpos = ((c - (ma20 - 2 * sd20)) / (4 * sd20)).clip(0, 1)
    r6 = rsi(c, 6)

    o = pd.DataFrame({"date": df["date"], "close": c})

    s_bias = pr(bias20)
    s_boll = pr(bollpos)
    dd250 = (c / c.rolling(250, min_periods=120).max() - 1) * 100
    s_dd = pr(dd250)
    o["dd250"] = dd250
    o["s_pos"] = 0.40 * s_bias + 0.30 * s_boll + 0.30 * s_dd

    # ---------- 拥挤 35% ----------
    # v4：成交额占两市比重 50% + 20 日已实现波动率分位 30% + 换手率分位 20%
    # 依据：前两项与位置模块相关性仅 0.26~0.29 与 -0.03~0.05，是真独立信息；
    #       原「成交额/20日均额」与「量价相关」与位置相关 0.54~0.60，已剔除。
    #       两融经检验与位置相关 0.64（杠杆追涨），不纳入分数，仅作观察项。
    turn, amt = df["turnover"], df["amount"]
    s_turn = pr(turn)
    vol20 = c.pct_change().rolling(20).std()
    s_vol = pr(vol20)
    o["vol20"] = vol20 * 100

    # 美股：amt_all 为 SPY 成交额（分母），turnover 为成交量（换手率代理）
    is_us = "market" in df.columns and (df["market"].astype(str) == "US").any()

    has_ctx = "amt_all" in df.columns and df["amt_all"].notna().sum() > 0.8 * len(df)
    if has_ctx:
        share = (amt / df["amt_all"] * 100).ffill()
        s_share = pr(share)
        o["share"] = share
        o["s_crowd"] = 0.50 * s_share + 0.30 * s_vol + 0.20 * s_turn
        o["c_share"], o["c_vol"], o["c_turn"] = s_share, s_vol, s_turn
        crowd_mode = "v4-us" if is_us else "v4"
    else:
        # 降级：缺少全市场成交额时退回旧口径，结果标记为 legacy
        s_amt = pr(amt / amt.rolling(20).mean())
        pv = c.pct_change().rolling(20).corr(turn.pct_change())
        o["s_crowd"] = 0.50 * s_turn + 0.30 * s_amt + 0.20 * ((pv.clip(-1, 1) + 1) * 50)
        crowd_mode = "legacy"

    if "lev" in df.columns and df["lev"].notna().sum() > 0.5 * len(df):
        # 杠杆环境：仅作观察项，不进入分数（与位置相关 0.64，非独立维度）
        o["lev"] = df["lev"]
        o["s_lev"] = pr(df["lev"])

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea

    vol20 = c.pct_change().rolling(20).std()
    ra10 = (c / c.shift(10) - 1) / (vol20 * np.sqrt(10))
    o["ra10"] = ra10
    o["s_mom"] = 0.50 * r6 + 0.50 * pr(ra10)

    raw = W["pos"] * o["s_pos"] + W["crowd"] * o["s_crowd"] + W["mom"] * o["s_mom"]
    o["score"] = raw.ewm(span=3, adjust=False).mean()
    o["rsi6"] = r6
    o["bias20"] = bias20 * 100
    o["bollpos"] = bollpos * 100
    o["crowd_mode"] = crowd_mode
    return o


def fwd_stats(close, n):
    c = close.values
    L = len(c)
    ret = np.full(L, np.nan)
    dmin = np.full(L, np.nan)
    dmax = np.full(L, np.nan)
    for i in range(L - n):
        w = c[i + 1: i + 1 + n]
        if len(w) < n:
            continue
        ret[i] = w[-1] / c[i] - 1
        dmin[i] = w.min() / c[i] - 1
        dmax[i] = w.max() / c[i] - 1
    return ret * 100, dmin * 100, dmax * 100


def bk(s):
    return pd.cut(s, [-0.1, 15, 30, 70, 85, 100.1],
                  labels=["极端超卖", "警戒超卖", "正常", "警戒超买", "极端超买"])


def parse_westock(text):
    """解析 westock kline 的 markdown 表格输出。"""
    rows = []
    for ln in text.splitlines():
        if ln.count("|") < 8:
            continue
        parts = [p.strip() for p in ln.strip().strip("|").split("|")]
        if len(parts) < 9:
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
            continue
        rows.append(parts[:9])
    if not rows:
        raise ValueError("未解析到任何 K 线数据，请检查 westock 输出格式")
    df = pd.DataFrame(rows, columns=COLS)
    for c in COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return df


def run(name, df):
    """df 需含 COLS 各列。返回与 result_v3_1.json 相同的结构。"""
    o = build(df).dropna(subset=["score"]).reset_index(drop=True)
    o["bk"] = bk(o["score"])

    for n in (1, 5, 10, 20):
        r, lo, hi = fwd_stats(o["close"], n)
        o[f"r{n}"], o[f"lo{n}"], o[f"hi{n}"] = r, lo, hi

    res = {"name": name, "start": o.date.iloc[0], "end": o.date.iloc[-1], "n": len(o)}

    last = o.iloc[-1]
    res["crowd_mode"] = str(last.crowd_mode)
    res["last"] = {
        "date": last.date, "score": round(last.score, 1), "close": round(last.close, 2),
        "rsi6": round(last.rsi6, 1), "bias20": round(last.bias20, 2), "bollpos": round(last.bollpos, 1),
        "s_pos": round(last.s_pos, 1), "s_crowd": round(last.s_crowd, 1), "s_mom": round(last.s_mom, 1),
        "bk": str(last.bk),
    }
    # 拥挤模块子项（v4 才有细分口径；v4-us 为美股代理口径）
    if res["crowd_mode"] in ("v4", "v4-us"):
        res["last"].update({
            "share": round(float(last.share), 1),
            "vol20": round(float(last.vol20), 2),
            "c_share": round(float(last.c_share), 1),
            "c_vol": round(float(last.c_vol), 1),
            "c_turn": round(float(last.c_turn), 1),
        })

    # 杠杆环境（观察项，两融数据晚一日公布，取最近有效值）
    if "lev" in o.columns and o["lev"].notna().any():
        lv = o.loc[o["lev"].notna()].iloc[-1]
        res["last"]["lev"] = round(float(lv.lev), 2)
        res["last"]["lev_pos"] = round(float(lv.s_lev), 1)
        res["last"]["lev_date"] = lv.date

    res["dist"] = {str(k): round(v, 1) for k, v in (o.bk.value_counts(normalize=True).sort_index() * 100).items()}

    rows = []
    for b, g in o.groupby("bk", observed=False):
        row = {"bk": str(b), "n": len(g)}
        if len(g):
            row["ret5"] = round(g.r5.mean(), 2)
            row["ret20"] = round(g.r20.mean(), 2)
            row["md20"] = round(g.lo20.mean(), 2)
            row["mu20"] = round(g.hi20.mean(), 2)
            row["win20"] = round((g.r20 > 0).mean() * 100, 1)
        rows.append(row)
    res["buckets"] = rows

    ic = {}
    for n in (1, 5, 10, 20):
        sub = o[["score", "rsi6", f"r{n}"]].dropna()
        rr = sub[f"r{n}"].rank()
        ic[f"ic{n}"] = round(float(sub.score.rank().corr(rr)), 4)
        ic[f"rsi{n}"] = round(float(sub.rsi6.rank().corr(rr)), 4)
    res["ic"] = ic

    dwell, cur, cnt = [], None, 0
    for b in o.bk:
        if b == cur:
            cnt += 1
        else:
            if cur is not None:
                dwell.append((str(cur), cnt))
            cur, cnt = b, 1
    dwell.append((str(cur), cnt))
    dw = pd.DataFrame(dwell, columns=["bk", "len"])
    res["dwell"] = {k: round(float(v), 1) for k, v in dw.groupby("bk")["len"].mean().items()}

    o["yy"] = pd.to_datetime(o["date"]).dt.year
    yr = o.groupby("yy").apply(
        lambda g: pd.Series({
            "y": int(g.name),
            "hi_d": g.loc[g.close.idxmax(), "date"],
            "hi_s": round(g.loc[g.close.idxmax(), "score"], 1),
            "lo_d": g.loc[g.close.idxmin(), "date"],
            "lo_s": round(g.loc[g.close.idxmin(), "score"], 1),
        }), include_groups=False).reset_index(drop=True)
    res["year_extremes"] = yr.to_dict("records")

    ev = []
    run_b, run_len, start_i = None, 0, 0
    for i, b in enumerate(o["bk"]):
        if b == run_b:
            run_len += 1
        else:
            if run_b is not None and run_len >= 3 and str(run_b) in ("极端超卖", "极端超买"):
                ev.append({"bk": str(run_b), "from": o["date"].iloc[start_i],
                           "to": o["date"].iloc[i - 1], "days": run_len,
                           "peak": round(float(o["score"].iloc[start_i:i].max() if "超买" in str(run_b)
                                                else o["score"].iloc[start_i:i].min()), 1)})
            run_b, run_len, start_i = b, 1, i
    if run_b is not None and run_len >= 3 and str(run_b) in ("极端超卖", "极端超买"):
        ev.append({"bk": str(run_b), "from": o["date"].iloc[start_i], "to": o["date"].iloc[-1],
                   "days": run_len, "peak": round(float(o["score"].iloc[start_i:].max() if "超买" in str(run_b)
                                                        else o["score"].iloc[start_i:].min()), 1)})
    res["events"] = ev

    res["series"] = {
        "date": o.date.tolist(),
        "score": [round(x, 1) for x in o.score],
        "close": [round(x, 2) for x in o.close],
        "s_pos": [round(x, 1) for x in o.s_pos],
        "s_crowd": [round(x, 1) for x in o.s_crowd],
        "s_mom": [round(x, 1) for x in o.s_mom],
        "rsi6": [round(x, 1) for x in o.rsi6],
    }
    return res


def pack(res, step=4):
    """压缩为前端所需的精简结构（每 step 个交易日取一点）。"""
    s = res["series"]
    idx = list(range(0, len(s["date"]), step))
    if idx[-1] != len(s["date"]) - 1:
        idx.append(len(s["date"]) - 1)
    out = {k: res[k] for k in ("name", "last", "dist", "buckets", "ic", "dwell",
                               "events", "start", "end", "n", "crowd_mode")}
    out["year"] = res["year_extremes"]
    out["series"] = [[s["date"][i], s["score"][i], s["close"][i]] for i in idx]
    return out
