# -*- coding: utf-8 -*-
"""市场新闻：抓取原始池、合并策展稿、校验与统计。

两段式，分工是刻意的：

  抓（本模块 fetch）—— 只搬运。拉 RSS / 快讯接口，落 data/news/raw.json，
      去重聚簇，给一个初始分桶建议。不做判断，也不写「为什么重要」。
  编（人写 issue*.json）—— 定头条、纠分桶、写一句为什么。程序造不出这句话，
      这与 wind 模块「判断不由程序拼装」是同一条红线。

页面数据来自 merge_issues() 合并后的 news.json，raw.json 只作为编辑时的原料。
"""
import json
import os
import re
import sys
import html
import datetime
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_DIR = os.path.join(ROOT, "data", "news")
RAW_FILE = os.path.join(NEWS_DIR, "raw.json")
NEWS_FILE = os.path.join(NEWS_DIR, "news.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# 分类桶：place 用这些 id。top 不是桶，是选品层，单独存在。
BUCKETS = [
    {"id": "macro", "name": "宏观政策", "desc": "利率 · 财政 · 数据",
     "rule": "只收央行、财政部、统计局口径"},
    {"id": "geo", "name": "地缘与贸易", "desc": "关税 · 冲突 · 外交",
     "rule": "只收两国以上的互动"},
    {"id": "us", "name": "美国国内", "desc": "监管 · 劳工 · 司法",
     "rule": "政策之外、但会改行业成本的事"},
    {"id": "cn", "name": "中国国内", "desc": "产业 · 地产 · 就业",
     "rule": "同上，政策与跨国摩擦之外"},
    {"id": "ai", "name": "AI 与科技", "desc": "按主体分，不看话题",
     "rule": "主体是科技公司才算，哪怕它在讲别的"},
    {"id": "industry", "name": "传统产业", "desc": "医药 · 消费 · 汽车",
     "rule": "主体是传统公司，哪怕内容在讲 AI"},
]
BUCKET_IDS = [b["id"] for b in BUCKETS]
IMPACTS = ("股", "债", "汇", "商品", "加密")

# 源清单。ok 与否由 fetch 时实测决定，坏源不要靠猜——WSJ 的 RSS 早就僵了，
# 返回 HTTP 200 但内容停在 2025-01，比 404 更危险，所以这里逐源校验新鲜度。
SOURCES = [
    {"id": "cnbc_top", "name": "CNBC", "lang": "en", "kind": "rss",
     "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
    {"id": "cnbc_econ", "name": "CNBC", "lang": "en", "kind": "rss",
     "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html"},
    {"id": "cnbc_tech", "name": "CNBC", "lang": "en", "kind": "rss",
     "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html"},
    {"id": "bbc_biz", "name": "BBC", "lang": "en", "kind": "rss",
     "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"id": "bbc_world", "name": "BBC", "lang": "en", "kind": "rss",
     "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"id": "bbbc_tech", "name": "BBC", "lang": "en", "kind": "rss",
     "url": "https://feeds.bbci.co.uk/news/technology/rss.xml"},
    {"id": "mw_top", "name": "MarketWatch", "lang": "en", "kind": "rss",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"id": "mw_bul", "name": "MarketWatch", "lang": "en", "kind": "rss",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_bulletins"},
    {"id": "wscn_global", "name": "华尔街见闻", "lang": "zh", "kind": "wscn",
     "url": "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=40"},
    {"id": "wscn_a", "name": "华尔街见闻", "lang": "zh", "kind": "wscn",
     "url": "https://api-one.wallstcn.com/apiv1/content/lives?channel=a-stock-channel&limit=40"},
    {"id": "wscn_us", "name": "华尔街见闻", "lang": "zh", "kind": "wscn",
     "url": "https://api-one.wallstcn.com/apiv1/content/lives?channel=us-stock-channel&limit=30"},
]

# 初始分桶建议：只给线索，最终由人改。
# 顺序即优先级——先命中先落桶，避免一条新闻被四个关键词同时抢走。
HINTS = [
    ("geo", ("tariff", "关税", "sanction", "制裁", "war", "ceasefire", "停火",
             "iran", "伊朗", "ukraine", "乌克兰", "israel", "以色列", "nato",
             "trade deal", "贸易协议", "export control", "出口管制", "summit", "峰会")),
    ("macro", ("fed", "fomc", "rate cut", "rate hike", "加息", "降息", "inflation",
               "cpi", "pce", "jobs report", "nonfarm", "非农", "treasury yield",
               "国债收益率", "central bank", "央行", "ecb", "boj", "budget", "财政",
               "gdp", "pmi", "unemployment", "就业")),
    ("ai", ("nvidia", "openai", "anthropic", "deepseek", "英伟达", "算力", "chip",
            "芯片", "semiconductor", "半导体", "data center", "数据中心", "model",
            "大模型", "ai ", "artificial intelligence", "人工智能", "tsmc", "台积电")),
    ("cn", ("china", "中国", "beijing", "北京", "pboc", "a股", "a-share", "沪深",
            "创业板", "港股", "恒生", "yuan", "人民币", "property", "地产",
            "stimulus", "刺激", "ndrc", "发改委")),
    ("us", ("u.s.", "美国", "wall street", "sec ", "doj", "antitrust", "反垄断",
            "congress", "国会", "shutdown", "停摆", "strike", "罢工", "election", "选举")),
]


def _now():
    return datetime.datetime.now(TZ)


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _clean(s):
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _iso(dt):
    return dt.astimezone(TZ).replace(microsecond=0).isoformat()


# ---------------- 抓取 ----------------

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_rss(raw, src):
    items = []
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        raise ValueError("XML 解析失败: %s" % e)
    for node in root.iter("item"):
        title = _clean(node.findtext("title"))
        link = _clean(node.findtext("link"))
        desc = _clean(node.findtext("description"))
        pub = _clean(node.findtext("pubDate"))
        if not title:
            continue
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        items.append({
            "src": src["name"], "lang": src["lang"],
            "title": title, "summary": desc[:220],
            "url": link, "ts": _iso(dt),
        })
    return items


def _parse_wscn(raw, src):
    items = []
    d = json.loads(raw.decode("utf-8", "ignore"))
    for it in (d.get("data") or {}).get("items", []):
        ts = it.get("display_time")
        if not ts:
            continue
        dt = datetime.datetime.fromtimestamp(int(ts), TZ)
        text = _clean(it.get("content_text") or it.get("content") or "")
        title = _clean(it.get("title") or "")
        # 快讯多数没有标题，正文首句就是标题
        if not title:
            title = re.split(r"[。！？\n]", text)[0][:70]
        if not title:
            continue
        items.append({
            "src": src["name"], "lang": src["lang"],
            "title": title, "summary": text[:220],
            "url": (it.get("uri") or ""), "ts": _iso(dt),
        })
    return items


def _norm(t):
    t = t.lower()
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _sim(a, b):
    """粗相似度：英文按词，中文按二元字。够用来把同一件事的十家报道并成一簇。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if re.search(r"[\u4e00-\u9fff]", na):
        sa = {na[i:i + 2] for i in range(len(na) - 1)}
        sb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    else:
        sa, sb = set(na.split()), set(nb.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _hint(title, summary):
    blob = (title + " " + summary).lower()
    for bid, kws in HINTS:
        for k in kws:
            if k in blob:
                return bid
    return ""


def cluster(items, thresh=0.42):
    """同事件聚簇：保留最新的一条做代表，其余记为同题报道。

    不聚类的话，40 条里会有十几条在讲同一件事——不同媒体换个标题而已。
    """
    items = sorted(items, key=lambda x: x["ts"], reverse=True)
    out = []
    for it in items:
        for rep in out:
            if _sim(it["title"], rep["title"]) >= thresh:
                rep["n_src"] += 1
                if it["src"] not in rep["also"]:
                    rep["also"].append(it["src"])
                break
        else:
            it["n_src"] = 1
            it["also"] = []
            out.append(it)
    return out


def fetch(save=True, timeout=20):
    """拉全部源，去重聚簇，落 raw.json。返回 (raw, report)。"""
    all_items, report, dead = [], [], []
    for src in SOURCES:
        try:
            raw = _get(src["url"], timeout=timeout)
            items = _parse_rss(raw, src) if src["kind"] == "rss" else _parse_wscn(raw, src)
            if not items:
                raise ValueError("解析到 0 条")
            newest = max(x["ts"] for x in items)
            age_h = (_now() - datetime.datetime.fromisoformat(newest)).total_seconds() / 3600
            if age_h > 96:
                # 200 但内容停在四天前，说明这个源僵了，宁可不收
                dead.append({"id": src["id"], "reason": "最新一条已 %d 小时前" % age_h})
                report.append({"id": src["id"], "name": src["name"], "ok": False,
                               "n": len(items), "err": "内容过旧"})
                continue
            for it in items:
                it["src_id"] = src["id"]
            all_items.extend(items)
            report.append({"id": src["id"], "name": src["name"], "ok": True,
                           "n": len(items), "newest": newest})
        except Exception as e:
            report.append({"id": src["id"], "name": src["name"], "ok": False,
                           "n": 0, "err": "%s: %s" % (type(e).__name__, e)})

    seen, uniq = set(), []
    for it in all_items:
        key = _norm(it["title"])[:80]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    cl = cluster(uniq)
    for it in cl:
        it["hint"] = _hint(it["title"], it["summary"])

    raw = {"fetched_at": _iso(_now()), "sources": report,
           "n_raw": len(all_items), "n_uniq": len(cl), "items": cl}
    if save:
        _write(RAW_FILE, raw)
    return raw, report


# ---------------- 合并策展稿 ----------------

def merge_issues(save=True):
    """把 data/news/issue*.json 合并成 news.json。

    issue1 需含 as_of / window / brief / top，其余只需含 items。
    同一 id 以先出现的为准。
    """
    fs = sorted(f for f in os.listdir(NEWS_DIR)
                if f.startswith("issue") and f.endswith(".json")) if os.path.isdir(NEWS_DIR) else []
    if not fs:
        raise SystemExit("data/news 下没有 issue*.json 可合并")

    base = _read(os.path.join(NEWS_DIR, fs[0]))
    seen, items = set(), []
    for f in fs:
        d = _read(os.path.join(NEWS_DIR, f))
        for it in d.get("items", []):
            if it.get("id") in seen:
                print("  [skip] 重复 id: %s (%s)" % (it.get("id"), f))
                continue
            seen.add(it.get("id"))
            items.append(it)

    base["items"] = items
    base["buckets"] = BUCKETS
    _fill_urls(base["items"])
    if save:
        _write(NEWS_FILE, base)
    return base


def _fill_urls(items, raw_path=None):
    """从原料池回填原文链接与同题家数。

    稿子里手写标题就够了，不必再抄一遍 URL——按标题对回原料池，
    对不上的就不填，宁可没链接也不猜。
    """
    raw_path = raw_path or RAW_FILE
    if not os.path.exists(raw_path):
        return
    try:
        raw = _read(raw_path)
    except Exception:
        return
    idx = {}
    for r in raw.get("items", []):
        idx.setdefault(_norm(r.get("title", ""))[:70], r)

    hit = 0
    for it in items:
        if it.get("url"):
            continue
        k = _norm(it.get("t", ""))[:70]
        r = idx.get(k)
        if not r:
            for kk, vv in idx.items():
                if k and (k in kk or kk in k):
                    r = vv
                    break
        if r:
            it["url"] = r.get("url", "")
            it.setdefault("n_src", r.get("n_src", 1))
            hit += 1
    if hit:
        print("  回填链接 %d / %d 条" % (hit, len(items)))


def validate(d, strict=True):
    """结构校验。返回 (errors, warnings)。"""
    err, warn = [], []
    places = set(BUCKET_IDS) | {"top"}
    win = d.get("window") or {}
    top_w = win.get("top") or ["", ""]
    flow_w = win.get("flow") or ["", ""]

    def _dt(s):
        try:
            dt = datetime.datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
        except Exception:
            return None

    tlo, thi = _dt(top_w[0]), _dt(top_w[1])
    flo, fhi = _dt(flow_w[0]), _dt(flow_w[1])
    if not (tlo and thi and flo and fhi):
        err.append("window 格式不对: %r" % (win,))

    ids, counts, carry_n = set(), {}, 0
    for it in d.get("items", []):
        iid = it.get("id")
        if not iid:
            err.append("有条目缺 id")
            continue
        if iid in ids:
            err.append("条目 id 重复: %s" % iid)
        ids.add(iid)

        place = it.get("place")
        if place not in places:
            err.append("%s 的 place 不合法: %s" % (iid, place))
        counts[place] = counts.get(place, 0) + 1

        for k in ("t", "tcn", "src", "date"):
            if not it.get(k):
                err.append("%s 缺字段 %s" % (iid, k))
        if not it.get("why"):
            err.append("%s 缺 why（为什么重要）" % iid)
        elif len(it["why"]) > 60:
            warn.append("%s 的 why 偏长(%d 字)" % (iid, len(it["why"])))

        for f in it.get("impact") or []:
            if f not in IMPACTS:
                err.append("%s 的影响面不合法: %s" % (iid, f))

        dt = _dt(it.get("date", ""))
        if dt and tlo and fhi:
            if place == "top":
                if not (tlo <= dt <= thi):
                    # 头条以 24 小时为主，但允许最多 2 条 72 小时内仍在发酵的
                    # 大事件带着 carry 标记进入——规则要写死，例外要显形，
                    # 不能让「持续」变成悄悄放宽口径的后门。
                    if it.get("carry") and flo <= dt <= thi:
                        carry_n += 1
                    else:
                        warn.append("%s 在头条窗口外且未标 carry（%s）"
                                    % (iid, it.get("date")))
            else:
                if not (flo <= dt <= fhi):
                    warn.append("%s 在分类窗口外（%s）" % (iid, it.get("date")))
            if dt > _now():
                err.append("%s 的时间在未来: %s" % (iid, it.get("date")))
        elif it.get("carry") and place != "top":
            warn.append("%s 标了 carry 但不在头条带" % iid)

    if carry_n > 2:
        warn.append("头条带里有 %d 条持续项，上限是 2 条" % carry_n)

    if counts.get("top", 0) and not (4 <= counts["top"] <= 9):
        warn.append("头条 %d 条，建议 6~8 条" % counts["top"])
    for b in BUCKET_IDS:
        n = counts.get(b, 0)
        if n == 0:
            warn.append("桶 %s 为空（页面会显示「窗口内无重大事项」，允许）" % b)
        elif n < 3:
            warn.append("桶 %s 只有 %d 条，偏少" % (b, n))
        elif n > 9:
            warn.append("桶 %s 有 %d 条，页面会过长" % (b, n))
    for c in (d.get("brief") or {}).get("items", []):
        for k in ("h", "t"):
            if not c.get(k):
                err.append("页首判断缺 %s" % k)

    if strict and err:
        for e in err:
            print("  [ERR] " + e)
        raise SystemExit("校验未通过：%d 个错误" % len(err))
    return err, warn


def stats(d):
    """按桶统计，页面顶部的仓位条与体检都用这份。"""
    out = []
    for b in BUCKETS:
        its = [x for x in d.get("items", []) if x.get("place") == b["id"]]
        latest = max([x.get("date", "") for x in its], default="")
        out.append({"id": b["id"], "name": b["name"], "n": len(its), "latest": latest})
    return out


def freshness(d, ref=None):
    """新鲜度：以最新一条为准算距今几小时。

    基准是运行时刻而不是 as_of——as_of 是快照日，用它算永远是「刚更新」，
    这套算法在 wind 那边已经踩过一次。
    """
    ref = ref or _now()
    if isinstance(ref, str):
        ref = datetime.datetime.fromisoformat(ref)
    dates = [x.get("date") for x in d.get("items", []) if x.get("date")]
    if not dates:
        return {"ref": _iso(ref), "newest": "", "age_h": None, "n": 0}
    newest = max(dates)
    try:
        dt = datetime.datetime.fromisoformat(newest)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        age = (ref - dt).total_seconds() / 3600
    except Exception:
        age = None
    return {"ref": _iso(ref), "newest": newest,
            "age_h": round(age, 1) if age is not None else None,
            "n": len(dates)}


def build():
    d = load() or merge_issues()
    validate(d)
    d["stats"] = stats(d)
    d["n_items"] = len(d.get("items", []))
    d["n_top"] = sum(1 for x in d["items"] if x.get("place") == "top")
    d["fresh"] = freshness(d)
    return d


def load():
    if not os.path.exists(NEWS_FILE):
        return None
    return _read(NEWS_FILE)


def reload(rebuild=True):
    """更新按钮走这里：重新合并 + 重新抓取 + 体检。

    先抓后合并：抓的是原料，合并的是稿子，顺序反了会把刚抓的热乎数据
    覆盖成旧快照。
    """
    fetched = None
    try:
        fetched, _ = fetch(save=True)
    except Exception as e:
        print("  [WARN] 抓取失败，沿用上次原料: %s" % e)
    d = merge_issues()
    validate(d)
    d["stats"] = stats(d)
    d["n_items"] = len(d.get("items", []))
    d["n_top"] = sum(1 for x in d["items"] if x.get("place") == "top")
    d["fresh"] = freshness(d)
    if fetched:
        d["n_raw"] = fetched["n_uniq"]
    return d


def main():
    if "--fetch" in sys.argv:
        raw, rep = fetch()
        print("抓取完成 -> %s" % RAW_FILE)
        print("%-12s %-14s %4s  %s" % ("id", "name", "n", "状态"))
        for r in rep:
            print("%-12s %-14s %4d  %s" % (r["id"], r.get("name", ""), r.get("n", 0),
                                           "ok" if r.get("ok") else "[" + r.get("err", "") + "]"))
        print("去重后 %d 条（原始 %d 条）" % (raw["n_uniq"], raw["n_raw"]))
        return

    if "--merge" in sys.argv:
        d = merge_issues()
        print("已合并 -> %s" % NEWS_FILE)
    else:
        d = load()
    if not d:
        raise SystemExit("没有数据，先跑 python scripts/news.py --merge")
    err, warn = validate(d, strict=False)
    st = stats(d)
    fr = freshness(d)
    print("条目 %d 条（头条 %d）／最新一条距现在 %.1f 小时"
          % (len(d.get("items", [])),
             sum(1 for x in d["items"] if x.get("place") == "top"),
             fr["age_h"] or -1))
    print("%-12s %4s  %s" % ("桶", "条数", "最新"))
    for r in st:
        print("%-12s %4d  %s" % (r["name"], r["n"], r["latest"][:16]))
    print("错误 %d / 提示 %d" % (len(err), len(warn)))
    for e in err:
        print("  [ERR] " + e)
    for w in warn:
        print("  [WARN] " + w)


if __name__ == "__main__":
    main()
