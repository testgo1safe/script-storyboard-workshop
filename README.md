# 剧本分镜工坊

一个运行在 Windows 本机的短剧制作工作台，覆盖剧本拆解、参考图、视频提示词、分段出片和后期剪辑。项目包含两个服务：主工作台 `workshop` 和可选的本地 ChatGPT 网页出图 API `image-api`。

## 功能

- **剧本与分镜**：调用 DeepSeek 拆解剧本，整理人物、场景、道具和分镜时间轴；支持估算时长、整本拆分多集和系列管理。
- **角色与场景参考图**：生成并管理连续剧集可复用的角色、场景和道具图片；也可跳过出图，只生成提示词。
- **视频提示词**：按镜头生成视频提示词，整理参考图绑定、画面风格、动作、摄影连续性、声音和约束，并支持 ZIP 导出。
- **视频生成**：配置后可通过 MiniMax H3 或 Seedance 生成分段视频，并查询、恢复任务。
- **后期剪辑**：拼接分段视频、生成字幕、去除模型字幕、合成豆包配音和环境音，并可把多集合并成合集。
- **本地图片 API**：通过已登录的 Chrome 浏览器生成图片，并提供 OpenAI 兼容接口；主工作台启动器会一并启动该服务。

## 环境要求

- Windows 10/11、PowerShell 5.1 和 Python 3.10 或更高版本。
- Chrome；使用网页出图时需要在启动器打开的浏览器中登录 ChatGPT。
- DeepSeek API Key 用于剧本拆解。视频生成、配音等服务按需配置各自的 API Key。
- 剪辑功能需要 FFmpeg；若程序找不到系统里的 FFmpeg，可在 `workshop/.env` 中设置 `FFMPEG_PATH`。

## 安装与启动

1. 克隆仓库后，在仓库目录复制配置模板：

   ```powershell
   Copy-Item workshop/.env.example workshop/.env
   ```

2. 编辑 `workshop/.env`，填写 `DEEPSEEK_API_KEY`。按需填写 `MINIMAX_API_KEY`、`SEEDANCE_API_KEY`、豆包配音及火山引擎配置。没有对应密钥的功能可以不启用。
3. 双击 `start.bat`（或 `启动.bat`）。首次启动会创建 `.venv`、安装 Python 依赖和 Playwright Chromium，并启动工作台及图片 API。
4. 使用图片生成功能时，在弹出的 Chrome 窗口登录 `https://chatgpt.com`，并保持该窗口运行。
5. 浏览器访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。停止服务请双击 `stop.bat`（或 `停止.bat`）。

启动器使用 `127.0.0.1` 提供本地服务。不要在不理解网络暴露风险的情况下修改监听地址。

## 使用流程

1. 在左侧新建或选择剧集，粘贴剧本，也可以先载入示例。
2. 设置目标画幅、画质和视觉风格；根据需要选择自动拆分多集、跳过出图或分镜后自动出片。
3. 点击“生成本集分镜”，等待剧本分析、参考图和视频提示词生成。
4. 查看“分镜头”“参考图”“提示词”标签页；提示词可以复制，也可以导出 ZIP。
5. 配置视频生成服务后，在“成片”页生成分段视频；可选择自动剪辑、字幕、去模型字幕、配音和环境音。系列剧集准备好后可使用“合集剪辑”。

## 目录说明

| 路径 | 内容 |
| --- | --- |
| `start.bat`、`start.ps1` | 启动工作台和图片 API |
| `stop.bat`、`stop.ps1` | 停止本次启动的服务 |
| `workshop/` | 剧本工作台、前端、API 和配置模板 |
| `image-api/` | 本地 ChatGPT 网页出图服务及 API 文档 |
| `workshop/data/projects/`、`workshop/data/series/` | 本机项目、图片、视频及剪辑产物；不会提交到 Git |
| `logs/` | 运行日志；不会提交到 Git |

更详细的单项说明见 [`workshop/README.md`](workshop/README.md) 和 [`image-api/README.md`](image-api/README.md)。启动失败时先查看 `logs/workshop.err.log`、`logs/image-api.err.log`。

## 安全与数据

- `workshop/.env`、`video.env` 和运行时密钥文件会被 Git 忽略。请只在本机填写密钥，不要把密钥提交到仓库。
- 项目数据、生成的视频、日志、虚拟环境和本地开发配置不会被提交。克隆仓库后，需在本机重新配置密钥并生成自己的项目数据。
- 图片 API 使用浏览器登录态访问 ChatGPT 网页，属于本地个人工作流；不要把服务端口公开到互联网，也不要把浏览器用户数据目录上传。

