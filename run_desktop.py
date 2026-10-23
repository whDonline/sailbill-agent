"""PyInstaller 打包入口：dist/sailbill.exe 双击启动桌面版。"""

from sailbill.server.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
