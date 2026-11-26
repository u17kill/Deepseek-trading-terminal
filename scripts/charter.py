# -*- coding: utf-8 -*-
"""基金宪法：装载、校验、备份、原子写回。

存储位置与理由
--------------
保存文件放在 ``data/charter/fund-charter.json``，**不在构建产物里**：
``build_dashboard.py`` 每次重建都会覆盖 ``output/*.html``，如果宪法正文住在那里，
一次重建就把人写的正文还原成种子——这正是需求文档第 6.1 条点名要防的事。
``data/charter/`` 不参与构建，也不入 git（``.gitignore`` 里 ``/data/*`` 默认忽略），
所以既不会被重建覆盖，也不会被一次 ``git checkout`` 回滚。

种子只在「没有保存文件」时使用，来自 ``scripts/charter_seed.json``（入库）。
**读取失败 ≠ 没有文件**：文件存在但解析不了时抛 ``CharterError(500)``，
页面必须显示「读取失败」，不许静默退回种子——否则用户会以为自己的修改丢了，
然后在这个「种子页面」上继续编辑、再保存，把真文件覆盖掉。

写回顺序（与个人笔记同一套模子）
--------------------------------
校验 → 冲突检查（revision 不等于基版本就 409）→ 备份旧文件（滚动 30 份）→
写临时文件 → ``os.replace`` 原子替换。任一步失败都不落盘。

revision 是文档规范化 JSON 的 SHA256，不是文件字节的哈希：
改了缩进或键序不该被判成「另一个版本」，否则编辑器保存一次就冲突。
"""
import datetime
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data", "charter")
FILE = os.path.join(DATA_DIR, "fund-charter.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
SEED = os.path.join(ROOT, "scripts", "charter_seed.json")

BACKUP_KEEP = 30            # 需求文档口径：保留最近 30 份

SCHEMA_VERSION = 1
MAX_PILLARS = 50
MAX_PRINCIPLES = 500
MAX_TITLE = 200             # 页面标题
MAX_PILLAR_NAME = 60        # 支柱中文名：目录里横排，太长会挤走别的行
MAX_PILLAR_EN = 120
MAX_PTITLE = 300            # 信条标题
MAX_PARAGRAPH = 20000       # 单段正文
MAX_PARAGRAPHS = 100        # 每条信条最多 100 段 / 要点最多 100 条
MAX_FILE = 1024 * 1024      # 请求与文件上限 1 MB
RESERVED_ID = "final-principle"


class CharterError(Exception):
    """带 HTTP 状态码的业务错误。code 由接口层直接用。"""

    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _canonical(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def revision_of(doc):
    return hashlib.sha256(_canonical(doc)).hexdigest()


def _read_json(path):
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) > MAX_FILE:
        raise CharterError(413, "文件超过 1 MB 上限")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise CharterError(500, "源文件不是 UTF-8 编码")
    except json.JSONDecodeError as e:
        raise CharterError(500, "源文件不是合法 JSON（第 %d 行第 %d 列）" % (e.lineno, e.colno))


def load():
    """返回 (document, meta)。meta: source=file|seed, updatedAt, revision, path。"""
    if os.path.exists(FILE):
        doc = _read_json(FILE)
        errs = validate(doc)
        if errs:
            # 文件在但结构坏了：报错，不退回种子。见模块说明。
            raise CharterError(500, "源文件校验失败：%s" % "；".join(errs[:3]),
                               {"errors": errs})
        st = os.stat(FILE)
        return doc, {"source": "file", "revision": revision_of(doc),
                     "updatedAt": datetime.datetime.fromtimestamp(st.st_mtime)
                     .strftime("%Y-%m-%d %H:%M"), "path": relpath(FILE)}
    if not os.path.exists(SEED):
        raise CharterError(503, "初始数据缺失：scripts/charter_seed.json 不存在")
    doc = _read_json(SEED)
    errs = validate(doc)
    if errs:
        raise CharterError(500, "初始数据校验失败：%s" % "；".join(errs[:3]),
                           {"errors": errs})
    return doc, {"source": "seed", "revision": revision_of(doc),
                 "updatedAt": "", "path": relpath(SEED)}


def relpath(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def _s(v):
    return v if isinstance(v, str) else ""


def validate(doc):
    """返回错误字符串列表。空列表 = 可保存。"""
    errs = []
    if not isinstance(doc, dict):
        return ["顶层不是对象"]
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        errs.append("schemaVersion 必须为 %d" % SCHEMA_VERSION)

    title = doc.get("title")
    if not isinstance(title, str):
        errs.append("title 必须是字符串（留空表示自动标题）")
    elif len(title) > MAX_TITLE:
        errs.append("页面标题 %d 字，超过 %d" % (len(title), MAX_TITLE))
    for k, label, cap in (("subtitle", "副标题", MAX_TITLE),
                          ("manifesto", "总纲", MAX_PARAGRAPH),
                          ("finalTitle", "最终准则标题", MAX_TITLE)):
        v = doc.get(k)
        if not isinstance(v, str):
            errs.append("%s 必须是字符串" % label)
        elif len(v) > cap:
            errs.append("%s %d 字，超过 %d" % (label, len(v), cap))

    fp = doc.get("finalParagraphs")
    if not isinstance(fp, list) or not all(isinstance(x, str) for x in fp):
        errs.append("finalParagraphs 必须是字符串数组")
    else:
        if len(fp) > MAX_PARAGRAPHS:
            errs.append("最终准则最多 %d 段" % MAX_PARAGRAPHS)
        for i, p in enumerate(fp):
            if len(p) > MAX_PARAGRAPH:
                errs.append("最终准则第 %d 段超过 %d 字" % (i + 1, MAX_PARAGRAPH))

    pillars = doc.get("pillars")
    if not isinstance(pillars, list):
        return errs + ["pillars 不是数组"]
    if len(pillars) > MAX_PILLARS:
        errs.append("支柱 %d 个，超过 %d" % (len(pillars), MAX_PILLARS))

    seen, n_pr = set(), 0
    for i, p in enumerate(pillars):
        tag = "第 %d 个支柱" % (i + 1)
        if not isinstance(p, dict):
            errs.append("%s 不是对象" % tag)
            continue
        pid = _s(p.get("id")).strip()
        if not pid:
            errs.append("%s 缺 id" % tag)
        elif pid == RESERVED_ID or not _id_ok(pid):
            errs.append("%s 的 id「%s」不合法（只允许字母数字下划线连字符，且不能用 %s）"
                        % (tag, pid, RESERVED_ID))
        elif pid in seen:
            errs.append("id 重复：%s" % pid)
        else:
            seen.add(pid)

        name = _s(p.get("name")).strip()
        if not name:
            errs.append("%s 缺中文名称" % tag)
        elif len(name) > MAX_PILLAR_NAME:
            errs.append("%s 名称 %d 字，超过 %d" % (tag, len(name), MAX_PILLAR_NAME))
        if len(_s(p.get("english"))) > MAX_PILLAR_EN:
            errs.append("%s 的英文名超过 %d 字" % (tag, MAX_PILLAR_EN))

        prs = p.get("principles")
        if not isinstance(prs, list):
            errs.append("%s 的 principles 不是数组" % tag)
            continue
        for j, k in enumerate(prs):
            n_pr += 1
            ktag = "%s第 %d 条信条" % (tag, j + 1)
            if not isinstance(k, dict):
                errs.append("%s 不是对象" % ktag)
                continue
            kid = _s(k.get("id")).strip()
            if not kid:
                errs.append("%s 缺 id" % ktag)
            elif kid == RESERVED_ID or not _id_ok(kid):
                errs.append("%s 的 id「%s」不合法" % (ktag, kid))
            elif kid in seen:
                errs.append("id 重复：%s" % kid)
            else:
                seen.add(kid)

            kt = _s(k.get("title")).strip()
            if not kt:
                errs.append("%s 缺标题" % ktag)
            elif len(kt) > MAX_PTITLE:
                errs.append("%s 标题 %d 字，超过 %d" % (ktag, len(kt), MAX_PTITLE))

            paras = k.get("paragraphs")
            if not isinstance(paras, list) or not all(isinstance(x, str) for x in paras):
                errs.append("%s 的 paragraphs 必须是字符串数组" % ktag)
            else:
                if len(paras) > MAX_PARAGRAPHS:
                    errs.append("%s 正文 %d 段，超过 %d" % (ktag, len(paras), MAX_PARAGRAPHS))
                for m, t in enumerate(paras):
                    if len(t) > MAX_PARAGRAPH:
                        errs.append("%s 第 %d 段超过 %d 字" % (ktag, m + 1, MAX_PARAGRAPH))

            bul = k.get("bullets")
            if bul is not None:
                if not isinstance(bul, list) or not all(isinstance(x, str) for x in bul):
                    errs.append("%s 的 bullets 必须是字符串数组" % ktag)
                else:
                    if len(bul) > MAX_PARAGRAPHS:
                        errs.append("%s 要点 %d 条，超过 %d" % (ktag, len(bul), MAX_PARAGRAPHS))
                    for m, t in enumerate(bul):
                        if len(t) > MAX_PARAGRAPH:
                            errs.append("%s 第 %d 条要点超过 %d 字"
                                        % (ktag, m + 1, MAX_PARAGRAPH))

            for key, label in (("afterBullets", "要点后补充"), ("conclusion", "条目总结")):
                v = k.get(key)
                if v is None:
                    continue
                if not isinstance(v, str):
                    errs.append("%s 的 %s 必须是字符串" % (ktag, label))
                elif len(v) > MAX_PARAGRAPH:
                    errs.append("%s 的 %s 超过 %d 字" % (ktag, label, MAX_PARAGRAPH))

    if n_pr > MAX_PRINCIPLES:
        errs.append("信条 %d 条，超过 %d" % (n_pr, MAX_PRINCIPLES))
    return errs


def _id_ok(v):
    if not v or len(v) > 80:
        return False
    for ch in v:
        # 只认 ASCII 字母数字：str.isalnum() 对中文也返回 True，会把「生存」放进来，
        # 而中文 id 在 URL 锚点、导入导出、目录定位里都要额外汇编，没必要。
        if not ((ch.isascii() and ch.isalnum()) or ch in "_-"):
            return False
    return True


def normalize(doc):
    """按固定键序输出；未知字段原样保留（将来加字段才有依据）。"""
    out = {
        "schemaVersion": SCHEMA_VERSION,
        "title": _s(doc.get("title")),
        "subtitle": _s(doc.get("subtitle")),
        "manifesto": _s(doc.get("manifesto")),
        "pillars": [],
        "finalTitle": _s(doc.get("finalTitle")),
        "finalParagraphs": [_s(x) for x in (doc.get("finalParagraphs") or [])],
    }
    for p in doc.get("pillars") or []:
        np = {"id": p["id"], "name": _s(p.get("name")), "english": _s(p.get("english")),
              "principles": []}
        for k in p.get("principles") or []:
            np["principles"].append({
                "id": k["id"], "title": _s(k.get("title")),
                "paragraphs": [_s(x) for x in (k.get("paragraphs") or [])],
                "bullets": [_s(x) for x in (k.get("bullets") or [])],
                "afterBullets": _s(k.get("afterBullets")),
                "conclusion": _s(k.get("conclusion")),
            })
            for key in k:
                if key not in np["principles"][-1]:
                    np["principles"][-1][key] = k[key]
        for key in p:
            if key not in np:
                np[key] = p[key]
        out["pillars"].append(np)
    for key in doc:
        if key not in out:
            out[key] = doc[key]
    return out


def save(doc, base_revision):
    """带乐观锁的写回。base_revision 与当前文件不一致 → 409。"""
    errs = validate(doc)
    if errs:
        raise CharterError(400, "数据校验失败：%s" % "；".join(errs[:3]), {"errors": errs})

    cur_doc, meta = load()
    cur_rev = meta["revision"]
    if base_revision and base_revision != cur_rev:
        raise CharterError(409, "文件已被其他页面修改", {
            "document": cur_doc, "revision": cur_rev, "updatedAt": meta["updatedAt"]})

    out = normalize(doc)
    payload = json.dumps(out, ensure_ascii=False, indent=1) + "\n"

    os.makedirs(DATA_DIR, exist_ok=True)
    backup = ""
    if os.path.exists(FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "fund-charter-%s.json" % stamp)
        n = 1
        while os.path.exists(backup):      # 同一秒连存两次会撞名
            backup = os.path.join(BACKUP_DIR, "fund-charter-%s-%d.json" % (stamp, n))
            n += 1
        with open(FILE, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        _prune()

    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
    os.replace(tmp, FILE)

    return {"ok": True, "revision": revision_of(out), "updatedAt": _now(),
            "bytes": os.path.getsize(FILE), "backup": os.path.basename(backup) if backup else "",
            "path": relpath(FILE), "document": out}


def _prune():
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR) if n.endswith(".json"))
    except OSError:
        return
    for n in names[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, n))
        except OSError:
            pass


