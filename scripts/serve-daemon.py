"""登录自启用：把 server.py 跑起来，控制台输出落到 data/server.stdout.log。

为什么不直接在任务计划里写 `python scripts\\server.py >> log 2>&1`：

1. 那要经过 cmd 的引号与重定向规则——`/c ""` 开头的拼接在路径含空格时极易拼坏，
   而拼坏的表现是「进程起来又立刻消失、日志里什么也没有」，非常难查；
2. `pythonw.exe` 下 `sys.stdout` 是 `None`，而 server.py 启动时就 `print()`，
   第一句就会抛 AttributeError 直接退出；`python.exe` 配无窗口则拿不到有效句柄。

让 Python 自己做重定向，这两个坑都不用踩：这里先把 stdout/stderr 换成文件，
再用 runpy 以 `__main__` 身份跑 server.py，行为与命令行启动完全一致。

业务日志仍由 server.py 自己写 data/server.log；本文件只收「没被接住的控制台输出」。
"""

import os
import runpy
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(BASE, "data", "server.stdout.log")
SERVER = os.path.join(BASE, "scripts", "server.py")


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    # buffering=1（行缓冲）：自启时没人盯着，出问题要能立刻在文件里看到那一行，
    # 而不是等进程退出才刷盘——server.py 一旦起失败就再也不会退出。
    f = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
    runpy.run_path(SERVER, run_name="__main__")


if __name__ == "__main__":
    main()
