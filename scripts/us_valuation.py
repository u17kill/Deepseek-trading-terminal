"""美股 ETF 估值分位（QQQ / SOXX）。

口径与 A 股 valuation.py 的差异，必须在页面上标明：

  QQQ  -> 蛋卷基金「纳指100」(NDX) 的 PE-TTM / PB，周度采样。
          与科创50 / 创业板指同为 TTM 加权口径，可直接横向比较。
          QQQ 是跟踪纳指100 的 ETF，费率层面的微小差异对 PE 无实质影响。

  SOXX -> 费城半导体指数的「12 个月前瞻 PE」（价格 ÷ 未来 12 个月共识 EPS），
          来源 History of Market（数据 CC BY 4.0）。这不是 TTM：
          分母是预期盈利而非已实现盈利，绝对水平不能与 A 股的 PE-TTM 横比，
          只能在同一口径内做纵向分位。半导体周期波动大，前瞻与 TTM 的差距也大。

SOX 序列的拼接问题（踩过）：
  官方 `series` 是周频，但只更新到 2026-08-05；`forwardOwn` 是日频，到最新交易日。
  只吃周频会把分位从 58.8% 高估到 79.6%（差 21 个百分点），
  所以这里用「周频历史 + 日频近段」拼接，衔接处 21.68 -> 22.00，口径连续。

  分位下限取 2011-01-01：2002-2009 有互联网泡沫期 PE 高达 195 倍，
  留在样本里会把整个分位压平，失去区分度。
"""
import datetime
import json

import requests

import valuation

H = {"User-Agent": "Mozilla/5.0 (compatible; personal-dashboard/1.0)"}
HOM = "https://historyofmarket.com/api/sectors/forward-pe.json"
# 样本起点与纳指100（2016-10-31 起）对齐，三个美股标的的分位才能互相比较。
# 不取 2002：互联网泡沫期费半 PE 高达 195 倍，会把分位压平。
SOX_FLOOR = "2016-10-31"
WINDOWS = {"3年": 3, "5年": 5, "10年": 10}
SOX_CITE = "History of Market (historyofmarket.com) · CC BY 4.0"


def _pct(points, years=None):
    """分位：当前值在样本中的百分位。years 用自然日回推，不用条数
    —— SOX 序列前段周频、末段日频，按条数截会把窗口缩成一年半。"""
    if not points:
        return None
    pts = points
    if years:
        end = datetime.date.fromisoformat(pts[-1][0])
        try:
            start = end.replace(year=end.year - years)
        except ValueError:                       # 2/29
            start = end.replace(year=end.year - years, day=28)
        pts = [p for p in pts if p[0] >= start.isoformat()]
        if len(pts) < 20:
            return None
    vals = [p[1] for p in pts]
    cur = vals[-1]
    return round(sum(1 for v in vals if v < cur) / len(vals) * 100, 1)


