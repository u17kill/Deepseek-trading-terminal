#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今日市场判断：取数底座与写盘闸门。

红线 14：判断文字由人写，程序不拼装。所以本文件里**没有任何一句结论模板**：
  · context 只把当日读数摊平（数值 + 日期 + 口径），排序是排序，不是判断
  · write   只做校验与落盘，写什么由调用方决定

三个子命令
  status                          今天该不该写
  context [--fetch] [--json F]    打印事实清单；--fetch 先确保行情是今天的
  write <draft.json> [--no-build] 校验 → 备份 → 落盘 → 重建页面

为什么要有这个文件
------------------
`data/market/brief.json` 的 as_of 从 2026-11-13 起停了十天，而仓库里没有任何
程序会写它——页面只能一遍遍重读同一段话，被反复当成「刷新没生效」的 bug。
恢复写作之后，真正容易出错的地方不是写作本身，而是**数字的来源与时点**：
同一栏里美股是上一交易日 ETF 收盘、纳指100 是期货盘中、A 股分位来自研究侧
11-23 的结果、A 股情绪停在 11-06。谁都不标注日期，写出来的判断就会悄悄
张冠李戴。所以清单里每一组、甚至每一行都带来源日期与滞后天数。

校验挡的是另一类事故：判断写错日期（as_of 与快照不符，页面立刻标 stale）、
条数不是 5 条（页面按 01~05 编号，少一条就空一位）、整段没有数字
（「市场情绪偏弱」这种话配上六个数字栏位，读者会以为数字是判断的依据）。
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")

MARKET = os.path.join(DATA, "market", "market.json")
BRIEF = os.path.join(DATA, "market", "brief.json")
HIST = os.path.join(DATA, "market", "brief_history")

APAC = os.path.join(DATA, "apac", "apac.json")
HK = os.path.join(DATA, "hk_senti", "hk_senti.json")
SENTI = os.path.join(DATA, "senti", "sentiment.json")
RES = os.path.join(DATA, "oos", "result_latest.json")

POINTS_N = 5          # 页面按 01~05 编号
TONE = ("risk", "calm", "mix")

# 只拦明确的**指令**措辞，不拦「融资买入额」「北向净卖出」这类中性描述。
# 裸词「买入/卖出」在财经文本里几乎全是中性用法，拦了会误伤。
BANNED = ("建议买", "建议卖", "建议加仓", "建议减仓", "可买入", "可卖出",
          "目标价", "止盈", "止损", "满仓", "清仓", "抄底", "追高")


# ---------------------------------------------------------------- 工具

