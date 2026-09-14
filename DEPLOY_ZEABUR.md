# Zeabur 远程 MCP 部署

本 fork 保留上游 `stdio` / `http` 行为，并额外提供一个面向公网部署的安全入口：

```bash
hefeng-qweather-mcp-cloud
```

Dockerfile 默认启动的就是该入口。

## 必填环境变量

在 Zeabur 服务中配置：

```text
HEFENG_API_HOST=<你的和风天气 API Host，不要附加路径>
HEFENG_API_KEY=<你的和风天气 API Key>
MCP_ACCESS_TOKEN=<至少 24 位的随机长字符串>
```

不要把任何真实 Key 或 Token 提交到 GitHub。

Zeabur 会注入 `PORT`；服务默认监听 `0.0.0.0:$PORT`。

可选：

```text
ENABLE_PAID_WEATHER_TOOLS=false
```

默认即为 `false`。此时不会向 MCP 客户端暴露：

- `get_storm_list`
- `get_storm_track`
- `get_storm_forecast`

只有在和风账号明确开通热带气旋付费 API 后，才应设为 `true`。

## 端点

部署完成并绑定公网域名后：

```text
Health: https://<你的域名>/health
MCP:    https://<你的域名>/mcp
```

`/health` 不需要认证；其余 HTTP 请求要求：

```text
Authorization: Bearer <MCP_ACCESS_TOKEN>
```

## OrangeChat / 橘瓣

新增远程 MCP：

- Transport: `Streamable HTTP`
- URL: `https://<你的域名>/mcp`
- Header:
  - Name: `Authorization`
  - Value: `Bearer <MCP_ACCESS_TOKEN>`

保存后刷新工具列表即可。

## Gateway（以后接入）

Gateway 只需要把同一个 `/mcp` 作为远程 MCP 服务消费，并携带相同 Authorization Header。此仓库的云部署不要求修改 Gateway 主仓库。

## 安全说明

- 云入口在 `MCP_ACCESS_TOKEN` 缺失或短于 24 字符时会直接拒绝启动，避免误把 MCP 裸奔到公网。
- 云入口导入上游实现时会抑制 INFO 日志，避免上游代码把 API Key 前缀写入部署日志。
- API Key 仍只存在于运行时环境变量中。
