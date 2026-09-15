# EPUB Fish Audiobook

将 EPUB 转换为分章 MP3 和整本 MP3 的命令行工具。项目使用 Alexandria 完成 EPUB 提取、人物识别、脚本生成和 Review，使用 Fish Audio 合成语音，最后由 FFmpeg 合并音频。

```text
EPUB
  -> 章节提取
  -> 本地 LLM 生成 speaker / text / instruct
  -> LLM Review 与文本完整性校验
  -> 主要角色固定音色、次要角色音色池分配
  -> Fish Audio 分段 MP3
  -> FFmpeg 分章 MP3 与整本 MP3
```

本仓库基于 [Finrandojin/alexandria-audiobook](https://github.com/Finrandojin/alexandria-audiobook) 修改，保留原项目的 MIT License 和提交历史。本 README 只介绍当前 Fish Audio CLI 流程。

## 当前能力

- 按 EPUB OPF、spine 和 TOC 顺序提取正文，过滤封面、版权页和目录页。
- 使用任意 OpenAI 兼容接口生成和 Review 中文有声书脚本。
- 对脚本执行文本完整性校验，LLM 漏字或改写时拒绝错误结果。
- 使用 Fish `reference_id` 固定主要角色音色，未绑定角色按首次出现顺序循环使用音色池。
- 每段独立缓存、失败重试和断点续跑。
- 分章输出单声道、44.1 kHz、128 kbps MP3。
- 相同人物片段间插入 250 ms 静音，不同人物间插入 500 ms 静音。
- 全部章节完成后无损拼接为整本 MP3。

## 环境要求

- Windows 10/11（当前主要测试平台）
- Python 3.11 或更高版本
- [FFmpeg](https://ffmpeg.org/) 已加入 `PATH`
- LM Studio 或其他 OpenAI 兼容 LLM 服务
- Fish Audio API Key 和可用的声音模型 `reference_id`

确认基础命令可用：

```powershell
python --version
ffmpeg -version
```

Windows 可通过 `winget install Gyan.FFmpeg` 安装 FFmpeg。

## 安装

```powershell
git clone https://github.com/jifengfeicui/epub-fish-audiobook.git
cd epub-fish-audiobook

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-cli.txt
```

`requirements-cli.txt` 只安装整书 CLI 所需的 `openai` 和 `requests`，不会安装 Alexandria 本地 Qwen TTS 的大型依赖。

## 配置本地 LLM

以 LM Studio 为例：加载模型，启动 Local Server，并确认 OpenAI 兼容接口可访问。默认地址为：

```text
http://localhost:1234/v1
```

复制配置模板：

```powershell
Copy-Item app\config.example.json app\config.json
```

编辑 `app/config.json`：

```json
{
  "llm": {
    "base_url": "http://localhost:1234/v1",
    "api_key": "lm-studio",
    "model_name": "LM Studio 中已加载的模型 ID"
  },
  "generation": {
    "chunk_size": 2000,
    "max_tokens": 8192,
    "temperature": 0.2,
    "top_p": 0.8,
    "lossless_fallback": true
  }
}
```

`app/config.json` 是本地文件，不会提交到 Git。`model_name` 必须与 `/v1/models` 返回的模型 ID 一致。

## 配置 Fish API Key

长期使用时复制本地配置：

```powershell
Copy-Item fish_adapter\config.local.example.json fish_adapter\config.local.json
```

编辑 `fish_adapter/config.local.json`：

```json
{
  "api": {
    "model": "s2.1-pro-free",
    "api_key": "你的 Fish API Key"
  }
}
```

也可以只为当前 PowerShell 会话设置环境变量：

```powershell
$env:FISH_API_KEY = "你的 Fish API Key"
```

配置优先级从低到高为：

```text
内置默认值
-> fish_adapter/config.json
-> fish_adapter/config.local.json
-> FISH_API_KEY 环境变量
-> 命令行参数
```

环境变量非空时会覆盖本地配置文件中的 Key。Key 不会写入日志或 manifest。

Fish 的非敏感默认参数位于 `fish_adapter/config.json`，包括模型、采样率、MP3 码率、temperature、top_p、worker 数和重试策略。

## 配置音色

复制音色池模板：

```powershell
Copy-Item fish_adapter\voice_pool.example.json fish_adapter\voice_pool.json
```

编辑 `fish_adapter/voice_pool.json`：

```json
{
  "bindings": {
    "NARRATOR": {
      "reference_id": "旁白声音模型 ID",
      "name": "旁白"
    },
    "主要角色名": {
      "reference_id": "主要角色声音模型 ID",
      "name": "主要角色"
    }
  },
  "pool": [
    {
      "reference_id": "次要角色声音模型 ID 1",
      "name": "次要音色 1"
    },
    {
      "reference_id": "次要角色声音模型 ID 2",
      "name": "次要音色 2"
    }
  ]
}
```

- `NARRATOR` 必须存在于 `bindings`。
- `bindings` 中的人物名必须与脚本中的 `speaker` 完全一致。
- 未绑定人物按首次出场顺序从 `pool` 循环分配。
- 已被主要角色绑定的音色不会再分给次要角色。
- 每本书的最终分配保存在 `books/<书名>/voice_assignments.json`，断点续跑时保持稳定。
- `voice_pool.json` 是本地文件，不会提交到 Git。

## 生成整本书

先检查自动识别出的章节：

```powershell
python tools\render_book.py `
  --epub "D:\books\示例.epub" `
  --list-chapters
```

生成全部章节和整本 MP3：

```powershell
python tools\render_book.py `
  --epub "D:\books\示例.epub" `
  --workers 2
```

同时保存终端日志：

```powershell
python tools\render_book.py `
  --epub "D:\books\示例.epub" `
  --workers 2 2>&1 | Tee-Object -FilePath ".\render.log"
```

只处理指定章节范围：

```powershell
python tools\render_book.py `
  --epub "D:\books\示例.epub" `
  --from-chapter 3 `
  --to-chapter 8 `
  --workers 2
```

第一人称小说可固定叙述者身份：

```powershell
python tools\render_book.py `
  --epub "D:\books\示例.epub" `
  --first-person-speaker "地下人" `
  --workers 2
```

重新运行相同命令会根据指纹跳过未变化的脚本和音频。需要重做某个阶段时使用：

```powershell
--force extract
--force script
--force review
--force audio
--force merge
--force all
```

同一本书不要同时启动多个生成进程，否则它们可能竞争相同的 manifest 和音频文件。

## 单独渲染 Alexandria 脚本

脚本必须是 `speaker`、`text`、`instruct` 三字段组成的 JSON 数组。复制单脚本音色模板并填写声音 ID：

```powershell
Copy-Item fish_adapter\voices.example.json fish_adapter\voices.json
```

渲染整个脚本：

```powershell
python fish_adapter\fish_adapter.py `
  --script annotated_script.json `
  --voices fish_adapter\voices.json `
  --output fish_adapter\output `
  --workers 2
```

强制重新生成第 23 段：

```powershell
python fish_adapter\fish_adapter.py `
  --script annotated_script.json `
  --voices fish_adapter\voices.json `
  --output fish_adapter\output `
  --only 23
```

`--only` 使用 1-based 段编号，并强制重做指定段。

## 更换音色与断点续跑

1. 在正在运行的终端按 `Ctrl+C`。
2. 修改 `fish_adapter/voice_pool.json` 中对应角色的 `reference_id`。
3. 从需要更换音色的第一章重新运行指定章节范围。

例如从第 18 章开始换音色：

```powershell
python tools\render_book.py `
  --epub "D:\books\示例.epub" `
  --from-chapter 18 `
  --to-chapter 28 `
  --workers 2
```

音频指纹包含文本、instruct、speaker、`reference_id`、Fish 模型和请求参数。音色改变后，所选章节中对应角色的旧片段会自动重新生成；未选择的章节不会处理。不要在任务运行期间修改音色配置，因为当前进程已经读取了本次分配。

## 输出和进度

默认输出结构：

```text
books/<书名>/
├── book_manifest.json
├── voice_assignments.json
├── chapters/
│   └── 001-章节名/
│       ├── source.txt
│       ├── generated.json
│       ├── reviewed.json
│       ├── stage.json
│       └── audio/
│           ├── 000001.mp3
│           └── manifest.json
└── output/
    ├── 001-章节名.mp3
    └── 书名.mp3
```

- `book_manifest.json`：全书章节选择和完成状态。
- `stage.json`：单章提取、脚本、Review 和合并指纹。
- `audio/manifest.json`：每段状态、尝试次数、错误和音频指纹。
- `output/<章节>.mp3`：分章成品。
- `output/<书名>.mp3`：完整范围全部成功后生成的整本成品。

## 只合并现有分章 MP3

当前 CLI 没有独立的 `--merge-only` 参数。如果所有分章 MP3 已经存在，可在仓库根目录执行：

```powershell
python -c "from pathlib import Path; from tools.render_book import concat_mp3; d=Path(r'books\示例\output'); concat_mp3(sorted(d.glob('[0-9][0-9][0-9]-*.mp3')), d/'示例.mp3')"
```

该命令按三位章节编号排序，并使用 FFmpeg stream copy 合并，不重新编码。

## 当前执行方式

整书 CLI 当前采用两阶段流程：

1. 所选章节全部完成脚本生成和 Review。
2. 再按章节调用 Fish 生成音频并合并。

因此前面章节的 `reviewed.json` 已完成时，也需要等待全部所选章节 Review 完成后才开始 Fish。非小说的快速旁白模式、SQLite 项目数据库和新的 Web 管理界面尚未实现。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q fish_adapter tools app tests
git diff --check
```

## License

[MIT License](LICENSE)。原始 Alexandria 项目版权信息保留在许可证和 Git 历史中。
