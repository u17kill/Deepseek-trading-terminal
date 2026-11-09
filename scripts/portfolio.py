# -*- coding: utf-8 -*-
"""持仓地图：数据装载、金额解析与汇总口径。

两个仓，两份文件，两套币种（v1.37.0 起）：

    data/portfolio/matrix.json   卫星仓 · 美元计（1k = 1,000 美元）
    data/portfolio/core.json     核心仓 · 人民币计（1k = 1,000 元）

**卫星仓的口径一个字都没改**：`build()` 的入参与出参、`summary()` 的字段、
`save()` 的路径与备份前缀，全都和 v1.36.0 一样。核心仓是新增的一条并行支路，
走同一个 `summary()`，只多一个 `currency` 参数决定「原币分」要不要折成人民币分。

总仓 = 核心仓（已是人民币）+ 卫星仓（美元 × 固定汇率 7.00）。汇率固定，
不取实时行情：只要汇率一动，历史合计就会跟着变，那就没法做月度对比了。

数据文件 data/portfolio/matrix.json 是用户自己维护的分组与卡片：

    groups  [{id, name, color}]           策略
    columns [{id, groupId, name}]         主题（列）
    regions [{id, name}]                  地区（行，其中 asia 是「日韩台印」合并地区）
    items   [{id, columnId, regionId, symbol, name, amount, status}]

`columnId` 允许为空字符串：那是「未归类」——主题被删掉、卡片还没重新指定的状态。
卡片本身、代码、金额、地区全都保留，只是**不进任何合计与三种拆分**，等用户给它
重新选一个主题才回到总额里。

这份文件是**本项目的权威副本**，页面上的编辑经 POST /api/portfolio 写回它
（`save()`：先校验、再备份、最后原子替换）。上游 Codex 导出的那一份只作为
一次性导入源，不在这里被反向覆盖——上游随时可能重新导出，写回去会让两边互相打架。

**口径（本期不改，任何调整都会让历史数字不可比）**

- 只统计 status 为 held（已持仓）与 add（计划加仓）的卡片；
- 且 amount 必须是完整的「数字k」形式，`1.5k` = 1,500 美元，k 固定按 1000 计；
- watch（观察中）、option（期权）一律不进普通合计；
- 纯文字、`put 115`、区间 `2.7k–5`、无 k 的裸数字，一律判定为「不可计算」，
  既不计入，也不当作 0；
- 汇总的是**用户填写的仓位备注**，不是市值、成本或盈亏；
- 同一张卡片只计一次。三种拆分（策略 / 地区 / 主题）是同一总额的不同切法，
  彼此不可相加。

金额一律用「分」存整数做累加，避免 0.1+0.2 这类浮点误差在十字相加后显形。
"""
import json
import os
import re
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PMX_FILE = os.path.join(ROOT, "data", "portfolio", "matrix.json")
CORE_FILE = os.path.join(ROOT, "data", "portfolio", "core.json")

SCHEMA_VERSION = 1     # 结构版本。加字段不必升；改字段含义必须升，并在 load 里做迁移
STATUS = ("held", "watch", "add", "option")
STATUS_CN = {"held": "已持仓", "watch": "观察中", "add": "计划加仓", "option": "期权"}
COUNTED = ("held", "add")           # 只有这两类参与普通合计

# 固定汇率：1 美元 = 7.00 人民币。不取实时价、不留配置项——
# 汇率一变，历史合计就跟着变，月度对比立刻失去意义。要调就改这里，并在 CHANGELOG 记账。
FX = {"USD": Decimal("7"), "CNY": Decimal("1")}

# 两个仓的登记表：名字、文件、币种、页面标题。新增仓只需在这里加一行。
BOOKS = {
    "sat":  {"name": "卫星仓", "file": PMX_FILE,  "cur": "USD", "api": "/api/portfolio",
             "prefix": "matrix", "local": "pmx_map_v1"},
    "core": {"name": "核心仓", "file": CORE_FILE, "cur": "CNY", "api": "/api/core",
             "prefix": "core",   "local": "pmx_core_v1"},
}

