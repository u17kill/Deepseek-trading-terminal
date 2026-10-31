"""市场背景数据：两市成交额 + 两融余额。

设计说明
--------
拥挤度模块需要两个「相对」口径，都必须以全市场为分母，因此单独成模块：

1. ``amt_all``：沪市 + 深市成交额之和，用于计算「该指数成交额占两市比重」。
   这是分量占比，不受市场规模扩张影响，是拥挤度最有效的口径。
2. ``rz_buy`` / ``lev``：两市融资买入额及其占两市成交额比重。
   经检验与价格位置相关性高达 0.64（两融天然追涨），**不进入指标分数**，
   仅在页面上作为「杠杆环境」观察项展示。

取数通道：行情走 westock，两融走 akshare（上交所 / 深交所官方汇总）。
"""
import os
import subprocess

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "oos")
CTX_PATH = os.path.join(DATA, "market_ctx.csv")


def _bin():
    for p in ("~/.local/bin/westock.exe", "~/.local/bin/westock"):
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return "westock"


def _kline(code, start, end):
    from indicator_core import parse_westock

    env = os.environ.copy()
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.expanduser("~/.local/bin")
    r = subprocess.run([_bin(), "kline", code, "--period", "day",
                        "--start", start, "--end", end],
                       capture_output=True, text=True, timeout=90, env=env)
    if r.returncode != 0:
        raise RuntimeError("取 %s 行情失败：%s" % (code, (r.stderr or r.stdout)[:200]))
    return parse_westock(r.stdout)


def fetch_turnover(start="2021-01-01", end=None):
    """两市成交额（沪市 + 深市），口径与交易所公布的「两市成交额」一致。"""
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    sh = _kline("sh000001", start, end)[["date", "amount"]].rename(columns={"amount": "amt_sh"})
    sz = _kline("sz399001", start, end)[["date", "amount"]].rename(columns={"amount": "amt_sz"})
    m = sh.merge(sz, on="date", how="inner")
    m["amt_all"] = m["amt_sh"] + m["amt_sz"]
    return m


def fetch_margin():
    """两市融资买入额（元）。取不到时返回 None，调用方降级处理。"""
    try:
        import akshare as ak

        a = ak.macro_china_market_margin_sh()[["日期", "融资买入额"]]
        b = ak.macro_china_market_margin_sz()[["日期", "融资买入额"]]
    except Exception:
        return None
    out = []
    for df in (a, b):
        df = df.rename(columns={"日期": "date", "融资买入额": "v"}).copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df["v"] = pd.to_numeric(df["v"], errors="coerce")
        out.append(df)
    m = out[0].merge(out[1], on="date", how="outer", suffixes=("_sh", "_sz"))
    m["rz_buy"] = m[["v_sh", "v_sz"]].sum(axis=1, min_count=1)
    return m[["date", "rz_buy"]]


def fetch(start="2021-01-01", end=None):
    """返回 date / amt_all / rz_buy / lev 的日频表，并落盘。"""
    ctx = fetch_turnover(start, end)
    mg = fetch_margin()
    if mg is not None:
        ctx = ctx.merge(mg, on="date", how="left")
        # 不做前值填充：两融数据晚于行情一日公布，填充会让「当日成交额 ÷ 昨日融资买入额」
        # 得到失真的杠杆率。保留 NaN，由前端取最近有效值并标注实际日期。
        ctx["lev"] = ctx["rz_buy"] / ctx["amt_all"] * 100
    else:
        ctx["rz_buy"] = pd.NA
        ctx["lev"] = pd.NA
    ctx = ctx.sort_values("date").reset_index(drop=True)
    os.makedirs(DATA, exist_ok=True)
    ctx.to_csv(CTX_PATH, index=False)
    return ctx


def load():
    """读本地缓存；不存在时返回 None（调用方回退旧算法）。"""
    if not os.path.exists(CTX_PATH):
        return None
    d = pd.read_csv(CTX_PATH)
    d["date"] = d["date"].astype(str).str.strip()
    return d
