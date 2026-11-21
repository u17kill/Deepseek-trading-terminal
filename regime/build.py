# -*- coding: utf-8 -*-
"""Regime 模块渲染：score.json + notes.json -> out/regime.html

单文件、无外部依赖、直接双击可开。所有判断文字来自 data/notes.json（人写），
本文件只负责把读数摆上去。
"""
import datetime
import html
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "out")

LIGHT = {
    "dg": ("深绿", "#1f6b3a", "#e7f3eb"),
    "lg": ("浅绿", "#4f8a37", "#f0f7ea"),
    "y": ("黄", "#a9761a", "#fdf6e6"),
    "lr": ("浅红", "#b8503a", "#fbeeea"),
    "dr": ("深红", "#9a2b25", "#fbe8e6"),
    "na": ("未获取", "#7a7770", "#f1efea"),
}
MODNAME = {"policy": "政策", "macro": "宏观", "industry": "产业", "tech": "技术"}

BAND = [(55, "dg", "深绿"), (20, "lg", "浅绿"), (-20, "y", "黄"),
        (-55, "lr", "浅红"), (-999, "dr", "深红")]


def band_of(score):
    for lo, code, cn in BAND:
        if score >= lo:
            return code, cn
    return "dr", "深红"


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def num(x, n=1, sign=True):
    if x is None:
        return "—"
    return ("%+.*f" if sign else "%.*f") % (n, x)


def lamp(code, label=None, small=False):
    cn, ink, bg = LIGHT.get(code, LIGHT["na"])
    txt = label or cn
    cls = "lamp sm" if small else "lamp"
    return ('<span class="%s" style="color:%s;background:%s">'
            '<i style="background:%s"></i>%s</span>' % (cls, ink, bg, ink, esc(txt)))


def bar(score):
    """从 0 向两侧延伸的条，负红正绿。宽度按 |score|/100。"""
    if score is None:
        return '<div class="bar"><span class="mid"></span></div>'
    w = min(abs(score), 100) / 100.0 * 50.0
    if score >= 0:
        style = "left:50%%;width:%.1f%%;background:#4f8a37" % w
    else:
        style = "right:50%%;width:%.1f%%;background:#b8503a" % w
    return ('<div class="bar"><span class="mid"></span>'
            '<i style="%s"></i></div>' % style)


