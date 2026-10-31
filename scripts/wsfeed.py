"""westock CLI 的取数封装：限频重试 + 调用节流 + 行数完整性校验。

为什么需要这一层
----------------
westock 服务端对连续 K 线请求有频率限制，触发后有两种表现：

1. **显式报错**：正常退出码 + 一段错误文本
   （`查询dayK线失败：[code=1620053006] error_type=2 suggest=2 msg=服务限频`）。
2. **静默截断**：不报错，但只返回 245~420 行，日期区间还会随机跳。
   第二种更危险——上层只看到"行数偏少"，到指标层才因为滚动窗口凑不齐而炸掉，
   报出来的错（IndexError: single positional indexer is out-of-bounds）与真实原因无关。

因此这一层做三件事：

1. **节流**：任意两次调用之间至少间隔 MIN_GAP 秒；
2. **显式 limit**：始终带 `--limit`，避免默认上限把历史截断；
3. **完整性校验**：按请求的日期跨度估算应有的交易日数，返回不足 70% 即视为
   截断并重试。这条是专门为"静默截断"设的——只看 returncode 发现不了。

节流是**进程级**的（模块级变量），A 股与美股通道共用同一节奏——它们在同一个
刷新里串行执行，本来就该排队。
"""
import os
import re
import subprocess
import time

MIN_GAP = 1.2          # 两次调用最小间隔（秒）
MAX_TRY = 5            # 含首次
LIMIT = 5000           # 显式上限，远大于任何标的的历史长度
FILL_RATIO = 0.70      # 实际行数 / 估算交易日数 低于此值视为截断
_last = [0.0]

ERROR_MARKS = ("服务限频", "限频", "查询dayK线失败", "查询失败")
DATE_RE = re.compile(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|")


def bin_path():
    for p in ("~/.local/bin/westock.exe", "~/.local/bin/westock"):
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return "westock"


def env():
    e = os.environ.copy()
    extra = os.path.expanduser("~/.local/bin")
    if extra not in e.get("PATH", ""):
        e["PATH"] = e.get("PATH", "") + os.pathsep + extra
    return e


def _throttle():
    gap = time.time() - _last[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last[0] = time.time()


def count_rows(text):
    return sum(1 for ln in (text or "").splitlines() if DATE_RE.match(ln))


def expected_rows(start, end):
    """按日历跨度估交易日数：一年约 243 个交易日，占 365 天的 0.666。"""
    import datetime as dt
    try:
        d0 = dt.date.fromisoformat(start)
        d1 = dt.date.fromisoformat(end)
    except ValueError:
        return 0
    return max(1, int((d1 - d0).days * 243 / 365))


def _looks_failed(text, returncode):
    if returncode != 0:
        return True
    return any(m in (text or "") for m in ERROR_MARKS)


def kline(code, start, end, timeout=180, strict=True):
    """取日线原始文本。

    strict=True：行数不足即视为截断并退避重试，耗尽后抛异常（上层走缓存兜底）。
    strict=False：只做限频重试，不校验行数——供调用方自己做分年拼接时使用，
                  因为单年份请求本就只需要那一年的行数。
    """
    cmd = [bin_path(), "kline", code, "--period", "day",
           "--start", start, "--end", end, "--limit", str(LIMIT)]
    need = int(expected_rows(start, end) * FILL_RATIO)
    last_msg = ""
    for i in range(MAX_TRY):
        _throttle()
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env(), shell=False)
        text = (r.stdout or "") + (r.stderr or "")
        if not _looks_failed(text, r.returncode):
            rows = count_rows(text)
            if not strict or rows >= need:
                return text
            last_msg = "仅返回 %d 行（期望约 %d 行），疑似截断" % (rows, need)
        else:
            last_msg = text.strip().splitlines()[0][:120] if text.strip() else "未知错误"
        if i < MAX_TRY - 1:
            wait = 3 * (i + 1)
            print("[重试] %s %s，%d 秒后重试（%d/%d）" % (code, last_msg, wait, i + 1, MAX_TRY - 1))
            time.sleep(wait)
    raise RuntimeError("%s 取数失败（已重试 %d 次）：%s" % (code, MAX_TRY, last_msg))
