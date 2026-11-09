# -*- coding: utf-8 -*-
"""持仓地图：数据装载、金额解析与汇总口径。

两个仓，两份文件，两套币种（v1.37.0 起）：

    data/portfolio/matrix.json   卫星仓 · 美元计（1k = 1,000 美元）· 单层：卡片＝个股
    data/portfolio/core.json     核心仓 · 人民币计（1k = 1,000 元）· 两层：基金 → 资产

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
from decimal import Decimal, InvalidOperation, ROUND_DOWN

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


# ==========================================================================
# 核心仓（v1.38.0 起）：基金 → 资产 两层
# ==========================================================================
# 卫星仓记的是个股，一只票就是一行，单层就够。基金不是这样：一只「多策略基金」
# 里装着红利 40%、商品 50%、黄金 10%，国家与风格落在**资产**这一层，不落在基金上。
# 所以核心仓的结构是 基金（名称 + 金额）→ 资产（名称 + 国家 + 风格 + 权重）。
#
# 统计一律按资产层：基金金额按各资产权重切开，再按国家 / 风格 / 资产名聚合。
# 权重不要求凑满 100：填 40/50/10 和填 4/5/1 是同一件事，按总和归一化。
# 一个都不填就等分——页面会把「等分」标出来，不让人误以为那是真实比例。

CORE_SCHEMA = 2

STYLES = [
    {"id": "attack",  "name": "进攻", "color": "#70d5a2"},
    {"id": "balance", "name": "均衡", "color": "#90d9f5"},
    {"id": "defense", "name": "防御", "color": "#ee8d8c"},
]
STYLE_IDS = tuple(s["id"] for s in STYLES)
STYLE_CN = {s["id"]: s["name"] for s in STYLES}

# 资产的国家下拉。比卫星仓那四行细一档：「海外」在基金里太笼统，
# 拆成美国 / 日本 / 欧洲 / 其他，才看得出钱到底压在哪个市场。
CORE_REGIONS = [
    {"id": "china", "name": "中国"},
    {"id": "hk",    "name": "中国香港"},
    {"id": "us",    "name": "美国"},
    {"id": "jp",    "name": "日本"},
    {"id": "eu",    "name": "欧洲"},
    {"id": "other", "name": "其他"},
]
CORE_REGION_IDS = tuple(r["id"] for r in CORE_REGIONS)
CORE_REGION_CN = {r["id"]: r["name"] for r in CORE_REGIONS}


def load_core():
    """读核心仓。旧版（schemaVersion 1，矩阵结构）就地迁移成「基金 → 资产」，
    不写盘——写盘要等用户真的改了一次，否则一打开页面就动文件太唐突。"""
    d = load(CORE_FILE)
    if d is None:
        return None
    if "funds" in d:
        return d
    return {"schemaVersion": CORE_SCHEMA, "currency": "CNY",
            "styles": d.get("styles") or STYLES,
            "regions": CORE_REGIONS,
            "funds": _migrate_v1_core(d)}


def _migrate_v1_core(d):
    """v1 的卡片 → 单资产基金。旧卡片本来就没有基金这层，一张卡就是一只基金，
    资产名取卡片名，风格取它原来所在的主题所属策略，国家沿用原来的地区 id。"""
    col_group = {c["id"]: c.get("groupId") for c in d.get("columns") or []}
    alias = {"global": "other", "asia": "other", "eu": "eu"}
    out = []
    for it in d.get("items") or []:
        reg = alias.get(it.get("regionId"), it.get("regionId"))
        if reg not in CORE_REGION_IDS:
            reg = "other"
        st = col_group.get(it.get("columnId"))
        if st not in STYLE_IDS:
            st = "balance"
        out.append({
            "id": "f_" + str(it.get("id") or ""),
            "name": it.get("name") or it.get("symbol") or "未命名基金",
            "amount": it.get("amount") or "",
            "note": "",
            "assets": [{"id": "a_" + str(it.get("id") or ""), "name": "本基金",
                        "regionId": reg, "style": st, "weight": 100}],
        })
    return out


def validate_core(d):
    """核心仓校验。返回 (errors, warnings)，与卫星仓同一套约定：
    错误阻断构建，提示只打印——脚本不替用户改数字。"""
    errors, warnings = [], []
    if not d:
        return ["核心仓数据缺失"], []
    if not isinstance(d.get("funds"), list):
        return ["缺少 funds 数组"], []

    style_ids = {s["id"] for s in d.get("styles") or STYLES}
    reg_ids = {r["id"] for r in d.get("regions") or CORE_REGIONS}

    seen_fund, seen_asset = set(), set()
    for f in d["funds"]:
        fid = f.get("id")
        if not fid:
            errors.append("有一只基金缺 id")
        elif fid in seen_fund:
            errors.append("基金 id 重复：%s" % fid)
        seen_fund.add(fid)
        if not str(f.get("name") or "").strip():
            errors.append("基金 %s 没有名字" % (fid or "?"))
        if not isinstance(f.get("amount"), str):
            errors.append("基金「%s」的金额必须是字符串（%r）" % (f.get("name"), f.get("amount")))
        assets = f.get("assets")
        if not isinstance(assets, list) or not assets:
            errors.append("基金「%s」下面一个资产都没有：金额没法摊到国家与风格上"
                          % f.get("name"))
            continue
        ws = []
        for a in assets:
            aid = a.get("id")
            if not aid:
                errors.append("基金「%s」里有资产缺 id" % f.get("name"))
            elif aid in seen_asset:
                errors.append("资产 id 重复：%s" % aid)
            seen_asset.add(aid)
            if not str(a.get("name") or "").strip():
                errors.append("基金「%s」里有资产没有名字" % f.get("name"))
            if a.get("regionId") not in reg_ids:
                errors.append("资产「%s」的国家 %s 不存在" % (a.get("name"), a.get("regionId")))
            if a.get("style") not in style_ids:
                errors.append("资产「%s」的风格 %s 不在 进攻/均衡/防御 之内"
                              % (a.get("name"), a.get("style")))
            w = a.get("weight")
            if w in (None, ""):
                continue
            if not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0:
                errors.append("资产「%s」的权重必须是 ≥0 的数字（%r）" % (a.get("name"), w))
                continue
            ws.append(float(w))
        if ws and 0 < sum(ws) and abs(sum(ws) - 100) > 0.01:
            warnings.append("基金「%s」的权重合计 %.1f%%，按比例归一化，不要求凑满 100"
                            % (f.get("name"), sum(ws)))
        if assets and not ws:
            warnings.append("基金「%s」的权重一个都没填，按资产个数等分" % f.get("name"))
    return errors, warnings


def _split(cents, weights):
    """基金金额 → 每个资产分到的金额（分）。整数分不留零头：
    逐项向下取整，余数补到金额最大的那一份，保证 Σ 恰好等于基金金额。"""
    n = len(weights)
    if not n:
        return []
    sw = sum(weights)
    if sw <= 0:
        base = cents // n
        out = [base] * n
        out[0] += cents - base * n
        return out
    out, acc = [], 0
    for w in weights:
        v = int((Decimal(cents) * Decimal(str(w)) / Decimal(str(sw))
                 ).to_integral_value(ROUND_DOWN))
        out.append(v)
        acc += v
    out[out.index(max(out))] += cents - acc
    return out


def core_summary(d):
    """按**资产层**汇总。字段刻意与卫星仓的 summary() 对齐（total/counted/r/c），
    这样总仓那条合并逻辑不用为两个仓写两套代码。

        r  国家 → 金额          s  风格 → 金额
        c  资产名 → 金额         funds  每只基金摊开后的明细
    """
    if not d:
        return None
    style_ids = {s["id"] for s in d.get("styles") or STYLES}
    reg_ids = {r["id"] for r in d.get("regions") or CORE_REGIONS}

    t = {"total": 0, "counted": 0, "assets": 0, "unparsed": 0, "funds": [],
         "r": {}, "s": {}, "c": {}, "cur": "CNY", "fx": "1"}
    for f in d.get("funds") or []:
        assets = f.get("assets") or []
        cents = parse_amount(f.get("amount"))
        if cents is None or not assets:
            t["unparsed"] += 1
            continue
        ws = []
        for a in assets:
            w = a.get("weight")
            ok = isinstance(w, (int, float)) and not isinstance(w, bool) and w >= 0
            ws.append(float(w) if ok else None)
        filled = [w for w in ws if w is not None]
        if not filled:
            eff = [1.0] * len(assets)          # 一个都没填 → 等分
            norm = "equal"
        elif len(filled) < len(assets):
            # 部分填写：没填的那些按剩余部分等分，不让它们变成 0
            # （0 的意思是「这只基金不配这个资产」，和「没填」不是一回事）
            rest = max(100.0 - sum(filled), 0.0)
            blank = len(assets) - len(filled)
            eff = [w if w is not None else rest / blank for w in ws]
            norm = "partial"
        else:
            eff = filled
            norm = "weight"
        sw = sum(eff)
        if sw <= 0:
            eff = [1.0] * len(assets)
        parts = _split(cents, eff)

        t["total"] += cents
        t["counted"] += 1
        t["assets"] += len(assets)
        rows = []
        for a, c in zip(assets, parts):
            rid = a.get("regionId") if a.get("regionId") in reg_ids else "other"
            st = a.get("style") if a.get("style") in style_ids else "balance"
            nm = str(a.get("name") or "").strip() or "未命名"
            t["r"][rid] = t["r"].get(rid, 0) + c
            t["s"][st] = t["s"].get(st, 0) + c
            t["c"][nm] = t["c"].get(nm, 0) + c
            rows.append({"id": a.get("id"), "name": nm, "regionId": rid, "style": st,
                         "weight": a.get("weight"), "cents": c,
                         "pct": (c / cents * 100) if cents else 0.0})
        t["funds"].append({"id": f.get("id"), "name": f.get("name") or "",
                           "amount": f.get("amount") or "", "note": f.get("note") or "",
                           "cents": cents, "assets": rows, "norm": norm,
                           "weightSum": sum(filled) if filled else None})
    return t


def build_core():
    d = load_core()
    if d is None:
        return None
    errors, warnings = validate_core(d)
    if errors:
        raise SystemExit("核心仓数据校验失败：%s" % "；".join(errors[:5]))
    for w in warnings:
        print("  [核心仓] 提示：%s" % w)
    # 排序固定：风格按 进攻→均衡→防御 的定义顺序，不按金额大小抖
    order = {s["id"]: i for i, s in enumerate(d.get("styles") or STYLES)}
    s = core_summary(d)
    s["s"] = dict(sorted(s["s"].items(), key=lambda kv: order.get(kv[0], 99)))
    return {
        "as_of": _mtime(CORE_FILE),
        "schemaVersion": d.get("schemaVersion", CORE_SCHEMA),
        "currency": "CNY", "book": "core", "name": "核心仓",
        "styles": d.get("styles") or STYLES,
        "regions": d.get("regions") or CORE_REGIONS,
        "funds": d.get("funds") or [],
        "sum": s,
    }


def save_core(d):
    """写回 core.json。校验、备份、原子替换三步与卫星仓完全一致，
    只是文件名与备份前缀换成 core。"""
    if not isinstance(d, dict):
        raise ValueError("请求体里没有核心仓对象")
    d = dict(d)
    d.setdefault("schemaVersion", CORE_SCHEMA)
    d["currency"] = "CNY"
    errors, _ = validate_core(d)
    if errors:
        raise ValueError("数据校验失败：%s" % "；".join(errors[:3]))

    payload = {k: d[k] for k in ("schemaVersion", "currency", "styles", "regions", "funds")
               if k in d}
    for k in d:
        if k not in payload:
            payload[k] = d[k]

    os.makedirs(os.path.dirname(CORE_FILE), exist_ok=True)
    backup = ""
    if os.path.exists(CORE_FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "core-%s.json" % stamp)
        n = 1
        while os.path.exists(backup):
            backup = os.path.join(BACKUP_DIR, "core-%s-%d.json" % (stamp, n))
            n += 1
        with open(CORE_FILE, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        _prune_backups("core")

    tmp = CORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, CORE_FILE)
    return {"path": os.path.relpath(CORE_FILE, ROOT).replace("\\", "/"),
            "bytes": os.path.getsize(CORE_FILE),
            "backup": os.path.basename(backup) if backup else "",
            "as_of": _mtime(CORE_FILE)}


def cat_key(name):
    """资产类别的合并规则（写死，用户口径）：名字里带「股票」就并成一项「股票」。

    「股票多头」「股票中性」「股票指增」是同一类资产的不同做法，按名字各列一行
    反而看不出股票这一类整体占多少。卫星仓整仓本来就记作「股票」，天然并进同一项。
    页面 `pmCatName()` 是同一套规则，两边必须一致，否则总仓的表格和图会对不上。
    """
    s = "" if name is None else str(name)
    return "股票" if "股票" in s else s


def total_book():
    """总仓 = 核心仓（人民币）+ 卫星仓（美元 × 7）。两边都落在人民币分上再相加。

    三个维度都合并，因为两边现在**语义是对齐的**：
      国家  ← 卫星仓的「地区」+ 核心仓资产的「国家」
      风格  ← 卫星仓的「策略」（本来就是进攻/均衡/防御）+ 核心仓资产的「风格」
      资产类别 ← 核心仓资产的「资产名」+ 卫星仓整体记为「股票」
    卫星仓全是个股，按用户口径整仓记作一类「股票」，不再拿主题去冒充资产类别。
    资产类别里凡是名字带「股票」的，一律并进「股票」（见 `cat_key`）。
    """
    sat = load(BOOKS["sat"]["file"])
    core = load_core()
    if sat is None and core is None:
        return None
    s_sat = to_cny(summary(sat, "USD")) if sat else None
    s_core = core_summary(core) if core else None

    out = {"cur": "CNY", "fx": str(FX["USD"]), "total": 0,
           "r": {}, "s": {}, "c": {}, "cmerged": [], "parts": {},
           "rname": {}, "sname": {},
           "fundN": 0, "assetN": 0, "cardN": 0}
    for key in ("core", "sat"):
        d = core if key == "core" else sat
        s = s_core if key == "core" else s_sat
        if not s:
            out["parts"][key] = None
            continue
        out["parts"][key] = {"total": s["total"], "counted": s["counted"],
                             "cur": BOOKS[key]["cur"], "name": BOOKS[key]["name"],
                             "unparsed": s.get("unparsed", 0)}
        out["total"] += s["total"]
        for rid, v in s["r"].items():
            out["r"][rid] = out["r"].get(rid, 0) + v

        if key == "core":
            out["fundN"] = s["counted"]
            out["assetN"] = s["assets"]
            for sid, v in s["s"].items():
                out["s"][sid] = out["s"].get(sid, 0) + v
            for nm, v in s["c"].items():
                k = cat_key(nm)
                if k != nm and nm not in out["cmerged"]:
                    out["cmerged"].append(nm)
                out["c"][k] = out["c"].get(k, 0) + v
            for r in (d.get("regions") or CORE_REGIONS):
                out["rname"].setdefault(r["id"], r.get("name") or r["id"])
        else:
            out["cardN"] = s["counted"]
            for gid, v in s["g"].items():
                out["s"][gid] = out["s"].get(gid, 0) + v
            # 卫星仓全是个股：整仓记作一类「股票」，不拿主题冒充资产类别
            out["c"]["股票"] = out["c"].get("股票", 0) + s["total"]
            for r in (d.get("regions") or []):
                out["rname"].setdefault(r["id"], r.get("name") or r["id"])
    for st in STYLES:
        out["sname"][st["id"]] = st["name"]
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
    `book="core"` 从 v1.38.0 起是另一套结构（基金 → 资产），转到 build_core()。
    """
    if book == "core":
        return build_core()
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

    c = build_core()
    if c:
        cs = c["sum"]
        print("核心仓：%d 只基金 · %d 项资产" % (len(c["funds"]), cs["assets"]))
        print("  合计 ¥%s（%d 只基金计入，%d 只金额不可计算）"
              % (rmb(cs["total"]), cs["counted"], cs["unparsed"]))
        for st in STYLES:
            v = cs["s"].get(st["id"], 0)
            if v:
                print("  风格 %-4s ¥%s（%.1f%%）"
                      % (st["name"], rmb(v), v / cs["total"] * 100 if cs["total"] else 0))
        for rid, v in sorted(cs["r"].items(), key=lambda kv: -kv[1]):
            print("  国家 %-8s ¥%s" % (CORE_REGION_CN.get(rid, rid), rmb(v)))
    else:
        print("核心仓：没有 data/portfolio/core.json")

    tb = total_book()
    if tb:
        print("总仓（人民币，卫星仓 × %s）：¥%s" % (tb["fx"], rmb(tb["total"])))
        for k in ("core", "sat"):
            p = tb["parts"].get(k)
            if p:
                print("  %s ¥%s" % (p["name"], rmb(p["total"])))
        for nm, v in sorted(tb["c"].items(), key=lambda kv: -kv[1]):
            print("  资产类别 %-12s ¥%s" % (nm, rmb(v)))
        if tb["cmerged"]:
            print("  ↑ 名称含「股票」已并入「股票」：%s" % " / ".join(tb["cmerged"]))
