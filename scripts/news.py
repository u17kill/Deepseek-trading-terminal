# -*- coding: utf-8 -*-
"""市场新闻：抓取原始池、合并策展稿、校验与统计。

两段式，分工是刻意的：

  抓（本模块 fetch）—— 只搬运。拉 RSS / 快讯接口，落 data/news/raw.json，
      去重聚簇，给一个初始分桶建议。不做判断，也不写「为什么重要」。
  编（人写 issue*.json）—— 定头条、纠分桶、写一句为什么。程序造不出这句话，
      这与 wind 模块「判断不由程序拼装」是同一条红线。

页面数据由 compose() 合成：手写稿（curated，带 why）+ 原料池（auto，不带 why），
两层在页面上分开标注。窗口是硬边界——只留最近 7 天，过期的一律丢，
手写过的也不留：页面上混进一周前的新闻，读的人就得逐条看日期。
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
    {"id": "wire", "name": "快讯流", "desc": "没命中分类 · 按时间倒序",
     "rule": "关键词一个都没中的，原样列出，不硬塞进别的桶"},
]
BUCKET_IDS = [b["id"] for b in BUCKETS]
IMPACTS = ("股", "债", "汇", "商品", "加密")

# 页面窗口：永远只显示最近 N 天。点「更新新闻」会重抓原料池并重算窗口，
# 早于 N 天的条目（含手写稿）直接丢弃，不做「历史归档」。
WIN_DAYS = 7
CAP_BUCKET = 12     # 单桶最多上屏条数，按时间倒序取
CAP_WIRE = 40       # 快讯流单独给一个更宽的额度
CAP_TOP = 8         # 头条上限，与 validate 的 4~9 建议一致


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
    # ---- v1.30.0 扩源：彭博三线 + FT 中文 + 东方财富 + 新浪财经 ----
    # 彭博按频道拆，markets 与 politics 更新最快（实测 0.7h / 7.2h），tech 与 economics
    # 每天只出几条，只取 tech；economics 只有 3 条且常与 markets 重复，不单列。
    {"id": "bloom_mkts", "name": "Bloomberg", "lang": "en", "kind": "rss",
     "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"id": "bloom_pol", "name": "Bloomberg", "lang": "en", "kind": "rss",
     "url": "https://feeds.bloomberg.com/politics/news.rss"},
    {"id": "bloom_tech", "name": "Bloomberg", "lang": "en", "kind": "rss",
     "url": "https://feeds.bloomberg.com/technology/news.rss"},
    {"id": "ft_cn", "name": "FT中文网", "lang": "zh", "kind": "rss",
     "url": "https://www.ftchinese.com/rss/news"},
    {"id": "eastmoney", "name": "东方财富", "lang": "zh", "kind": "rss",
     "url": "http://rss.eastmoney.com/rss_partener.xml"},
    # 新浪财经滚动：JSON 接口，30 条 / 次，media_name 带原始媒体名（上海证券报等）
    {"id": "sina_roll", "name": "新浪财经", "lang": "zh", "kind": "sina",
     "url": "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=30&page=1"},
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


def _parse_sina(raw, src):
    """新浪财经滚动（JSON）。media_name 是原始媒体名，优先用它当来源——
    一条写「上海证券报」的稿子标成「新浪财经」，会让人误判可信度与口径。"""
    items = []
    j = json.loads(raw.decode("utf-8", "ignore"))
    for it in (j.get("result") or {}).get("data", []):
        title = _clean(it.get("title") or "")
        if not title:
            continue
        ct = it.get("ctime") or it.get("intime") or it.get("mtime")
        try:
            dt = datetime.datetime.fromtimestamp(int(ct), TZ)
        except Exception:
            continue
        items.append({
            "src": _clean(it.get("media_name") or "") or src["name"],
            "lang": src["lang"],
            "title": title,
            "summary": _clean(it.get("intro") or "")[:220],
            "url": it.get("url") or "",
            "ts": _iso(dt),
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
            kind = src.get("kind")
            if kind == "rss":
                items = _parse_rss(raw, src)
            elif kind == "sina":
                items = _parse_sina(raw, src)
            else:
                items = _parse_wscn(raw, src)
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


def _ts(s):
    try:
        dt = datetime.datetime.fromisoformat(str(s))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _trim(s, n):
    s = _clean(s)
    return s if len(s) <= n else s[:n].rstrip() + "…"


def compose(now=None, days=WIN_DAYS, save=True):
    """合成页面数据：最近 N 天的手写稿 + 原料池，窗口外的一律丢。

    两层内容分得很清，页面上也分开标：

      curated（issue*.json，人写）—— 有 why，进六个分类桶与头条；
      auto（raw.json，机器搬）—— 没有 why 就不装作有，卡片标「未策展」。

    去重靠标题相似度：手写稿已经覆盖的事，原料池里同一件事不再重复出一条。
    分桶用抓取时的 hint，一个关键词都没命中的落 wire，不硬塞。
    """
    now = now or _now()
    lo = now - datetime.timedelta(days=days)

    # 手写稿：只取窗口内的。过期的即使人写过也丢——「写过」不是留下来的理由。
    curated, brief, n_expired_cur = [], None, 0
    try:
        base = merge_issues(save=False)
        # 导语是「当天的判断」，挂到六天后就是误导：超过 48 小时直接不给。
        b_as = _ts(base.get("as_of"))
        if base.get("brief") and b_as and (now - b_as).total_seconds() <= 48 * 3600:
            brief = dict(base["brief"])
            brief["as_of"] = base.get("as_of")
        rows = base.get("items", [])
        _fill_urls(rows)
        for it in rows:
            dt = _ts(it.get("date"))
            if not dt or not (lo <= dt <= now):
                n_expired_cur += 1
                continue
            it = dict(it)
            it["curated"] = True
            curated.append(it)
    except SystemExit:
        rows = []

    # 原料池：窗口内、且没和手写稿撞车的，按时间倒序补进来。
    raw_items, raw_meta, n_expired_raw = [], {}, 0
    try:
        raw = _read(RAW_FILE)
        raw_meta = {"fetched_at": raw.get("fetched_at"),
                    "n_pool": raw.get("n_uniq") or len(raw.get("items", [])),
                    "sources": raw.get("sources") or []}
        raw_items = raw.get("items", [])
    except Exception as e:  # noqa: BLE001  原料池缺失不该让整页构建失败
        print("  [WARN] 原料池读取失败：%s" % e)

    seen = [it.get("t") or "" for it in curated]
    auto = []
    for r in raw_items:
        dt = _ts(r.get("ts"))
        if not dt or not (lo <= dt <= now):
            n_expired_raw += 1
            continue
        title = _clean(r.get("title"))
        if not title:
            continue
        if any(_sim(title, s) >= 0.42 for s in seen if s):
            continue
        seen.append(title)
        hid = r.get("hint") or ""
        auto.append({
            "id": "a%03d" % (len(auto) + 1),
            "auto": True,
            "place": hid if hid in BUCKET_IDS else "wire",
            "t": title,
            "tcn": _trim(r.get("summary"), 90),
            "src": r.get("src") or "",
            "date": _iso(dt),
            "url": r.get("url") or "",
            "n_src": r.get("n_src") or 1,
            "lang": r.get("lang") or "",
        })

    # 限额：每个桶给固定额度，手写稿优先占位，自动条目补位。超出的留在 raw.json——
    # 页面不是存档，一周的原料全铺开会有几百条，读的人只会去翻时间。
    tops = [it for it in curated if it.get("place") == "top"]
    others = [it for it in curated + auto if it.get("place") != "top"]
    by_place = {}
    for it in others:
        by_place.setdefault(it["place"], []).append(it)
    kept, n_capped = [], 0
    for p, arr in by_place.items():
        arr.sort(key=lambda x: (0 if x.get("curated") else 1))   # 稳定排序，组内维持原序
        cap = CAP_WIRE if p == "wire" else CAP_BUCKET
        kept += arr[:cap]
        n_capped += max(0, len(arr) - cap)

    # 头条：手写头条在前，但只要窗口里还有比它更新的自动条目，就补进来——
    # 一个 6 天前定的头条压在今天的新闻上面，读的人第一眼就被误导。
    # 补到上限 8 条而不是只补到 6：手写稿过期后头条带会整条变成旧闻，
    # 只有把当天的重要新闻吸收进来，主轴（取头条前五）才可能全是新的。
    newest_top = max([x["date"] for x in tops], default="")
    fillers = sorted([x for x in kept if x["date"] > newest_top],
                     key=lambda x: x["date"], reverse=True)
    if fillers or len(tops) < 4:
        for it in fillers:
            if len(tops) >= CAP_TOP:
                break
            it["place"] = "top"
            tops.append(it)
        moved = {id(x) for x in tops if x.get("auto")}
        kept = [x for x in kept if id(x) not in moved]
    # 头条按重要性排，不按时间排：否则一条 6 天前的手写头条会压在今天的新闻上面。
    # 先按时间倒序、再按分数倒序（两次稳定排序），同分时新的在前。
    tops.sort(key=lambda x: x["date"], reverse=True)
    tops.sort(key=lambda x: -_score(x, now))
    for i, it in enumerate(tops):
        it["rank"] = i + 1

    items = tops + kept
    # id 必须唯一：手写稿可能有重号，自动条目从 a001 起。
    used, uniq = set(), []
    for it in items:
        iid = it.get("id") or ""
        if not iid or iid in used:
            iid = "x%03d" % (len(uniq) + 1)
            it["id"] = iid
        used.add(iid)
        uniq.append(it)

    # 手写导语过期时，退到自动摘要而不是留空——空着读者只会以为页面坏了。
    # 但两层的标注必须不同：自动的写「自动摘要」，谁写的说了什么一目了然。
    if not brief:
        brief = auto_brief(uniq, now)
        if not brief["items"]:
            brief = None

    d = {
        "as_of": now.strftime("%Y-%m-%d"),
        "window": {"top": [_iso(lo), _iso(now)], "flow": [_iso(lo), _iso(now)],
                   "days": days},
        "brief": brief,
        "buckets": BUCKETS,
        "items": uniq,
        "n_items": len(uniq),
        "n_top": len(tops),
        "n_auto": sum(1 for x in uniq if x.get("auto")),
        "n_curated": sum(1 for x in uniq if not x.get("auto")),
        "n_drop": {"curated": n_expired_cur, "raw": n_expired_raw, "capped": n_capped},
        "raw": {k: v for k, v in raw_meta.items() if k != "sources"},
    }
    d["stats"] = stats(d)
    d["fresh"] = freshness(d)
    if save:
        _write(NEWS_FILE, d)
    return d



def _first_sent(s, n=80):
    s = _clean(s)
    if not s:
        return ""
    out = re.split(r"(?<=[。！？!?])\s*", s)[0]
    return out if len(out) <= n else out[:n].rstrip("，,、 ") + "…"


def _score(it, now):
    """重要性打分：时效（6/24/72 小时三档）＋ 同报家数 ＋ 是否人写过。

    只用来排序，不参与取舍，所以不必精确；但每一项都能指回原文，
    谁排在前面可以当场解释清楚——这是「核心五条」能不能被信任的前提。
    """
    dt = _ts(it.get("date"))
    age = (now - dt).total_seconds() / 3600 if dt else 999
    s = 4 if age <= 6 else (3 if age <= 24 else (2 if age <= 72 else 1))
    s += 2 * max(0, (it.get("n_src") or 1) - 1)
    s += 2 if it.get("curated") else 0
    return s


def auto_brief(items, now=None, n=5):
    """没有手写导语时，用头条带里的条目顶成「核心五条」。

    与头条**同源同序**：主轴是这五条的一句话版，下面头条区是同一批的卡片展开。
    两边各挑各的会立刻露馅——同一件事在页面上出现两次，标题还不完全一样，
    读者只会怀疑哪条是真的。

    头条带不足五条时，从六个分类桶里按分数补，每桶最多一条，wire 不参与：
    wire 是关键词一个都没命中的长尾（公告、人事、个股回购），把它放进
    「最核心」等于把分类失败当成重要性。

    一句话取原文摘要（或人写的 why）的**首句**，不改写、不压缩——把两句并成
    一句就是在替作者下判断，那是这个模块明确不做的事。
    """
    now = now or _now()

    def pack(it):
        return {"h": _trim(it.get("t"), 40),
                "t": _first_sent(it.get("why") or it.get("tcn") or "", 80),
                "src": it.get("src") or "", "date": it.get("date") or "",
                "bucket": it.get("place") or ""}

    tops = [x for x in items if x.get("place") == "top" and pack(x)["t"]]
    tops.sort(key=lambda x: -_score(x, now))
    out = [pack(x) for x in tops[:n]]

    if len(out) < n:
        rest = [x for x in items
                if x.get("place") not in ("top", "wire") and pack(x)["t"]]
        rest.sort(key=lambda x: -_score(x, now))
        used = {}
        for x in rest:
            p = x.get("place")
            if used.get(p):
                continue
            used[p] = 1
            out.append(pack(x))
            if len(out) >= n:
                break

    return {"auto": True, "items": out,
            "rule": "取头条带前五，不足则按时效 · 同报家数 · 是否人写补齐，每桶最多一条；"
                    "一句话取自原文首句，未改写"}


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

        if it.get("auto"):
            # 机器搬来的条目：没有 why 是设计，不是缺陷。只要求能上屏的三件套。
            for k in ("t", "src", "date"):
                if not it.get(k):
                    err.append("%s 缺字段 %s" % (iid, k))
            if it.get("why"):
                warn.append("%s 是 auto 条目却带 why（自动层不该混进人工判断）" % iid)
        else:
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
        if b == "wire":
            continue          # 快讯流本来就是长尾，不按「桶条数」管
        n = counts.get(b, 0)
        if n == 0:
            warn.append("桶 %s 为空（页面会显示「窗口内无重大事项」，允许）" % b)
        elif n < 3:
            warn.append("桶 %s 只有 %d 条，偏少" % (b, n))
        elif n > CAP_BUCKET:
            # 超过单桶额度说明限额没生效，页面会被撑长
            warn.append("桶 %s 有 %d 条，超过单桶额度 %d" % (b, n, CAP_BUCKET))
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
    """页面构建与 GET /api/news 都走这里。

    每次构建都重新合成并落盘：窗口跟着当天走，昨天进来、今天过期的条目
    自动消失，不会因为「上次构建是三天前」把旧内容一直留在页面上。
    """
    d = compose()
    validate(d, strict=False)
    return d


def load():
    if not os.path.exists(NEWS_FILE):
        return None
    return _read(NEWS_FILE)


def reload(rebuild=True):
    """更新按钮走这里：重抓原料池 -> 重新合成最近 N 天 -> 体检。

    先抓后合成。合成要靠刚抓的 ts 算窗口，反过来会用昨天的原料算今天的窗口。
    抓取失败不算更新失败——沿用上次原料池，页面照旧按窗口重算，不至于一动不动。
    """
    try:
        fetch(save=True)
    except Exception as e:  # noqa: BLE001
        print("  [WARN] 抓取失败，沿用上次原料: %s" % e)
    d = compose()
    validate(d, strict=False)
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
    elif "--compose" in sys.argv or "--window" in sys.argv:
        d = compose()
        print("已按窗口（%d 天）合成 -> %s" % (WIN_DAYS, NEWS_FILE))
    else:
        d = load()
    if not d:
        raise SystemExit("没有数据，先跑 python scripts/news.py --merge")
    err, warn = validate(d, strict=False)
    st = stats(d)
    fr = freshness(d)
    win = (d.get("window") or {}).get("flow") or ["", ""]
    print("窗口 %s ~ %s（%s 天）" % (win[0][:16], win[1][:16],
                                  (d.get("window") or {}).get("days", WIN_DAYS)))
    print("条目 %d 条（头条 %d / 未策展 %d）／最新一条距现在 %.1f 小时"
          % (len(d.get("items", [])),
             sum(1 for x in d["items"] if x.get("place") == "top"),
             sum(1 for x in d["items"] if x.get("auto")),
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
