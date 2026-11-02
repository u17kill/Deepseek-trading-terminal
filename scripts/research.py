# -*- coding: utf-8 -*-
"""机构研报：数据装载、校验与统计。

数据文件 data/research/research.json 是策展内容（入库），由检索任务写回。
纵向是主题（宏观 / 市场 / 特殊资产 / AI 产业 / 其他产业），横向是机构；
一篇报告 = 一个 (机构, 主题) 单元格，同一格可以有多篇。

本模块只负责装载、结构校验、按主题与机构双向统计，供构建与页面使用。
"""
import json
import os
import datetime
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RPT_FILE = os.path.join(ROOT, "data", "research", "research.json")

TAGS = ("bull", "neutral", "bear")
GRP_CN = {"global": "全球投行", "alt": "资管与另类", "tech": "科技专门", "cn": "中国券商"}


def load():
    if not os.path.exists(RPT_FILE):
        return None
    with open(RPT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def build():
    """装载 + 统计。页面只负责画，计数一律在这里算，避免前端与文档口径脱节。"""
    d = load()
    if d is None:
        return None

    topics = d["topics"]
    insts = d["insts"]
    reports = d["reports"]
    tmap = {t["id"]: t for t in topics}
    imap = {i["id"]: i for i in insts}

    # 主题内按日期倒序：同一格里最新的那篇排前面
    reports = sorted(reports, key=lambda r: (r["date"], r["id"]), reverse=True)

    for t in topics:
        t["reports"] = []
        t["stat"] = {k: 0 for k in TAGS}
        t["n"] = 0
        t["n_inst"] = 0

    for i in insts:
        i["topics"] = []
        i["n"] = 0

    for r in reports:
        t = tmap[r["topic"]]
        t["reports"].append(r["id"])
        t["stat"][r["tag"]] += 1
        t["n"] += 1
        if r["topic"] not in imap[r["inst"]]["topics"]:
            imap[r["inst"]]["topics"].append(r["topic"])
        imap[r["inst"]]["n"] += 1

    for t in topics:
        t["n_inst"] = len({r["inst"] for r in reports if r["topic"] == t["id"]})

    groups, seen = [], {}
    for i in insts:
        g = i.get("grp", "global")
        if g not in seen:
            seen[g] = {"id": g, "name": GRP_CN.get(g, g), "insts": []}
            groups.append(seen[g])
        seen[g]["insts"].append(i)

    out = {
        "as_of": d.get("as_of"),
        "window": d.get("window", ["", ""]),
        "topics": topics,
        "groups": groups,
        "insts": insts,
        "reports": reports,
        "n_report": len(reports),
        "n_inst": len(insts),
        "n_inst_covered": len({r["inst"] for r in reports}),
        "n_topic": len(topics),
        "n_cell": len({(r["inst"], r["topic"]) for r in reports}),
        "stat": {k: sum(1 for r in reports if r["tag"] == k) for k in TAGS},
    }
    return out


def validate(d, strict=True):
    """结构校验。返回 (errors, warnings)。"""
    err, warn = [], []
    if not d:
        return ["数据文件不存在或为空"], []

    topics = d.get("topics", [])
    insts = d.get("insts", [])
    reports = d.get("reports", [])

    if len(topics) < 1:
        err.append("没有主题定义")
    if len(insts) < 1:
        err.append("没有机构定义")

    tids = [t.get("id") for t in topics]
    if len(set(tids)) != len(tids):
        err.append("主题 id 重复")
    iids = [i.get("id") for i in insts]
    if len(set(iids)) != len(iids):
        err.append("机构 id 重复")

    for t in topics:
        for k in ("id", "name", "desc"):
            if not t.get(k):
                err.append("主题缺字段 %s: %r" % (k, t.get("id")))

    for i in insts:
        for k in ("id", "cn", "en", "grp"):
            if not i.get(k):
                err.append("机构缺字段 %s: %r" % (k, i.get("id")))
        if i.get("grp") not in GRP_CN:
            err.append("机构分组未知: %s → %r" % (i.get("id"), i.get("grp")))

    w0, w1 = (d.get("window") or ["", ""])[:2]
    try:
        lo = datetime.date.fromisoformat(w0)
        hi = datetime.date.fromisoformat(w1)
    except Exception:
        err.append("window 日期格式不对: %r" % (d.get("window"),))
        lo = hi = None

    seen = set()
    for r in reports:
        rid = r.get("id") or "?"
        if rid in seen:
            err.append("报告 id 重复: %s" % rid)
        seen.add(rid)
        if r.get("topic") not in tids:
            err.append("%s 的主题不存在: %r" % (rid, r.get("topic")))
        if r.get("inst") not in iids:
            err.append("%s 的机构不存在: %r" % (rid, r.get("inst")))
        for k in ("title", "thesis", "scope", "call", "src"):
            if not r.get(k):
                err.append("%s 缺字段 %s" % (rid, k))
        if r.get("tag") not in TAGS:
            err.append("%s 的立场标签非法: %r" % (rid, r.get("tag")))
        try:
            dt = datetime.date.fromisoformat(r.get("date", ""))
            if lo and hi and not (lo <= dt <= hi):
                warn.append("%s 的日期 %s 在窗口 %s~%s 之外" % (rid, dt, lo, hi))
        except Exception:
            err.append("%s 的日期格式不对: %r" % (rid, r.get("date")))

        pts = r.get("pts") or []
        if not (1 <= len(pts) <= 4):
            err.append("%s 的要点数应为 1-4，实际 %d" % (rid, len(pts)))
        if len(r.get("thesis", "")) > 90:
            warn.append("%s 的结论过长（%d 字），卡片会被撑高" % (rid, len(r.get("thesis", ""))))
        for k, p in enumerate(pts, 1):
            if len(p) > 110:
                warn.append("%s 第 %d 条要点 %d 字，偏长" % (rid, k, len(p)))

    return err, warn


def main():
    d = build()
    err, warn = validate(d)
    for e in err:
        print("  [ERR ] %s" % e)
    for w in warn:
        print("  [WARN] %s" % w)
    if d:
        print("研报 %d 篇 / 机构 %d 家（覆盖 %d）/ 主题 %d / 有稿单元格 %d"
              % (d["n_report"], d["n_inst"], d["n_inst_covered"], d["n_topic"], d["n_cell"]))
        for t in d["topics"]:
            print("  %-12s %-8s %2d 篇  %2d 家  多/中/空 %d/%d/%d"
                  % (t["id"], t["name"], t["n"], t["n_inst"],
                     t["stat"]["bull"], t["stat"]["neutral"], t["stat"]["bear"]))
    print("校验：%d 错误，%d 提示" % (len(err), len(warn)))
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