def snapshot():
    """接口与构建期共用：文档 + 版本 + 来源 + 备份数量。"""
    doc, meta = load()
    n_backup = 0
    try:
        n_backup = len([n for n in os.listdir(BACKUP_DIR) if n.endswith(".json")])
    except OSError:
        pass
    return {"document": doc, "revision": meta["revision"], "updatedAt": meta["updatedAt"],
            "source": meta["source"], "path": meta["path"], "backups": n_backup}


if __name__ == "__main__":
    try:
        s = snapshot()
    except CharterError as e:
        raise SystemExit("基金宪法：%s（HTTP %d）" % (e.message, e.code))
    d = s["document"]
    n = sum(len(p.get("principles") or []) for p in d["pillars"])
    chars = len(d.get("manifesto") or "") + sum(len(x) for x in d.get("finalParagraphs") or [])
    for p in d["pillars"]:
        for k in p.get("principles") or []:
            chars += len(k.get("title") or "") + len(k.get("conclusion") or "")
            chars += sum(len(x) for x in (k.get("paragraphs") or []))
            chars += sum(len(x) for x in (k.get("bullets") or []))
            chars += len(k.get("afterBullets") or "")
    print("基金宪法：%d 支柱 / %d 信条 / %d 字" % (len(d["pillars"]), n, chars))
    print("  来源 %s ｜ revision %s ｜ 更新 %s ｜ 备份 %d 份"
          % (s["source"], s["revision"][:12], s["updatedAt"] or "（未写入文件）", s["backups"]))
