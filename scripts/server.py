"""本地看板服务：托管页面 + 提供「一键更新」取数接口。

启动：python scripts/server.py
然后浏览器打开 http://127.0.0.1:8765/

接口：
  GET  /                    看板页面
  GET  /api/data            当前缓存数据（短期动能 / 估值）
  POST /api/refresh         调 westock 取最新日线 -> 重算指标 -> 返回新数据
  GET  /api/sentiment       「短线精灵」情绪复盘缓存
  POST /api/sentiment/refresh 取市场总览 / 连板梯队 / 龙虎榜 -> 重算情绪温度
  GET  /api/us_senti        「美股精灵」情绪与结构缓存
  POST /api/us_senti/refresh   CNN 恐惧贪婪 + 四池宽度 + 板块 / Mag7 -> 重算
  GET  /api/hk_senti        「港股全景」情绪、宽度、沽空、南向缓存
  POST /api/hk_senti/refresh   三指数温度 + 并集宽度 + 沽空 + 南向 -> 重算
  GET  /api/apac            亚太五地指数温度缓存
  POST /api/apac/refresh    日韩台印新日线 + 相对强弱 -> 重算
  GET  /api/market          「市场总览」资产 + 自选股缓存
  POST /api/market/refresh  指数 / 期货 / 外汇 / 加密 + 分时 -> 重算
  GET  /api/market/search   搜索可添加的股票 / 指数 / 期货 / 外汇 / 加密
  GET  /api/market/config   自选股与资产清单
  POST /api/market/config   保存清单（增删资产 / 导入自选股）
"""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_core as core  # noqa: E402
import apac  # noqa: E402
import hk_senti  # noqa: E402
import market_context  # noqa: E402
import sentiment  # noqa: E402
import us_etf  # noqa: E402
import us_senti  # noqa: E402
import market  # noqa: E402
import us_valuation  # noqa: E402
import valuation  # noqa: E402
import wsfeed  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "oos")
OUT = os.path.join(BASE, "output")
PAGE = os.path.join(OUT, "指数超买超卖看板.html")
LOG = os.path.join(BASE, "data", "server.log")


def _version():
    """根目录 VERSION 文件是唯一来源。缺文件时返回 unknown，不猜版本号。"""
    try:
        return open(os.path.join(BASE, "VERSION"), encoding="utf-8").read().strip() or "unknown"
    except OSError:
        return "unknown"


CODES = {"科创50": "sh000688", "创业板指": "sz399006",
         "沪深300": "sh000300", "中证红利": "sh000922"}
US_NAMES = ["QQQ", "SOXX", "标普500", "道琼斯"]   # 美股 ETF：口径见 us_etf.py 模块说明
# 落盘文件名。用 slug 而不是拼音缩写，新增标的时不必再改这里。
SLUG = {"科创50": "kc50", "创业板指": "cyb", "沪深300": "hs300", "中证红利": "zzhl",
        "QQQ": "qqq", "SOXX": "soxx", "标普500": "sp500", "道琼斯": "dji"}
HOST, PORT0 = "127.0.0.1", 8765

_lock = threading.Lock()
_senti_lock = threading.Lock()
_us_lock = threading.Lock()
_hk_lock = threading.Lock()
_apac_lock = threading.Lock()
_market_lock = threading.Lock()
_log_lock = threading.Lock()
_state = {"data": None, "val": None, "updated": None, "senti": None, "us_senti": None,
          "hk_senti": None, "apac": None}


def log(msg):
    """写 data/server.log，方便事后追查「点更新失败」到底是哪一步断的。"""
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with _log_lock:
            if os.path.exists(LOG) and os.path.getsize(LOG) > 2 * 1024 * 1024:
                os.replace(LOG, LOG + ".1")
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # noqa: BLE001  日志失败绝不能影响主流程
        pass


def westock_bin():
    """Windows 下必须用绝对路径调用，subprocess 不会自动补 .exe。

    保留此函数仅为兼容 sentiment.py 的旧引用方式；取数逻辑已统一走 wsfeed。
    """
    return wsfeed.bin_path()


def westock_env():
    return wsfeed.env()


