# -*- coding: utf-8 -*-
"""个人笔记：看板式笔记的装载、校验、汇总与写回。

数据文件 data/notes/notes.json 是用户自己写的看板：

    columns [{id, title, cards[{id, title, body, created, updated}]}]

**为什么是「列 + 卡片」而不是一张大表。** 笔记的内容是发散的，但用途是收敛的：
同一件事会从「想法」走到「待研究」再走到「已处理」，中间还会回头补材料。
平铺的清单每次都要重读一遍才知道某条走到哪一步了，这正是看板擅长的事。
列就是泳道，列名由用户自己定（交易日志 / 待研究 / 已处理 只是本期初始化的一种切法）。

**正文是纯文本，不做 Markdown 渲染。** 理由是三条：笔记的第一要求是「写得下来」，
不是「排得好看」；渲染意味着解析器要跟 HTML 转义、链接注入、粘贴富文本搏斗；
而且一旦渲染，用户复制回去就带着一堆标记。要排版时导出 Markdown 即可。

**这份文件是权威副本。** 页面上的编辑经 POST /api/notes 写回它
（`save()`：先校验、再备份、最后原子替换），浏览器里那份只是写盘失败时的草稿兜底。
笔记是人一个字一个字写进去的，丢了重跑任何脚本都回不来。
"""
import datetime
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_FILE = os.path.join(ROOT, "data", "notes", "notes.json")
BACKUP_DIR = os.path.join(ROOT, "data", "notes", "backups")
BACKUP_KEEP = 40            # 一份约 20 KB，留 40 份够回溯一个月

SCHEMA_VERSION = 1          # 加字段不必升；改字段含义必须升，并在 load() 里做迁移

TITLE_MAX = 60              # 卡片标题在列里只显示一行，再长就被截断
COL_TITLE_MAX = 16          # 列头要横着排下多条泳道，名字太长会把别的列挤走


