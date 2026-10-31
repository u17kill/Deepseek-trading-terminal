"""美股 ETF 取数：QQQ（纳指 100）、SOXX（半导体）。

与 A 股通道的差异，全部记录在此，避免口径混淆
------------------------------------------------
1. **无换手率、无股本**：westock `quote` 对美股 ETF 的 turnover_rate 与
   total_market_cap 恒为 0，ETF 份额不在返回字段内。因此「换手率分位」改用
   「成交量分位」代理——ETF 份额变动缓慢（申赎以 10%~20%/年 计），
   成交量分位 ≈ 换手率分位。
2. **无全市场成交额**：A 股用「两市成交额」做分母，美股没有对应口径。
   改用 SPY（标普 500 ETF，全市场流动性锚）成交额做分母：
   share = 标的成交额 / SPY 成交额。实测该比值与位置模块的相关性为
   -0.06 / 0.01，比 A 股口径（0.26）更独立。
3. **历史单位错误**：westock 返回的 2022-03-08 之前数据，volume 与 amount
   均为真实值的 100 倍（amount/(volume*close) ≈ 100，之后恒为 1）。
   不修正会让滚动分位在断点处失真，故统一除以 100。
4. **高低价为 0**：2022-01-03 ~ 2022-03-07 共 44 行的 high/low 为 0。
   本指标仅用收盘价（位置、动能均如此），故不影响结果。
5. **样本起点**：美股数据自 2022-01-03 起。DD250 需先累积 120 个值、
   再过 175 个分位样本，因此有效分数自 2023-03-13 开始，约 896 个交易日。
"""
import os
import re
import subprocess

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "us")

SERIES = {"QQQ": "usQQQ", "SOXX": "usSOXX"}
BENCH = ("SPY", "usSPY")          # 分母基准
UNIT_FIX_BEFORE = "2022-03-08"    # 此日期之前的 volume/amount 为真实值 100 倍


def _bin():
    for p in ("~/.local/bin/westock.exe", "~/.local/bin/westock"):
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return "westock"


def _raw(code, start, end):
    env = os.environ.copy()
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.expanduser("~/.local/bin")
    r = subprocess.run([_bin(), "kline", code, "--period", "day",
                        "--start", start, "--end", end],
                       capture_output=True, text=True, timeout=90, env=env)
    if r.returncode != 0:
        raise RuntimeError("取 %s 行情失败：%s" % (code, (r.stderr or r.stdout)[:200]))
    return r.stdout


def parse(text):
    """美股 K 线：date|open|last|high|low|volume|amount|exchange|change_pct。

    注意第 3 列名为 last（收盘价），第 8 列为交易所代码而非换手率。
    """
    rows = []
    for ln in text.splitlines():
        if ln.count("|") < 8:
            continue
        p = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(p) < 9 or not re.match(r"^\d{4}-\d{2}-\d{2}$", p[0]):
            continue
        rows.append(p[:9])
    if not rows:
        raise ValueError("未解析到美股 K 线数据")
    d = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low",
                                    "volume", "amount", "exch", "chg"])
    for c in ["open", "close", "high", "low", "volume", "amount", "chg"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

    # 单位修正：2022-03-08 之前的 volume / amount 为真实值 100 倍
    fix = d["date"] < UNIT_FIX_BEFORE
    d.loc[fix, "volume"] = d.loc[fix, "volume"] / 100.0
    d.loc[fix, "amount"] = d.loc[fix, "amount"] / 100.0
    return d


_bench_memo = {}


def fetch(name, start="2021-01-01", end=None):
    """返回可直接喂给 indicator_core 的 DataFrame。

    turnover 由 volume 代理；amt_all 为 SPY 成交额（分母）。
    """
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    code = SERIES[name]
    d = parse(_raw(code, start, end))
    key = (start, end)
    if key not in _bench_memo:                      # 同一次刷新里 SPY 只拉一次
        _bench_memo[key] = parse(_raw(BENCH[1], start, end))[["date", "amount"]]
    b = _bench_memo[key].rename(columns={"amount": "amt_all"})
    d = d.merge(b, on="date", how="left")
    d["turnover"] = d["volume"]          # 换手率代理
    d["market"] = "US"
    cols = ["date", "open", "close", "high", "low", "volume",
            "amount", "turnover", "chg", "amt_all", "market"]
    return d[cols]


def fetch_all(start="2021-01-01", end=None):
    """拉全部标的并落盘，返回 {名称: DataFrame}。"""
    os.makedirs(DATA, exist_ok=True)
    out = {}
    for name in SERIES:
        df = fetch(name, start, end)
        df.to_csv(os.path.join(DATA, "%s.csv" % name), index=False)
        out[name] = df
    return out


def load(name):
    """读本地缓存；不存在返回 None。"""
    p = os.path.join(DATA, "%s.csv" % name)
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p)
    d["date"] = d["date"].astype(str).str.strip()
    return d


if __name__ == "__main__":
    import sys
    for name, df in fetch_all().items():
        print("%-5s %d 行  %s ~ %s  末收 %s"
              % (name, len(df), df.date.iloc[0], df.date.iloc[-1], df.close.iloc[-1]))
