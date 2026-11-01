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

# 短线精灵（情绪复盘）首屏数据，服务不可用时也能看到上一版
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

dst = os.path.join(OUT, "指数超买超卖看板.html")
open(dst, "w", encoding="utf-8").write(html)

# 语法校验用的 JS 片段放 .tmpchk/（已在 .gitignore），不写进 output/：
# output/ 是服务端直接对外提供的目录，交付物旁边不该躺着一个可被请求的 .js
script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
chk = os.path.join(BASE, ".tmpchk")
os.makedirs(chk, exist_ok=True)
open(os.path.join(chk, "check.js"), "w", encoding="utf-8").write(script)
print("written %s  v%s  %.1f KB  | data %.1f | val %.1f | senti %.1f | us %.1f | hk %.1f | apac %.1f KB"
      % (dst, VER, os.path.getsize(dst) / 1024, len(data_js) / 1024, len(val_js) / 1024,
         len(sen_js) / 1024, len(uss_js) / 1024, len(hk_js) / 1024, len(ap_js) / 1024))
print("points %s" % {k: len(v["series"]) for k, v in packed.items()})
