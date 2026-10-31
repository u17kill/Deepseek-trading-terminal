"""美股 ETF 取数：QQQ（纳指100）、SOXX（半导体）、IVV（标普500）、DIA（道琼斯）。

与 A 股通道的差异，全部记录在此，避免口径混淆
------------------------------------------------
1. **无换手率、无股本**：westock `quote` 对美股 ETF 的 turnover_rate 与
   total_market_cap 恒为 0，ETF 份额不在返回字段内。因此「换手率分位」改用
   「成交量分位」代理——ETF 份额变动缓慢（申赎以 10%~20%/年 计），
   成交量分位 ≈ 换手率分位。
2. **无全市场成交额**：A 股用「两市成交额」做分母，美股没有对应口径。
   改用 SPY（标普 500 ETF，全市场流动性锚）成交额做分母：
   share = 标的成交额 / SPY 成交额。实测该比值与位置模块的相关性为
   -0.06 / 0.01 / -0.02 / 0.15（QQQ / SOXX / IVV / DIA），比 A 股口径
   （0.26~0.29）更独立。
3. **标的必须用 ETF，不能用指数**，这一点和 A 股相反，原因有三：
   - `usSPX` 在 westock 既无 quote 也无 kline，标普500 只能用 ETF 代理；
   - `us.DJI` 有日线但 amount 单位异常（quote 给 3.4e13、kline 给 1.1e13，
     互相矛盾，且比 30 只成分股真实成交额高两个数量级），不可用于占比计算；
   - `usSPY` 不能用作标的本身——它是拥挤模块的分母，SPY/SPY 恒为 1，
     分位退化为常数。故标普500 取 IVV（iShares 标普500），道琼斯取 DIA。
4. **历史单位错误**：westock 返回的 2022-03-08 之前数据，volume 为真实值 100 倍、
   amount 为真实值 10000 倍（amount/(volume*close) ≈ 100，之后恒为 1）。
   旧版代码对 volume 与 amount 同时除以 100，结果 volume 修对了、amount 仍大 100 倍；
   因为占比计算里分子分母同时失真、比例抵消，所以历史上没有暴露出来。
   现按 volume/100、amount/10000 修正，指标读数不受影响（已验证）。
5. **高低价为 0**：2022-01-03 ~ 2022-03-07 共 44 行的 high/low 为 0。
   本指标仅用收盘价（位置、动能均如此），故不影响结果。
6. **样本起点**：美股数据自 2022-01-03 起。DD250 需先累积 120 个值、
   再过 175 个分位样本，因此有效分数自 2023-03-13 开始，约 896 个交易日。
"""
import os
import re

import pandas as pd

import wsfeed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "us")

SERIES = {"QQQ": "usQQQ", "SOXX": "usSOXX",
          "标普500": "usIVV", "道琼斯": "usDIA"}
BENCH = ("SPY", "usSPY")          # 分母基准
UNIT_FIX_BEFORE = "2022-03-08"    # 此日期之前 volume 为真实值 100 倍、amount 为 10000 倍


MIN_ROWS = 500       # 少于这个数不足以支撑 250 日滚动窗口
TAIL_DAYS = 12       # 增量抓取的回看天数（覆盖周末与假期）


def _raw(code, start, end):
    """走 wsfeed：限频自动重试 + 与 A 股通道共用调用节流。"""
    return wsfeed.kline(code, start, end, strict=False)