def css():
    return """
:root{
  --bg:#f7f6f2; --card:#ffffff; --line:#e6e2d8; --line2:#efecE4;
  --ink:#1f1e1c; --ink2:#5f5e5a; --ink3:#8f8b82;
  --dg:#1f6b3a; --lg:#4f8a37; --y:#a9761a; --lr:#b8503a; --dr:#9a2b25;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:13px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
main{max-width:1180px;margin:0 auto;padding:34px 26px 70px}
h1{font-size:21px;font-weight:600;margin:0 0 4px;letter-spacing:-.2px}
h2{font-size:14px;font-weight:600;margin:38px 0 12px;color:var(--ink);
  display:flex;align-items:center;gap:9px}
h2:after{content:"";flex:1;height:1px;background:var(--line)}
.sub{color:var(--ink3);font-size:12px;margin:0}
.mono{font-variant-numeric:tabular-nums}
.head{display:flex;justify-content:space-between;align-items:flex-start;
  gap:20px;flex-wrap:wrap;margin-bottom:22px}
.tags{display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:11px;color:var(--ink2);background:#fff;border:1px solid var(--line);
  border-radius:20px;padding:3px 10px}
.tag b{font-weight:600;color:var(--ink)}

.card{background:var(--card);border:1px solid var(--line);border-radius:12px}
.hero{padding:22px 24px;display:grid;grid-template-columns:230px 1fr;gap:26px}
.big{font-size:52px;font-weight:600;line-height:1;letter-spacing:-2px;
  font-variant-numeric:tabular-nums}
.big.na{color:var(--ink3);font-size:34px}
.bigsub{font-size:11.5px;color:var(--ink3);margin-top:8px}
.heroL{display:flex;flex-direction:column;justify-content:space-between;gap:14px}
.cov{font-size:11px;color:var(--ink3)}
.lamp{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  padding:3px 9px;border-radius:20px;font-weight:500;white-space:nowrap}
.lamp i{width:6px;height:6px;border-radius:50%;display:block}
.lamp.sm{font-size:11px;padding:2px 7px}

.mrow{display:grid;grid-template-columns:50px 58px 1fr 96px 92px;
  gap:12px;align-items:center;padding:7px 0}
.mrow+.mrow{border-top:1px solid var(--line2)}
.mname{font-size:12.5px;color:var(--ink2)}
.mval{font-variant-numeric:tabular-nums;font-weight:600;font-size:13.5px;text-align:right}
.mscore{font-size:10.5px;color:var(--ink3);text-align:right;white-space:nowrap}
.bar{position:relative;height:7px;background:#f0eee7;border-radius:4px;min-width:60px}
.bar .mid{position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:#d8d4c9}
.bar i{position:absolute;top:0;height:7px;border-radius:4px}

table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.tbl{overflow-x:auto;-webkit-overflow-scrolling:touch}
th{font-size:11px;font-weight:500;color:var(--ink3);text-align:right;padding:0 10px 8px;
  border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:8px 10px;border-bottom:1px solid var(--line2);font-size:12.5px;text-align:right}
tr:last-child td{border-bottom:none}
td.lbl{color:var(--ink2)}
tr.sum td{font-weight:600;border-top:1px solid var(--line);background:#fbfaf6}
.cell{display:inline-block;min-width:44px;padding:2px 6px;border-radius:5px;font-size:12px}
.pend{opacity:.72;background-image:repeating-linear-gradient(45deg,
  rgba(0,0,0,.05) 0 3px,transparent 3px 6px);border-radius:5px;
  outline:1px dashed #d9d4c6;outline-offset:-1px}

.agrid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.acard{padding:15px 16px 13px}
.ahead{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.aname{font-size:13.5px;font-weight:600}
.atick{font-size:10.5px;color:var(--ink3);margin-left:5px;font-weight:400}
.ascore{font-size:24px;font-weight:600;letter-spacing:-.6px;margin:8px 0 2px;
  font-variant-numeric:tabular-nums}
.apx{font-size:11px;color:var(--ink3)}
.ctb{margin-top:11px;padding-top:10px;border-top:1px solid var(--line2)}
.crow{display:grid;grid-template-columns:38px 1fr 38px;gap:8px;align-items:center;
  font-size:10.5px;color:var(--ink3);padding:2px 0}
.crow b{font-weight:500;text-align:right;font-variant-numeric:tabular-nums;color:var(--ink2)}
.cbar{height:4px;background:#f0eee7;border-radius:2px;position:relative}
.cbar .mid{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#d8d4c9}
.cbar i{position:absolute;top:0;height:4px;border-radius:2px}
.crow-pv{opacity:.78}
.crow .pstar{font-style:normal;color:#a9761a;font-size:9px;margin-left:1px;letter-spacing:0}

.etab th{font-size:10.5px}
.etab td{font-size:12px;padding:6px 10px}
.ev{display:inline-block;width:30px;text-align:center;border-radius:4px;font-size:11px;
  padding:1px 0;font-weight:500}
.lag{font-size:10.5px;color:var(--ink3)}
.lag.warn{color:#a9761a}

.cgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.ccard{padding:13px 15px}
.ct{font-size:11px;color:var(--ink3);margin-bottom:5px}
.cv{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.4px}
.cn{font-size:11px;color:var(--ink2);margin-top:4px;line-height:1.5}

.pdim{padding:13px 16px;border-bottom:1px solid var(--line2)}
.pdim:last-child{border-bottom:none}
.pdh{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.pdn{font-size:12.5px;font-weight:600}
.pdq{font-size:11.5px;color:var(--ink3);margin-top:2px}
.pda{margin-top:8px;font-size:12px;color:var(--ink2);padding-left:10px;
  border-left:2px solid var(--line)}
.pda .st{font-size:10px;padding:1px 6px;border-radius:10px;background:#fdf6e6;
  color:#a9761a;margin-left:6px;white-space:nowrap}
.pda .st.ok{background:#e7f3eb;color:#1f6b3a}

.note{background:#fbfaf6;border:1px dashed var(--line);border-radius:9px;
  padding:11px 14px;font-size:12px;color:var(--ink2);margin-top:10px}
.note b{color:var(--ink)}
.warnbox{background:#fdf6e6;border:1px solid #f0e2c0;border-radius:9px;
  padding:12px 15px;font-size:12px;color:#7a5512;margin-bottom:14px}
.warnbox ul{margin:6px 0 0;padding-left:18px}
footer{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);
  font-size:11px;color:var(--ink3);line-height:1.75}
@media(max-width:900px){
  .hero{grid-template-columns:1fr}
  .agrid{grid-template-columns:repeat(2,1fr)}
  .cgrid{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:620px){
  main{padding:20px 14px 50px}
  .agrid,.cgrid{grid-template-columns:1fr}
  .big{font-size:42px}
  .hero{padding:18px}
  .mrow{grid-template-columns:40px 50px 1fr 82px;gap:8px}
  .bar{min-width:0}
  .mscore{display:none}
  table{font-size:11px;min-width:540px}
  th,td{padding:6px 5px}
}
"""


