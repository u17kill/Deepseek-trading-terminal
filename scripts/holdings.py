# -*- coding: utf-8 -*-
"""大佬持仓：SEC EDGAR 13F 申报抓取、聚合与校验。

设计口径（与 SPEC 第 6.8 节一致）：
- 只读 13F-HR / 13F-HR/A 的 information table，不做任何估算。
- 同一 (CUSIP, 证券类别, PUT/CALL) 的多行合并为一条证券；申报项数另行保留原始行数。
- 占比分母固定用申报封面页的 tableValueTotal，不重算、不剔除期权。
- 13F-NT 是通知不是空仓：跟随 otherManager.cik 找到代报主体，把代报主体的申报记在这张卡上，
  并标注是代报。
- 修正申报（13F-HR/A）保留 amendmentType，页面明确标记。
- 拿不到就报错，保留上一次成功快照，绝不填占位数字。
"""

import gzip
import io
import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "holdings")
SNAP = os.path.join(DATA, "holdings.json")
UA = {
    "User-Agent": "TradingTerminal Research admin@example.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, text/xml, */*",
}

# ---------------------------------------------------------------- 机构名单
# name=页面显示名，filer=13F 申报主体（可能与页面名不同），cik=申报主体 CIK
INSTS = [
    # 价值与质量 11
    {"id": "brk", "name": "伯克希尔·哈撒韦", "en": "Berkshire Hathaway",
     "person": "巴菲特", "filer": "Berkshire Hathaway Inc", "cik": "0001067983", "group": "value"},
    {"id": "hh", "name": "段永平 · H&H", "en": "H&H International Investment",
     "person": "段永平", "filer": "H&H International Investment, LLC", "cik": "0001759760", "group": "value"},
    {"id": "himalaya", "name": "李录 · 喜马拉雅", "en": "Himalaya Capital",
     "person": "李录", "filer": "Himalaya Capital Management LLC", "cik": "0001709323", "group": "value"},
    {"id": "dalal", "name": "帕布来 · Dalal Street", "en": "Dalal Street",
     "person": "Mohnish Pabrai", "filer": "Dalal Street, LLC", "cik": "0001549575", "group": "value"},
    {"id": "greenwoods", "name": "景林资产", "en": "Greenwoods",
     "person": "蒋锦志", "filer": "Greenwoods Asset Management Hong Kong Ltd.", "cik": "0001848138", "group": "value"},
    {"id": "fundsmith", "name": "Terry Smith · Fundsmith", "en": "Fundsmith",
     "person": "Terry Smith", "filer": "Fundsmith LLP", "cik": "0001569205", "group": "value"},
    {"id": "akre", "name": "Chuck Akre · Akre Capital", "en": "Akre Capital",
     "person": "Chuck Akre", "filer": "Akre Capital Management LLC", "cik": "0001112520", "group": "value"},
    {"id": "valleyforge", "name": "Dev Kantesaria · Valley Forge", "en": "Valley Forge",
     "person": "Dev Kantesaria", "filer": "Valley Forge Capital Management LP", "cik": "0001697868", "group": "value"},
    {"id": "gardner", "name": "Tom Russo · Gardner Russo", "en": "Gardner Russo & Quinn",
     "person": "Tom Russo", "filer": "Gardner Russo & Quinn LLC", "cik": "0000860643", "group": "value"},
    {"id": "baupost", "name": "Seth Klarman · Baupost", "en": "Baupost Group",
     "person": "Seth Klarman", "filer": "Baupost Group LLC/MA", "cik": "0001061768", "group": "value"},
    {"id": "primecap", "name": "PRIMECAP", "en": "PRIMECAP Management",
     "person": "", "filer": "PRIMECAP Management Co", "cik": "0000763212", "group": "value"},
    # 主动与特殊机会 3
    {"id": "pershing", "name": "Bill Ackman · Pershing Square", "en": "Pershing Square",
     "person": "Bill Ackman", "filer": "Pershing Square Capital Management LP", "cik": "0001336528", "group": "active"},
    {"id": "oaktree", "name": "Howard Marks · Oaktree", "en": "Oaktree Capital",
     "person": "Howard Marks", "filer": "Oaktree Capital Management LP", "cik": "0000949509", "group": "active"},
    {"id": "tci", "name": "Chris Hohn · TCI", "en": "TCI Fund Management",
     "person": "Chris Hohn", "filer": "TCI Fund Management Ltd", "cik": "0001647251", "group": "active"},
    # 成长与科技 9
    {"id": "ark", "name": "ARK Investment", "en": "ARK Investment Management",
     "person": "Cathie Wood", "filer": "ARK Investment Management LLC", "cik": "0001697748", "group": "growth"},
    {"id": "situational", "name": "Leopold Aschenbrenner", "en": "Situational Awareness",
     "person": "Leopold Aschenbrenner", "filer": "Situational Awareness LP", "cik": "0002045724", "group": "growth"},
    {"id": "oriental", "name": "但斌 · 东方港湾", "en": "Oriental Harbor",
     "person": "但斌", "filer": "Oriental Harbor Investment Master Fund", "cik": "0002046333", "group": "growth"},
    {"id": "tiger", "name": "Chase Coleman · Tiger Global", "en": "Tiger Global",
     "person": "Chase Coleman", "filer": "Tiger Global Management LLC", "cik": "0001167483", "group": "growth"},
    {"id": "coatue", "name": "Coatue Management", "en": "Coatue",
     "person": "Philippe Laffont", "filer": "Coatue Management LLC", "cik": "0001135730", "group": "growth"},
    {"id": "altimeter", "name": "Brad Gerstner · Altimeter", "en": "Altimeter Capital",
     "person": "Brad Gerstner", "filer": "Altimeter Capital Management LP", "cik": "0001541617", "group": "growth"},
    {"id": "atreides", "name": "Gavin Baker · Atreides", "en": "Atreides Management",
     "person": "Gavin Baker", "filer": "Atreides Management, LP", "cik": "0001777813", "group": "growth"},
    {"id": "baillie", "name": "柏基投资 · Baillie Gifford", "en": "Baillie Gifford",
     "person": "", "filer": "Baillie Gifford & Co", "cik": "0001088875", "group": "growth"},
    {"id": "sands", "name": "Sands Capital", "en": "Sands Capital Management",
     "person": "", "filer": "Sands Capital Management LLC", "cik": "0001020066", "group": "growth"},
    # 宏观与多策略 7
    {"id": "bridgewater", "name": "桥水 · Bridgewater", "en": "Bridgewater Associates",
     "person": "Ray Dalio 创始", "filer": "Bridgewater Associates, LP", "cik": "0001350694", "group": "macro"},
    {"id": "soros", "name": "索罗斯基金", "en": "Soros Fund Management",
     "person": "Soros 家族", "filer": "Soros Fund Management LLC", "cik": "0001029160", "group": "macro"},
    {"id": "scion", "name": "Michael Burry · Scion", "en": "Scion Asset Management",
     "person": "Michael Burry", "filer": "Scion Asset Management, LLC", "cik": "0001649339", "group": "macro"},
    {"id": "tudor", "name": "Paul Tudor Jones · Tudor", "en": "Tudor Investment",
     "person": "Paul Tudor Jones", "filer": "Tudor Investment Corp et al", "cik": "0000923093", "group": "macro"},
    {"id": "appaloosa", "name": "David Tepper · Appaloosa", "en": "Appaloosa",
     "person": "David Tepper", "filer": "Appaloosa LP", "cik": "0001656456", "group": "macro"},
    {"id": "citadel", "name": "Citadel", "en": "Citadel Advisors",
     "person": "Ken Griffin", "filer": "Citadel Advisors LLC", "cik": "0001423053", "group": "macro"},
    {"id": "chanos", "name": "Jim Chanos · Chanos & Co", "en": "Chanos & Co",
     "person": "Jim Chanos", "filer": "Chanos & Co LP", "cik": "0001446440", "group": "macro",
     "note": "原 Kynikos Associates。最后一份 13F 为 2023 Q3，此后未再申报。"},
]

