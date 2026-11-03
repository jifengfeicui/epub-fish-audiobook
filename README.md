# 书声工坊 · Shusheng

从电子书到多角色有声书的制作工坊。导入 EPUB 或 TXT，通过 LLM 标注与审核脚本，分配角色音色，使用 Fish Audio 生成章节及整书 MP3，并可继续制作视频、发布到 B 站。

[English](README_EN.md) · [项目 Agent](docs/agent.md) · [B 站投稿 CLI](tools/bilibili_cli/README.md)

## 工作流程与功能

**导入书籍 → 识别与选择章节 → 脚本标注与审核 → 角色音色 → 配音与合成 → 可选 B 站发布**

- **项目与章节**：导入 EPUB/TXT、管理启用章节、归档与恢复项目。
- **脚本制作**：通过 OpenAI 兼容 LLM 接口标注说话人和语气、审核脚本，并支持人工逐条编辑。
- **角色配音**：配置 Fish Audio 音色库、旁白和角色音色，生成章节 MP3 与整书 MP3。
- **分批生成**：默认每审核完 3 章即可进入配音，也可选择整书审核完成后再生成。
- **任务管理**：查看任务进度、日志和产物，支持暂停、继续、重试与取消；已完成片段按缓存条件复用。
- **B 站发布**：登录账号、准备章节视频、首发、追加及替换分 P；一本书对应一个稿件，每章对应一个分 P。
- **项目 Agent**：在项目中查询状态、编辑脚本、操作任务和发布流程，支持独立会话与定时计划。详见 [Agent 使用说明](docs/agent.md)。

本文按当前工作区编写。Agent 多会话、扩展操作与定时任务包含尚未提交的开发改动；仅检出较早提交时，可能没有这些能力。定时计划需要后端持续运行，每轮模型调用可能产生费用。

当前应用使用 **FastAPI + SQLite 后端、Vue 3 前端和 Fish Audio API**。不需要为 TTS 在本机安装 CUDA 或下载 Qwen 模型；本地 LLM 的硬件要求由所选模型决定。

## 环境与安装

本地安装建议使用 Python 3.12、Node.js 22 和 npm，与 Docker 构建环境保持一致。音频合成与视频准备需要 PATH 中可用的 FFmpeg；B 站工具还需 FFprobe。另需可用的 OpenAI 兼容 LLM 服务和 Fish Audio API Key。