def cell(v, code=None, pending=False):
    if v is None:
        return '<span class="cell" style="color:#8f8b82">—</span>'
    ink = "#5f5e5a"
    bg = "transparent"
    if code and code in LIGHT:
        ink, bg = LIGHT[code][1], LIGHT[code][2]
    elif v > 3:
        ink, bg = "#4f8a37", "#f0f7ea"
    elif v < -3:
        ink, bg = "#b8503a", "#fbeeea"
    cls = "cell pend" if pending else "cell"
    return '<span class="%s" style="color:%s;background:%s">%s</span>' % (
        cls, ink, bg, num(v))


def main():
    with open(os.path.join(DATA, "score.json"), encoding="utf-8") as f:
        d = json.load(f)
    notes = {}
    np_ = os.path.join(DATA, "notes.json")
    if os.path.exists(np_):
        with open(np_, encoding="utf-8") as f:
            notes = json.load(f)

    mods = d["modules"]
    assets = d["assets"]
    ra = d["risk_appetite"]
    pol = d.get("policy", {})
    W = d.get("weights", {})

    # ---- hero ----
    big_cls = "big na" if ra["score"] is None else "big"
    hero_rows = []
    for m in ("policy", "macro", "industry", "tech"):
        mo = mods[m]
        sc = mo["score"]
        ref = mo.get("preview") if m == "policy" else None
        shown = sc if sc is not None else (ref["score"] if ref else None)
        mark = "*" if (sc is None and ref) else ""
        code = mo["band"] if sc is not None else (ref["band"] if ref else mo["band"])
        lab = mo["label"] if sc is not None else (
            (ref["label"] + " · 参考") if ref else mo["label"])
        flag = ""
        if mo.get("downgraded"):
            flag = '<span style="color:#a9761a;font-size:10px">降</span>'
        elif mo.get("weak") and sc is not None:
            flag = '<span style="color:#8f8b82;font-size:10px">弱</span>'
        if m == "policy" and sc is None:
            meta = "%d 条待确认" % len(pol.get("rows") or [])
        else:
            meta = "disp %s · %d 项" % (
                num(mo["disp"], 2) if mo["disp"] is not None else "—", mo["n"])
        hero_rows.append(
            '<div class="mrow"><span class="mname">%s</span>'
            '<span class="mval mono">%s%s</span>%s'
            '<div style="display:flex;gap:5px;align-items:center;justify-content:flex-end">%s%s</div>'
            '<span class="mscore">%s</span></div>' % (
                MODNAME[m], num(shown), mark, bar(shown),
                lamp(code, lab, small=True), flag, meta))

    used = ra.get("modules_used") or []
    wcov = sum(W.get(m, 0) for m in used) * 100
    hero = """
<div class="card hero">
  <div class="heroL">
    <div>
      <div class="%s mono">%s</div>
      <div style="margin-top:10px">%s</div>
      <div class="bigsub">风险偏好总分 · %s</div>
    </div>
    <div class="cov">覆盖 %d/4 模块 · 权重 %.0f%%</div>
  </div>
  <div>%s</div>
</div>""" % (big_cls, num(ra["score"]), lamp(ra["band"], ra["label"]),
             esc(d.get("horizon_label", "")), len(used), wcov, "".join(hero_rows))

    # ---- 矩阵 ----
    aid_order = [a["id"] for a in assets]
    aname = dict((a["id"], a["name"]) for a in assets)
    thead = "".join("<th>%s</th>" % esc(aname[i]) for i in aid_order)
    pv = pol.get("preview_score")
    EL = d.get("elastic") or {}
    rows = []
    for m in ("policy", "macro", "industry", "tech"):
        mo = mods[m]
        cells = []
        for aid in aid_order:
            if m == "tech":
                a = next(x for x in assets if x["id"] == aid)
                cells.append("<td>%s</td>" % cell(
                    round(W.get("tech", .2) * (a["tech"] or 0), 1) if a["tech"] is not None else None))
            elif m == "policy" and mo["score"] is None:
                # 政策未确认也要能看出「若确认会怎样」：弹性取全局表，不依赖 contrib
                e = (EL.get("policy") or {}).get(aid)
                v = round(W.get("policy", .2) * (pv / 100.0) * e * 100, 1) if (
                    pv is not None and e is not None) else None
                cells.append("<td>%s</td>" % cell(v, pending=True))
            else:
                a = next(x for x in assets if x["id"] == aid)
                v = next((c["v"] for c in a["contrib"] if c["m"] == m), None)
                cells.append("<td>%s</td>" % cell(v))
        shown = pv if (m == "policy" and mo["score"] is None) else mo["score"]
        star = '<span style="color:#8f8b82;font-size:10px">*</span>' if (
            m == "policy" and mo["score"] is None) else ""
        rows.append('<tr><td class="lbl">%s%s</td><td>%s%s</td>%s</tr>' % (
            MODNAME[m], "", num(shown), star, "".join(cells)))
    sum_cells = "".join("<td>%s</td>" % cell(a["score"]) for a in assets)
    rows.append('<tr class="sum"><td class="lbl">合计</td><td></td>%s</tr>' % sum_cells)

    matrix = """
<div class="tbl"><table>
<tr><th>模块</th><th>模块分</th>%s</tr>
%s
</table></div>
<div class="note">格子里的数是「该模块给这个资产贡献了多少分」（权重 × 模块分 × 弹性），
加起来就是合计行。技术上那一行不是弹性映射，是各资产自己的价格行为打分。
%s</div>""" % (thead, "".join(rows),
              ('<b>*</b> 政策模块当前无 confirmed 条目，这一行是<b>参考值</b>（斜纹格），'
               '未计入合计行。' if pol.get("preview_score") is not None else ""))

    # ---- 资产 ----
    acards = []
    for a in assets:
        crows = []
        for c in a["contrib"]:
            v = c["v"]
            w = min(abs(v), 25) / 25.0 * 50.0
            pend = bool(c.get("pending"))
            if pend:
                style = "left:50%%;width:%.1f%%;background:#b8b2a6" % w if v >= 0 else (
                    "right:50%%;width:%.1f%%;background:#b8b2a6" % w)
            else:
                style = ("left:50%%;width:%.1f%%;background:#4f8a37" % w if v >= 0
                         else "right:50%%;width:%.1f%%;background:#b8503a" % w)
            tag = '<i class="pstar">*</i>' if pend else ""
            crows.append(
                '<div class="crow%s"><span>%s%s</span>'
                '<div class="cbar"><span class="mid"></span><i style="%s"></i></div>'
                '<b>%s</b></div>' % (" crow-pv" if pend else "", MODNAME[c["m"]], tag, style, num(v)))
        acards.append("""
<div class="card acard">
  <div class="ahead"><div><span class="aname">%s</span><span class="atick">%s</span></div>
    %s</div>
  <div class="ascore">%s</div>
  <div class="apx">%s</div>
  <div class="ctb">%s</div>
</div>""" % (esc(a["name"]), esc(a["ticker"]), lamp(a["band"], a["label"], small=True),
             num(a["score"]),
             "技术 %s · %s" % (num(a["tech"]), esc(a["px_date"] or "")),
             "".join(crows)))

    acard_note = ""
    if pol.get("preview_score") is not None:
        acard_note = ('<div class="note"><b>*</b> 政策这一行是<b>参考值</b>（灰色柱）：'
                      '政策模块当前无 confirmed 条目，该行不计入资产分，'
                      '只用来看「若政策确认，大概会往哪个方向推」。</div>')

    # ---- 证据表 ----
    ev_rows = []
    for i in d["indicators"]:
        b = i["band"]
        code = ("dg" if b >= 1.5 else "lg" if b >= 0.5 else "y" if b > -0.5
                else "lr" if b > -1.5 else "dr")
        lag = i.get("lag")
        lagcls = "lag warn" if (lag is not None and lag > 45) else "lag"
        lagtxt = ("%d 天前" % lag) if lag is not None else "—"
        pr = ("%.0f%%" % (i["p"] * 100)) if i["p"] is not None else "—"
        ev_rows.append(
            '<tr><td class="lbl">%s</td><td class="lbl" style="color:#8f8b82">%s</td>'
            '<td>%s</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>' % (
                esc(i["name"]), esc(i["date"]), esc(i["disp"]), pr,
                '<span class="ev" style="color:%s;background:%s">%s</span>' % (
                    LIGHT[code][1], LIGHT[code][2], num(b)),
                lagcls, lagtxt))

    # ---- 确认层 ----
    cmap = [("vix_ratio", "VIX / VIX3M", "波动率期限结构"),
            ("hy_chg", "HY OAS 20 日变化", "信用是否承认"),
            ("corr", "股债 60 日相关性", "避险是否有效"),
            ("gold_real", "黄金 20 日", "交易利率还是信用")]
    ccards = []
    for k, title, sub in cmap:
        c = d["confirm"].get(k)
        if not c:
            ccards.append('<div class="card ccard"><div class="ct">%s</div>'
                          '<div class="cv" style="color:#8f8b82">未获取</div>'
                          '<div class="cn">%s</div></div>' % (title, sub))
            continue
        v = c["v"]
        if k == "vix_ratio":
            vs = "%.3f" % v
        elif k == "hy_chg":
            vs = "%+.0f bp" % v
        elif k == "corr":
            vs = "%.2f" % v
        else:
            vs = "%+.1f%%" % v
        ccards.append('<div class="card ccard"><div class="ct">%s · %s</div>'
                      '<div class="cv">%s</div><div class="cn">%s</div></div>' % (
                          title, esc(c["date"]), vs, esc(c["note"])))

    # ---- 政策 ----
    pmod = mods["policy"]
    pdims = []
    seen = set()
    for r in pol.get("rows", []):
        seen.add(r["dim"])
        ok = r["state"] == "confirmed"
        pdims.append("""
<div class="pdim">
  <div class="pdh"><div><div class="pdn">%s　<span class="mono">%s</span></div>
    <div class="pdq">权重 %.0f%%</div></div>
    <div>%s<span class="st%s">%s</span></div></div>
  <div class="pda">%s%s</div>
</div>""" % (esc(r["name"]), num(r["score"]), r["weight"] * 100,
             lamp(("dg" if r["score"] >= 1.5 else "lg" if r["score"] >= .5 else "y"
                   if r["score"] > -.5 else "lr" if r["score"] > -1.5 else "dr"), None, True),
             " ok" if ok else "", "已确认" if ok else "待确认",
             esc(r["fact"]),
             ('<br><span style="color:#8f8b82;font-size:11px">%s</span>'
              % esc(r.get("by", ""))) if r.get("by") else ""))

    # 政策未确认时，明说「为什么不计分」以及「确认后会怎样」——
    # 只写「待确认」等于没解释，用户不知道自己该做什么。
    pnote = ""
    pv0 = pol.get("preview_score")
    if pv0 is not None and mods["policy"]["score"] is None:
        sc4 = dict((m, mods[m]["score"]) for m in W)
        sc4["policy"] = pv0
        den = sum(W[m] for m in W if sc4.get(m) is not None)
        risk_with = round(sum(W[m] * sc4[m] for m in W if sc4.get(m) is not None) / den, 1)
        b1, l1 = band_of(pv0)
        b2, l2 = band_of(risk_with)
        pnote = ('<div class="note">这 %d 条均为<b>待确认</b>：没有一手来源链接与事件日，'
                 '按规范不计入正式分。若能补齐来源并改为 confirmed，政策模块为 '
                 '<b>%s %s</b>，风险偏好总分将从 <b>%s %s</b> 变为 <b>%s %s</b>。'
                 '这不是程序判断，只是按当前权重算出的结果。</div>' % (
                     len(pol.get("rows") or []), l1, num(pv0),
                     ra["label"], num(ra["score"]), l2, num(risk_with)))

    warn = ""
    if d.get("warnings"):
        warn = '<div class="warnbox"><b>读数说明</b><ul>%s</ul></div>' % "".join(
            "<li>%s</li>" % esc(x) for x in d["warnings"])

    # ---- 人写的结论 ----
    hl = notes.get("headline") or ""
    note_block = ""
    if hl:
        note_block = ('<div class="card" style="padding:16px 18px;margin-bottom:14px">'
                      '<div style="font-size:11px;color:#8f8b82;margin-bottom:6px">判断（人写）</div>'
                      '<div style="font-size:13.5px;line-height:1.8">%s</div></div>' % esc(hl))
    else:
        note_block = ('<div class="note"><b>判断待填写</b>：这一栏留给结论与理由，'
                      '写在 data/notes.json，程序不生成。</div>')

    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>全球大类资产方向框架 · %s</title>