# 完整「数字k」：1k / 1.5k / 10k。前后不允许有其他字符，
# 所以 2.7k–5、put 720、115、约 3k 都会被判为不可计算。
_AMT = re.compile(r"^(\d+(?:\.\d{1,5})?)k$", re.I)


def parse_amount(raw):
    """备注 → 分值（1 分 = 0.01 美元）。不可计算时返回 None，绝不返回 0。"""
    t = str(raw or "").strip()
    m = _AMT.match(t)
    if not m:
        return None
    try:
        usd = Decimal(m.group(1)) * 1000
    except InvalidOperation:
        return None
    cents = int((usd * 100).to_integral_value())
    return cents if cents >= 0 else None


def load(path=None):
    """读一份地图。path 缺省仍是卫星仓的 matrix.json——老调用一个字都不用改。"""
    path = path or PMX_FILE
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _cell_key(col, reg):
    return col + "\x1f" + reg


def summary(d, currency="USD"):
    """按上面的口径汇总。金额是**原币分**：美元仓是美分，人民币仓是人民币分。

    折算不放进来：调用方要人民币口径就再过一次 `to_cny()`。
    这样同一个 `summary()` 既能服务卫星仓（口径与 v1.36.0 逐字一致），
    也能服务核心仓，而「有没有折过」永远写在返回值的 `cur` 字段里，不会靠猜。
    """
    if not d:
        return None
    col_group = {c["id"]: c.get("groupId") for c in d["columns"]}
    reg_ids = {r["id"] for r in d["regions"]}
    grp_ids = {g["id"] for g in d["groups"]}

    t = {
        "total": 0, "counted": 0, "watch": 0, "option": 0, "unparsed": 0,
        "held": 0, "add": 0, "orphan_n": 0, "orphan": 0,
        "g": {}, "r": {}, "c": {}, "cell": {},
        "options": [],
        "cur": currency, "fx": str(FX.get(currency, Decimal("1"))),
    }
    for it in d["items"]:
        st = it.get("status")
        if st == "option":
            t["option"] += 1
            t["options"].append({
                "symbol": it.get("symbol", ""),
                "name": it.get("name", ""),
                "amount": it.get("amount", ""),
                "regionId": it.get("regionId", ""),
            })
            continue
        if st == "watch":
            t["watch"] += 1
            continue
        if st not in COUNTED:
            t["unparsed"] += 1
            continue
        cents = parse_amount(it.get("amount"))
        # 未归类：卡片照常显示，但三种拆分和合计都不收它——否则「三种切法是同一
        # 总额的不同视角」这条不变量就断了（地区算它、主题不算它）。
        if not it.get("columnId"):
            t["orphan_n"] += 1
            t["orphan"] += cents or 0
            continue
        if cents is None:
            t["unparsed"] += 1
            continue
        gid = col_group.get(it.get("columnId"))
        if gid is None or gid not in grp_ids or it.get("regionId") not in reg_ids:
            t["unparsed"] += 1
            continue
        t["total"] += cents
        t["counted"] += 1
        t["held" if st == "held" else "add"] += 1
        t["g"][gid] = t["g"].get(gid, 0) + cents
        t["r"][it["regionId"]] = t["r"].get(it["regionId"], 0) + cents
        t["c"][it["columnId"]] = t["c"].get(it["columnId"], 0) + cents
        k = _cell_key(it["columnId"], it["regionId"])
        t["cell"][k] = t["cell"].get(k, 0) + cents
    return t


