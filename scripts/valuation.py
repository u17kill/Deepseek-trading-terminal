"""指数估值数据抓取（蛋卷基金公开接口）。

口径说明：PE / PB 为蛋卷基金的加权口径，与中证指数官网的「总股本法」PE 存在差异
（同一日科创50：蛋卷 105.19 vs 中证 84.64）。因此本模块内部分位自洽，
但不同平台的 PE 绝对值不可直接横向比较，只有分位可比。
"""
import datetime

import requests

H = {"User-Agent": "Mozilla/5.0"}
BASE = "https://danjuanfunds.com/djapi/index_eva"
CODES = {"科创50": "SH000688", "创业板指": "SZ399006",
         "沪深300": "SH000300", "中证红利": "SH000922"}
# 各周期回溯的周数（数据为周频）
WINDOWS = {"3年": 156, "5年": 260, "10年": 520}


def _get(url, timeout=30):
    r = requests.get(url, headers=H, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_detail(code):
    j = _get(f"{BASE}/detail/{code}")
    if j.get("result_code") != 0:
        raise RuntimeError("估值详情接口异常：%s" % j.get("message"))
    return j["data"]


def fetch_series(code, kind="pe"):
    """返回 [[date, value], ...]，周频，升序。"""
    j = _get(f"{BASE}/{kind}_history/{code}?day=all")
    if j.get("result_code") != 0:
        raise RuntimeError("估值历史接口异常：%s" % j.get("message"))
    key = "index_eva_pe_growths" if kind == "pe" else "index_eva_pb_growths"
    arr = (j.get("data") or {}).get(key) or []
    out = []
    for it in arr:
        ts, v = it.get("ts"), it.get(kind)
        if ts is None or v is None:
            continue
        d = datetime.datetime.utcfromtimestamp(ts / 1000) + datetime.timedelta(hours=8)
        out.append([d.strftime("%Y-%m-%d"), round(float(v), 2)])
    out.sort(key=lambda x: x[0])
    return out


def percentile(series, years=None):
    """当前值在样本中的百分位（0-100）。years 指定回溯年数。"""
    if not series:
        return None
    vals = [p[1] for p in series]
    if years:
        n = WINDOWS.get(years)
        vals = vals[-n:] if n and len(vals) > n else vals
    cur = vals[-1]
    below = sum(1 for v in vals if v < cur)
    return round(below / len(vals) * 100, 1)


def summarize(series):
    vals = [p[1] for p in series]
    cur = vals[-1]
    srt = sorted(vals)
    return {
        "cur": round(cur, 2),
        "pos": {k: percentile(series, k) for k in WINDOWS},
        "pos_all": percentile(series),
        "min": round(srt[0], 2),
        "max": round(srt[-1], 2),
        "median": round(srt[len(srt) // 2], 2),
        "p20": round(srt[int(len(srt) * 0.2)], 2),
        "p80": round(srt[int(len(srt) * 0.8)], 2),
        "n": len(vals),
        "start": series[0][0],
        "end": series[-1][0],
    }


def fetch_all():
    out = {}
    for name, code in CODES.items():
        pe = fetch_series(code, "pe")
        try:
            pb = fetch_series(code, "pb")
        except Exception:  # noqa: BLE001
            pb = []
        try:
            detail = fetch_detail(code)
        except Exception:  # noqa: BLE001
            detail = {}
        if not pe:
            raise RuntimeError("%s 未取到 PE 序列" % name)
        out[name] = {
            "name": name,
            "code": code,
            "basis": "PE-TTM",
            "src": "蛋卷基金指数估值",
            "pe": pe,
            "pb": pb,
            "pe_stats": summarize(pe),
            "pb_stats": summarize(pb) if pb else None,
            "detail": {k: detail.get(k) for k in
                       ("pe", "pb", "pe_percentile", "pb_percentile", "roe",
                        "yeild", "eva_type", "eva_type_int", "date")},
        }
    return out


if __name__ == "__main__":
    d = fetch_all()
    for k, v in d.items():
        st = v["pe_stats"]
        print("%s PE %.2f | 分位 3年 %s 5年 %s 10年 %s 全部 %s | 区间 %.2f~%.2f 中位 %.2f | %s~%s (%d点)"
              % (k, st["cur"], st["pos"]["3年"], st["pos"]["5年"], st["pos"]["10年"],
                 st["pos_all"], st["min"], st["max"], st["median"], st["start"], st["end"], st["n"]))
