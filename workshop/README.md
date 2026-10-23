# 剧本分镜工坊

把一段剧本拆成电影级分镜头，生成角色/场景参考图，并输出可直接用于视频生成的提示词。视频合成由你自己完成；本应用负责把 `<Picture 1>`、`<Picture 2>` 等参考图和分镜提示词准备好。

## 能力

1. **DeepSeek** 拆解剧本：人物、场景、道具、完整事件弧、按秒分镜（起始/过程/结果）。
2. **本地 ChatGPT 出图服务**（`E:\aippt\gpt-5.6-instruct\chatgpt-web-api`）生成参考图。
3. 组装 **视频提示词**：参考图绑定、整体视觉、时间轴、表演动态、摄影连续性、声音、负面约束。

## 启动

```powershell
cd D:\chaifen
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)

DeepSeek Key 已写在 `.env`。若要换 Key，编辑该文件中的 `DEEPSEEK_API_KEY`。

## 出图服务（可选但推荐）

参考图依赖本机已登录的 ChatGPT 出图 API：

```powershell
cd E:\aippt\gpt-5.6-instruct\chatgpt-web-api
python -m app.main
```

并按该项目 README 打开带调试端口的 Chrome。本应用默认请求 `http://127.0.0.1:8765`。

若暂时不出图，勾选界面上的「跳过出图，只出提示词」。提示词里仍会保留 `<Picture N>` 绑定位。

## 使用

1. 粘贴剧本，或点「填入示例」。
2. 选择时长和风格。
3. 点「开始拆解生成」。
4. 在「分镜头」查看时间轴，在「参考图」查看 `<Picture N>`，在「视频提示词」复制全文或导出 ZIP。

ZIP 内含 `video_prompt.txt`、`storyboard.json` 和 `images/`，可交给你自己的视频生成流程。