def to_cny(s):
    """把一份 summary 折成人民币分。已经是人民币的原样返回（同一个对象）。

    为什么可以整数乘：1 美分 = 0.07 元 = 7 分，7 是整数，所以 `美分 × 7`
    精确等于人民币分，不产生任何舍入。这就是选 7 而不是 7.13 的好处之一。
    """
    if not s:
        return s
    rate = FX.get(s.get("cur", "USD"), Decimal("1"))
    if rate == 1:
        return s
    k = int(rate)
    out = dict(s)
    for f in ("total", "orphan"):
        out[f] = s[f] * k
    for f in ("g", "r", "c", "cell"):
        out[f] = {key: v * k for key, v in s[f].items()}
    out["cur"] = "CNY"
    out["fx"] = "1"
    out["fxFrom"] = s.get("cur", "USD")
    out["fxRate"] = str(rate)
    return out


def total_book():
    """总仓 = 核心仓（人民币）+ 卫星仓（美元 × 7）。两边都落在人民币分上再相加。

    合并只在**策略**和**地区**两个维度上做：主题是各仓自己的语义（核心仓的
    「红利价值」和卫星仓的「AI 基建」不是一回事），硬合并只会造出假分类。
    所以总仓视图只给策略与地区两种切法，主题留给各自的仓。
    """
    sat = load(BOOKS["sat"]["file"])
    core = load(BOOKS["core"]["file"])
    if sat is None and core is None:
        return None
    s_sat = to_cny(summary(sat, "USD")) if sat else None
    s_core = to_cny(summary(core, "CNY")) if core else None

    out = {"cur": "CNY", "fx": str(FX["USD"]), "total": 0, "counted": 0,
           "held": 0, "add": 0, "watch": 0, "option": 0, "orphan_n": 0, "orphan": 0,
           "g": {}, "r": {}, "parts": {}, "gname": {}, "rname": {}}
    for key, s, d in (("core", s_core, core), ("sat", s_sat, sat)):
        if not s:
            out["parts"][key] = None
            continue
        out["parts"][key] = {
            "total": s["total"], "counted": s["counted"], "held": s["held"],
            "add": s["add"], "watch": s["watch"], "option": s["option"],
            "unparsed": s["unparsed"], "orphan_n": s["orphan_n"], "orphan": s["orphan"],
            "cur": BOOKS[key]["cur"], "name": BOOKS[key]["name"],
        }
        out["total"] += s["total"]
        for f in ("counted", "held", "add", "watch", "option", "orphan_n", "orphan"):
            out[f] += s[f]
        for gid, v in s["g"].items():
            out["g"][gid] = out["g"].get(gid, 0) + v
        for rid, v in s["r"].items():
            out["r"][rid] = out["r"].get(rid, 0) + v
        for g in (d.get("groups") or []):
            out["gname"].setdefault(g["id"], g.get("name") or g["id"])
        for r in (d.get("regions") or []):
            out["rname"].setdefault(r["id"], r.get("name") or r["id"])
    return out