def _summarize(points):
    vals = [p[1] for p in points]
    cur = vals[-1]
    srt = sorted(vals)
    return {
        "cur": round(cur, 2),
        "pos": {k: _pct(points, n) for k, n in WINDOWS.items()},
        "pos_all": _pct(points),
        "min": round(srt[0], 2),
        "max": round(srt[-1], 2),
        "median": round(srt[len(srt) // 2], 2),
        "p20": round(srt[int(len(srt) * 0.2)], 2),
        "p80": round(srt[int(len(srt) * 0.8)], 2),
        "n": len(vals),
        "start": points[0][0],
        "end": points[-1][0],
    }


def _from_danjuan(name, code, undl, basis="PE-TTM"):
    """蛋卷口径估值：与 A 股同一套接口，PE-TTM 加权，可直接横比。"""
    pe = valuation.fetch_series(code, "pe")
    try:
        pb = valuation.fetch_series(code, "pb")
    except Exception:  # noqa: BLE001
        pb = []
    if not pe:
        raise RuntimeError("%s 未取到 PE 序列" % undl)
    return {
        "name": name,
        "code": code,
        "pe": pe,
        "pb": pb,
        "pe_stats": _summarize(pe),
        "pb_stats": _summarize(pb) if pb else None,
        "basis": basis,
        "undl": undl,
        "src": "蛋卷基金指数估值",
        "detail": {"date": pe[-1][0]},
    }


def fetch_qqq():
    """纳指100 的 PE-TTM / PB，走蛋卷，与 A 股同一套接口和口径。"""
    return _from_danjuan("QQQ", "NDX", "纳斯达克100")


def fetch_sp500():
    """标普500 的 PE-TTM / PB，走蛋卷。行情用 IVV 代理（见 us_etf.py 注释），
    估值仍取指数本身的 PE，两者跟踪同一篮子，不存在口径错配。"""
    return _from_danjuan("标普500", "SP500", "标普500（IVV 代理行情）")


def fetch_soxx():
    r = requests.get(HOM, headers=H, timeout=40)
    r.raise_for_status()
    sec = (r.json().get("sectors") or {}).get("sox")
    if not sec:
        raise RuntimeError("费城半导体板块缺失")

    weekly = [(x["date"], round(float(x["value"]), 2))
              for x in (sec.get("series") or []) if x.get("value") is not None]
    daily = [(x["date"], round(float(x["value"]), 2))
             for x in (sec.get("forwardOwn") or []) if x.get("value") is not None]
    if not weekly:
        raise RuntimeError("费城半导体周频序列为空")

    last = weekly[-1][0]
    pts = sorted(weekly + [p for p in daily if p[0] > last], key=lambda x: x[0])
    pts = [p for p in pts if p[0] >= SOX_FLOOR]
    if len(pts) < 60:
        raise RuntimeError("费城半导体样本仅 %d 点" % len(pts))

    st = _summarize(pts)
    return {
        "name": "SOXX",
        "code": "SOX",
        "pe": pts,
        "pb": [],
        "pe_stats": st,
        "pb_stats": None,
        "basis": "前瞻PE",
        "undl": "费城半导体",
        "src": SOX_CITE,
        "note": "前瞻 PE = 价格 ÷ 未来 12 个月共识 EPS，非 TTM，不可与 A 股 PE 横比；"
                "分位基于 %s 起样本（与纳指100 同窗口）" % SOX_FLOOR,
        "detail": {"date": pts[-1][0]},
    }


def fetch_dji():
    """道琼斯工业平均：**目前没有可用的免费估值源**，这里显式返回缺失标记。

    已逐个验证过的候选（2026-11-01）：
      - 蛋卷基金 index_eva：DJI / DJIA / INDU / DOW / US30 / DJX / US.DJI / DJI.GI
        全部返回 result_code=0 但 name 为 null，即该指数不在其覆盖范围；
      - History of Market：只有 inft / finl / sox / cond / cons 五个行业板块，无宽基；
      - westock quote：usDJI 的 pe_ratio / pb_ratio 恒为 0。

    根本原因是道指为**价格加权**指数（30 只成分股按股价加权，不是市值加权），
    指数 PE 需按「30 只合计市值 ÷ 合计盈利」另行汇总，与常见指数编制口径不同，
    免费数据源普遍不提供。

    与其用别的指数冒充，不如把缺失摆出来——页面上显式标注，比填一个错数安全。
    """
    return {
        "name": "道琼斯",
        "code": "DJI",
        "pe": [],
        "pb": [],
        "pe_stats": None,
        "pb_stats": None,
        "basis": "不可得",
        "undl": "道琼斯工业平均（DIA 代理行情）",
        "src": "—",
        "missing": True,
        "reason": "道指为价格加权指数，指数 PE 需按 30 只成分股合计市值÷合计盈利另行汇总，"
                  "蛋卷 / History of Market / westock 均不提供该口径；不建议用其他指数的 PE 顶替。",
        "detail": {},
    }


def fetch_all():
    out = {}
    for fn in (fetch_qqq, fetch_soxx, fetch_sp500):
        try:
            d = fn()
            out[d["name"]] = d
        except Exception as e:  # noqa: BLE001  单个标的失败不拖垮其他
            print("[警告] %s 估值失败：%s" % (fn.__name__, e))
    d = fetch_dji()          # 恒定缺失，不需要 try
    out[d["name"]] = d
    return out


if __name__ == "__main__":
    for k, v in fetch_all().items():
        if v.get("missing"):
            print("%s 估值不可得：%s" % (k, v["reason"][:40]))
            continue
        st = v["pe_stats"]
        print("%s(%s) %s %.2f | 分位 3年 %s 5年 %s 10年 %s 全部 %s | 中位 %.2f 区间 %.2f~%.2f | %d点 %s~%s [%s]"
              % (k, v["undl"], v["basis"], st["cur"], st["pos"]["3年"], st["pos"]["5年"],
                 st["pos"]["10年"], st["pos_all"], st["median"], st["min"], st["max"],
                 st["n"], st["start"], st["end"], v["src"]))
