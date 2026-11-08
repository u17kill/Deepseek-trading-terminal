"""生成看板数据并注入模板，产出单文件 HTML。

数据源优先用服务刷新写入的 result_latest.json（含最新交易日的完整结果），
不存在时才回退到定稿快照 result_v3_1.json。
"""
import json
import os
import re
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "output")


def read_version():
    """读根目录 VERSION 文件。单一来源，避免版本号散落在代码和文档里各写一份。"""
    p = os.path.join(BASE, "VERSION")
    try:
        v = open(p, encoding="utf-8").read().strip()
    except OSError:
        return "0.0.0"
    return v or "0.0.0"


VER = read_version()


SRC = os.path.join(BASE, "data/oos/result_latest.json")
if not os.path.exists(SRC):
    SRC = os.path.join(BASE, "data/oos/result_v3_1.json")
r = json.load(open(SRC, encoding="utf-8"))
print("数据源：%s" % os.path.basename(SRC))

packed = {}
for k, v in r.items():
    s = v["series"]
    idx = list(range(0, len(s["date"]), 4))
    if idx[-1] != len(s["date"]) - 1:
        idx.append(len(s["date"]) - 1)
    packed[k] = {
        "name": k,
        "last": v["last"],
        "dist": v["dist"],
        "buckets": v["buckets"],
        "ic": v["ic"],
        "dwell": v["dwell"],
        "events": v["events"],
        "year": v["year_extremes"],
        "series": [[s["date"][i], s["score"][i], s["close"][i]] for i in idx],
        "start": v["start"],
        "end": v["end"],
        "n": v["n"],
        "crowd_mode": v.get("crowd_mode"),
    }