def validate(d):
    """结构校验。返回 (errors, warnings)：errors 阻断构建，warnings 只打印。

    这里刻意不自动修数据。分组、卡片和金额都是用户手填的，
    脚本一旦「顺手修正」，页面显示的和用户以为的就会悄悄分叉。
    """
    errors, warnings = [], []
    if not d:
        return ["持仓地图数据缺失"], []
    for k in ("groups", "columns", "regions", "items"):
        if not isinstance(d.get(k), list):
            errors.append("缺少数组字段 %s" % k)
    if errors:
        return errors, warnings

    def _dupe(name, rows):
        seen = set()
        for r in rows:
            i = r.get("id")
            if not i:
                errors.append("%s 里有一行缺 id" % name)
            elif i in seen:
                errors.append("%s id 重复：%s" % (name, i))
            seen.add(i)

    _dupe("groups", d["groups"])
    _dupe("columns", d["columns"])
    _dupe("regions", d["regions"])
    _dupe("items", d["items"])

    grp_ids = {g["id"] for g in d["groups"]}
    reg_ids = {r["id"] for r in d["regions"]}
    col_ids = {c["id"] for c in d["columns"]}

    for c in d["columns"]:
        if c.get("groupId") not in grp_ids:
            errors.append("主题「%s」挂在不存在的策略 %s 上" % (c.get("name"), c.get("groupId")))
    if not d["columns"]:
        errors.append("一个主题都没有：矩阵没有任何列可以画")
    if not d["regions"]:
        errors.append("一个地区都没有：矩阵没有任何行可以画")

    seen_cell = {}
    orphans = 0
    for it in d["items"]:
        sym = it.get("symbol") or "（空）"
        # columnId 为空 = 未归类（主题被删掉），这是合法状态，不是错误。
        # 只有「非空但查无此列」才是脏数据。
        if it.get("columnId"):
            if it["columnId"] not in col_ids:
                errors.append("卡片 %s 的主题 %s 不存在" % (sym, it.get("columnId")))
        else:
            orphans += 1
        if it.get("regionId") not in reg_ids:
            errors.append("卡片 %s 的地区 %s 不存在" % (sym, it.get("regionId")))
        if it.get("status") not in STATUS:
            errors.append("卡片 %s 的状态 %r 不在 %s 之内" % (sym, it.get("status"), "/".join(STATUS)))
        if not isinstance(it.get("amount"), str):
            errors.append("卡片 %s 的备注必须是字符串（%r）" % (sym, it.get("amount")))
        # 同一格里的同一标的出现两张「计入」卡 → 总额会被算两次。
        # 按卡片计是既定口径，所以只告警，不阻断，也不合并。
        if it.get("status") in COUNTED and parse_amount(it.get("amount")) is not None:
            key = (it.get("columnId"), it.get("regionId"), (it.get("symbol") or "").upper())
            seen_cell[key] = seen_cell.get(key, 0) + 1

    for (c, r, s), n in seen_cell.items():
        if n > 1:
            warnings.append("同一格 %s×%s 里 %s 有 %d 张计入卡片，总额会重复计入" % (c, r, s, n))

    if orphans:
        warnings.append("有 %d 张卡片未归类：卡片与金额都还在，但不计入合计" % orphans)

    # 「日韩台印」是合并地区，任何把它当单一国家读的表述都会误导。
    if any(r["id"] == "asia" for r in d["regions"]):
        warnings.append("asia 是「日韩台印」合并地区，不构成国别统计")
    return errors, warnings


def build(book="sat"):
    """装载 + 校验 + 汇总。页面只负责画，数字一律在这里算一次。

    `book="sat"` 是默认值，走的就是 v1.36.0 那条老路：matrix.json + 美元口径。
    """
    cfg = BOOKS[book]
    d = load(cfg["file"])
    if d is None:
        return None
    errors, warnings = validate(d)
    if errors:
        raise SystemExit("%s数据校验失败：%s" % (cfg["name"], "；".join(errors[:5])))
    for w in warnings:
        print("  [%s] 提示：%s" % (cfg["name"], w))
    return {
        "as_of": _mtime(cfg["file"]),
        "schemaVersion": d.get("schemaVersion", SCHEMA_VERSION),
        "groups": d["groups"],
        "columns": d["columns"],
        "regions": d["regions"],
        "items": d["items"],
        "currency": cfg["cur"],
        "book": book,
        "name": cfg["name"],
        "sum": summary(d, cfg["cur"]),
    }


BACKUP_DIR = os.path.join(ROOT, "data", "portfolio", "backups")
BACKUP_KEEP = 30              # 滚动保留最近 30 份；一份 13 KB，够回溯一个月