以下命令从仓库根目录执行。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm --prefix frontend ci
ffmpeg -version
ffprobe -version
```

### Windows 开发启动

```powershell
.\start-web.ps1
```

打开 [开发页面](http://127.0.0.1:5173)。脚本同时启动后端与前端，按 Ctrl+C 停止；4200 或 5173 被占用时会拒绝重复启动。

也可以在两个终端分别运行：

```powershell
# 终端 1：后端
.\.venv\Scripts\python.exe -m backend.alexandria.main
```

```powershell
# 终端 2：前端
npm --prefix frontend run dev
```

其他系统可使用虚拟环境中的 `python` 执行同一后端入口。开发前端运行在 5173，通过代理连接 4200 的后端。

### 构建后运行

```powershell
npm --prefix frontend run build
.\.venv\Scripts\python.exe -m backend.alexandria.main
```

打开 [应用页面](http://127.0.0.1:4200)。此时后端直接提供 `frontend/dist`，不需要另启 Vite；前端代码更新后需重新构建。

### Docker

```sh
docker compose up --build -d
```

打开 [应用页面](http://127.0.0.1:4200)。镜像包含前端构建结果和 FFmpeg，无需 GPU 透传；Compose 将宿主机 `./data` 挂载到容器的 `/alexandria/data`。

Docker 内的 `localhost` 指容器自身；连接宿主机 LLM 时，请使用容器可访问的地址，例如 Docker Desktop 下的 `host.docker.internal`。

本项目沿用本机、单用户使用方式，没有多用户登录系统。Docker 会映射宿主机 4200 端口，应在可信网络中使用。

## 第一次制作有声书

1. 在设置中填写 LLM 的 Base URL、API Key 和模型名称。LLM 用于脚本标注、审核及角色分析；使用 Agent 的模型还需支持 Chat Completions 工具调用。
2. 配置 Fish Audio 的 API Key、服务地址、模型与音色库。网页配置与 CLI 配置独立，不会自动互通。
3. 导入 EPUB 或 TXT，检查章节识别与启用范围，设置旁白和生成策略。
4. 提交预解析，检查标注与审核后的脚本，确认角色音色。需要编辑时先等待任务暂停完成或结束。
5. 提交配音任务，在任务面板查看进度与日志，试听并下载章节及整书 MP3。修改脚本会使对应音频与整书产物过期，需要重新生成。
6. 如需 B 站发布，在设置中登录账号，补全书籍来源等投稿信息，再准备视频并选择首发、追加或替换。发布是可选步骤，不影响音频制作。

LLM 与 Fish Audio 请求可能产生服务费用。启动应用不代表生成服务已配置完成。

## 数据、备份与排错

默认数据保存在仓库的 `data/`。可在启动前通过 `ALEXANDRIA_DATA_DIR` 指定其他目录；内部名称保留兼容。

| 位置（相对数据目录） | 内容 |
| --- | --- |
| `alexandria.db` | 项目、脚本、设置、音色元数据、任务与事件，以及 Agent 会话和计划 |
| `projects/<项目 ID>/` | 导入文件、章节中间文件、音频、合成产物与 B 站工作文件 |
| `voices/` | 音色试听与校验文件 |
| `secrets/bilibili-cookies.json` | 网页端 B 站登录凭据 |
| `tools/biliup/` | 网页投稿所用的上传工具 |

网页填写的服务配置（包括 API Key）保存在本地数据库中；数据目录包含敏感信息，不应提交到仓库或直接共享。独立 CLI 有各自的配置与凭据目录。

升级前停止后端并备份完整数据目录，再更新依赖、构建前端并重启。数据库迁移在后端启动时自动执行；不要让两个后端实例共用同一数据库。

| 现象 | 检查方式 |
| --- | --- |
| 4200 显示前端未构建 | 执行 `npm --prefix frontend run build`，或在开发时访问 5173 |
| 启动提示端口占用 | 检查已有进程，停止旧实例后再启动 |
| 标注、审核或配音失败 | 查看项目任务日志和后端终端输出，核对服务地址、模型、密钥及错误回执 |
| 合成或视频准备失败 | 确认 FFmpeg/FFprobe 可用，检查源音频和任务日志 |
| 服务重启后任务中断 | 查看任务状态，按界面提供的继续或重试操作恢复，不假定任务已自动完成 |
| 定时任务没有执行 | 确认后端运行、项目未归档、计划已启用，并检查最近结果和暂停原因 |

重新识别章节会清除该项目已有脚本、角色、音频、产物与任务记录；永久删除项目也会删除本地项目文件。操作前先备份需要保留的内容。

## 命令行与进阶文档

- [项目 Agent](docs/agent.md)：会话、业务操作、定时计划、API 与验证方法。
- [独立 B 站投稿 CLI](tools/bilibili_cli/README.md)：将现有章节 MP3 制作成视频并投稿，独立管理登录与 checkpoint。
- [整书渲染 CLI 源码](tools/render_book.py)：EPUB 章节提取、脚本生成与审核、Fish 配音、章节及整书合成。

整书 CLI 不读取网页设置。先配置 `app/config.json` 中的 LLM（可参考 [配置示例](app/config.example.json)），准备 `fish_adapter/voice_pool.json` 音色池，并通过 `FISH_API_KEY` 或 `fish_adapter/config.local.json` 设置 Fish 密钥。其 Python 依赖见 `requirements-cli.txt`，同样需要 FFmpeg。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-cli.txt
.\.venv\Scripts\python.exe tools/render_book.py --help
# 仅列出章节，不发起配音
.\.venv\Scripts\python.exe tools/render_book.py --epub book.epub --list-chapters
# 配置完成后执行整书制作
.\.venv\Scripts\python.exe tools/render_book.py --epub book.epub --render-start after-review-batch --release-batch-size 3
```

使用 `--render-start after-all-reviews` 可等待所有章节审核完成后再配音；`--book-dir` 可指定工作目录。CLI 中间文件与音频保存在自己的工作目录，不会自动成为网页项目。

## 来源与许可

本项目基于 [Finrandojin/alexandria-audiobook](https://github.com/Finrandojin/alexandria-audiobook) 发展而来，感谢原作者及贡献者。原有 Qwen TTS、声音设计、LoRA、数据集和 Pinokio/Colab 相关文件仍有保留，但不是当前默认入口的功能或安装方式。

采用 [MIT License](LICENSE)，保留原作者版权声明。此次使用「书声工坊 · Shusheng」作为文档品牌，程序包名、界面、环境变量、数据库名与仓库目录暂未更改。