def _raw_full(code, start, end):
    """长区间请求会被服务端按 250~500 行静默截断，不足时分段拼接。

    实测 usIVV / usDIA / usSPY 对 2021 年起的请求常只返回 252~505 行，
    且重试时多时少，说明是服务端按响应体量截断，不是异常。
    分段后每段约 1 年（~250 行），低于截断阈值。
    """
    txt = wsfeed.kline(code, start, end, strict=False)
    need = int(wsfeed.expected_rows(start, end) * 0.7)
    if wsfeed.count_rows(txt) >= need:
        return txt
    got = wsfeed.count_rows(txt)
    print("[分段拼接] %s 单次仅 %d 行（需约 %d 行），改为按年抓取" % (code, got, need))

    import datetime as dt
    cur = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    rows = {}
    while cur <= stop:
        nxt = min(stop, cur + dt.timedelta(days=330))
        seg = wsfeed.kline(code, cur.isoformat(), nxt.isoformat(), strict=False)
        for ln in seg.splitlines():
            if wsfeed.DATE_RE.match(ln):
                rows[ln.split("|")[1].strip()] = ln
        cur = nxt + dt.timedelta(days=1)
    print("[分段拼接] %s 合并后 %d 行" % (code, len(rows)))
    hdr = ("| date | open | last | high | low | volume | amount | exchange | change_pct |\n"
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    return hdr + "\n" + "\n".join(rows[k] for k in sorted(rows, reverse=True))


RAW_COLS = ["date", "open", "close", "high", "low", "volume", "amount", "chg"]


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

    # 单位修正：2022-03-08 之前的 volume 为真实值 100 倍、amount 为 10000 倍。
    # 两列倍数不同——用 amount/(volume*close) 衡量，断点前恒为 100、断点后恒为 1，
    # 说明 amount 比 volume 又多错了 100 倍。旧代码两列同除 100，
    # volume 修对、amount 仍大 100 倍；因占比计算里分子分母同步失真而未被发现。
    fix = d["date"] < UNIT_FIX_BEFORE
    d.loc[fix, "volume"] = d.loc[fix, "volume"] / 100.0
    d.loc[fix, "amount"] = d.loc[fix, "amount"] / 10000.0
    return d


_bench_memo = {}
BENCH_CACHE = os.path.join(DATA, "_bench.csv")


def path_of(name):
    """缓存文件名用 ticker（usIVV.csv），不用中文名——跨平台和命令行都省事。"""
    return os.path.join(DATA, "%s.csv" % SERIES[name])


def _bench(start, end):
    """SPY 成交额（拥挤模块分母）。缓存优先 + 增量补尾，同 fetch()。

    分母一旦残缺，四只美股标的全部算不出来，所以这里单独兜底到底：
    补尾失败沿用旧缓存，只有连缓存都没有才抛异常。
    """
    key = (start, end)
    if key in _bench_memo:
        return _bench_memo[key]

    cached = None
    if os.path.exists(BENCH_CACHE):
        try:
            cached = pd.read_csv(BENCH_CACHE)
            cached["date"] = cached["date"].astype(str).str.strip()
        except Exception:  # noqa: BLE001
            cached = None

    if cached is not None and len(cached) >= MIN_ROWS:
        last = str(cached["date"].iloc[-1])
        tail_from = (pd.Timestamp(last) - pd.Timedelta(days=TAIL_DAYS)).strftime("%Y-%m-%d")
        try:
            t = parse(_raw(BENCH[1], tail_from, end))[["date", "amount"]]
            b = pd.concat([cached[cached["date"] < tail_from], t], ignore_index=True)
            b = b.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            print("[警告] SPY 分母补尾失败（%s），沿用缓存 %d 行" % (e, len(cached)))
            b = cached
    else:
        try:
            b = parse(_raw_full(BENCH[1], start, end))[["date", "amount"]]
        except Exception as e:  # noqa: BLE001
            if cached is None:
                raise
            print("[警告] SPY 分母全量失败（%s），沿用缓存 %d 行" % (e, len(cached)))
            b = cached

    os.makedirs(DATA, exist_ok=True)
    b.to_csv(BENCH_CACHE, index=False)
    _bench_memo[key] = b
    return b


def _load_raw(name):
    """读缓存的原始行情（只保留 RAW_COLS）。缺失或列不全返回 None。"""
    p = path_of(name)
    if not os.path.exists(p):
        return None
    try:
        d = pd.read_csv(p)
        d["date"] = d["date"].astype(str).str.strip()
        if not set(RAW_COLS).issubset(d.columns):
            return None
        return d[RAW_COLS].copy()
    except Exception:  # noqa: BLE001
        return None


def fetch(name, start="2021-01-01", end=None):
    """返回可直接喂给 indicator_core 的 DataFrame。

    turnover 由 volume 代理；amt_all 为 SPY 成交额（分母）。

    **缓存优先 + 增量补尾**：完整历史第一次抓（长区间会被截断，走分段拼接），
    之后每天只补最近 12 天再与缓存合并。否则 8 个标的每天重抓全量历史，
    既慢又必然撞上截断与限频。
    """
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    code = SERIES[name]

    raw = _load_raw(name)
    if raw is not None and len(raw) >= MIN_ROWS:
        last = str(raw["date"].iloc[-1])
        tail_from = (pd.Timestamp(last) - pd.Timedelta(days=TAIL_DAYS)).strftime("%Y-%m-%d")
        try:
            t = parse(_raw(code, tail_from, end))
            raw = pd.concat([raw[raw["date"] < tail_from], t], ignore_index=True)
            raw = (raw.drop_duplicates(subset="date")
                      .sort_values("date").reset_index(drop=True))
        except Exception as e:  # noqa: BLE001  补尾失败就沿用缓存，不让整轮刷新失败
            print("[警告] %s 增量补尾失败（%s），沿用缓存 %d 行" % (name, e, len(raw)))
    else:
        raw = parse(_raw_full(code, start, end))

    b = _bench(start, end).rename(columns={"amount": "amt_all"})
    d = raw.merge(b, on="date", how="left")
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
        df.to_csv(path_of(name), index=False)
        out[name] = df
    return out


def load(name):
    """读本地缓存；不存在返回 None。"""
    p = path_of(name)
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
