# -*- coding: utf-8 -*-
"""专家观点 · 自动抓取「每人最近说了什么」。

分工和 news 模块一样，界线不能挪：

  抓（本模块）—— 只搬运。按人名去新闻索引里检索，把**标题原文、日期、来源、链接**
      搬进 data/wind/feed.json。不写判断、不标多空、不改人物的 tag / stance。
  写（人写 part*.json）—— 仍然是唯一能产出「要点」正文的地方，一句人写的分析
      顶十条机器标题，这里抓的东西永远不覆盖它。

为什么要分开存：`points` 是人写的策展内容，`feed` 是每点一次「更新观点」就刷新的
原料层。混在一个数组里，重抓一次就把人写的东西冲掉，而这正是这个项目最不能丢的部分。

上限按用户要求：每人最多 CAP 条、按日期倒序，新条目进来把该人最旧的挤出去。
挤掉的是「机器抓的旧标题」；人写的要条永不因抓取而减少。
"""
import json
import os
import re
import sys
import time
import random
import datetime
import threading
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import email.utils
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIND_DIR = os.path.join(ROOT, "data", "wind")
FEED_FILE = os.path.join(WIND_DIR, "feed.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

CAP = 5        # 每人最多几条
DATE_CAP = 1   # 同一天最多几条。

# 试过按标题相似度合并（字符二元组 Jaccard / 包含率），但同一件事的不同写法
# 得分 0.20~0.58，不同的两件事也能到 0.35，分不开——宁可规则笨一点：
# 一天只留最新一条，五个位置自然落在五天，也不会被一次出访的十篇通稿占满。
DAYS = 21      # 只收最近多少天
WORKERS = 5    # 并发的检索线程数：再高 Google 会开始返回空

CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 检索

def _get(url, timeout=25, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 + random.random())
    raise last


def _locale_args(locale):
    return {
        "zh": ("zh-CN", "CN", "CN:zh-Hans"),
        "hk": ("zh-HK", "HK", "HK:zh-Hant"),
        "en": ("en-US", "US", "US:en"),
    }[locale]


def queries_for(p):
    """给一个人出哪些检索词。

    - 中文母语的人（org 或 role 是中文）：中文名走 zh 语区——中文媒体不写拼音，
      「洪灏」能搜到，`Hong Hao` 只会搜到同名的高管任命公告。
    - 加「·」的是音译名（杰米·戴蒙 / 克里斯蒂娜·拉加德），中文媒体不这么写，
      只走英文名。
    - 英文名一律也搜一遍，两边结果合起来再去重。
    """
    out = []
    cn, name = p.get("cn") or "", p.get("name") or ""
    org, role = p.get("org") or "", p.get("role") or ""
    # 只看 org：role 一律是中文写的（页面语言），拿它判断会把所有人都当成中国人
    domestic_cn = bool(CJK.search(cn) and "·" not in cn and CJK.search(org))
    if domestic_cn:
        # 只搜中文：`Hong Hao` 这类拼音名在英文库里会撞上一堆同名高管任命公告，
        # 而当事人真正被报道的场合几乎都在中文媒体
        out.append((cn, "zh"))
    else:
        if CJK.search(cn) and "·" not in cn:
            out.append((cn, "zh"))
        if name and name.isascii():
            out.append((name, "en"))
    if not out:
        out.append((cn or name, "zh" if CJK.search(cn or name) else "en"))
    return out


def _parse_rss(xml_bytes, locale):
    root = ET.fromstring(xml_bytes)
    rows = []
    for it in root.iter("item"):
        src_el = it.find("{*}source")
        rows.append({
            "title": (it.findtext("title") or "").strip(),
            "url": (it.findtext("link") or "").strip(),
            "pub": (it.findtext("pubDate") or "").strip(),
            "src": (src_el.text or "").strip() if src_el is not None else "",
            "locale": locale,
        })
    return rows


def _google(key, locale, days):
    hl, gl, ceid = _locale_args(locale)
    q = '"%s" when:%dd' % (key, days)
    url = ("https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s"
           % (urllib.parse.quote(q), hl, gl, ceid))
    return _parse_rss(_get(url), locale)


def _bing(key, locale, days):
    """兜底源。Google 一条都没给时才用——必应中文新闻库比 Google 薄，
    但英文检索是好的，且不会被同一个出口连续请求卡住。"""
    url = ("https://www.bing.com/news/search?q=%s&format=RSS&qft=sortbydate%%3d%%221%%22"
           % urllib.parse.quote('"%s"' % key))
    rows = _parse_rss(_get(url), locale)
    for r in rows:
        if not r["src"]:
            # 必应给的是 apiclick 跳转链，域名一律 bing.com，拿它当来源等于没写
            m = re.search(r"https?://([^/]+)/", r["url"] or "")
            host = m.group(1).replace("www.", "") if m else ""
            r["src"] = host if host and "bing." not in host else "必应新闻"
    return rows


def _date_of(s):
    """pubDate -> 北京时间的 YYYY-MM-DD。

    不能直接截字符串：RSS 给的是 GMT，晚间发布的条目按 GMT 取日期会早一天，
    页面按日期排序时会把这批系统性排错位。
    """
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(TZ).date().isoformat()
    except Exception:
        return ""


def _norm(t):
    """归一化标题，用于判重：同一家通讯社的稿子会被十几家转载，
    标题几乎一样、链接各不相同。不去重的话，一个人的五条位置会被同一条新闻占满。"""
    s = (t or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。、；：！？,.;:!?\"'“”‘’（）()\[\]【】《》…—-]+", "", s)
    return s


def _clean_title(title, src):
    """Google News 的标题尾巴挂着「 - 来源」，来源另有字段，别在页面上印两遍。"""
    if src and title.endswith(" - " + src):
        return title[:-(len(src) + 3)].strip()
    return title


def _pick(rows, key, lo, seen):
    """从一堆 RSS 条目里挑出「标题真提到这个人、且在窗口内、还没出现过」的。"""
    out = []
    for r in rows:
        title = r["title"]
        if not title or key.lower() not in title.lower():
            continue          # 标题里没这个人：正文提一句的不算「他的表态」
        d = _date_of(r["pub"])
        if not d or d < lo:
            continue
        src = r["src"] or ""
        clean = _clean_title(title, src)
        nk = _norm(clean)
        if not nk or nk in seen:
            continue
        seen.add(nk)
        out.append({"date": d, "src": src, "title": clean,
                    "url": r["url"], "lang": r["locale"]})
    return out


def collect(p, days=DAYS):
    """抓一个人。返回 (items, notices)。抓不到不算错，但要留下原因。"""
    notices = []
    lo = (datetime.datetime.now(TZ).date() - datetime.timedelta(days=days)).isoformat()
    seen, items = set(), []

    for key, locale in queries_for(p):
        try:
            rows = _google(key, locale, days)
        except Exception as e:  # noqa: BLE001
            notices.append("%s/%s 检索失败：%s" % (key, locale, type(e).__name__))
            rows = []
        items += _pick(rows, key, lo, seen)

    # Google 一条命中的都没有时再问一次必应。注意判据是「有没有命中」而不是
    # 「有没有返回」——中文检索常常返回一堆沾边但标题里没这个人的稿子，
    # 按返回判的话兜底永远触发不了（王兴、梁汝波就是这么空着的）。
    if not items:
        for key, locale in queries_for(p):
            try:
                rows = _bing(key, locale, days)
            except Exception as e:  # noqa: BLE001
                notices.append("%s 必应兜底失败：%s" % (key, type(e).__name__))
                continue
            got = _pick(rows, key, lo, seen)
            if got:
                notices.append("%s 走必应兜到 %d 条" % (key, len(got)))
                items += got

    items.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    # 同日上限：一次出访会被十家媒体写十遍，不压的话五个位置全是同一件事
    per_day, capped = {}, []
    for it in items:
        if per_day.get(it["date"], 0) >= DATE_CAP:
            continue
        per_day[it["date"]] = per_day.get(it["date"], 0) + 1
        capped.append(it)
    return capped, notices


# ---------------------------------------------------------------- 合并与落盘

def _load_feed():
    if not os.path.exists(FEED_FILE):
        return {"people": {}}
    try:
        with open(FEED_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("people", {})
        return d
    except Exception:
        return {"people": {}}


def merge_person(old, new, cap=CAP):
    """新的排前面，旧的补齐；同一条（归一化标题相同）只留一条。

    保留旧条目是必须的：同一批新闻几天后会从索引里掉出去，只按本次抓到的结果存，
    页面就会「今天有、明天没」，看起来像抓取坏了。
    """
    out, seen = [], set()
    for it in list(new) + list(old or []):
        k = _norm(it.get("title"))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    out.sort(key=lambda x: (x.get("date", ""), x.get("title", "")), reverse=True)
    return out[:cap]


def run(people=None, write=True, progress=None, days=DAYS, cap=CAP, ids=None):
    """抓全部（或 ids 指定的几位），合并进 feed.json。

    progress(done, total, failed) 由调用方给，服务端拿它更新进度条——
    126 个人要跑四十秒上下，没有进度就是一个假死按钮。
    """
    old = _load_feed()
    oldp = old.get("people") or {}
    by_id = {}
    for f in sorted(os.listdir(WIND_DIR)):
        if f.startswith("part") and f.endswith(".json"):
            try:
                for p in json.load(open(os.path.join(WIND_DIR, f), encoding="utf-8")).get("people", []):
                    by_id.setdefault(p.get("id"), p)
            except Exception:
                pass
    if people:
        for p in people:
            by_id[p.get("id")] = p
    roster = [p for p in by_id.values() if p.get("id")]
    if ids:
        want = set(ids)
        roster = [p for p in roster if p["id"] in want]
    roster.sort(key=lambda p: p["id"])
    total = len(roster)
    if progress:
        progress(0, total, 0)

    result = dict(oldp)
    added_by = {}
    fail, notices, new_count, hit_people = [], {}, 0, 0
    lock = threading.Lock()
    done = [0]

    def one(p):
        nonlocal new_count, hit_people
        pid = p["id"]
        try:
            items, nt = collect(p, days=days)
        except Exception as e:  # noqa: BLE001
            items, nt = [], ["整体失败：%s: %s" % (type(e).__name__, e)]
        merged = merge_person(oldp.get(pid) or [], items, cap=cap)
        added = max(0, len(merged) - len(oldp.get(pid) or []))
        with lock:
            done[0] += 1
            if items or merged:
                result[pid] = merged
            if merged:
                hit_people += 1
            new_count += added
            if added:
                added_by[pid] = added
            if not items:
                fail.append({"id": pid, "cn": p.get("cn"), "why": "无命中"})
            if nt:
                notices[pid] = nt
            if progress:
                progress(done[0], total, len(fail))
        return pid

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(one, roster))

    out = {
        "fetched_at": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "cap": cap,
        "people": result,
        "fail": fail,
        "notices": notices,
        "stats": {"total": total, "hit_people": hit_people,
                  "items": sum(len(v) for v in result.values()), "new": new_count,
                  "added_by": added_by},
    }
    if write:
        # newline="\n"：Windows 默认把 \n 翻成 \r\n，一写就整篇换行都变，diff 全红
        with open(FEED_FILE, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def attach(d, feed=None):
    """把 feed 挂到人物上，供 build / reload 使用。

    允许某人 feed 为空：页面上不显示这一块，比显示「没有新表态」干净。
    """
    f = feed if feed is not None else _load_feed()
    ppl = (f.get("people") or {})
    n = 0
    for p in d.get("people", []):
        items = ppl.get(p.get("id")) or []
        if items:
            p["feed"] = items[:CAP]
            p["feed_date"] = items[0].get("date", "")
            n += len(p["feed"])
        else:
            p.pop("feed", None)
            p.pop("feed_date", None)
    d["feed_fetched_at"] = f.get("fetched_at") or ""
    d["feed_days"] = f.get("days") or DAYS
    d["n_feed"] = n
    return d


def main():
    args = sys.argv[1:]
    ids = None
    for a in args:
        if a.startswith("--only="):
            ids = [x for x in a.split("=", 1)[1].split(",") if x]
    t0 = time.time()

    def prog(done, total, failed):
        step = max(1, total // 12)
        if done % step == 0 or done == total:
            print("  %d/%d  失败 %d  %ds" % (done, total, failed, int(time.time() - t0)),
                  flush=True)

    out = run(write=True, progress=prog, ids=ids)
    st = out["stats"]
    print("抓到 %d 人 / 共 %d 人，条目 %d 条，新增 %d 条，用时 %ds"
          % (st["hit_people"], st["total"], st["items"], st["new"], int(time.time() - t0)))
    if out["fail"]:
        print("无命中 %d 位：%s" % (len(out["fail"]),
                                  "、".join(f["cn"] or f["id"] for f in out["fail"][:12])))


if __name__ == "__main__":
    main()
