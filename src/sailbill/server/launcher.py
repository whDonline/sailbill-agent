"""桌面版启动器：双击即用的入口。

启动本地 uvicorn 服务（仅监听 127.0.0.1），就绪后自动打开默认浏览器；
Ctrl+C 优雅退出。端口从 8686 起依次探测，避免冲突。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

_PORT_BASE = 8686


def _free_port() -> int:
    """从 8686 开始找一个空闲端口。"""
    for port in range(_PORT_BASE, _PORT_BASE + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return _PORT_BASE


def main() -> int:
    import uvicorn

    from .app import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(50):  # 最多等 10 秒，服务就绪后再开浏览器
        if server.started:
            break
        time.sleep(0.2)
    print(f"[sailbill] 桌面版已启动：{url}（Ctrl+C 退出）")
    webbrowser.open(url)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[sailbill] 正在退出…")
        server.should_exit = True
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
