# ChatGPT Web Local API

把**已登录的网页版 ChatGPT** 暴露成本地 OpenAI 兼容 API。

## 能力

| 功能 | 方式 | 稳定性 |
|------|------|--------|
| 对话 `/v1/chat/completions` | 自动读取 **Chrome** Cookie + backend-api | 中等（可能触发风控） |
| 图片 `/v1/images/generations` | 连接 **Chrome** 调试窗口 (CDP) | 较高（真实浏览器执行） |

## 重要说明

- 这是**非官方**用法，可能违反 OpenAI 服务条款，存在封号风险。
- Cookie 方式不如官方 API 稳定；若对话失败，请改用 CDP 模式或现成项目 [ChatGPT-Web2API](https://github.com/Octo-Lex/ChatGPT-Web2API)。
- 图片生成必须走浏览器，因为网页端有 Turnstile / PoW 校验。

## 安装

```powershell
cd C:\path\to\chatgpt-web-api
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 启动

### 1. 启动 API 服务

```powershell
$env:CHATGPT_API_KEY = "local-dev-key"
$env:CHATGPT_BROWSER = "chrome"
python -m app.main
```

默认地址：`http://127.0.0.1:8765`

### 2. 图片生成：用 Chrome 开调试窗口

**你的 Cookie 在 Chrome 里**，图片也要走 Chrome。

#### 方案 A（推荐）：复用你已登录的 Chrome 配置

1. **完全退出 Chrome**（任务栏右键 → 退出，不是只关窗口）
2. 运行：

```powershell
.\start-browser-existing.ps1
```

这会打开带 CDP 的 Chrome，并复用 `%LOCALAPPDATA%\Google\Chrome\User Data`，一般**不用重新登录**。

#### 方案 B：独立 Chrome 配置（需重新登录一次）

```powershell
.\start-browser.ps1
```

配置目录：`%LOCALAPPDATA%\chatgpt-web-api-chrome`

检查 CDP：

```powershell
curl http://127.0.0.1:8765/v1/images/diagnostics -H "Authorization: Bearer local-dev-key"
```

看到 `"reachable": true` 后再调用图片接口。

## 调用示例

### 检查会话是否读取成功

```powershell
curl http://127.0.0.1:8765/v1/session `
  -H "Authorization: Bearer local-dev-key"
```

### 对话

```powershell
curl http://127.0.0.1:8765/v1/chat/completions `
  -H "Authorization: Bearer local-dev-key" `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好，介绍一下你自己\"}]}"
```

### Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",
    api_key="local-dev-key",
)

print(client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "1+1=?"}],
).choices[0].message.content)
```

### 图片生成

```powershell
curl http://127.0.0.1:8765/v1/images/generations `
  -H "Authorization: Bearer local-dev-key" `
  -H "Content-Type: application/json" `
  -d "{\"prompt\":\"a cute cat wearing sunglasses, cartoon style\"}"
```

生成图片保存在 `output/` 目录。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CHATGPT_API_HOST` | `127.0.0.1` | 监听地址 |
| `CHATGPT_API_PORT` | `8765` | 端口 |
| `CHATGPT_API_KEY` | `local-dev-key` | 本地鉴权 key |
| `CHATGPT_BROWSER` | `chrome` | Cookie/调试浏览器：`chrome` / `edge` |
| `CHATGPT_BROWSER_PROFILE` | 见上文 | 调试浏览器 profile 目录 |
| `CHATGPT_CDP_URL` | `http://127.0.0.1:9222` | 图片生成用的 CDP 地址 |
| `CHATGPT_IMAGE_DELETE` | `true` | 出图后自动删除 ChatGPT 会话 |
| `CHATGPT_IMAGE_MAX_CONCURRENT` | `1` | 最大并行出图数（建议 1，防限流） |

## 多任务 / 批量出图

默认每次出图在**独立标签页**执行，完成后**自动删除会话**（可通过 `keep_conversation: true` 保留）。

### 单张（同步，默认）

```powershell
curl http://127.0.0.1:8765/v1/images/generations `
  -H "Authorization: Bearer local-dev-key" `
  -H "Content-Type: application/json" `
  -d "{\"prompt\":\"a cute cat\"}"
```

### 单张（异步，立即返回 job_id）

```powershell
curl http://127.0.0.1:8765/v1/images/generations `
  -H "Authorization: Bearer local-dev-key" `
  -H "Content-Type: application/json" `
  -d "{\"prompt\":\"a cute cat\",\"async_mode\":true}"

# 轮询状态
curl http://127.0.0.1:8765/v1/images/jobs/<job_id> -H "Authorization: Bearer local-dev-key"
```

### 批量（排队执行，适合多个任务）

```powershell
curl http://127.0.0.1:8765/v1/images/generations/batch `
  -H "Authorization: Bearer local-dev-key" `
  -H "Content-Type: application/json" `
  -d "{\"prompts\":[\"a red apple\",\"a blue sky\",\"a green tree\"]}"
```

### 查看队列状态

```powershell
curl http://127.0.0.1:8765/v1/images/queue/stats -H "Authorization: Bearer local-dev-key"
```

## Cookie 读取失败时

1. 确认 **Chrome** 里已登录 `https://chatgpt.com`
2. **完全退出 Chrome** 后再启动 API（Windows 会锁 Cookie 数据库）
3. 确认环境变量：`$env:CHATGPT_BROWSER="chrome"`

## 更省事的替代方案

若你只想尽快用起来，不一定要自己维护：

- [ChatGPT-Web2API](https://github.com/Octo-Lex/ChatGPT-Web2API) — 自动开 Chrome，对话 + MCP
- [chatgpt-free-api](https://github.com/kodelyx/chatgpt-free-api) — Go 服务 + Chrome 扩展同步 Cookie
- [auth2api](https://github.com/dev-longshun/auth2api) — OAuth 登录，偏 Codex 后端

## 回滚 / 安全

- 服务只监听本机，不要把端口暴露到公网。
- 不会上传 Cookie；只在本地读取并调用 ChatGPT。

