# 工作室起不来

1. 终端说没有 uv：安装 uv，再双击 `GXR Studio.command` 或跑 `make dev`。
2. 退出码 75，或文字是「工作室已经开着了」：回到已经开着的窗口。确认没有残留进程后，锁在 `<图库>/.studio/studio.lock`，进程结束会自己放开，不要删图库。
3. 页面打不开：用终端里印出来的那条带 `token` 的链接。自己拼的地址会 401。
4. 端口被占用：`uv run --frozen gxr-studio --port 0 --no-browser` 会换一个随机端口。