GROUPS = [
    {"id": "value", "name": "价值与质量", "desc": "长期持有、换手低，看生意本身"},
    {"id": "active", "name": "主动与特殊机会", "desc": "集中持仓、事件驱动、有时带投票权诉求"},
    {"id": "growth", "name": "成长与科技", "desc": "高换手、押渗透率与产业迁移"},
    {"id": "macro", "name": "宏观与多策略", "desc": "自上而下，仓位随环境大幅切换"},
]

TOP_KEEP = 60      # 快照里每家保留的证券数（够前 12 + 变化摘要）
PAGE_TOP = 12      # 页面详情最多展示项数


# ---------------------------------------------------------------- 抓取
def _get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code == 404:
                raise RuntimeError("SEC 404：%s" % url)
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        time.sleep(0.6 * (i + 1))
    raise RuntimeError("SEC 请求失败（%s）" % last)


def _json(url):
    return json.loads(_get(url))


def _tag(e):
    return e.tag.split("}")[-1]


def _txt(root, name, default=""):
    for e in root.iter():
        if _tag(e) == name:
            return (e.text or "").strip()
    return default


def _latest_filing(cik):
    """返回最新一份 13F 的 (form, filed, period, accession, primary_doc)。"""
    d = _json("https://data.sec.gov/submissions/CIK%s.json" % cik)
    r = d["filings"]["recent"]
    rows = list(zip(r["form"], r["filingDate"], r["reportDate"],
                    r["accessionNumber"], r["primaryDocument"]))
    for form, filed, period, acc, doc in rows:
        if form.startswith("13F"):
            return {"form": form, "filed": filed, "period": period,
                    "accession": acc, "doc": doc}, d.get("name", "")
    return None, d.get("name", "")