def save(d, book="sat"):
    """把整份地图写回源文件：先校验，再备份，最后原子替换。

    `book` 决定写哪一份：缺省的 "sat" 就是 matrix.json，路径、备份前缀、
    返回字段都与 v1.36.0 一致；核心仓走 "core"，只换文件名与前缀。

    为什么必须有这一步：页面上的编辑原先只落在浏览器 localStorage，
    清缓存、换浏览器、换一台机器就没了。数据是用户手填的，不能只活在浏览器里。

    校验不过一律拒写——宁可返回错误让用户看见，也不要把半份数据落盘。
    写之前把旧文件复制到 backups/ 并带时间戳，写坏了还能捞回来。
    """
    cfg = BOOKS[book]
    path = cfg["file"]
    if not isinstance(d, dict):
        raise ValueError("请求体里没有地图对象")
    errors, _ = validate(d)
    if errors:
        raise ValueError("数据校验失败：%s" % "；".join(errors[:3]))

    d = dict(d)
    d.setdefault("schemaVersion", SCHEMA_VERSION)
    # 固定字段顺序，diff 才不会因为键序抖动而满屏变红
    payload = {k: d[k] for k in ("schemaVersion", "groups", "columns", "regions", "items") if k in d}
    for k in d:                       # 未知字段原样保留，将来的迁移才有依据
        if k not in payload:
            payload[k] = d[k]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = ""
    if os.path.exists(path):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "%s-%s.json" % (cfg["prefix"], stamp))
        # 同一秒内连写两次会撞名，加序号
        n = 1
        while os.path.exists(backup):
            backup = os.path.join(BACKUP_DIR, "%s-%s-%d.json" % (cfg["prefix"], stamp, n))
            n += 1
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        _prune_backups(cfg["prefix"])

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)              # 原子替换：中途断电也不会留下半份文件

    return {"path": os.path.relpath(path, ROOT).replace("\\", "/"),
            "bytes": os.path.getsize(path),
            "backup": os.path.basename(backup) if backup else "",
            "as_of": _mtime(path)}


def _prune_backups(prefix):
    """按前缀分别滚动：核心仓写 30 次不该把卫星仓的 30 份挤没。"""
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR)
                       if n.startswith(prefix + "-") and n.endswith(".json"))
    except OSError:
        return
    for n in names[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, n))
        except OSError:
            pass



def _mtime(path=None):
    import datetime
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path or PMX_FILE)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def usd(cents):
    """分值 → 展示串（与页面同一套规则：≥1000 用 k）。"""
    if cents is None:
        return "—"
    v = cents / 100.0
    if abs(v) >= 1000:
        return ("%.1fk" % (v / 1000)).replace(".0k", "k")
    return "%.0f" % v


def rmb(cents):
    """人民币分值 → 展示串。中文读数用「万」，不用 k：¥215.2万 比 ¥2,152.2k 好读。"""
    if cents is None:
        return "—"
    v = cents / 100.0
    if abs(v) >= 10000:
        s = "%.1f" % (v / 10000.0)
        return (s[:-2] if s.endswith(".0") else s) + "万"
    return "%.0f" % v


if __name__ == "__main__":
    b = build()
    if not b:
        print("没有 data/portfolio/matrix.json")
        raise SystemExit(1)
    s = b["sum"]
    print("卫星仓：%d 张卡片" % len(b["items"]))
    print("  合计 US$%s（%d 张计入：已持仓 %d + 计划加仓 %d）"
          % (usd(s["total"]), s["counted"], s["held"], s["add"]))
    print("  观察 %d · 期权 %d · 备注不可计算 %d" % (s["watch"], s["option"], s["unparsed"]))
    for g in b["groups"]:
        print("  策略 %-6s %s" % (g["name"], usd(s["g"].get(g["id"]))))
    for r in b["regions"]:
        print("  地区 %-8s %s" % (r["name"], usd(s["r"].get(r["id"]))))

    c = build("core")
    if c:
        cs = c["sum"]
        print("核心仓：%d 张卡片" % len(c["items"]))
        print("  合计 ¥%s（%d 张计入）" % (rmb(cs["total"]), cs["counted"]))
    else:
        print("核心仓：没有 data/portfolio/core.json")

    tb = total_book()
    if tb:
        print("总仓（人民币，卫星仓 × %s）：¥%s" % (tb["fx"], rmb(tb["total"])))
        for k in ("core", "sat"):
            p = tb["parts"].get(k)
            if p:
                print("  %s %d 张，折算后 ¥%s" % (p["name"], p["counted"], rmb(p["total"])))
