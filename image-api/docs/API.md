# ChatGPT Web Local API 说明文档

将网页版 ChatGPT（Playwright 驱动）暴露为 **OpenAI 兼容** 的本地 HTTP API，支持局域网访问。

---

## 基本信息

| 项目 | 值 |
|------|-----|
| 默认地址 | `http://<服务器IP>:8765` |
| 本机示例 | `http://127.0.0.1:8765` |
| 局域网示例 | `http://172.26.11.2:8765`（以实际 IP 为准） |
| 协议 | HTTP |
| 兼容格式 | OpenAI API 子集 |
| 默认 API Key | `local-dev-key` |

> 图片文件保存在**服务器本机**的 `output/` 目录；局域网客户端拿到的是 `file://` 路径，需自行从服务器拷贝或通过共享目录访问。

---

## 鉴权

除 `/health` 外，所有接口需在请求头携带：

```http
Authorization: Bearer local-dev-key
```

错误时返回：

| HTTP 状态码 | 说明 |
|-------------|------|
| `401` | API Key 无效 |
| `404` | 资源不存在 |
| `500` | 服务器内部错误（如 Cookie 读取失败） |
| `502` | 上游 ChatGPT / 浏览器执行失败 |
| `501` | 功能未实现 |

---

## 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（无需鉴权） |
| GET | `/v1/session` | 查看 Cookie 会话状态 |
| POST | `/v1/chat/completions` | 对话（OpenAI 兼容） |
| POST | `/v1/images/generations` | 单张图片生成 |
| POST | `/v1/images/generations/batch` | 批量图片生成 |
| GET | `/v1/images/jobs/{job_id}` | 查询异步图片任务 |
| GET | `/v1/images/queue/stats` | 图片任务队列统计 |
| GET | `/v1/images/diagnostics` | 浏览器 CDP 诊断 |

---

## 1. 健康检查

```http
GET /health
```

**响应示例：**

```json
{"status": "ok"}
```

---

## 2. 对话

兼容 OpenAI `POST /v1/chat/completions`。

```http
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer local-dev-key
```

**请求体：**

```json
{
  "model": "auto",
  "messages": [
    {"role": "user", "content": "你好，介绍一下你自己"}
  ],
  "stream": false,
  "conversation_id": null
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 否 | 默认 `auto` |
| `messages` | array | 是 | 消息列表，`role` 支持 `user` / `assistant` / `system` |
| `stream` | boolean | 否 | 暂不支持 `true`，传 `true` 返回 `501` |
| `conversation_id` | string | 否 | 多轮对话 ID（Cookie 模式有效） |

**响应示例：**

```json
{
  "id": "chatcmpl-xxxxxxxx",
  "object": "chat.completion",
  "created": 1787626452,
  "model": "auto",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！我是……"
      },
      "finish_reason": "stop"
    }
  ],
  "conversation_id": "00000000-0000-0000-0000-000000000000"
}
```

**调用逻辑：**

1. 优先尝试读取 Chrome Cookie 直连 ChatGPT backend-api
2. 失败则自动切换 Playwright 浏览器模式（需服务器已登录 ChatGPT）

**curl 示例（局域网）：**

```bash
curl http://172.26.11.2:8765/v1/chat/completions \
  -H "Authorization: Bearer local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"1+1=?"}]}'
```

**Python 示例：**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://172.26.11.2:8765/v1",
    api_key="local-dev-key",
)

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

**JavaScript 示例：**

```javascript
const resp = await fetch("http://172.26.11.2:8765/v1/chat/completions", {
  method: "POST",
  headers: {
    Authorization: "Bearer local-dev-key",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "auto",
    messages: [{ role: "user", content: "你好" }],
  }),
});
const data = await resp.json();
console.log(data.choices[0].message.content);
```

---

## 3. 单张图片生成

```http
POST /v1/images/generations
Content-Type: application/json
Authorization: Bearer local-dev-key
```

**请求体：**

```json
{
  "prompt": "a cute cat wearing sunglasses, cartoon style",
  "model": "gpt-image-1",
  "size": null,
  "keep_conversation": false,
  "async_mode": false,
  "reference_image_base64": null,
  "reference_image_url": null,
  "reference_image_path": null
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | 是 | 图片描述 |
| `model` | string | 否 | 兼容字段，当前未实际路由 |
| `size` | string | 否 | 兼容字段，暂未使用 |
| `keep_conversation` | boolean | 否 | 默认 `false`，出图后自动删除 ChatGPT 会话 |
| `async_mode` | boolean | 否 | 默认 `false`；`true` 时立即返回任务 ID |
| `reference_image_base64` | string | 否 | 参考图 Base64，支持 `data:image/png;base64,...` 或纯 Base64 |
| `reference_image_url` | string | 否 | 参考图 URL（服务端下载） |
| `reference_image_path` | string | 否 | 参考图本地路径（**仅 API 服务器本机**可访问） |

> **垫图**：以上三个参考图字段三选一。提供参考图后，ChatGPT 会基于该图 + `prompt` 生成新图。例如「保持人物姿势，换成水彩风格」。

### 3.0 垫图示例

**Base64（推荐局域网客户端使用）：**

```json
{
  "prompt": "保持参考图中的人物姿势，改成赛博朋克风格",
  "reference_image_base64": "data:image/png;base64,iVBORw0KGgo..."
}
```

**服务器本地路径：**

```json
{
  "prompt": "把这张图画成卡通风格",
  "reference_image_path": "C:\\path\\to\\input.png"
}
```

**URL：**

```json
{
  "prompt": "参考这张图的风格，画一只猫",
  "reference_image_url": "https://example.com/style-ref.jpg"
}
```

### 3.1 同步模式（默认）

等待生成完成后返回，耗时通常 **30–120 秒**。

**响应示例：**

```json
{
  "created": 1787626452,
  "data": [
    {
      "url": "file:///C:/path/to/output/image_xxx.png",
      "local_path": "C:\\path\\to\\output\\image_xxx.png"
    }
  ]
}
```

**curl 示例：**

```bash
curl http://172.26.11.2:8765/v1/images/generations \
  -H "Authorization: Bearer local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a cute cat wearing sunglasses"}'
```

### 3.2 异步模式

适合长任务，立即返回，客户端轮询状态。

**请求：**

```json
{"prompt": "a cute cat", "async_mode": true}
```

**响应：**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "poll_url": "/v1/images/jobs/550e8400-e29b-41d4-a716-446655440000"
}
```

**轮询：**

```bash
curl http://172.26.11.2:8765/v1/images/jobs/550e8400-e29b-41d4-a716-446655440000 \
  -H "Authorization: Bearer local-dev-key"
