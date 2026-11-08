#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「最新风向」导出成一份给大模型当上下文用的 Markdown 文档。

    python scripts/export_wind.py                 # 全量，写 output/wind-for-chatgpt.md
    python scripts/export_wind.py --lite          # 只要主线 + 每人一句话立场
    python scripts/export_wind.py -o some.md      # 指定输出路径

为什么单独写一个脚本而不是让模型手抄：448 条要点手抄必有错漏，且下次窗口
顺延还要再抄一遍。这里只做机械搬运 + 排版，一条不改、一字不删。
"""
import argparse
import json
import os
import sys
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIND = os.path.join(ROOT, "data", "wind", "wind.json")

TAG_CN = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}
TAG_ORDER = ["bull", "bear", "neutral"]


def load():
    with open(WIND, encoding="utf-8") as f:
        return json.load(f)


def cat_map(d):
    """cat id → {name, desc, groups:{gid:gname}}，保留定义顺序。"""
    out = OrderedDict()
    for c in d.get("cats", []):
        out[c["id"]] = {
            "name": c["name"],
            "desc": c.get("desc", ""),
            "groups": OrderedDict((g["id"], g["name"]) for g in c.get("groups", []) or []),
        }
    return out


def pdate(s):
    """'2026-08' → '2026-08'（月份级），'2026-11-04' → '2026-11-04'。原样返回。"""
    return s or ""


def month_level(s):
    return bool(s) and len(s) == 7


def brief_block(d):
    items = (d.get("brief") or {}).get("items") or []
    if not items:
        return ""
    out = ["## 2. 本期主线（作者判断，非引述）", ""]
    for i, it in enumerate(items, 1):
        out.append(f"### 主线 {i}｜{it.get('h', '')}")
        out.append("")
        out.append(it.get("t", "").strip())
        out.append("")
    return "\n".join(out)


def stats_block(d):
    ps = d["people"]
    cm = cat_map(d)
    tags = Counter(p.get("tag") for p in ps)
    npts = sum(len(p.get("points") or []) for p in ps)
    lines = ["## 1. 一页摘要", ""]
    lines.append(f"- **覆盖窗口**：{d['window'][0]} ~ {d['window'][1]}（数据截止 {d['as_of']}）")
    lines.append(f"- **规模**：{len(ps)} 位人物 / {npts} 条观点与动作")
    lines.append("- **标签分布**：" + "、".join(
        f"{TAG_CN[t]} {tags.get(t, 0)}" for t in TAG_ORDER))
    lines.append("")
    lines.append("| 分类 | 人数 | 要点数 | 偏多 / 中性 / 偏空 |")
    lines.append("|---|---|---|---|")
    for cid, c in cm.items():
        sub = [p for p in ps if p.get("cat") == cid]
        if not sub:
            continue
        t = Counter(p.get("tag") for p in sub)
        n = sum(len(p.get("points") or []) for p in sub)
        lines.append(f"| {c['name']} | {len(sub)} | {n} | "
                     f"{t.get('bull', 0)} / {t.get('neutral', 0)} / {t.get('bear', 0)} |")
    lines.append("")
    return "\n".join(lines)


def person_block(p, lite=False):
    tag = p.get("tag")
    out = [f"##### {p.get('cn') or p.get('name')} · {p.get('name')}", ""]
    out.append(f"- **身份**：{p.get('role', '')}（{p.get('org', '')}）")
    out.append(f"- **标签**：{TAG_CN.get(tag, tag)}")
    if p.get("stance"):
        out.append(f"- **立场**：{p['stance']}")
    pts = sorted(p.get("points") or [], key=lambda x: x.get("date", ""), reverse=True)
    if lite:
        if pts:
            latest = pts[0]
            out.append(f"- **最近一条**：`{latest.get('date')}` {latest.get('src', '')} — "
                       f"{latest.get('text', '')}")
        out.append("")
        return "\n".join(out)
    if pts:
        out.append(f"- **要点**（{len(pts)} 条，新→旧）：")
        for q in pts:
            dt = q.get("date", "")
            mark = "（月份级）" if month_level(dt) else ""
            out.append(f"  - `{dt}`{mark}｜{q.get('src', '')}｜{q.get('text', '')}")
    else:
        out.append("- **要点**：无")
    out.append("")
    return "\n".join(out)


def people_block(d, lite=False):
    ps = d["people"]
    cm = cat_map(d)
    out = ["## 3. 人物卡片（按分类）", ""]
    out.append("> 顺序是作者编排的顺序，不是按热度或字母。每人的要点已按日期倒序。")
    out.append("")
    ci = 0
    for cid, c in cm.items():
        sub = [p for p in ps if p.get("cat") == cid]
        if not sub:
            continue
        ci += 1
        n = sum(len(p.get("points") or []) for p in sub)
        out.append(f"### 3.{ci} {c['name']}（{len(sub)} 位 / {n} 条）")
        if c["desc"]:
            out.append("")
            out.append(f"> {c['desc']}")
        out.append("")
        if c["groups"]:
            gi = 0
            for gid, gname in c["groups"].items():
                gsub = [p for p in sub if p.get("grp") == gid]
                if not gsub:
                    continue
                gi += 1
                out.append(f"#### {gname}（{len(gsub)} 位）")
                out.append("")
                for p in gsub:
                    out.append(person_block(p, lite))
            # 分组里没被任何 grp 收走的人（数据瑕疵兜底，不能静默丢人）
            rest = [p for p in sub if p.get("grp") not in c["groups"]]
            if rest:
                out.append(f"#### 未分组（{len(rest)} 位）")
                out.append("")
                for p in rest:
                    out.append(person_block(p, lite))
        else:
            for p in sub:
                out.append(person_block(p, lite))
    return "\n".join(out)


def index_block(d):
    ps = d["people"]
    out = ["## 4. 按标签交叉索引", ""]
    out.append("> 要问「谁看空 AI」「多空双方各自理由」时，从这张表里挑人，再去第 3 节读细节。")
    out.append("")
    for t in TAG_ORDER:
        sub = [p for p in ps if p.get("tag") == t]
        out.append(f"### {TAG_CN[t]}（{len(sub)} 位）")
        out.append("")
        if not sub:
            out.append("（无）")
            out.append("")
            continue
        for p in sub:
            out.append(f"- **{p.get('cn') or p.get('name')}**（{p.get('role', '')}）："
                       f"{p.get('stance', '')}")
        out.append("")
    return "\n".join(out)


def timeline_block(d, limit=None):
    rows = []
    for p in d["people"]:
        for q in p.get("points") or []:
            rows.append((q.get("date", ""), p.get("cn") or p.get("name"), q.get("src", ""),
                         q.get("text", "")))
    rows.sort(key=lambda r: r[0], reverse=True)
    if limit:
        rows = rows[:limit]
    out = ["## 5. 时间轴（全部要点，新→旧）", ""]
    out.append("> 同一日期内顺序按数据文件原序，不保证严格时分。月份级日期表示原文只到月。")
    out.append("")
    for dt, who, src, txt in rows:
        out.append(f"- `{dt}` **{who}**｜{src}｜{txt}")
    out.append("")
    return "\n".join(out)


def header(d, lite=False):
    ps = d["people"]
    npts = sum(len(p.get("points") or []) for p in ps)
    return "\n".join([
        "# 最新风向 · 人物观点库",
        "",
        f"> 窗口 {d['window'][0]} ~ {d['window'][1]}｜{len(ps)} 位人物 / {npts} 条观点与动作"
        f"｜数据截止 {d['as_of']}｜导出 {d.get('updated', '')}",
        "",
        "## 0. 读法（把这段一起给模型，别只粘正文）",
        "",
        "**这是什么。** 一份人工整理的观点与动作库：把市场上真正说得上话的人，在这三个多月里"
        "说过的话、做过的仓、报过的数，逐条摘出来，每条都带日期和出处。不是新闻摘要，"
        "是「谁在什么时间、以什么口径、说了什么」。",
        "",
        "**怎么读才不出错。**",
        "",
        "1. **第 2 节是作者判断，第 3 节起才是引述。** 主线里的因果链是整理者写的，不是当事人说的；"
        "人物卡片里 `立场` 一行也是整理者的概括，`要点` 里的正文才是原话或报道事实。"
        "要引用，引 `要点`，别引 `立场`。",
        "2. **`标签` 是表态口径，不是评级、不是买卖建议。** 偏多 / 偏空指的是这个人在窗口内"
        "对主要资产的方向性表述，跟他的持仓、跟资产未来涨跌都不是一回事。同一个人可以"
        "「看空但满仓」（嘴上谨慎、13F 加仓），这种矛盾在原数据里保留，没有抹平。",
        "3. **日期精度不齐。** `2026-11-04` 是日级；`2026-08` 是月份级（原始报道只到月），"
        "已逐条标注「（月份级）」，不要把它当某一天的事。",
        "4. **来源是二手转述居多。** `src` 写的是「X 帖 / 路透社 / 财报电话会 / 13F」这类出口，"
        "不等于原文链接。要点文字是压缩过的转述，不是逐字引语——需要逐字引用时必须回原源核对。",
        "5. **不要把这批人当成市场。** 125 人是有偏样本：英文媒体覆盖多、买方与卖方多、"
        "中国科技与政府板块的表述口径与美方不同。用他们统计「共识」会系统性高估某几类声音。",
        "",
        "**建议怎么用。** 这份文档适合做四类事：① 找同一议题上的对立面（第 4 节按标签索引）；"
        "② 拉某条线的时间演进（第 5 节时间轴）；③ 核对某人的说法前后是否一致；"
        "④ 以第 2 节主线为骨架，用第 3 节素材重写一篇有出处的分析。不适合做：预测点位、"
        "给买卖结论、把标签当评级排序。",
        "",
        "---",
        "",
    ])


def build(d, lite=False, timeline=True):
    parts = [header(d, lite), stats_block(d), brief_block(d),
             people_block(d, lite), index_block(d)]
    if timeline and not lite:
        parts.append(timeline_block(d))
    parts.append("\n---\n\n> 本文档由 `scripts/export_wind.py` 从 `data/wind/wind.json` 机械导出，"
                 "未做任何改写。原始数据窗口之外的信息不在其中。\n")
    return "\n".join(p for p in parts if p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "output", "wind-for-chatgpt.md"))
    ap.add_argument("--lite", action="store_true", help="只出主线与每人一句话")
    ap.add_argument("--no-timeline", action="store_true", help="不输出第 5 节时间轴")
    a = ap.parse_args()

    d = load()
    md = build(d, lite=a.lite, timeline=not a.no_timeline)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(md)

    kb = len(md.encode("utf-8")) / 1024
    # 中文在 cl100k 下约 0.7 token/字（英文约 0.25 token/字符），按字符数估比按字节准
    est = int(len(md) / 1.4)
    print(f"written {a.out}")
    print(f"  {kb:.1f} KB / {len(md.splitlines())} 行 / 约 {est/1000:.1f}k tokens（粗估）")


if __name__ == "__main__":
    main()