def _j(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _w(s):
    """显示宽度：中日韩全角算 2 列。终端对齐不能按 len()。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, n):
    s = str(s)
    return s + " " * max(0, n - _w(s))


def _rpad(s, n):
    """右对齐：数字列。"""
    s = str(s)
    return " " * max(0, n - _w(s)) + s


def _pct(v, nd=2, sign=True):
    if v is None:
        return "—"
    return ("%+.*f%%" % (nd, v)) if sign else ("%.*f%%" % (nd, v))


def _px(v):
    if v is None:
        return "—"
    return "{:,.2f}".format(v) if abs(v) < 100000 else "{:,.0f}".format(v)


def _days_ago(datestr, today=None):
    if not datestr:
        return None
    today = today or time.strftime("%Y-%m-%d")
    try:
        a = time.mktime(time.strptime(str(datestr)[:10], "%Y-%m-%d"))
        b = time.mktime(time.strptime(today[:10], "%Y-%m-%d"))
        return int((b - a) / 86400)
    except Exception:
        return None


def _lag(datestr, today=None):
    n = _days_ago(datestr, today)
    if n is None:
        return "日期未知"
    if n <= 0:
        return "当日"
    return "%d 天前" % n


# ---------------------------------------------------------------- 正文里的 HTML

# 判断正文是 innerHTML 渲染的（要支持涨绿跌红），所以只放行三种标签：
# <b> / <i> / <span class="u|d">。其余标签一律剥掉、只留内容——不清洗的话，
# 一个 <script> 或 onerror 就能从数据文件一路跑到页面上。
_TAG_OK = re.compile(r"^(?:</?(?:b|i)>|<span class=\"(?:u|d)\">|</span>)$")
_TAG_ANY = re.compile(r"<[^>]*>")


def clean(s):
    """白名单清洗：合法标签原样保留，非法标签去掉但保留文字。"""
    if not isinstance(s, str):
        return s
    out, i = [], 0
    for m in _TAG_ANY.finditer(s):
        out.append(s[i:m.start()])
        if _TAG_OK.match(m.group(0)):
            out.append(m.group(0))
        i = m.end()
    out.append(s[i:])
    return "".join(out)


def plain(s):
    """去掉标签后的纯文本。长度与「有没有数字」都要按它算，
    否则 <span class="d">−3.31%</span> 这 30 个字符会把 24 字上限顶爆。"""
    return _TAG_ANY.sub("", s if isinstance(s, str) else "")


def _intraday_pos(a):
    """分时位置：收盘落在当日高低区间的百分之几。纯计算，不含判断。

    `spark.type` 有两种，**不能混算**：`minute` 是当日分时点，`daily` 是最近
    20 个交易日的收盘序列。对 daily 算出来的不是「日内位置」而是「20 日区间位置」，
    写成「收在日内最低」就是错的——日经那种 −5% 的日子两者恰好都靠底，不核对
    根本发现不了。daily 一律不出位置：亚太组下面已经有更完整的位置/动量/回撤，
    同一页出现两种「位置」反而更难读。
    """
    sp = (a or {}).get("spark") or {}
    pts = sp.get("pts") or []
    if len(pts) < 3 or sp.get("type") != "minute":
        return None, None
    vals = []
    for x in pts:
        try:
            vals.append(float(x[1]) if isinstance(x, (list, tuple)) else float(x))
        except Exception:
            pass
    if len(vals) < 3:
        return None, None
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return None, None
    lab = "日内位置"
    return lab, round((vals[-1] - lo) / (hi - lo) * 100, 1)


# ---------------------------------------------------------------- 取数

def _by_name(assets):
    return {a.get("name"): a for a in (assets or [])}


def _group_line(rows, note=""):
    """一组读数：名字 / 代码 / 价格 / 涨跌 / 日内位置。返回行列表。"""
    out = []
    for nm, a, extra in rows:
        if not a:
            out.append("  %s（缺）" % _pad(nm, 16))
            continue
        unit = "bp" if a.get("kind") == "bd" else "%"
        chg = a.get("chg")
        if chg is None:
            c = "—"
        elif unit == "bp":
            c = "%+d bp" % int(round(chg))
        else:
            c = _pct(chg)
        lab, pos = _intraday_pos(a)
        pos_s = "" if pos is None else "%s %5.1f" % (lab, pos)
        out.append("  %s%s  %s  %s" % (
            _pad(nm, 16), _rpad(_px(a.get("price")), 12), _rpad(c, 9),
            pos_s + ("  " + extra if extra else "")))
    return out


def _rank(rows):
    """同日横向排序：强者 / 弱者。排的是涨跌幅，不是好坏。"""
    ok = [(nm, a.get("chg")) for nm, a, _ in rows if a and a.get("chg") is not None]
    if len(ok) < 3:
        return ""
    ok.sort(key=lambda kv: kv[1], reverse=True)
    up = "、".join("%s %s" % (n, _pct(c)) for n, c in ok[:2])
    dn = "、".join("%s %s" % (n, _pct(c)) for n, c in ok[-2:][::-1])
    return "  强 %s ｜ 弱 %s" % (up, dn)


def collect(fetch=False):
    """把当日事实收成一个 dict（不做任何结论）。"""
    mkt = _j(MARKET)
    if not mkt:
        raise SystemExit("读不到 %s" % MARKET)

    today = time.strftime("%Y-%m-%d")
    if fetch and mkt.get("date") != today:
        sys.stderr.write("[取数] 行情停在 %s，先刷新一次（约 50 秒）…\n" % mkt.get("date"))
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import market as _m
        _m.build()
        mkt = _j(MARKET) or mkt

    assets = mkt.get("assets") or []
    by = _by_name(assets)
    date = mkt.get("date")

    apac = _j(APAC) or {}
    hk = _j(HK) or {}
    senti = _j(SENTI) or {}
    res = _j(RES) or {}

    facts = {
        "trade_date": date,
        "updated": mkt.get("updated"),
        "elapsed": mkt.get("elapsed"),
        "today": today,
        "brief_as_of": (_j(BRIEF) or {}).get("as_of"),
        "groups": {},
        "flags": [],
    }

    # ---- 美股（ETF 价格 / 期货，上一交易日）
    us = [("道琼斯", by.get("道琼斯"), ""),
          ("标普500", by.get("标普500"), "IVV"),
          ("纳斯达克100", by.get("纳斯达克100"), "NQ 期货"),
          ("费城半导体", by.get("费城半导体"), "SOXX")]
    facts["groups"]["美股"] = {"rows": _group_line(us), "rank": _rank(us),
                              "note": "ETF 收盘价（纳指100 为期货）；与 A 股不同交易日"}

    # ---- A 股（当日收盘）
    cn = [("上证指数", by.get("上证指数"), ""),
          ("深证成指", by.get("深证成指"), ""),
          ("沪深300", by.get("沪深300"), ""),
          ("科创50", by.get("科创50"), ""),
          ("创业板指", by.get("创业板指"), "")]
    facts["groups"]["A股"] = {"rows": _group_line(cn), "rank": _rank(cn), "note": "当日收盘"}

    # ---- 港股
    hk_idx = [("恒生指数", by.get("恒生指数"), ""), ("恒生科技", by.get("恒生科技"), "")]
    g = {"rows": _group_line(hk_idx), "rank": _rank(hk_idx), "note": "当日收盘"}
    if hk:
        br = (hk.get("breadth") or {}).get("all") or {}
        extra = []
        if br:
            extra.append("宽度 %s 只：涨 %s / 跌 %s / 平 %s" % (
                br.get("n"), br.get("up"), br.get("down"), br.get("flat")))
        gl = (hk.get("gainers") or [])[:3]
        ll = (hk.get("losers") or [])[:3]
        if gl:
            extra.append("领涨 " + "、".join("%s %s" % (x.get("name"), _pct(x.get("chg"))) for x in gl))
        if ll:
            extra.append("领跌 " + "、".join("%s %s" % (x.get("name"), _pct(x.get("chg"))) for x in ll))
        g["extra"] = extra
        g["date"] = hk.get("date")
    facts["groups"]["港股"] = g

    # ---- 亚太（apex 的 name 与资产清单不同名：资产叫「韩国 KOSPI」，apex 叫「KOSPI」，
    #      只能按 code 对，按 name 会三行全缺且不报错）
    by_code = {a.get("code"): a for a in assets}
    ap = []
    for mk in (apac.get("markets") or []):
        ap.append((mk.get("name"), by_code.get(mk.get("sym")), ""))
    g = {"rows": _group_line(ap), "rank": _rank(ap), "note": "当日或最近收盘"}
    if apac.get("markets"):
        lines = []
        for mk in apac["markets"]:
            lines.append("    %s 位置 %5.1f  动量 %5.1f  RSI6 %4.1f  250日回撤 %6.1f%%  %s  （数据 %s）" % (
                _pad(mk.get("name"), 12), mk.get("pos") or 0, mk.get("mom") or 0,
                mk.get("rsi6") or 0, mk.get("dd250") or 0, mk.get("label") or "",
                mk.get("end") or "—"))
        g["extra"] = lines
        g["date"] = apac.get("date")
    facts["groups"]["亚太"] = g

    # ---- 商品 / 外汇 / 加密
    cm = [("COMEX 黄金", by.get("COMEX 黄金"), ""), ("COMEX 白银", by.get("COMEX 白银"), ""),
          ("COMEX 铜", by.get("COMEX 铜"), ""), ("WTI 原油", by.get("WTI 原油"), "")]
    fx = [("美元指数", by.get("美元指数"), ""), ("美元/人民币", by.get("美元/人民币"), ""),
          ("美元/日元", by.get("美元/日元"), "")]
    cy = [("比特币", by.get("比特币"), ""), ("以太坊", by.get("以太坊"), "")]
    bd = [("10年期美债", by.get("10年期美债"), ""), ("中国10年国债", by.get("中国10年国债"), ""),
          ("日本10年国债", by.get("日本10年国债"), "")]
    facts["groups"]["商品"] = {"rows": _group_line(cm), "rank": _rank(cm), "note": "期货"}
    facts["groups"]["外汇"] = {"rows": _group_line(fx), "rank": "", "note": ""}
    facts["groups"]["加密"] = {"rows": _group_line(cy), "rank": "", "note": ""}
    facts["groups"]["利率"] = {"rows": _group_line(bd), "rank": "", "note": "变化为基点 bp，不是 %"}

    # ---- A 股拥挤度 / 位置（研究侧结果，逐标的带日期）
    if res:
        lines = []
        for nm, d in res.items():
            last = (d or {}).get("last") or {}
            if not last:
                continue
            lines.append("    %s 收 %s  拥挤度 %5.1f  位置 %5.1f  动量 %5.1f  RSI6 %4.1f  %s  （数据 %s）" % (
                _pad(nm, 10), _rpad(_px(last.get("close")), 10),
                last.get("score") or 0, last.get("s_pos") or 0, last.get("s_mom") or 0,
                last.get("rsi6") or 0, last.get("bk") or "", last.get("date") or "—"))
        facts["groups"]["A股结构"] = {"rows": lines, "rank": "",
                                    "note": "研究侧结果，日期见行尾"}

    # ---- A 股情绪（可能停更，必须逐项标）
    if senti:
        sd = senti.get("date")
        lag = _days_ago(sd)
        br = senti.get("breadth") or {}
        vol = senti.get("volume") or {}
        lines = []
        lines.append("    温度 %s（前值 %s，变化 %s）区间 %s" % (
            senti.get("temp"), senti.get("temp_prev"), senti.get("temp_delta"), senti.get("band")))
        if br:
            lines.append("    宽度 %s 只：涨 %s / 跌 %s / 平 %s（上涨占比 %s%%）涨停 %s 跌停 %s" % (
                br.get("total"), br.get("up"), br.get("down"), br.get("flat"),
                br.get("up_pct"), br.get("lu"), br.get("ld")))
        if vol:
            # amt 的单位是**万亿**（sentiment.py 里 /1e8 后的值再 /1e4），不是亿。
            # 写成「1.658 亿」会小四个数量级，而这种错在正文里没人看得出来。
            lines.append("    成交额 %s 万亿（约 %s 亿），为 120 日均量的 %s%%" % (
                vol.get("amt"), round((vol.get("amt") or 0) * 1e4), vol.get("pct120")))
        g = {"rows": lines, "rank": "", "date": sd}
        if lag is not None and lag > 2:
            g["warn"] = "停更 %d 天（%s），本项不得当作今日读数使用" % (lag, sd)
            facts["flags"].append("A股情绪停在 %s（%d 天前）" % (sd, lag))
        facts["groups"]["A股情绪"] = g

    # ---- 判断文件现状
    facts["flags"].append("brief.as_of = %s，行情 date = %s" % (facts["brief_as_of"], date))

    return facts


# ---------------------------------------------------------------- 渲染

def render(facts):
    L = []
    bar = "─" * 74
    L.append(bar)
    L.append("今日市场判断 · 取数底座（只有读数，没有结论）")
    L.append(bar)
    L.append("行情交易日  %s　抓取于 %s（%.1fs）" % (
        facts["trade_date"], facts.get("updated"), facts.get("elapsed") or 0))
    L.append("当前时间    %s" % facts["today"])
    as_of = facts.get("brief_as_of")
    same = as_of == facts["trade_date"]
    L.append("判断文件    brief.json 停在 %s%s" % (
        as_of or "（无）", "　←　与行情同日，无需重写" if same else "　←　落后，今日应写"))
    L.append("")

    order = ["美股", "A股", "A股结构", "港股", "亚太", "商品", "外汇", "加密", "利率", "A股情绪"]
    for key in order:
        g = facts["groups"].get(key)
        if not g:
            continue
        head = key
        bits = [b for b in (g.get("note"), ("数据 %s" % g.get("date")) if g.get("date") else "") if b]
        if bits:
            head += "　·　" + "　".join(bits)
        L.append("【%s】" % head)
        for line in (g.get("rows") or []):
            L.append(line)
        for line in (g.get("extra") or []):
            L.append(line)
        if g.get("rank"):
            L.append(g["rank"])
        if g.get("warn"):
            L.append("  ⚠ %s" % g["warn"])
        L.append("")

    L.append(bar)
    L.append("写之前看这三条")
    L.append("  1) 每一组都能不同日期。相差超过 1 个交易日就必须在正文里说出来，不能并排当同一天。")
    L.append("  2) 上面是读数，判断由写的人下。程序不会替你写「所以市场如何」。")
    L.append("  3) 写盘用：python scripts/brief.py write <draft.json>")
    return "\n".join(L)


# ---------------------------------------------------------------- 校验

def validate(draft, trade_date):
    err, warn = [], []
    if not isinstance(draft, dict):
        return ["草稿不是 JSON 对象"], []

    as_of = draft.get("as_of")
    if not as_of:
        err.append("缺 as_of（应等于行情交易日 %s）" % trade_date)
    elif str(as_of)[:10] != str(trade_date)[:10]:
        err.append("as_of=%s 与行情交易日 %s 不符——页面会立刻把这段标成过期" % (as_of, trade_date))

    for k, lo, hi in (("verdict", 2, 24), ("mood", 1, 12), ("headline", 4, 48), ("lead", 10, 220)):
        v = plain(draft.get(k) or "").strip()
        if not v:
            err.append("缺 %s" % k)
        elif not (lo <= len(v) <= hi):
            err.append("%s 正文 %d 字，应在 %d~%d 字（按去标签后的字数算）" % (k, len(v), lo, hi))

    tone = draft.get("tone")
    if tone not in TONE:
        err.append("tone=%r 必须是 %s 之一" % (tone, " / ".join(TONE)))

    pts = draft.get("points") or []
    if not isinstance(pts, list):
        err.append("points 必须是数组")
        pts = []
    elif len(pts) != POINTS_N:
        err.append("points 有 %d 条，页面按 01~%02d 编号，必须正好 %d 条" % (len(pts), POINTS_N, POINTS_N))
    for i, p in enumerate(pts, 1):
        if not isinstance(p, str) or not p.strip():
            err.append("points 第 %d 条为空" % i)
        elif len(plain(p)) > 240:
            err.append("points 第 %d 条正文 %d 字，超过 240 会挤掉版面" % (i, len(plain(p))))

    # 必须带数字：整段没数字，读者会以为右边六个数字栏是它的依据
    if plain(draft.get("headline") or "") and not any(c.isdigit() for c in plain(draft["headline"])):
        err.append("headline 里没有数字——这一栏的价值就在具体读数")
    if plain(draft.get("lead") or "") and not any(c.isdigit() for c in plain(draft["lead"])):
        err.append("lead 里没有数字")
    for i, p in enumerate(pts, 1):
        if isinstance(p, str) and p.strip() and not any(c.isdigit() for c in plain(p)):
            warn.append("points 第 %d 条没有数字" % i)

    blob = "".join([draft.get("verdict") or "", draft.get("headline") or "",
                    draft.get("lead") or ""] + [p for p in pts if isinstance(p, str)])
    for w in BANNED:
        if w in blob:
            err.append("出现「%s」——这一段是描述与结构，不写买卖指令" % w)

    if draft.get("author") and draft["author"] not in ("AI", "人"):
        warn.append("author=%r 不在 AI / 人 之内" % draft["author"])
    return err, warn


def write(draft, allow_stale=False, rebuild=True, commit=True):
    mkt = _j(MARKET) or {}
    trade_date = mkt.get("date")
    err, warn = validate(draft, trade_date)
    if err and allow_stale:
        err = [e for e in err if "as_of" not in e]
        warn.append("已用 --allow-stale 放行 as_of 不符")
    if err:
        sys.stderr.write("拒绝写入，%d 处不合格：\n" % len(err))
        for e in err:
            sys.stderr.write("  ✗ %s\n" % e)
        for w in warn:
            sys.stderr.write("  ! %s\n" % w)
        return 2

    out = {
        "as_of": str(draft["as_of"])[:10],
        # 补写历史某一天时，generated 要保留当时的时间，不能填成此刻——
        # 否则「判断生成」这一栏会显示成今天是哪天补的，看的人会误判时效。
        "generated": draft.get("generated") or time.strftime("%Y-%m-%d %H:%M"),
        "author": draft.get("author") or "AI",
        "verdict": clean(draft["verdict"]).strip(),
        "tone": draft["tone"],
        "mood": clean(draft["mood"]).strip(),
        "headline": clean(draft["headline"]).strip(),
        "lead": clean(draft["lead"]).strip(),
        "points": [clean(p).strip() for p in draft["points"]],
        "disclaimer": draft.get("disclaimer") or
                      "本段判断由 %s 按当日行情快照撰写，跨交易日自动失效；不构成投资建议。"
                      % (draft.get("author") or "AI"),
    }

    # 备份：判断是会老的。留着历史，才能回头看「当时为什么这么读」。
    os.makedirs(HIST, exist_ok=True)
    old = _j(BRIEF)
    if old and old.get("as_of"):
        tag = str(old.get("as_of"))[:10]
        bak = os.path.join(HIST, "brief_%s.json" % tag)
        if not os.path.exists(bak):
            with io.open(bak, "w", encoding="utf-8", newline="\n") as f:
                json.dump(old, f, ensure_ascii=False, indent=2)
    keep = sorted([x for x in os.listdir(HIST) if x.endswith(".json")])[-200:]
    for x in set(os.listdir(HIST)) - set(keep):
        try:
            if x.endswith(".json"):
                os.remove(os.path.join(HIST, x))
        except OSError:
            pass

    with io.open(BRIEF, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("已写入 %s" % os.path.relpath(BRIEF, BASE))
    print("  as_of %s ｜ 生成 %s ｜ 作者 %s ｜ %d 条要点" % (
        out["as_of"], out["generated"], out["author"], len(out["points"])))
    for w in warn:
        print("  ! %s" % w)

    if rebuild:
        # 页面读的是 market.json 里内嵌的 brief 副本（build_dashboard 只认它），
        # 不是 brief.json 本身。少这一步，写入会「成功」但页面一个字都不变——
        # 与 v1.50.0 那个「刷新只搬数字」是同一类故障，只是这次断在更前面。
        try:
            if not sync_market():
                print("  ✗ market.json 不存在，无法同步")
                return 4
        except Exception as e:  # noqa: BLE001
            print("  ✗ 同步 market.json 失败：%s" % e)
            return 4
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(BASE, "scripts", "build_dashboard.py")],
                           cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        if r.returncode != 0:
            print("  ✗ 重建页面失败：%s" % ((r.stderr or "").strip().splitlines()[-3:]))
            return 3
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        print("  页面已重建 %.1fs ｜ %s" % (time.time() - t0, tail[0]))
    if commit:
        _commit(out)
    return 0


def _commit(out):
    """判断逐日入库。`.gitignore` 里写明了 brief.json 要进仓库（「逐日写下的判断文字」）。

    不提交的后果不是「少个历史」——是**会被还原**：11-23 16:37 那份确实写进过磁盘
    （快照里存着全文），但没入库，之后的某次 `git checkout -- .` 把它退回了 11-13，
    于是页面上「判断停在 11-13」又持续了一天。所以写完必须提交。
    """
    try:
        subprocess.run(["git", "add", "data/market/brief.json"], cwd=BASE,
                       capture_output=True, timeout=60)
        st = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=BASE,
                            capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace")
        staged = [x.strip() for x in (st.stdout or "").splitlines() if x.strip()]
        # 暂存区里有别的东西就停手：这个提交只该包含判断文件，混进代码改动会很难查。
        if set(staged) - {"data/market/brief.json"}:
            print("  ! 暂存区还有其它文件（%s），跳过自动提交" % "、".join(
                sorted(set(staged) - {"data/market/brief.json"})))
            return
        msg = "数据：%s 收盘判断（%s）" % (out["as_of"], plain(out["verdict"]))
        r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=BASE,
                           capture_output=True, timeout=60, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            print("  已提交：%s" % msg)
        else:
            print("  ! 提交未成功（%s），文件已落盘但未入库" % (r.stderr or "").strip()[:120])
    except Exception as e:  # noqa: BLE001
        print("  ! 提交失败：%s" % e)


def sync_market():
    """把 brief.json 同步进 market.json 的内嵌副本。

    页面读的是 market.json.brief，不是 brief.json。反过来，market.json 每次
    被恢复/重刷时又会把 brief 字段一起带回来——**两份文件必须一起动**，
    只改一份就会出现「判断写了但页面不变」，或者「页面有判断但丢了作者」。
    """
    sys.path.insert(0, os.path.join(BASE, "scripts"))
    import market as _m
    st = _j(MARKET)
    if not st:
        return False
    st["brief"] = _m.load_brief(st.get("date"))
    _m.save_state(st)
    return True


def status():
    mkt = _j(MARKET) or {}
    b = _j(BRIEF) or {}
    td, as_of = mkt.get("date"), b.get("as_of")
    print("行情交易日  %s（抓取于 %s）" % (td, mkt.get("updated")))
    print("判断 as_of  %s（生成 %s）" % (as_of, b.get("generated")))
    if not b.get("headline"):
        print("结论        判断文件里没有正文，需要写")
        return 1
    if as_of == td:
        print("结论        同一交易日，不必重写（补改文字仍可 write）")
        return 0
    print("结论        判断落后 %s 个交易日，应写" % (_days_ago(as_of, td) if as_of else "?"))
    return 1


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="今日市场判断：取数底座与写盘闸门")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="今天该不该写")
    sub.add_parser("sync", help="把 brief.json 同步进 market.json 并重建页面（判断没改、数据被覆盖时用）")

    c = sub.add_parser("context", help="打印事实清单")
    c.add_argument("--fetch", action="store_true", help="行情不是今天就先刷新")
    c.add_argument("--json", dest="json_out", help="把事实另存为 JSON")

    w = sub.add_parser("write", help="校验并写入草稿")
    w.add_argument("draft")
    w.add_argument("--allow-stale", action="store_true")
    w.add_argument("--no-build", action="store_true")
    w.add_argument("--no-commit", action="store_true")

    a = ap.parse_args()
    if a.cmd == "status":
        return status()
    if a.cmd == "sync":
        if not sync_market():
            sys.stderr.write("market.json 不存在\n")
            return 4
        st = _j(MARKET) or {}
        b = st.get("brief") or {}
        print("已同步：market.date=%s，brief.as_of=%s，%s" % (
            st.get("date"), b.get("as_of"), plain(b.get("verdict") or "")))
        r = subprocess.run([sys.executable, os.path.join(BASE, "scripts", "build_dashboard.py")],
                           cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        print("页面已重建" if r.returncode == 0 else "✗ 重建失败：%s" % (r.stderr or "")[-200:])
        return 0 if r.returncode == 0 else 3
    if a.cmd == "context":
        facts = collect(fetch=a.fetch)
        print(render(facts))
        if a.json_out:
            with io.open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
                json.dump(facts, f, ensure_ascii=False, indent=2)
        if facts.get("brief_as_of") == facts.get("trade_date"):
            print("\n[提示] 判断已与行情同日；若只是补改文字，仍可 write。")
        return 0
    if a.cmd == "write":
        try:
            with io.open(a.draft, encoding="utf-8") as f:
                draft = json.load(f)
        except Exception as e:
            sys.stderr.write("读草稿失败：%s\n" % e)
            return 2
        return write(draft, allow_stale=a.allow_stale, rebuild=not a.no_build,
                     commit=not a.no_commit)

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