```

**完成时响应：**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "prompt": "a cute cat",
  "status": "completed",
  "created_at": 1787626452.1,
  "started_at": 1787626453.0,
  "finished_at": 1787626510.5,
  "local_path": "E:\\...\\output\\image_1787626510.png",
  "url": "file:///E:/.../output/image_1787626510.png"
}
```

**任务状态枚举：**

| 状态 | 说明 |
|------|------|
| `queued` | 排队中 |
| `running` | 生成中 |
| `completed` | 成功 |
| `failed` | 失败，见 `error` 字段 |

---

## 4. 批量图片生成

一次提交多个 prompt，服务端**排队**执行（默认并发 1）。

```http
POST /v1/images/generations/batch
Content-Type: application/json
Authorization: Bearer local-dev-key
```

**请求体：**

```json
{
  "prompts": [
    "a red apple on white background",
    "a blue sky with clouds",
    "a green tree in spring"
  ],
  "keep_conversation": false
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompts` | string[] | 是 | 1–20 条 prompt |
| `keep_conversation` | boolean | 否 | 是否保留 ChatGPT 会话 |

**响应示例：**

```json
{
  "created": 1787626452,
  "data": [
    {
      "id": "uuid-1",
      "prompt": "a red apple on white background",
      "status": "completed",
      "local_path": "E:\\...\\output\\image_1.png",
      "url": "file:///E:/.../output/image_1.png"
    },
    {
      "id": "uuid-2",
      "prompt": "a blue sky with clouds",
      "status": "failed",
      "error": "等待图片生成超时……"
    }
  ]
}
```

**curl 示例：**

```bash
curl http://172.26.11.2:8765/v1/images/generations/batch \
  -H "Authorization: Bearer local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"prompts":["red apple","blue sky","green tree"]}'
```

---

## 5. 队列统计

```http
GET /v1/images/queue/stats
Authorization: Bearer local-dev-key
```

**响应示例：**

```json
{
  "max_concurrent": 1,
  "jobs": {
    "queued": 2,
    "running": 1,
    "completed": 10,
    "failed": 0
  }
}
```

---

## 6. 会话诊断

```http
GET /v1/session
Authorization: Bearer local-dev-key
```

检查 Chrome Cookie 是否可读（新版 Chrome 可能失败，不影响 Playwright 模式）。

**响应示例：**

```json
{
  "browser": "chrome-rookiepy",
  "email": "user@example.com",
  "expires_at": 1787700000.0,
  "has_token": true
}
```

---

## 环境变量（服务器端）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CHATGPT_API_HOST` | `0.0.0.0` | 监听地址 |
| `CHATGPT_API_PORT` | `8765` | 端口 |
| `CHATGPT_API_KEY` | `local-dev-key` | API 鉴权密钥 |
| `CHATGPT_BROWSER` | `chrome` | Cookie 读取目标浏览器 |
| `CHATGPT_IMAGE_DELETE` | `true` | 出图后自动删会话 |
| `CHATGPT_IMAGE_MAX_CONCURRENT` | `1` | 最大并行出图数 |

**启动示例：**

```powershell
$env:CHATGPT_API_HOST = "0.0.0.0"
$env:CHATGPT_API_KEY = "local-dev-key"
$env:CHATGPT_BROWSER = "chrome"
cd C:\path\to\chatgpt-web-api
python -m app.main
```

---

## 架构说明

```
局域网客户端 ──HTTP──► API 服务器 (8765)
                           │
                           ├─ 对话：Cookie 模式 / Playwright 浏览器
                           │
                           └─ 图片：任务队列 → 独立 Chrome 标签页 → output/
```

- **对话、图片均依赖服务器本机 Chrome**（Playwright 配置目录 `.chrome-profile`）
- 局域网机器只发 HTTP 请求，**不会**在客户端打开浏览器
- 图片生成时服务器可能弹出 Chrome 窗口，属正常现象

---

## 注意事项

1. **首次使用**：需在服务器 Playwright 弹出的 Chrome 中登录 ChatGPT（只需一次）
2. **图片路径**：`local_path` 是服务器本地路径，局域网客户端需通过文件共享/SMB 等方式获取
3. **并发限制**：网页端出图建议 `CHATGPT_IMAGE_MAX_CONCURRENT=1`，过高可能触发 ChatGPT 限流
4. **会话清理**：默认每次出图后删除 ChatGPT 会话；保留请设 `keep_conversation: true`
5. **安全**：仅限可信内网使用；不要将 8765 端口暴露到公网
6. **非官方接口**：使用网页版自动化，存在封号与服务变更风险

---

## 在线文档

服务启动后也可访问自动生成的 Swagger UI：

```
http://<服务器IP>:8765/docs
```

ReDoc：

```
http://<服务器IP>:8765/redoc
```

