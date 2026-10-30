# 启动之后看哪里

工作室没有远程仪表盘。一次启动好不好，看这几处：

- 终端里的启动行：URL、端口、图库路径。第二个实例会说已经开着并以 75 退出。
- `GET /api/health`：`{"ok": true, "version": ...}`，并带 `X-Request-ID`。
- `GET /api/metrics`：这个进程里的请求数和状态码，Prometheus 文本。
- `GET /api/usage`：按路由记的本地使用次数，不上传。
- `GET /api/alerts`：失败任务的告警。同一条也会以 JSON 打到 stderr。
- `<图库>/.studio/import-log.jsonl`：导入追加日志。

这些接口和页面一样要令牌。请求号从通过访问控制的响应头 `X-Request-ID` 来，日志里的 `request_id` 是同一个值。
