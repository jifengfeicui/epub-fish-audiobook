# 网页 Agent

在项目页打开 **Agent 助手**。复用“设置”中当前选中的 LLM 配置，模型必须支持 Chat Completions 工具调用。

可用指令示例：

- 查询当前项目进度。
- 预处理第 1 到 3 章。
- 开始第 2 章的 TTS。
- 暂停任务；有多个候选时，明确指定任务编号。
- 把当前章旁白的语气改得沉稳，保留正文。

章号与网页的启用章节编号一致；“当前章”指页面选中的章节。预处理复用可用结果，不强制覆盖人工修改。

## 修改脚本

Agent 先读取已保存脚本，再提供修改前后对照。只有点击“确认保存脚本”才会创建手工修订；拒绝不修改脚本。

默认逐字保留正文，只允许调整说话人、语气及保持正文不变的分段。需要改写时，必须勾选“本条指令允许改写正文”，并在指令中说明改法。确认保存后，对应章节音频和整书合并产物过期，需要重新配音/合并。

脚本只能在项目任务暂停或完成时保存。暂停请求尚未完成、脚本版本已变化或存在未保存的人工编辑时，不能直接覆盖。服务器出现新版本时，本地人工编辑保留，用户选择保存以检查冲突或放弃后加载新版本。

## 运行与隔离

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
.\.venv\Scripts\python.exe -m uvicorn backend.alexandria.main:create_app --factory --host 127.0.0.1 --port 4201
```

打开 http://127.0.0.1:4201 。数据默认在当前 worktree 的 `data/`，首次使用需要配置 LLM 并导入测试书。不要让两个实例共享数据库目录。沿用现有本机使用方式，不包含多用户登录和权限系统。

每个项目只有一个对话和一个同时执行的 Agent 运行。每轮最多一个写操作、8 次模型请求、12 次工具调用，最多 120 秒。聊天展示最近 50 轮，模型使用最近 6 轮成功对话及操作状态。对话和操作记录保存在 SQLite；不会自动重放中断的操作。停止 Agent 请求不取消已提交的配音任务，应通过项目任务控制处理。

首版不包含逐字流式输出、跨阶段自动连续执行、整书批量改稿、B站发布、源代码修改和外部工作流服务。

## API

所有入口位于 `/api/v1/projects/{project_id}/agent`：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/`（实际请求无结尾斜线） | 最近对话，响应 `{ runs: [...] }` |
| POST | `/messages` | 提交运行，返回 HTTP 202 |
| GET | `/runs/{run_id}` | 查询运行及操作状态 |
| POST | `/proposals/{proposal_id}` | 确认或拒绝服务端提案 |

消息体为 `{ request_id: UUID, message: string, selected_chapter_id?: number, allow_text_rewrite?: boolean }`；重试必须沿用同一编号和消息内容。确认体仅为 `{ approve: boolean }`，不接受客户端替换脚本内容。提案确认和修订保存属于同一 SQLite 写事务。

任务工具先持久化执行凭据，再执行业务调用；完成后保存结果。进程若在执行与记录结果之间退出，会标记结果待核对，不自动重放。脚本确认重复提交返回原结果，不增加修订。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
npm --prefix frontend test
$env:PLAYWRIGHT_PORT = '5184'
npm --prefix frontend run test:e2e
npm --prefix frontend run build
```

真实模型验证可只读使用已配置数据库中的连接信息；仅在内存使用密钥，所有业务写入临时数据库，调度器关闭，不实际运行付费配音或预处理：

```powershell
.\.venv\Scripts\python.exe -m tools.validate_agent --settings-db 'D:\path\to\configured\alexandria.db'
```

验证会调用真实 LLM，可能产生模型费用；依次验证状态查询、任务提交、脚本提案与确认，并销毁临时数据。
