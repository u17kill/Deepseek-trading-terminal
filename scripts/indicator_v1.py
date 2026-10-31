import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "oos")

WINDOW_RANK = 250
WINDOW_FINAL = 750
W_POS, W_CROWD, W_MOM = 0.40, 0.35, 0.25


def rsi(close, n=6):
    d = close.diff()
    gain = d.clip(lower=0)
    loss = (-d).clip(lower=0)
    ag = gain.ewm(alpha=1 / n, adjust=False).mean()
    al = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = ag / al.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def pct_rank(s, win=WINDOW_RANK):
    return s.rolling(win, min_periods=int(win * 0.6)).rank(pct=True) * 100


def build(df):
    c, h, l = df["close"], df["high"], df["low"]
    out = pd.DataFrame({"date": df["date"], "close": c})

    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    upper, lower = ma20 + 2 * sd20, ma20 - 2 * sd20
    bias20 = c / ma20 - 1
    bollpos = ((c - lower) / (upper - lower)).clip(0, 1)
    r6 = rsi(c, 6)

    out["bias20"] = bias20 * 100
    out["bollpos"] = bollpos * 100
    out["rsi6"] = r6

    s_bias = pct_rank(bias20)
    s_boll = pct_rank(bollpos)
    s_rsi = r6.clip(0, 100)
    out["s_pos"] = W_POS * (0.40 * s_bias + 0.30 * s_boll + 0.30 * s_rsi)

    turn = df["turnover"]
    amt = df["amount"]
    s_turn = pct_rank(turn)
    amt_ratio = amt / amt.rolling(20).mean()
    s_amt = pct_rank(amt_ratio)
    s_amtsd = pct_rank(amt.rolling(20).std() / amt.rolling(20).mean())
    out["s_crowd"] = W_CROWD * (0.50 * s_turn + 0.30 * s_amt + 0.20 * s_amtsd)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea
    rsi_slope = r6.diff(5)
    hist_slope = hist.diff(5)
    pv_corr = c.pct_change().rolling(20).corr(turn.pct_change())

    s_rslope = pct_rank(rsi_slope)
    s_hslope = pct_rank(hist_slope)
    s_pv = (pv_corr.clip(-1, 1) + 1) * 50
    out["s_mom"] = W_MOM * (0.40 * s_rslope + 0.40 * s_hslope + 0.20 * s_pv)

    raw = out["s_pos"] + out["s_crowd"] + out["s_mom"]
    out["raw"] = raw
    out["score"] = pct_rank(raw, WINDOW_FINAL)
    out["macd_hist"] = hist
    return out


def fwd_ret(c, n):
    return c.shift(-n) / c - 1


def bucket(s):
    return pd.cut(
        s,
        [-0.1, 15, 30, 70, 85, 100.1],
        labels=["极端超卖≤15", "警戒超卖15-30", "正常30-70", "警戒超买70-85", "极端超买≥85"],
    )


def analyse(df, name, res):
    d = df.dropna(subset=["score"]).copy()
    d["bucket"] = bucket(d["score"])
    for n in (1, 5, 10, 20):
        d[f"f{n}"] = fwd_ret(d["close"], n)

    res["meta"] = {
        "name": name,
        "start": d["date"].iloc[0],
        "end": d["date"].iloc[-1],
        "n": int(len(d)),
        "last_score": round(float(d["score"].iloc[-1]), 1),
        "last_date": d["date"].iloc[-1],
        "last_parts": {
            "bias20": round(float(d["bias20"].iloc[-1]), 2),
            "bollpos": round(float(d["bollpos"].iloc[-1]), 1),
            "rsi6": round(float(d["rsi6"].iloc[-1]), 1),
            "s_pos": round(float(d["s_pos"].iloc[-1] / W_POS), 1),
            "s_crowd": round(float(d["s_crowd"].iloc[-1] / W_CROWD), 1),
            "s_mom": round(float(d["s_mom"].iloc[-1] / W_MOM), 1),
        },
    }

    dist = d["bucket"].value_counts(normalize=True).sort_index() * 100
    res["dist"] = {str(k): round(float(v), 1) for k, v in dist.items()}

    rows = []
    for b, g in d.groupby("bucket", observed=False):
        row = {"bucket": str(b), "days": int(len(g))}
        for n in (1, 5, 10, 20):
            v = g[f"f{n}"].dropna()
            row[f"mean{n}"] = round(float(v.mean() * 100), 2) if len(v) else None
            row[f"win{n}"] = round(float((v > 0).mean() * 100), 1) if len(v) else None
        rows.append(row)
    res["fwd"] = rows

    ic = {}
    for n in (1, 5, 10, 20):
        sub = d[["score", "rsi6", f"f{n}"]].dropna()
        r = sub[f"f{n}"].rank()
        ic[f"ic{n}"] = round(float(sub["score"].rank().corr(r)), 4)
        ic[f"ic_rsi{n}"] = round(float(sub["rsi6"].rank().corr(r)), 4)
    res["ic"] = ic

    res["series"] = {
        "date": d["date"].tolist(),
        "score": [round(float(x), 1) for x in d["score"]],
        "close": [round(float(x), 2) for x in d["close"]],
        "s_pos": [round(float(x / W_POS), 1) for x in d["s_pos"]],
        "s_crowd": [round(float(x / W_CROWD), 1) for x in d["s_crowd"]],
        "s_mom": [round(float(x / W_MOM), 1) for x in d["s_mom"]],
        "rsi6": [round(float(x), 1) if pd.notna(x) else None for x in d["rsi6"]],
    }
    return res


def main():
    allres = {}
    for fn, name in [("kc50.csv", "科创50"), ("cyb.csv", "创业板指")]:
        df = pd.read_csv(os.path.join(OUT, fn))
        ind = build(df)
        allres[name] = analyse(ind, name, {})
        ind.to_csv(os.path.join(OUT, fn.replace(".csv", "_ind.csv")), index=False)

    with open(os.path.join(OUT, "result_v1.json"), "w", encoding="utf-8") as f:
        json.dump(allres, f, ensure_ascii=False, indent=1)

    for name, r in allres.items():
        m = r["meta"]
        print(f"\n===== {name}  {m['start']} ~ {m['end']}  n={m['n']} =====")
        print(f"当前读数 {m['last_score']}  (分模块: {m['last_parts']})")
        print("分档占比:", r["dist"])
        for row in r["fwd"]:
            print(
                f"  {row['bucket']:<14} n={row['days']:<4} 1d={row['mean1']}% "
                f"5d={row['mean5']}% 10d={row['mean10']}% 20d={row['mean20']}% "
                f"胜率20d={row['win20']}%"
            )
        print("IC(复合):", {k: v for k, v in r["ic"].items() if k.startswith("ic") and "rsi" not in k})
        print("IC(纯RSI6):", {k: v for k, v in r["ic"].items() if "rsi" in k})


if __name__ == "__main__":
    main()