def fetch(code, start="2021-01-01"):
    end = datetime.date.today().isoformat()
    df = core.parse_westock(wsfeed.kline(code, start, end))
    if len(df) < 300:
        raise RuntimeError("取回数据仅 %d 条，疑似接口异常" % len(df))
    return df


def load_valuation():
    p = os.path.join(DATA, "valuation.json")
    if os.path.exists(p):
        return json.loads(open(p, encoding="utf-8").read())
    return None


def load_cached():
    """启动时的首屏数据。

    优先用 result_latest.json 重新打包，而不是直接读 html_data.json——
    后者是构建产物，可能落后于最近一次刷新，导致页面拉 /api/data 后
    把内嵌的新数据覆盖回旧值。
    """
    p = os.path.join(DATA, "result_latest.json")
    if os.path.exists(p):
        try:
            res = json.loads(open(p, encoding="utf-8").read())
            return {k: core.pack(v) for k, v in res.items()}
        except Exception:  # noqa: BLE001
            pass
    p = os.path.join(DATA, "html_data.json")
    if os.path.exists(p):
        return json.loads(open(p, encoding="utf-8").read())
    return None


def refresh():
    """取数 + 重算 + 落盘，返回前端所需的精简数据。"""
    t0 = time.time()

    # 市场背景（两市成交额 + 两融）：拥挤度模块的必需分母。
    # 失败时沿用本地缓存，仍无缓存则算法自动降级为旧口径并在结果中标记。
    try:
        ctx = market_context.fetch(start="2021-01-01")
    except Exception as e:  # noqa: BLE001
        print("[警告] 市场背景更新失败，沿用缓存：%s" % e)
        ctx = market_context.load()

    results, packed = {}, {}
    for name, code in CODES.items():
        df = fetch(code)
        if ctx is not None:
            keep = [c for c in ("date", "amt_all", "lev", "rz_buy") if c in ctx.columns]
            df = df.merge(ctx[keep], on="date", how="left")
        csv = os.path.join(DATA, "%s.csv" % SLUG.get(name, name))
        df.to_csv(csv, index=False)
        res = core.run(name, df)
        results[name] = res
        packed[name] = core.pack(res)

    # 美股 ETF：独立通道，失败不应拖垮 A 股刷新，故单独兜底
    for name in US_NAMES:
        try:
            df = us_etf.fetch(name)
            df.to_csv(us_etf.path_of(name), index=False)
            res = core.run(name, df)
        except Exception as e:  # noqa: BLE001
            print("[警告] %s 更新失败，沿用缓存：%s" % (name, e))
            df = us_etf.load(name)
            if df is None:
                continue
            res = core.run(name, df)
        results[name] = res
        packed[name] = core.pack(res)

    with open(os.path.join(DATA, "result_latest.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "html_data.json"), "w", encoding="utf-8") as f:
        json.dump(packed, f, ensure_ascii=False, separators=(",", ":"))

    try:
        val = valuation.fetch_all()
        # 美股估值单独兜底：SOXX 走第三方源，挂了不能连累 A 股估值
        try:
            val.update(us_valuation.fetch_all())
        except Exception as e:  # noqa: BLE001
            log("[警告] 美股估值更新失败：%s" % e)
        with open(os.path.join(DATA, "valuation.json"), "w", encoding="utf-8") as f:
            json.dump(val, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:  # noqa: BLE001  估值源异常不应阻断行情更新
        val = load_valuation()
        log("[警告] 估值数据更新失败：%s" % e)

    _state["data"] = packed
    _state["val"] = val
    _state["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return packed, val, round(time.time() - t0, 1)


def refresh_senti():
    """「短线精灵」频道：情绪周期复盘口径。

    与指标刷新完全独立（两把锁、两套数据），因为它取的是另一个维度的东西——
    市场总览与龙虎榜，而不是指数日线。
    """
    t0 = time.time()
    p = sentiment.build()
    sentiment.save(p)
    _state["senti"] = p
    return p, round(time.time() - t0, 1)


def refresh_us_senti():
    """「美股精灵」频道：CNN 恐惧贪婪 + 四池宽度 + 板块结构。

    第三把锁。美股与 A 股交易时段不同，刷新互不依赖，不能共用锁——
    否则 A 股盘中点更新时，美股频道会被 429 挡掉。
    """
    t0 = time.time()
    p = us_senti.build()
    us_senti.save(p)
    _state["us_senti"] = p
    return p, round(time.time() - t0, 1)


def refresh_hk_senti():
    """「港股全景」频道：三指数温度 + 并集宽度 + 沽空 + 南向。

    第四把锁。港股与 A 股交易时段部分重叠（09:30-16:00 对 09:30-15:00），
    但取数完全走 westock 的港股接口，和 A 股通道不是同一批请求，仍然独立加锁。
    """
    t0 = time.time()
    p = hk_senti.build()
    hk_senti.save(p)
    _state["hk_senti"] = p
    return p, round(time.time() - t0, 1)


def refresh_market():
    """「市场总览」频道：资产卡 + 自选股矩阵。

    第六把锁。三条 westock 通道（股票 / 期货 / 外汇）必须分开请求——混批时
    期货与外汇会被静默丢弃且退出码仍是 0，这条约束写在 market.py 里。
    """
    t0 = time.time()
    d = market.build()
    _state["market"] = d
    return d, round(time.time() - t0, 1)


def refresh_apac():
    """「亚太温度」频道：日韩台印新五地指数卡 + 相对强弱。

    第五把锁。取数走新浪 gi（不走 westock），与其它四条通道没有共享请求，
    单独加锁是为了不让一次 30 秒的全量刷新把其它页的按钮一起堵住。
    """
    t0 = time.time()
    p = apac.build()
    apac.save(p)
    _state["apac"] = p
    return p, round(time.time() - t0, 1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # 必须配合 Content-Length 使用，否则浏览器会挂住

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/看板"):
            try:
                self._send(200, open(PAGE, "rb").read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, json.dumps({"error": "页面文件不存在"}, ensure_ascii=False))
            return
        if path == "/api/data":
            self._send(200, json.dumps({"ok": True, "data": _state["data"],
                                        "val": _state["val"],
                                        "updated": _state["updated"]}, ensure_ascii=False))
            return
        if path == "/api/sentiment":
            self._send(200, json.dumps({"ok": True, "senti": _state["senti"]},
                                       ensure_ascii=False))
            return
        if path == "/api/us_senti":
            self._send(200, json.dumps({"ok": True, "us": _state["us_senti"]},
                                       ensure_ascii=False))
            return
        if path == "/api/hk_senti":
            self._send(200, json.dumps({"ok": True, "hk": _state["hk_senti"]},
                                       ensure_ascii=False))
            return
        if path == "/api/apac":
            self._send(200, json.dumps({"ok": True, "apac": _state["apac"]},
                                       ensure_ascii=False))
            return
        if path == "/api/market":
            self._send(200, json.dumps({"ok": True, "market": _state["market"]},
                                       ensure_ascii=False))
            return
        if path == "/api/market/search":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)   # 查询串在 self.path 上，path 已被截断
            q = (qs.get("q") or [""])[0]
            kind = (qs.get("kind") or ["stock"])[0]
            try:
                items = market.search(q, kind)
                self._send(200, json.dumps({"ok": True, "items": items},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"ok": False, "items": [], "error": str(e)},
                                           ensure_ascii=False))
            return
        if path == "/api/market/config":
            self._send(200, json.dumps({"ok": True, "config": market.load_config()},
                                       ensure_ascii=False))
            return
        if path == "/api/ping":
            self._send(200, json.dumps({"ok": True, "version": _version()}))
            return
        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    def do_POST(self):
        # HTTP/1.1 keep-alive 下必须把请求体读干净，否则下一个请求会解析错位。
        # 读出来的内容留在 _body 里：请求体只能读一次，后面再 self.rfile.read()
        # 拿到的是空串——配置接口最初就是这么踩的，表现为 400 且客户端无响应。
        try:
            n = int(self.headers.get("Content-Length") or 0)
            _body = self.rfile.read(n) if n > 0 else b""
        except Exception:  # noqa: BLE001
            _body = b""

        path = self.path.split("?")[0]
        if path == "/api/sentiment/refresh":
            if not _senti_lock.acquire(blocking=False):
                self._send(429, json.dumps({"ok": False, "error": "情绪复盘正在更新中"},
                                           ensure_ascii=False))
                return
            try:
                p, secs = refresh_senti()
                self._send(200, json.dumps({"ok": True, "senti": p, "elapsed": secs},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                log("[错误] 情绪刷新失败：%s\n%s" % (e, traceback.format_exc()))
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            finally:
                _senti_lock.release()
            return
        if path == "/api/us_senti/refresh":
            if not _us_lock.acquire(blocking=False):
                self._send(429, json.dumps({"ok": False, "error": "美股精灵正在更新中"},
                                           ensure_ascii=False))
                return
            try:
                p, secs = refresh_us_senti()
                self._send(200, json.dumps({"ok": True, "us": p, "elapsed": secs},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                log("[错误] 美股精灵刷新失败：%s\n%s" % (e, traceback.format_exc()))
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            finally:
                _us_lock.release()
            return
        if path == "/api/hk_senti/refresh":
            if not _hk_lock.acquire(blocking=False):
                self._send(429, json.dumps({"ok": False, "error": "港股全景正在更新中"},
                                           ensure_ascii=False))
                return
            try:
                p, secs = refresh_hk_senti()
                self._send(200, json.dumps({"ok": True, "hk": p, "elapsed": secs},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                log("[错误] 港股全景刷新失败：%s\n%s" % (e, traceback.format_exc()))
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            finally:
                _hk_lock.release()
            return
        if path == "/api/apac/refresh":
            if not _apac_lock.acquire(blocking=False):
                self._send(429, json.dumps({"ok": False, "error": "亚太温度正在更新中"},
                                           ensure_ascii=False))
                return
            try:
                p, secs = refresh_apac()
                self._send(200, json.dumps({"ok": True, "apac": p, "elapsed": secs},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                log("[错误] 亚太温度刷新失败：%s\n%s" % (e, traceback.format_exc()))
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            finally:
                _apac_lock.release()
            return
        if path == "/api/market/refresh":
            if not _market_lock.acquire(blocking=False):
                self._send(429, json.dumps({"ok": False, "error": "市场总览正在更新中"},
                                           ensure_ascii=False))
                return
            try:
                d, secs = refresh_market()
                self._send(200, json.dumps({"ok": True, "market": d, "elapsed": secs},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                log("[错误] 市场总览刷新失败：%s\n%s" % (e, traceback.format_exc()))
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            finally:
                _market_lock.release()
            return
        if path == "/api/market/config":
            try:
                body = json.loads(_body.decode("utf-8") or "{}")
                cfg = market.load_config()
                if "assets" in body:
                    cfg["assets"] = body["assets"]
                if "watchlist" in body:
                    cfg["watchlist"] = body["watchlist"]
                market.save_config(cfg)
                self._send(200, json.dumps({"ok": True, "config": cfg},
                                           ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(400, json.dumps({"ok": False, "error": str(e)},
                                           ensure_ascii=False))
            return
        if path != "/api/refresh":
            self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))
            return
        if not _lock.acquire(blocking=False):
            log("[拒绝] 指标刷新：已有任务在进行中")
            self._send(429, json.dumps({"ok": False, "error": "已有更新任务在进行中"},
                                       ensure_ascii=False))
            return
        try:
            packed, val, secs = refresh()
            self._send(200, json.dumps({"ok": True, "data": packed, "val": val,
                                        "updated": _state["updated"],
                                        "elapsed": secs}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log("[错误] 指标刷新失败：%s\n%s" % (e, traceback.format_exc()))
            self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        finally:
            _lock.release()


def main():
    if not os.path.exists(PAGE):
        print("[错误] 未找到页面文件：%s" % PAGE)
        sys.exit(1)
    _state["data"] = load_cached()
    _state["val"] = load_valuation()
    _state["senti"] = sentiment.load()
    _state["us_senti"] = us_senti.load()
    _state["hk_senti"] = hk_senti.load()
    _state["apac"] = apac.load()
    _state["market"] = market.load()

    port = PORT0
    for p in range(PORT0, PORT0 + 12):
        try:
            httpd = ThreadingHTTPServer((HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    else:
        print("[错误] 8765-8776 端口均被占用")
        sys.exit(1)

    print("看板服务已启动： http://%s:%d/   (v%s)" % (HOST, port, _version()))
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
