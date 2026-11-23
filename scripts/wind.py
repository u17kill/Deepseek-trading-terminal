# -*- coding: utf-8 -*-
"""专家观点（原「最新风向」）：数据装载、校验与统计。

数据文件 data/wind/wind.json 是策展内容（入库），由检索任务写回；
本模块只负责装载、结构校验、按分类统计多空分布，供构建与页面使用。
"""
import json
import os
import re
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIND_DIR = os.path.join(ROOT, "data", "wind")
WIND_FILE = os.path.join(WIND_DIR, "wind.json")

TAGS = ("bull", "neutral", "bear")
TAG_CN = {"bull": "偏多", "neutral": "中性", "bear": "偏空"}

# 本期主线的条数上限。超出的按顺序从末尾剔除（人写的顺序就是重要度顺序），
# 但**不删数据**：被挤掉的那几条仍留在 part1.json 里，页面注明「另有 N 条未显示」。
# 静默丢弃策展内容是这个项目一直在避免的事——用户看不见少了哪条，就没法判断该不该删。
BRIEF_MAX = 10


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load():
    if not os.path.exists(WIND_FILE):
        return None
    return _read(WIND_FILE)


def _parts():
    """part*.json 按编号自然排序。

    不能直接 sorted()：字符串序里 part10 排在 part2 前面（'part1' + '.' < 'part1' + '0'），
    结果是新加的人插到分类中间而不是末尾，「重复 id 以先出现为准」的优先级也跟着错。
    """
    names = [f for f in os.listdir(WIND_DIR)
             if f.startswith("part") and f.endswith(".json")]
    def key(f):
        m = re.fullmatch(r"part(\d+)\.json", f)
        return (0, int(m.group(1)), "") if m else (1, 0, f)
    return sorted(names, key=key)


def merge_parts(save=True):
    """把 data/wind/part*.json 合并成 wind.json。

    part1 需含 as_of / window / brief / cats / people，
    part2 及之后只需含 people。重复 id 以先出现的为准。
    """
    parts = _parts()
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
        # newline="\n"：Windows 上默认会把 \n 翻成 \r\n，入库文件每合并一次就整篇换行都变，
        # git diff 全红，真改了哪一行反而看不出来。
        with open(WIND_FILE, "w", encoding="utf-8", newline="\n") as f:
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

        # 自动抓取块：结构必须齐，否则页面上就是一个点不开的标题
        feed = p.get("feed") or []
        if len(feed) > 5:
            warn.append("%s 自动抓取 %d 条，超过 5 条上限" % (pid, len(feed)))
        for i, fd in enumerate(feed):
            for k in ("date", "title"):
                if not fd.get(k):
                    err.append("%s 第 %d 条自动抓取缺 %s" % (pid, i + 1, k))
            if not fd.get("url"):
                warn.append("%s 第 %d 条自动抓取没有链接" % (pid, i + 1))

    items = (d.get("brief") or {}).get("items") or []
    if len(items) > BRIEF_MAX:
        warn.append("本期主线 %d 条，超过上限 %d，末尾 %d 条不会显示"
                    % (len(items), BRIEF_MAX, len(items) - BRIEF_MAX))
    if not items:
        warn.append("本期主线为空")

    if strict and err:
        for e in err:
            print("  [ERR] " + e)
        raise SystemExit("校验未通过：%d 个错误" % len(err))
    return err, warn


def freshness(d, ref=None, days=30):
    """数据新鲜度体检：每人最近一条要点距基准日多久。

    要点是策展内容，程序造不出来，所以「更新」能做的只有两件事：
    把改过的数据文件重新读进来，以及告诉人哪几位该补了。
    基准日默认取运行当天，而不是 as_of——as_of 是快照日，用它算的话
    快照永远显示「刚刚更新过」，反而掩盖了内容在变旧。
    """
    ref = ref or datetime.date.today().isoformat()
    try:
        refd = datetime.date.fromisoformat(ref)
    except Exception:
        refd = datetime.date.today()
        ref = refd.isoformat()

    stale, empty = [], []
    for p in d.get("people", []):
        pts = p.get("points") or []
        if not pts:
            empty.append({"id": p.get("id"), "cn": p.get("cn")})
            continue
        latest = max(pt.get("date", "") for pt in pts)
        try:
            dd = datetime.date.fromisoformat(latest if len(latest) > 7 else latest + "-01")
        except Exception:
            continue
        gap = (refd - dd).days
        if gap > days:
            stale.append({"id": p.get("id"), "cn": p.get("cn"),
                          "cat": p.get("cat"), "latest": latest, "gap": gap,
                          # 自动抓到的最近一条：人写的旧了，但可能机器这边刚有新闻。
                          # 页面上两个数并排放，才知道该补的是「判断」还是「压根没人报道」。
                          "auto": p.get("feed_date") or ""})

    stale.sort(key=lambda x: -x["gap"])
    return {"ref": ref, "days": days, "stale": stale, "empty": empty,
            "ok": len(d.get("people", [])) - len(stale) - len(empty)}


