#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""财经日历：装载 / 校验 / 汇总。

    python scripts/econcal.py            # 校验并打印统计
    python scripts/econcal.py --json     # 打印 build() 结果

不叫 calendar.py 是因为会遮蔽标准库 calendar——build_dashboard 会把自己的
目录插到 sys.path 最前，任何 import calendar 的库都会拿到这一份。
"""
import datetime
import json
import os
import re
import sys
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "calendar", "calendar.json")

# 七类事件。颜色不是随手挑的：都在米白底（#f6f5f2）上压过中低饱和，
# 七个色相两两之间拉开，相邻两格不会看混。
TYPES = [
    ("earnings", "财报", "#2f6da8", "美 / 中 / 日 / 韩 / 台 / 港核心公司的业绩发布"),
    ("conf",     "产业大会", "#6b5aa6", "算力、半导体、消费电子、AI 的年度大会"),
    ("launch",   "产品发布", "#a8477a", "厂商发布会与开发者大会"),
    ("cb",       "央行会议", "#c1762a", "议息、纪要、经济预测"),
    ("data",     "经济数据", "#6f7d2a", "CPI / PPI / 非农 / PCE / GDP / PMI 的发布日程"),
    ("pol",      "政治会议", "#2e7d6e", "政治局会议、两会、中央经济工作会议"),
    ("summit",   "国际峰会", "#a8452f", "G20、APEC、达沃斯、联大、北约"),
]
TYPE_NAME = {t[0]: t[1] for t in TYPES}
TYPE_COLOR = {t[0]: t[2] for t in TYPES}

MKTS = OrderedDict([
    ("us", "美国"), ("cn", "中国"), ("hk", "中国香港"), ("tw", "中国台湾"),
    ("jp", "日本"), ("kr", "韩国"), ("eu", "欧洲"), ("global", "全球"),
])

CONF = {"confirmed": "已确认", "estimated": "推测", "tbd": "待定"}

# 重复规则：目前只有 M<n>（每月第 n 日）。做成一门小规则而不是把十几条平铺进 JSON，
# 是因为「每月 20 日报价」这件事只该有一个地方维护——写死十几条，改一次 range 就得重排一遍。
_REP = re.compile(r"^M(\d{1,2})$")


def load(path=SRC):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _d(s):
    return datetime.date.fromisoformat(s)


def validate(d):
    """返回 (errors, warnings)。校验只查结构与自相矛盾，不查内容真假。"""
    err, warn = [], []
    rng = d.get("range") or ["", ""]
    lo = hi = None
    try:
        lo, hi = _d(rng[0]), _d(rng[1])
    except Exception:
        err.append("range 日期格式不对: %r" % (rng,))

    ids = set()
    evs = d.get("events") or []
    for i, e in enumerate(evs):
        tag = e.get("id") or ("#%d" % (i + 1))
        if not e.get("id"):
            err.append("第 %d 条事件缺 id" % (i + 1))
        elif e["id"] in ids:
            err.append("事件 id 重复: %s" % e["id"])
        else:
            ids.add(e["id"])

        for k in ("date", "type", "mkt", "title"):
            if not e.get(k):
                err.append("%s 缺字段 %s" % (tag, k))
        if e.get("type") not in TYPE_NAME:
            err.append("%s 的类型不合法: %s" % (tag, e.get("type")))
        if e.get("mkt") not in MKTS:
            err.append("%s 的市场不合法: %s" % (tag, e.get("mkt")))
        if e.get("conf") not in CONF:
            err.append("%s 的确认度不合法: %s" % (tag, e.get("conf")))
        if e.get("repeat") and not _REP.match(e["repeat"]):
            err.append("%s 的 repeat 规则不认识: %s（现在只支持 M<日>）" % (tag, e["repeat"]))

        try:
            s = _d(e.get("date", ""))
            en = _d(e["end"]) if e.get("end") else s
            if en < s:
                err.append("%s 的 end 早于 date" % tag)
            if en > s + datetime.timedelta(days=14):
                warn.append("%s 跨期超过 14 天（%s ~ %s）" % (tag, s, en))
            if lo and hi and not (lo <= s <= hi):
                warn.append("%s 的日期 %s 在 range 之外" % (tag, s))
        except Exception:
            err.append("%s 的日期格式不对: %r" % (tag, e.get("date")))

        if not e.get("src"):
            warn.append("%s 没有出处" % tag)
        if e.get("conf") == "estimated" and not e.get("note"):
            warn.append("%s 标了推测但没写依据" % tag)
        if len(e.get("title", "")) > 24:
            warn.append("%s 标题偏长(%d 字)，格子里放不下" % (tag, len(e["title"])))

    # series：一组共用标题的日期。查的是「这一系列整体靠不靠得住」，
    # 而不是逐个日期挑错——一个 CPI 系列的 14 个日期要么都来自官方日程，
    # 要么整条都是按惯例推的，不该出现一半确认一半推测。
    for s in d.get("series") or []:
        tag = "series:" + (s.get("id") or "?")
        for k in ("id", "title", "type", "mkt", "conf"):
            if not s.get(k):
                err.append("%s 缺字段 %s" % (tag, k))
        if s.get("type") not in TYPE_NAME:
            err.append("%s 的类型不合法: %s" % (tag, s.get("type")))
        if s.get("mkt") not in MKTS:
            err.append("%s 的市场不合法: %s" % (tag, s.get("mkt")))
        if s.get("conf") not in CONF:
            err.append("%s 的确认度不合法: %s" % (tag, s.get("conf")))
        if not s.get("dates"):
            err.append("%s 没有 dates" % tag)
        for ds in s.get("dates") or []:
            try:
                dd = _d(ds)
            except Exception:
                err.append("%s 的日期格式不对: %r" % (tag, ds))
                continue
            if lo and hi and not (lo <= dd <= hi):
                warn.append("%s 的日期 %s 在 range 之外" % (tag, dd))
        if not s.get("src"):
            warn.append("%s 没有出处" % tag)
        if s.get("conf") == "estimated" and not s.get("note"):
            warn.append("%s 标了推测但没写依据" % tag)
        if len(s.get("title", "")) > 24:
            warn.append("%s 标题偏长(%d 字)，格子里放不下" % (tag, len(s["title"])))
    return err, warn


def _span(e):
    s = _d(e["date"])
    en = _d(e["end"]) if e.get("end") else s
    return s, en


def _expand_series(series, hi):
    """把 `series`（一组共用标题/类别/市场的日期）展开成逐条事件。

    月度数据写 14 个月就是 14 条几乎一样的对象（标题、类别、市场、出处全一样，
    只有日期不同）。写成 series 之后，一处改标题全局生效，也不必担心某一年的
    备注和别年写得不一致——这类复制粘贴出来的不一致，是最难发现的那种错。
    """
    out = []
    for s in series or []:
        sid = s.get("id") or "series"
        for ds in s.get("dates") or []:
            try:
                d = _d(ds)
            except Exception:
                continue  # 格式错误由 validate() 报，这里只负责跳过
            if hi and d > hi:
                continue
            e = dict(s)
            e.pop("dates", None)
            e["date"] = d.isoformat()
            e["id"] = "%s-%s" % (sid, d.strftime("%Y%m%d"))
            out.append(e)
    return out


def _expand(evs, hi):
    """按 repeat 把一条事件展开成逐月日期，返回新列表（不改入参）。

    遇周末顺延到下一个工作日——这与 LPR 的实际做法一致（2023-05-20 是周六，
    实际 05-22 周一公布）。法定节假日（春节、国庆）的顺延没有单列，note 里写明了：
    与其猜一个可能的挪动日，不如让读者自己去看交易中心的公告。

    展开出来的条目沿用原条目的 conf：每月 20 日报价是官方固定安排，不是猜的；
    真正会动的只有节假日那一两次，note 已经交代。按红线第 19 条，只有「按惯例推出来的
    具体某一天」才标 estimated——两者的区别是「规则本身是不是官方的」。
    """
    out = []
    for e in evs:
        out.append(e)
        m = _REP.match(e.get("repeat") or "")
        if not m or not hi:
            continue
        day = int(m.group(1))
        d = _d(e["date"])
        y, mo = d.year, d.month
        while True:
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
            try:
                cur = datetime.date(y, mo, day)
            except ValueError:
                continue  # 该月没有这一天（如 31 日），跳过
            if cur > hi:
                break
            while cur.weekday() >= 5:
                cur += datetime.timedelta(days=1)
            g = dict(e)
            g["date"] = cur.isoformat()
            g.pop("end", None)  # 重复出来的条目都是单日的
            g["id"] = "%s-%s" % (e["id"], cur.strftime("%Y%m"))
            g["note"] = ("每月 %d 日 9:15 报价，遇周末顺延（本次已按周末调整）；"
                         "法定节假日顺延未单列，以交易中心公告为准" % day)
            out.append(g)
    return out


def build(path=SRC):
    d = load(path)
    rng = d.get("range") or [None, None]
    hi = _d(rng[1]) if rng[1] else None
    evs = _expand_series(d.get("series") or [], hi) + list(d.get("events") or [])
    evs = _expand(evs, hi)
    evs = sorted(evs, key=lambda e: (e["date"], e.get("end") or e["date"],
                                     e.get("pri", 2), e["title"]))
    as_of = _d(d["as_of"])

    # 当周：以 as_of 所在自然周（周一 ~ 周日）为准。日历按周读，不按「未来 7 天」读——
    # 后者会让周一的「本周」从周二算起，与月历上的行对不上。
    wk_start = as_of - datetime.timedelta(days=as_of.weekday())
    wk_end = wk_start + datetime.timedelta(days=6)

    week = []
    for e in evs:
        s, en = _span(e)
        if en < wk_start or s > wk_end:
            continue
        week.append(e)

    stats = {
        "n": len(evs),
        "by_type": {t: 0 for t in TYPE_NAME},
        "by_mkt": {m: 0 for m in MKTS},
        "by_conf": {c: 0 for c in CONF},
        "by_month": {},
    }
    for e in evs:
        stats["by_type"][e["type"]] = stats["by_type"].get(e["type"], 0) + 1
        stats["by_mkt"][e["mkt"]] = stats["by_mkt"].get(e["mkt"], 0) + 1
        stats["by_conf"][e.get("conf", "confirmed")] = \
            stats["by_conf"].get(e.get("conf", "confirmed"), 0) + 1
        mon = e["date"][:7]
        stats["by_month"][mon] = stats["by_month"].get(mon, 0) + 1

    return {
        "as_of": d["as_of"],
        "range": d.get("range"),
        "updated": d.get("updated"),
        "types": [{"id": t[0], "name": t[1], "color": t[2], "desc": t[3]} for t in TYPES],
        "mkts": [{"id": k, "name": v} for k, v in MKTS.items()],
        "events": evs,
        "week": {"start": wk_start.isoformat(), "end": wk_end.isoformat(),
                 "events": week},
        "stats": stats,
    }


def main():
    d = load()
    err, warn = validate(d)
    if "--json" in sys.argv:
        print(json.dumps(build(), ensure_ascii=False, indent=1))
        return
    for e in err:
        print("  [ERR] " + e)
    for w in warn:
        print("  [WARN] " + w)
    b = build()
    s = b["stats"]
    print("事件 %d 条，%s ~ %s，as_of %s" % (s["n"], d["range"][0], d["range"][1], d["as_of"]))
    print("  类型 " + "  ".join("%s %d" % (TYPE_NAME[k], v) for k, v in s["by_type"].items()))
    print("  市场 " + "  ".join("%s %d" % (MKTS[k], v) for k, v in s["by_mkt"].items()))
    print("  确认度 " + "  ".join("%s %d" % (CONF[k], v) for k, v in s["by_conf"].items()))
    wk = b["week"]
    print("  当周 %s ~ %s：%d 条" % (wk["start"], wk["end"], len(wk["events"])))
    print("  %d 错误 / %d 提示" % (len(err), len(warn)))


if __name__ == "__main__":
    main()