def _archive(cik, acc):
    return "https://www.sec.gov/Archives/edgar/data/%d/%s" % (int(cik), acc.replace("-", ""))


def _parse_cover(xml_bytes):
    root = ET.fromstring(xml_bytes)
    cover = {
        "form": _txt(root, "submissionType"),
        "period": _txt(root, "periodOfReport"),
        "amendment": _txt(root, "isAmendment").lower() == "true",
        "amend_type": _txt(root, "amendmentType"),
        "filer": "",
        "rows": 0,
        "value_total": None,
        "other_managers": [],
        "note": "",
    }
    for e in root.iter():
        t = _tag(e)
        if t == "filingManager":
            nm = _txt(e, "name")
            if nm:
                cover["filer"] = nm
        elif t == "tableEntryTotal":
            try:
                cover["rows"] = int((e.text or "0").strip() or 0)
            except ValueError:
                pass
        elif t == "tableValueTotal":
            try:
                cover["value_total"] = int(float((e.text or "0").strip() or 0))
            except ValueError:
                pass
        elif t == "otherManager":
            cik = _txt(e, "cik")
            nm = _txt(e, "name")
            if cik:
                cover["other_managers"].append({"cik": cik.zfill(10), "name": nm})
        elif t == "additionalInformation":
            cover["note"] = (e.text or "").strip()
    # periodOfReport 是 MM-DD-YYYY
    p = cover["period"]
    if len(p) == 10 and p[2] == "-" and p[5] == "-":
        mm, dd, yy = p.split("-")
        cover["period"] = "%s-%s-%s" % (yy, mm, dd)
    return cover


