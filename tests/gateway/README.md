# tests/gateway — 薄网关适配层契约测试

`packaging/fpk/app/runtime/gateway/gateway_adapter.py` 的行为测试（stdlib
unittest，不依赖第三方库）。

## 运行

```bash
python -m unittest discover -s tests -v
# 只跑网关用例
python -m unittest tests.gateway.test_adapter tests.gateway.test_contract -v
```

## 覆盖

| 文件 | 内容 |
|---|---|
| `test_adapter.py` | 纯函数（前缀剥离严格边界、CSP 只改 `frame-ancestors`、XFO、header 构造/重写、`--help/--version`）在全部平台运行；AF_UNIX 集成用例仅在 Linux CI 运行（Windows 自动跳过）。集成用例覆盖：响应体逐字节相等、CSP 仅 `frame-ancestors 'none'→'self'`、Location/Set-Cookie 原样、X-Forwarded-Prefix 注入、Cookie/Authorization 透传、301/302、HEAD/204/304、SSE 流式、1MB 大响应、chunked 请求解码、header/body 超限、健康端点、路径边界、WebSocket 101+回显。 |
| `test_contract.py` | `scripts/check_gateway_contract.py` 的契约用例：gateway.conf / ui/config / cmd/main / adapter 三方一致通过，以及 conf 前缀不符、缺 conf、入口属性不符、main 不读 conf、main 引用旧代理、缺 adapter、exit 2 等坏例。 |

## fixture

无提交的二进制或网络资源：纯函数用例直接 import adapter；集成用例用
`http.server.ThreadingHTTPServer` 起 fake upstream（含最小 WS 握手/回显），
adapter 以 `ProxyServer` 在临时 AF_UNIX socket 上运行。