def load(path=NOTES_FILE):
    if not os.path.exists(path):
        return {"schemaVersion": SCHEMA_VERSION, "updated": "", "columns": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(d):
    """返回 (errors, warnings)。

    分成两档是有意的：**错误**让它写不进去（结构坏了，写进去只会更难修）；
    **提示**只提醒不改数据——空卡片、空列名这类状态往往是用户正在编辑的中间态，
    拦住它等于在用户打字打到一半时把笔抢走。
    """
    err, warn = [], []
    if not isinstance(d, dict):
        return ["顶层不是对象"], warn

    cols = d.get("columns")
    if not isinstance(cols, list):
        return ["columns 不是数组"], warn
    if not cols:
        warn.append("一条泳道都没有，页面上会显示空看板")

    seen_col, seen_card = set(), set()
    n_cards = 0
    for i, c in enumerate(cols):
        if not isinstance(c, dict):
            err.append("第 %d 条泳道不是对象" % (i + 1))
            continue
        cid = str(c.get("id") or "").strip()
        tag = cid or ("第 %d 条泳道" % (i + 1))
        if not cid:
            err.append("%s 缺 id" % tag)
        elif cid in seen_col:
            err.append("泳道 id 重复：%s" % cid)
        else:
            seen_col.add(cid)

        title = str(c.get("title") or "")
        if not title.strip():
            warn.append("%s 还没有名字" % tag)
        elif len(title) > COL_TITLE_MAX:
            warn.append("%s 的名字 %d 字，列头放不下，会被截断" % (tag, len(title)))

        cards = c.get("cards")
        if cards is None:
            continue
        if not isinstance(cards, list):
            err.append("%s 的 cards 不是数组" % tag)
            continue
        for j, k in enumerate(cards):
            n_cards += 1
            if not isinstance(k, dict):
                err.append("%s 第 %d 张卡片不是对象" % (tag, j + 1))
                continue
            kid = str(k.get("id") or "").strip()
            ktag = kid or ("%s 第 %d 张卡片" % (tag, j + 1))
            if not kid:
                err.append("%s 缺 id" % ktag)
            elif kid in seen_card:
                err.append("卡片 id 重复：%s" % kid)
            else:
                seen_card.add(kid)

            kt = str(k.get("title") or "")
            kb = str(k.get("body") or "")
            if not kt.strip() and not kb.strip():
                warn.append("%s 标题和正文都是空的" % ktag)
            elif len(kt) > TITLE_MAX:
                warn.append("%s 标题 %d 字，列里只显示一行" % (ktag, len(kt)))

    if n_cards and not seen_card:
        err.append("有卡片但一个合法 id 都没有")
    return err, warn


def build(path=NOTES_FILE):
    """给接口与页面用的现成结构：原始内容 + 统计。

    统计放在后端算，是为了让「共 N 张卡片」在服务不可用时仍能由构建期数据给出，
    且口径只有一处——前端各算一遍迟早对不上。
    """
    d = load(path)
    err, warn = validate(d)
    cols = d.get("columns") or []
    by_col = []
    n_cards = n_chars = 0
    for c in cols:
        cards = c.get("cards") or []
        n = len(cards)
        chars = sum(len(str(k.get("body") or "")) for k in cards)
        n_cards += n
        n_chars += chars
        by_col.append({"id": c.get("id"), "title": c.get("title") or "",
                       "n": n, "chars": chars})
    return {
        "schemaVersion": d.get("schemaVersion") or SCHEMA_VERSION,
        "updated": d.get("updated") or "",
        "columns": cols,
        "stats": {"columns": len(cols), "cards": n_cards, "chars": n_chars,
                  "by_column": by_col},
        "errors": err,
        "warnings": warn,
    }


def save(d):
    """整份看板写回 notes.json：先校验，再备份，最后原子替换。

    校验不过一律拒写——宁可返回错误让用户看见，也不要把半份数据落盘。
    「半份」在这里的具体形态是：id 重复导致页面渲染出两张一样的卡片，
    用户删掉一张、其实删的是另一张。
    """
    if not isinstance(d, dict):
        raise ValueError("请求体里没有笔记对象")
    err, _ = validate(d)
    if err:
        raise ValueError("数据校验失败：%s" % "；".join(err[:3]))

    out = dict(d)
    out.setdefault("schemaVersion", SCHEMA_VERSION)
    out["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 固定键序：diff 才不会因为键序抖动而满屏变红
    payload = {"schemaVersion": out["schemaVersion"], "updated": out["updated"],
               "columns": out.get("columns") or []}
    for k in out:                      # 未知字段原样保留，将来迁移才有依据
        if k not in payload:
            payload[k] = out[k]

    os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
    backup = ""
    if os.path.exists(NOTES_FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "notes-%s.json" % stamp)
        n = 1
        while os.path.exists(backup):   # 同一秒内连写两次会撞名
            backup = os.path.join(BACKUP_DIR, "notes-%s-%d.json" % (stamp, n))
            n += 1
        with open(NOTES_FILE, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        _prune_backups()

    tmp = NOTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, NOTES_FILE)        # 原子替换：中途断电不留半份文件

    return {"path": os.path.relpath(NOTES_FILE, ROOT).replace("\\", "/"),
            "bytes": os.path.getsize(NOTES_FILE),
            "backup": os.path.basename(backup) if backup else "",
            "as_of": payload["updated"]}


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


if __name__ == "__main__":
    b = build()
    s = b["stats"]
    print("notes: %d 条泳道 / %d 张卡片 / %d 字" % (s["columns"], s["cards"], s["chars"]))
    for c in s["by_column"]:
        print("  %-18s %2d 张" % (c["title"] or "(未命名)", c["n"]))
    print("  %d 错误 / %d 提示" % (len(b["errors"]), len(b["warnings"])))
    for e in b["errors"]:
        print("  E", e)
    for w in b["warnings"]:
        print("  W", w)