data_js = json.dumps(packed, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
json.dump(packed, open(os.path.join(BASE, "data/oos/html_data.json"), "w", encoding="utf-8"),
          ensure_ascii=False, separators=(",", ":"))

tpl = open(os.path.join(BASE, "scripts/tpl.html"), encoding="utf-8").read()
assert "/*__DATA__*/{}" in tpl, "模板占位符缺失"
html = tpl.replace("/*__DATA__*/{}", data_js)

# 版本号与构建时间：写进页面右上角角标，用于确认「当前打开的是哪一版」
assert "/*__VER__*/\"\"" in html, "版本占位符缺失"
html = html.replace("/*__VER__*/\"\"", json.dumps(VER))
assert "/*__BUILT__*/\"\"" in html, "构建时间占位符缺失"
html = html.replace("/*__BUILT__*/\"\"", json.dumps(time.strftime("%Y-%m-%d %H:%M")))


# 估值数据（可选）
vpath = os.path.join(BASE, "data/oos/valuation.json")
val_js = "null"
if os.path.exists(vpath):
    val_js = open(vpath, encoding="utf-8").read().replace("</", "<\\/")
    assert "/*__VAL__*/null" in html, "估值占位符缺失"
    html = html.replace("/*__VAL__*/null", val_js)

# A股雷达（情绪复盘）首屏数据，服务不可用时也能看到上一版
spath = os.path.join(BASE, "data/senti/sentiment.json")
sen_js = "null"
if os.path.exists(spath):
    sen_js = open(spath, encoding="utf-8").read().replace("</", "<\\/")
    assert "/*__SEN__*/null" in html, "情绪复盘占位符缺失"
    html = html.replace("/*__SEN__*/null", sen_js)

# 美股精灵（情绪 / 宽度 / 结构）首屏数据
upath = os.path.join(BASE, "data/us_senti/us_senti.json")
uss_js = "null"
if os.path.exists(upath):
    uss_js = open(upath, encoding="utf-8").read().replace("</", "<\\/")
    assert "/*__USS__*/null" in html, "美股精灵占位符缺失"
    html = html.replace("/*__USS__*/null", uss_js)

# 港股全景（情绪 / 宽度 / 沽空 / 南向）首屏数据
hpath = os.path.join(BASE, "data/hk_senti/hk_senti.json")
hk_js = "null"
if os.path.exists(hpath):
    hk_js = open(hpath, encoding="utf-8").read().replace("</", "<\\/")
    assert "/*__HK__*/null" in html, "港股全景占位符缺失"
    html = html.replace("/*__HK__*/null", hk_js)

# 亚太温度（五地指数卡 / 相对强弱）首屏数据
apath = os.path.join(BASE, "data/apac/apac.json")
ap_js = "null"
if os.path.exists(apath):
    ap_js = open(apath, encoding="utf-8").read().replace("</", "<\\/")
    assert "/*__APAC__*/null" in html, "亚太温度占位符缺失"
    html = html.replace("/*__APAC__*/null", ap_js)

# 市场总览（页〇）首屏数据：资产卡 + 自选股 + 当日判断
mpath = os.path.join(BASE, "data/market/market.json")
m_js = "null"
if os.path.exists(mpath):
    m_js = open(mpath, encoding="utf-8").read().replace("</", "<\\/")
    assert "/*__MARKET__*/null" in html, "市场总览占位符缺失"
    html = html.replace("/*__MARKET__*/null", m_js)

# 最新风向（页七）首屏数据：策展内容 + 分类统计（统计在这里算，页面只管画）
wpath = os.path.join(BASE, "data/wind/wind.json")
w_js = "null"
w_kb = ""
if os.path.exists(wpath):
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import wind as _wind
    _wd = _wind.build()
    w_js = json.dumps(_wd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    w_kb = "%.1f" % (len(w_js) / 1024)
    assert "/*__WIND__*/null" in html, "最新风向占位符缺失"
    html = html.replace("/*__WIND__*/null", w_js)

# 市场新闻（页八）首屏数据：头条 + 六个分类流
npath = os.path.join(BASE, "data/news/news.json")
n_js = "null"
n_kb = ""
if os.path.exists(npath):
    import sys as _sys2
    _sys2.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import news as _news
    _nd = _news.build()
    n_js = json.dumps(_nd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    n_kb = "%.1f" % (len(n_js) / 1024)
    assert "/*__NEWS__*/null" in html, "市场新闻占位符缺失"
    html = html.replace("/*__NEWS__*/null", n_js)

# 机构研报（页九）首屏数据：主题 × 机构矩阵 + 报告卡片
rpath = os.path.join(BASE, "data/research/research.json")
r_js = "null"
r_kb = ""
if os.path.exists(rpath):
    import sys as _sys3
    _sys3.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import research as _research
    _rd = _research.build()
    _err, _warn = _research.validate(_rd)
    if _err:
        raise SystemExit("研报数据校验失败：%s" % "；".join(_err[:5]))
    r_js = json.dumps(_rd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    r_kb = "%.1f" % (len(r_js) / 1024)
    assert "/*__RPT__*/null" in html, "机构研报占位符缺失"
    html = html.replace("/*__RPT__*/null", r_js)

# 财经日历（页十二）首屏数据：事件库 + 当周切片 + 统计
# 与风向 / 研报同源：读盘不抓网，事件是人查证后写进 json 的。
kpath = os.path.join(BASE, "data/calendar/calendar.json")
k_js = "null"
k_kb = ""
if os.path.exists(kpath):
    import sys as _sys5
    _sys5.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import econcal as _cal
    _kerr, _kwarn = _cal.validate(_cal.load())
    if _kerr:
        raise SystemExit("财经日历校验失败：%s" % "；".join(_kerr[:5]))
    _kd = _cal.build()
    k_js = json.dumps(_kd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    k_kb = "%.1f" % (len(k_js) / 1024)
    assert "/*__CAL__*/null" in html, "财经日历占位符缺失"
    html = html.replace("/*__CAL__*/null", k_js)

# 个人笔记（页十四）首屏数据：泳道 + 卡片
# 与日历同理是「人写的内容」，不是抓来的；区别在于它会被页面高频改写，
# 所以这里只负责首屏，进页面后立刻由 GET /api/notes 换成源文件的最新一版。
ntpath = os.path.join(BASE, "data/notes/notes.json")
nt_js = "null"
nt_kb = ""
if os.path.exists(ntpath):
    import sys as _sys6
    _sys6.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import notes as _ntm
    _nerr, _nwarn = _ntm.validate(_ntm.load())
    if _nerr:
        raise SystemExit("个人笔记校验失败：%s" % "；".join(_nerr[:5]))
    _ntd = _ntm.build()
    nt_js = json.dumps(_ntd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    nt_kb = "%.1f" % (len(nt_js) / 1024)
    assert "/*__NOTES__*/null" in html, "个人笔记占位符缺失"
    html = html.replace("/*__NOTES__*/null", nt_js)

# 大佬持仓（页十）首屏数据：SEC 13F 快照，详情只内联前 12 项
# 注意：这里读快照、不抓网络。抓取只发生在页面上的「更新持仓」按钮，
# 以及手动跑 scripts/holdings.py 的时候。
hpath = os.path.join(BASE, "data/holdings/holdings.json")
h_js = "null"
h_kb = ""
h_bad = 0
if os.path.exists(hpath):
    import sys as _sys4
    _sys4.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import holdings as _hl
    _hd = _hl.load()
    _herr, _hwarn = _hl.validate(_hd)
    for _r in _hd["insts"]:
        _r["holdings"] = (_r.get("holdings") or [])[:_hl.PAGE_TOP]
    h_bad = _hd.get("n_error", 0)
    h_js = json.dumps(_hd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    h_kb = "%.1f" % (len(h_js) / 1024)
    assert "/*__HLD__*/null" in html, "大佬持仓占位符缺失"
    html = html.replace("/*__HLD__*/null", h_js)

# 持仓地图（页十一）首屏数据：策略 / 主题 / 地区分组 + 卡片 + 构建期汇总
# 汇总在 portfolio.summary() 里算一次并随数据下发，页面初始渲染直接用；
# 前端改卡片后会自己重算，两者是同一套规则（见 portfolio 模块文档串）。
ppath = os.path.join(BASE, "data/portfolio/matrix.json")
p_js = "null"
p_kb = ""
p_sum = None
if os.path.exists(ppath):
    import sys as _sys5
    _sys5.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import portfolio as _pf
    _pd = _pf.build()
    p_sum = _pd["sum"]
    p_js = json.dumps(_pd, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    p_kb = "%.1f" % (len(p_js) / 1024)
    assert "/*__PMX__*/null" in html, "持仓地图占位符缺失"
    html = html.replace("/*__PMX__*/null", p_js)

dst = os.path.join(OUT, "指数超买超卖看板.html")
open(dst, "w", encoding="utf-8").write(html)

# 语法校验用的 JS 片段放 .tmpchk/（已在 .gitignore），不写进 output/：
# output/ 是服务端直接对外提供的目录，交付物旁边不该躺着一个可被请求的 .js
script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
chk = os.path.join(BASE, ".tmpchk")
os.makedirs(chk, exist_ok=True)
open(os.path.join(chk, "check.js"), "w", encoding="utf-8").write(script)
print("written %s  v%s  %.1f KB  | data %.1f | val %.1f | senti %.1f | us %.1f | hk %.1f | apac %.1f | mkt %.1f | wind %s KB | news %s KB | notes %s KB"
      % (dst, VER, os.path.getsize(dst) / 1024, len(data_js) / 1024, len(val_js) / 1024,
         len(sen_js) / 1024, len(uss_js) / 1024, len(hk_js) / 1024, len(ap_js) / 1024,
         len(m_js) / 1024, w_kb or "-", n_kb or "-", nt_kb or "-"))
print("research %s KB, %s 篇" % (r_kb or "-", len(_rd["reports"]) if r_js != "null" else "0"))
print("calendar %s KB, %s 条（窗口 %s ~ %s）" % (k_kb or "-", len(_kd["events"]) if k_js != "null" else "0", (_kd.get("range") or ["?","?"])[0] if k_js != "null" else "-", (_kd.get("range") or ["?","?"])[1] if k_js != "null" else "-"))
print("holdings %s KB, %s 家（失败 %d）" % (h_kb or "-", len(_hd["insts"]) if h_js != "null" else "0", h_bad))
if p_sum:
    print("portfolio %s KB, %d 张卡片，合计 US$%s（%d 张计入 · 观察 %d · 期权 %d · 不可计算 %d）"
          % (p_kb, len(_pd["items"]), _pf.usd(p_sum["total"]), p_sum["counted"],
             p_sum["watch"], p_sum["option"], p_sum["unparsed"]))
else:
    print("portfolio -（没有 data/portfolio/matrix.json）")
print("points %s" % {k: len(v["series"]) for k, v in packed.items()})