<style>%s</style></head>
<body><main>
<div class="head">
  <div>
    <h1>全球大类资产方向框架</h1>
    <p class="sub">四层读数 · 六类资产 · 中期口径（1~6 个月）</p>
  </div>
  <div class="tags">
    <span class="tag">数据日 <b>%s</b></span>
    <span class="tag">取数 <b>%s</b></span>
    <span class="tag">指标 <b>%d</b> 项</span>
    <span class="tag">产品 <b>regime v0.1</b></span>
  </div>
</div>
%s
<h2>模块 × 资产</h2>
%s
<h2>六类资产</h2>
<div class="agrid">%s</div>
%s
<h2>读数明细</h2>
<div class="tbl"><table class="etab">
<tr><th>指标</th><th>数据日</th><th>读数</th><th>分位</th><th>档位</th><th>滞后</th></tr>
%s
</table></div>
<h2>确认层</h2>
<div class="cgrid">%s</div>
<div class="note">这一层不产生方向，只回答「前面四层说的，市场承认了吗」。
信用利差没走阔而波动率期限结构倒挂，说明是估值压缩而非信用事件。</div>
<h2>政策模块（人工）</h2>
<div class="card">%s</div>
%s
<footer>
口径：单指标 → 滚动 5 年分位 → 五档（±2）→ 模块内截尾均值 → 模块分（±100）；
四模块加权（宏观 30 / 产业 30 / 政策 20 / 技术 20）→ 风险偏好总分；
模块分经先验弹性矩阵摊到六类资产。分歧度 &gt;1.0 档强制降一级灯。<br>
限制：利率路径用 2Y 收益率变化代理，不是 FedWatch；盈利修正广度与云厂 capex
缺失，以新订单/产能利用率/半导体相对强弱三角替代；分位窗口 5 年，
含 2022 高通胀期，极端读数需人工识别；政策分完全依赖人工填写。<br>
本页只陈述读数与机械合成结果，不构成投资建议。
</footer>
</main></body></html>""" % (
        esc(d["as_of"]), css(), esc(d["as_of"]), esc(d.get("fetched_at", "")),
        len(d["indicators"]), hero + warn + note_block, matrix,
        "".join(acards), acard_note, "".join(ev_rows), "".join(ccards), "".join(pdims), pnote)

    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "regime.html")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    print("written %s  %.1f KB" % (p, os.path.getsize(p) / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