def stats(d):
    """按分类统计多空分布与要点数，页面顶部的温度条直接用这份。

    automatic 列（自动抓取的条数）单独算：它能说明「这一块最近有没有消息」，
    但不参与多空温度——机器搬来的标题没有立场，让它进多空比就是替人下判断了。
    """
    out = []
    for c in d.get("cats", []):
        row = {"id": c["id"], "name": c["name"], "bull": 0, "neutral": 0, "bear": 0,
               "n": 0, "points": 0, "latest": "", "auto": 0, "auto_latest": ""}
        for p in d.get("people", []):
            if p.get("cat") != c["id"]:
                continue
            row["n"] += 1
            row[p["tag"]] = row.get(p["tag"], 0) + 1
            row["points"] += len(p.get("points") or [])
            for pt in p.get("points") or []:
                if pt.get("date", "") > row["latest"]:
                    row["latest"] = pt["date"]
            row["auto"] += len(p.get("feed") or [])
            if (p.get("feed_date") or "") > row["auto_latest"]:
                row["auto_latest"] = p["feed_date"]
        out.append(row)
    return out


def _finalize(d):
    """统一出口：补统计、计数、体检，并执行主线上限。

    build() 与 reload() 都走这里，页面、静态页、接口才不会各算一套——
    之前 stats 与 fresh 是分别在两处各写一遍的，加字段必然漏一处。
    """
    brief = d.get("brief") or {}
    items = brief.get("items") or []
    if len(items) > BRIEF_MAX:
        over = items[BRIEF_MAX:]
        brief["items"] = items[:BRIEF_MAX]
        # 只留标题：前端要能说出「被挤掉的是哪几条」，又不该把整段正文再拖一遍
        brief["dropped"] = [it.get("h", "") for it in over]
    else:
        brief["dropped"] = []
    d["brief"] = brief

    d["stats"] = stats(d)
    d["n_people"] = len(d.get("people", []))
    d["n_points"] = sum(len(p.get("points") or []) for p in d["people"])
    d["n_feed"] = sum(len(p.get("feed") or []) for p in d["people"])
    d["n_feed_people"] = sum(1 for p in d["people"] if p.get("feed"))

    # 内容实际新鲜度：最新一条要点是哪天。页首「数据截至」写的是窗口末端（人定的），
    # 两者可能差很久——11-15 点开看到「截至 11-08」，才知道这一周没人补内容。
    ds = [pt.get("date", "") for p in d["people"] for pt in (p.get("points") or [])]
    d["latest_point"] = max(ds) if ds else ""

    d["fresh"] = freshness(d)
    return d


def build():
    d = load() or merge_parts()
    _attach_feed(d)
    validate(d)
    return _finalize(d)


def reload(rebuild=True):
    """重新合并 part*.json 并返回装好的数据。

    合并而不是只 load()：数据文件是分片手写的，直接读 wind.json
    会漏掉对 part 的改动——这正是「点了更新却没变化」的常见来源。
    """
    d = merge_parts()
    _attach_feed(d)
    validate(d)
    return _finalize(d)


def _attach_feed(d):
    """把 data/wind/feed.json 挂到人身上。

    feed 与 points 分两处存，是因为来源不同、责任不同：
    points 是人写的判断（part*.json），feed 是「更新观点」抓来的标题（feed.json）。
    混在一个数组里，重抓一次就把人写的东西冲掉了。
    """
    try:
        import windfeed
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import windfeed
    return windfeed.attach(d)


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
