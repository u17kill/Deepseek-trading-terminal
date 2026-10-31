import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "oos")

WIN = 250
W = {"pos": 0.40, "crowd": 0.35, "mom": 0.25}


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

    turn, amt = df["turnover"], df["amount"]
    s_turn = pr(turn)
    s_amt = pr(amt / amt.rolling(20).mean())
    pv = c.pct_change().rolling(20).corr(turn.pct_change())
    s_pv = (pv.clip(-1, 1) + 1) * 50
    o["s_crowd"] = 0.50 * s_turn + 0.30 * s_amt + 0.20 * s_pv

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
    return o


def fwd_stats(close, n):
    c = close.values
    L = len(c)
    ret = np.full(L, np.nan)
    dmin = np.full(L, np.nan)
    dmax = np.full(L, np.nan)
    for i in range(L - n):
        w = c[i + 1 : i + 1 + n]
        if len(w) < n:
            continue
        ret[i] = w[-1] / c[i] - 1
        dmin[i] = w.min() / c[i] - 1
        dmax[i] = w.max() / c[i] - 1
    return ret * 100, dmin * 100, dmax * 100


def bk(s):
    return pd.cut(s, [-0.1, 15, 30, 70, 85, 100.1],
                  labels=["极端超卖", "警戒超卖", "正常", "警戒超买", "极端超买"])


def run(name, fn):
    df = pd.read_csv(os.path.join(OUT, fn))
    df["date"] = df["date"].astype(str).str.strip()   # v3.1 修正：原始CSV日期带前后空格，导致事件表 slice 错位
    o = build(df).dropna(subset=["score"]).reset_index(drop=True)
    o["bk"] = bk(o["score"])

    for n in (1, 5, 10, 20):
        r, lo, hi = fwd_stats(o["close"], n)
        o[f"r{n}"], o[f"lo{n}"], o[f"hi{n}"] = r, lo, hi

    res = {"name": name, "start": o.date.iloc[0], "end": o.date.iloc[-1], "n": len(o)}

    last = o.iloc[-1]
    res["last"] = {
        "date": last.date, "score": round(last.score, 1), "close": round(last.close, 2),
        "rsi6": round(last.rsi6, 1), "bias20": round(last.bias20, 2), "bollpos": round(last.bollpos, 1),
        "s_pos": round(last.s_pos, 1), "s_crowd": round(last.s_crowd, 1), "s_mom": round(last.s_mom, 1),
        "bk": str(last.bk),
    }

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


def main():
    out = {n: run(n, f) for n, f in [("科创50", "kc50.csv"), ("创业板指", "cyb.csv")]}
    with open(os.path.join(OUT, "result_v3_1.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for n, r in out.items():
        print(f"\n===== {n}  {r['start']} ~ {r['end']}  n={r['n']} =====")
        L = r["last"]
        print(f"最新 {L['date']} 读数 {L['score']} [{L['bk']}]  位置{L['s_pos']} 拥挤{L['s_crowd']} 动能{L['s_mom']}")
        print("分档占比:", r["dist"])
        print(f"{'档位':<6}{'天数':>6}{'5日':>9}{'20日':>9}{'20日均回撤':>11}{'20日均上冲':>11}{'20日胜率':>9}")
        for b in r["buckets"]:
            if b["n"]:
                print(f"{b['bk']:<6}{b['n']:>6}{b['ret5']:>9}{b['ret20']:>9}{b['md20']:>11}{b['mu20']:>11}{b['win20']:>9}")
        print("平均维持天数:", r["dwell"])
        print("IC 复合 vs RSI6:", {k: v for k, v in r["ic"].items()})
        print("年度极值对照:")
        for y in r["year_extremes"]:
            print(f"  {y['y']} 高点 {y['hi_d']} 读数{y['hi_s']} | 低点 {y['lo_d']} 读数{y['lo_s']}")
        print("极端状态持续>=3天的事件:")
        for e in r["events"]:
            print(f"  {e['bk']} {e['from']}~{e['to']} {e['days']}天 极值{e['peak']}")


if __name__ == "__main__":
    main()
