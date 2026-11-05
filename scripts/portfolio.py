# -*- coding: utf-8 -*-
"""持仓地图：数据装载、金额解析与汇总口径。

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

SCHEMA_VERSION = 1     # 结构版本。加字段不必升；改字段含义必须升，并在 load 里做迁移
STATUS = ("held", "watch", "add", "option")
STATUS_CN = {"held": "已持仓", "watch": "观察中", "add": "计划加仓", "option": "期权"}
COUNTED = ("held", "add")           # 只有这两类参与普通合计

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


def load():
    if not os.path.exists(PMX_FILE):
        return None
    with open(PMX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _cell_key(col, reg):
    return col + "\x1f" + reg


def summary(d):
    """按上面的口径汇总。返回的分值单位是「分」。"""
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


def build():
    """装载 + 校验 + 汇总。页面只负责画，数字一律在这里算一次。"""
    d = load()
    if d is None:
        return None
    errors, warnings = validate(d)
    if errors:
        raise SystemExit("持仓地图数据校验失败：%s" % "；".join(errors[:5]))
    for w in warnings:
        print("  [持仓地图] 提示：%s" % w)
    return {
        "as_of": _mtime(),
        "schemaVersion": d.get("schemaVersion", SCHEMA_VERSION),
        "groups": d["groups"],
        "columns": d["columns"],
        "regions": d["regions"],
        "items": d["items"],
        "sum": summary(d),
    }


BACKUP_DIR = os.path.join(ROOT, "data", "portfolio", "backups")
BACKUP_KEEP = 30              # 滚动保留最近 30 份；一份 13 KB，够回溯一个月


def save(d):
    """把整份地图写回 matrix.json：先校验，再备份，最后原子替换。

    为什么必须有这一步：页面上的编辑原先只落在浏览器 localStorage，
    清缓存、换浏览器、换一台机器就没了。数据是用户手填的，不能只活在浏览器里。

    校验不过一律拒写——宁可返回错误让用户看见，也不要把半份数据落盘。
    写之前把旧文件复制到 backups/ 并带时间戳，写坏了还能捞回来。
    """
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

    os.makedirs(os.path.dirname(PMX_FILE), exist_ok=True)
    backup = ""
    if os.path.exists(PMX_FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "matrix-%s.json" % stamp)
        # 同一秒内连写两次会撞名，加序号
        n = 1
        while os.path.exists(backup):
            backup = os.path.join(BACKUP_DIR, "matrix-%s-%d.json" % (stamp, n))
            n += 1
        with open(PMX_FILE, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        _prune_backups()

    tmp = PMX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, PMX_FILE)          # 原子替换：中途断电也不会留下半份文件

    return {"path": os.path.relpath(PMX_FILE, ROOT).replace("\\", "/"),
            "bytes": os.path.getsize(PMX_FILE),
            "backup": os.path.basename(backup) if backup else "",
            "as_of": _mtime()}


def _prune_backups():
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR) if n.endswith(".json"))
    except OSError:
        return
    for n in names[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, n))
        except OSError:
            pass



def _mtime():
    import datetime
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(PMX_FILE)).strftime("%Y-%m-%d %H:%M")
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


if __name__ == "__main__":
    b = build()
    if not b:
        print("没有 data/portfolio/matrix.json")
        raise SystemExit(1)
    s = b["sum"]
    print("持仓地图：%d 张卡片" % len(b["items"]))
    print("  合计 US$%s（%d 张计入：已持仓 %d + 计划加仓 %d）"
          % (usd(s["total"]), s["counted"], s["held"], s["add"]))
    print("  观察 %d · 期权 %d · 备注不可计算 %d" % (s["watch"], s["option"], s["unparsed"]))
    for g in b["groups"]:
        print("  策略 %-6s %s" % (g["name"], usd(s["g"].get(g["id"]))))
    for r in b["regions"]:
        print("  地区 %-8s %s" % (r["name"], usd(s["r"].get(r["id"]))))
