"""`uv run gxr-studio`：只监听 127.0.0.1，端口默认随机，启动时打印带令牌的链接。"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import webbrowser

import uvicorn

from .app import create_app
from .config import Settings
from .library.lock import BUSY_EXIT_CODE, LibraryBusy, LibraryLock


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="gxr-studio", description="GXR 电脑工作室（只监听本机）")
    ap.add_argument("--port", type=int, default=0, help="端口，0 = 随机")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    try:
        lock = LibraryLock(settings.library_dir).acquire()   # 进程结束时系统自动放锁
    except LibraryBusy:
        print("工作室已经开着了：请回到第一个打开的窗口（或浏览器页面）继续用。", flush=True)
        print("如果确认没有开着，请先关掉所有“GXR Studio”的终端窗口再试。", flush=True)
        sys.exit(BUSY_EXIT_CODE)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    settings = settings.with_(port=port)
    app = create_app(settings)
    url = f"http://127.0.0.1:{port}/?token={settings.token}"
    print(f"GXR 工作室已启动：{url}", flush=True)
    print(f"图库：{settings.library_dir}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    # 直接关掉终端窗口会发 SIGHUP：按正常退出处理，这样拟合子进程等清理才会执行（默认动作是立刻死掉，什么都不清理）
    signal.signal(signal.SIGHUP, lambda *_: setattr(server, "should_exit", True))
    try:
        server.run(sockets=[sock])
    finally:
        lock.release()


if __name__ == "__main__":
    main()