def _parse_table(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter():
        if _tag(it) != "infoTable":
            continue
        rec = {"issuer": "", "cls": "", "cusip": "", "value": 0,
               "shares": 0, "sh_type": "", "put_call": "", "discretion": ""}
        for e in it.iter():
            t = _tag(e)
            if t == "nameOfIssuer":
                rec["issuer"] = (e.text or "").strip()
            elif t == "titleOfClass":
                rec["cls"] = (e.text or "").strip()
            elif t == "cusip":
                rec["cusip"] = (e.text or "").strip().upper()
            elif t == "value":
                try:
                    rec["value"] = int(float((e.text or "0").strip() or 0))
                except ValueError:
                    rec["value"] = 0
            elif t == "sshPrnamt":
                try:
                    rec["shares"] = int(float((e.text or "0").strip() or 0))
                except ValueError:
                    rec["shares"] = 0
            elif t == "sshPrnamtType":
                rec["sh_type"] = (e.text or "").strip()
            elif t in ("putCall",):
                rec["put_call"] = (e.text or "").strip().upper()
            elif t == "investmentDiscretion":
                rec["discretion"] = (e.text or "").strip()
        if rec["cusip"] or rec["issuer"]:
            out.append(rec)
    return out


def _agg(rows):
    """按 (cusip, cls, put_call) 合并，返回聚合后的证券列表。"""
    box = {}
    for r in rows:
        key = (r["cusip"], r["cls"], r["put_call"])
        a = box.get(key)
        if a is None:
            box[key] = {"issuer": r["issuer"], "cls": r["cls"], "cusip": r["cusip"],
                        "value": r["value"], "shares": r["shares"],
                        "sh_type": r["sh_type"], "put_call": r["put_call"], "rows": 1}
        else:
            a["value"] += r["value"]
            a["shares"] += r["shares"]
            a["rows"] += 1
            if len(r["issuer"]) > len(a["issuer"]):
                a["issuer"] = r["issuer"]
    return list(box.values())


def _fetch_one(inst):
    """抓一家。返回 (记录, 是否需要跟随代报)。"""
    meta, sec_name = _latest_filing(inst["cik"])
    if not meta:
        raise RuntimeError("SEC 上没有 13F 申报")

    base = _archive(inst["cik"], meta["accession"])
    idx = _json(base + "/index.json")
    names = [it["name"] for it in idx["directory"]["item"]]
    prim = None
    for cand in ("primary_doc.xml",):
        if cand in names:
            prim = cand
    xmls = [n for n in names if n.lower().endswith(".xml") and n != prim
            and "primary_doc" not in n.lower()]
    if not prim:
        raise RuntimeError("申报里没有 primary_doc.xml")

    cover = _parse_cover(_get(base + "/" + prim))

    # 13F-NT：不带持仓表，跟随代报主体
    if meta["form"].endswith("-NT") or (cover["rows"] == 0 and not xmls):
        om = [x for x in cover["other_managers"] if x["cik"] and x["cik"] != inst["cik"]]
        if not om:
            raise RuntimeError("13F-NT 且未指明代报主体，无法定位持仓")
        return {"_follow": om[0], "cover": cover, "meta": meta, "sec_name": sec_name}, True

    if not xmls:
        raise RuntimeError("申报里找不到 information table")

    rows = []
    for n in xmls:
        try:
            rows.extend(_parse_table(_get(base + "/" + n)))
        except Exception:
            continue
    if not rows:
        raise RuntimeError("information table 解析出 0 条记录")

    secs = _agg(rows)

    # 单位校正一：早期申报以千美元计，与 tableValueTotal 差 1000 倍
    tot = cover["value_total"] or 0
    if tot > 0:
        s = sum(x["value"] for x in secs)
        if s > 0 and abs(s / tot - 1) > 0.05 and abs(s / 1000 / tot - 1) < 0.05:
            for x in secs:
                x["value"] *= 1000
            cover["value_total"] = tot * 1000
            cover["unit_note"] = "原申报以千美元计，已乘 1000"

    # 单位校正二：表格与封面都用千美元（SEC 2023 年起要求美元，但仍有 filer 没改）。
    # 这种情形两个数字自洽，只能靠「每股隐含价格」识别：前十大持仓的 value/shares
    # 中位数低于 1 美元，在实际股票里不可能出现（那意味着几乎所有持仓都是仙股）。
    px = sorted(x["value"] / x["shares"]
                for x in sorted(secs, key=lambda y: -y["value"])[:20]
                if x["shares"] > 0 and x["value"] > 0)
    if px:
        med = px[len(px) // 2]
        if 0 < med < 1:
            for x in secs:
                x["value"] *= 1000
            if cover["value_total"]:
                cover["value_total"] *= 1000
            cover["unit_note"] = ("原申报以千美元填列（封面与表格一致，无法用总额比对识别），"
                                  "按每股隐含价格 %.2f 美元判定，已乘 1000" % med)

    opt = [x for x in secs if x["put_call"]]
    stk = [x for x in secs if not x["put_call"]]
    stk.sort(key=lambda x: -x["value"])

    denom = cover["value_total"] or sum(x["value"] for x in secs) or 0
    for x in secs:
        x["pct"] = (x["value"] / denom * 100) if denom else None

    rec = {
        "id": inst["id"],
        "cik": inst["cik"],
        "filer": cover.get("filer") or inst["filer"],
        "sec_name": sec_name,
        "form": meta["form"],
        "period": cover["period"] or meta["period"],
        "filed": meta["filed"],
        "accession": meta["accession"],
        "url_index": base + "/%s-index.htm" % meta["accession"],
        "rows": cover["rows"] or len(rows),
        "n_sec": len(stk),
        "n_opt": len(opt),
        "value": cover["value_total"],
        "amended": bool(cover["amendment"]),
        "amend_type": cover["amend_type"],
        "holdings": [{"issuer": x["issuer"], "cls": x["cls"], "cusip": x["cusip"],
                      "value": x["value"], "shares": x["shares"], "sh_type": x["sh_type"],
                      "pct": round(x["pct"], 2) if x["pct"] is not None else None,
                      "rows": x["rows"]} for x in stk[:TOP_KEEP]],
        "n_holdings_kept": min(len(stk), TOP_KEEP),
    }
    if cover.get("unit_note"):
        rec["unit_note"] = cover["unit_note"]
    return rec, False


def fetch_all(verbose=True, progress=None):
    out = []
    for i, inst in enumerate(INSTS):
        t0 = time.time()
        try:
            rec, follow = _fetch_one(inst)
            if follow:
                om = rec["_follow"]
                # 记下通知本身，再抓代报主体
                proxy_note = ("本机构最新提交为 13F-NT（通知），持仓由其公开母公司 %s 代报；"
                              "通知不等于空仓。" % (om["name"] or om["cik"]))
                sub = dict(inst)
                sub["cik"] = om["cik"]
                rec2, _ = _fetch_one(sub)
                rec2["id"] = inst["id"]
                rec2["cik"] = inst["cik"]
                rec2["proxied_by"] = {"cik": om["cik"], "name": om["name"] or rec2["filer"]}
                rec2["note"] = proxy_note
                rec = rec2
            rec["name"] = inst["name"]
            rec["person"] = inst.get("person", "")
            rec["en"] = inst.get("en", "")
            rec["group"] = inst["group"]
            rec["status"] = "ok"
            rec["error"] = None
            if verbose:
                print("  %-28s %-10s %s  %5d 行 / %4d 只  %s  %.2fs"
                      % (inst["name"][:26], rec["period"], rec["form"],
                         rec["rows"], rec["n_sec"],
                         ("%.1f 亿" % (rec["value"] / 1e8)) if rec["value"] else "-",
                         time.time() - t0))
        except Exception as e:
            rec = {"id": inst["id"], "cik": inst["cik"], "name": inst["name"],
                   "person": inst.get("person", ""), "en": inst.get("en", ""),
                   "group": inst["group"], "filer": inst["filer"],
                   "status": "error", "error": str(e), "holdings": []}
            if verbose:
                print("  %-28s ERROR %s" % (inst["name"][:26], e))
        out.append(rec)
        if progress:
            try:
                progress(i + 1, len(INSTS), sum(1 for x in out if x.get("status") == "error"))
            except Exception:
                pass
        if i < len(INSTS) - 1:
            time.sleep(0.16)
    return out


def mark_stale(recs, today=None):
    """距报告期超过 400 天的，标为历史资料。"""
    today = today or date.today()
    for r in recs:
        if r.get("status") != "ok" or not r.get("period"):
            continue
        try:
            p = datetime.strptime(r["period"], "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (today - p).days
        r["age_days"] = days
        if days > 400:
            r["stale"] = True
            r["stale_reason"] = "报告期为 %s，距今 %d 天，属历史申报" % (r["period"], days)
    return recs


def build(write=True, verbose=True, progress=None):
    recs = mark_stale(fetch_all(verbose=verbose, progress=progress))
    ok = [r for r in recs if r["status"] == "ok"]
    snap = {
        "as_of": date.today().isoformat(),
        "source": "SEC EDGAR 13F-HR / 13F-HR/A information table",
        "n_inst": len(recs),
        "n_ok": len(ok),
        "n_error": len(recs) - len(ok),
        "insts": recs,
    }
    if write:
        os.makedirs(DATA, exist_ok=True)
        with io.open(SNAP, "w", encoding="utf-8", newline="\n") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=False)
            f.write("\n")
    return snap


def load():
    if not os.path.exists(SNAP):
        return None
    return json.load(io.open(SNAP, encoding="utf-8"))


def quarters(recs):
    """统计各季度分布 —— 页面要如实展示「不同机构不在同一季度」。"""
    box = {}
    for r in recs:
        if r.get("status") == "ok" and r.get("period"):
            q = r["period"][:4] + "Q%d" % ((int(r["period"][5:7]) - 1) // 3 + 1)
            box[q] = box.get(q, 0) + 1
    return box


def validate(snap):
    err, warn = [], []
    if not snap:
        return ["没有快照"], []
    for r in snap["insts"]:
        if r.get("status") != "ok":
            err.append("%s：%s" % (r.get("name"), r.get("error")))
            continue
        if not r.get("holdings"):
            err.append("%s：持仓为空" % r["name"])
        if r.get("value") and r["holdings"]:
            s = sum(h["value"] for h in r["holdings"])
            if s > r["value"] * 1.001:
                err.append("%s：前 %d 项合计超过申报总值" % (r["name"], len(r["holdings"])))
        if r.get("stale"):
            warn.append("%s：%s" % (r["name"], r.get("stale_reason")))
        if r.get("amended"):
            warn.append("%s：修正申报 %s" % (r["name"], r.get("amend_type") or ""))
        if r.get("proxied_by"):
            warn.append("%s：13F-NT 由 %s 代报" % (r["name"], r["proxied_by"]["name"]))
    return err, warn


if __name__ == "__main__":
    import sys
    t0 = time.time()
    if "--load" in sys.argv:
        snap = load()
    else:
        snap = build()
    if not snap:
        print("没有快照")
        sys.exit(1)
    err, warn = validate(snap)
    ok = [r for r in snap["insts"] if r["status"] == "ok"]
    print("\n机构 %d 家，成功 %d，失败 %d；用时 %.1fs"
          % (snap["n_inst"], len(ok), snap["n_error"], time.time() - t0))
    print("季度分布：", quarters(snap["insts"]))
    print("错误 %d / 提示 %d" % (len(err), len(warn)))
    for e in err:
        print("  ERR ", e)
    for w in warn:
        print("  WARN", w)
