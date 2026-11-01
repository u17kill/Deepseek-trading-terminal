# -*- coding: utf-8 -*-
"""最新风向：数据装载、校验与统计。

数据文件 data/wind/wind.json 是策展内容（入库），由检索任务写回；
本模块只负责装载、结构校验、按分类统计多空分布，供构建与页面使用。
"""
import json
import os
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIND_DIR = os.path.join(ROOT, "data", "wind")
WIND_FILE = os.path.join(WIND_DIR, "wind.json")

TAGS = ("bull", "neutral", "bear")
TAG_CN = {"bull": "偏多", "neutral": "中性", "bear": "偏空"}


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load():
    if not os.path.exists(WIND_FILE):
        return None
    return _read(WIND_FILE)


def merge_parts(save=True):
    """把 data/wind/part*.json 合并成 wind.json。

    part1 需含 as_of / window / brief / cats / people，
    part2 及之后只需含 people。重复 id 以先出现的为准。
    """
    parts = sorted(
        [f for f in os.listdir(WIND_DIR) if f.startswith("part") and f.endswith(".json")]
    )
    if not parts:
        raise SystemExit("data/wind 下没有 part*.json 可合并")

    base = _read(os.path.join(WIND_DIR, parts[0]))
    seen = set()
    people = []

    for f in parts:
        d = _read(os.path.join(WIND_DIR, f))
        for p in d.get("people", []):
            if p.get("id") in seen:
                print("  [skip] 重复 id: %s (%s)" % (p.get("id"), f))
                continue
            seen.add(p.get("id"))
            people.append(p)

    base["people"] = people
    if save:
        with open(WIND_FILE, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=1)
    return base


def validate(d, strict=True):
    """结构校验。返回 (errors, warnings)。"""
    err, warn = [], []
    cat_ids = {c["id"] for c in d.get("cats", [])}
    grp_ids = set()
    for c in d.get("cats", []):
        for g in c.get("groups", []):
            grp_ids.add(g["id"])

    w0, w1 = d.get("window", ["", ""])[:2]
    try:
        lo = datetime.date.fromisoformat(w0)
        hi = datetime.date.fromisoformat(w1)
    except Exception:
        err.append("window 日期格式不对: %r" % (d.get("window"),))
        lo = hi = None

    ids = set()
    for p in d.get("people", []):
        pid = p.get("id")
        if not pid:
            err.append("有人物缺 id")
            continue
        if pid in ids:
            err.append("人物 id 重复: %s" % pid)
        ids.add(pid)

        for k in ("name", "cn", "role", "cat", "tag"):
            if not p.get(k):
                err.append("%s 缺字段 %s" % (pid, k))
        if p.get("cat") not in cat_ids:
            err.append("%s 的分类不存在: %s" % (pid, p.get("cat")))
        if p.get("tag") not in TAGS:
            err.append("%s 的 tag 不合法: %s" % (pid, p.get("tag")))
        if p.get("grp") and p["grp"] not in grp_ids:
            err.append("%s 的分组不存在: %s" % (pid, p["grp"]))

        pts = p.get("points") or []
        if not pts:
            warn.append("%s 没有要点" % pid)
        if len(pts) > 5:
            warn.append("%s 要点偏多(%d 条)，页面会过长" % (pid, len(pts)))
        for i, pt in enumerate(pts):
            for k in ("date", "src", "text"):
                if not pt.get(k):
                    err.append("%s 第 %d 条要点缺 %s" % (pid, i + 1, k))
            if lo and hi:
                raw = pt.get("date", "")
                try:
                    # 允许 YYYY-MM：部分来源只报到月份，按月初比较，不硬凑日
                    dd = datetime.date.fromisoformat(raw if len(raw) > 7 else raw + "-01")
                    if not (lo <= dd <= hi):
                        warn.append("%s 第 %d 条日期 %s 在窗口外" % (pid, i + 1, raw))
                except Exception:
                    err.append("%s 第 %d 条日期格式不对: %r" % (pid, i + 1, raw))
            if len(pt.get("text", "")) > 220:
                warn.append("%s 第 %d 条文字偏长(%d 字)" % (pid, i + 1, len(pt["text"])))

    if strict and err:
        for e in err:
            print("  [ERR] " + e)
        raise SystemExit("校验未通过：%d 个错误" % len(err))
    return err, warn


def stats(d):
    """按分类统计多空分布与要点数，页面顶部的温度条直接用这份。"""
    out = []
    for c in d.get("cats", []):
        row = {"id": c["id"], "name": c["name"], "bull": 0, "neutral": 0, "bear": 0,
               "n": 0, "points": 0, "latest": ""}
        for p in d.get("people", []):
            if p.get("cat") != c["id"]:
                continue
            row["n"] += 1
            row[p["tag"]] = row.get(p["tag"], 0) + 1
            row["points"] += len(p.get("points") or [])
            for pt in p.get("points") or []:
                if pt.get("date", "") > row["latest"]:
                    row["latest"] = pt["date"]
        out.append(row)
    return out


def build():
    d = load() or merge_parts()
    validate(d)
    d["stats"] = stats(d)
    d["n_people"] = len(d.get("people", []))
    d["n_points"] = sum(len(p.get("points") or []) for p in d["people"])
    return d


def main():
    if "--merge" in sys.argv:
        d = merge_parts()
        print("已合并 -> %s" % WIND_FILE)
    else:
        d = load()
    if not d:
        raise SystemExit("没有数据，先跑 python scripts/wind.py --merge")
    err, warn = validate(d, strict=False)
    st = stats(d)
    print("人物 %d 位 / 要点 %d 条 / 窗口 %s ~ %s"
          % (len(d["people"]), sum(len(p.get("points") or []) for p in d["people"]),
             d["window"][0], d["window"][1]))
    print("%-12s %4s %4s %4s %4s %6s  %s" % ("分类", "偏多", "中性", "偏空", "人数", "要点", "最新"))
    for r in st:
        print("%-12s %4d %4d %4d %4d %6d  %s"
              % (r["name"], r["bull"], r["neutral"], r["bear"], r["n"], r["points"], r["latest"]))
    print("错误 %d / 提示 %d" % (len(err), len(warn)))
    for e in err:
        print("  [ERR] " + e)
    for w in warn:
        print("  [WARN] " + w)


if __name__ == "__main__":
    main()
