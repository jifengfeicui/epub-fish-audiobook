# Shusheng · 书声工坊

A workshop for turning ebooks into multi-character audiobooks. Import EPUB or TXT, annotate and review scripts with an LLM, assign character voices, and generate chapter and full-book MP3 files with Fish Audio. Optionally turn chapters into videos and publish them to Bilibili.

[中文](README.md) · [Project Agent](docs/agent.md) · [Bilibili publishing CLI](tools/bilibili_cli/README.md)

## Workflow and features

**Import → select chapters → annotate and review scripts → assign voices → render and merge audio → optionally publish to Bilibili**

- **Projects and chapters:** import EPUB/TXT, select included chapters, archive and restore projects.
- **Scripts:** annotate speakers and delivery instructions through an OpenAI-compatible LLM, review the results, and edit individual entries manually.
- **Character voices:** configure a Fish Audio voice library, narrator and character voices; produce chapter and full-book MP3 files.
- **Batch rendering:** by default, rendering can start after each batch of 3 reviewed chapters. Waiting for all reviews is also supported.
- **Tasks:** track progress, logs and outputs; pause, resume, retry or cancel tasks. Completed segments are reused when cache conditions match.
- **Bilibili publishing:** sign in, prepare chapter videos, publish, append or replace parts. One book maps to one submission, with one part per chapter.
- **Project Agent:** query state, edit scripts, operate tasks and publishing workflows, with separate conversations and scheduled plans. See the [Agent guide](docs/agent.md) (Chinese).

This README describes the current working tree. Agent conversations, expanded operations and scheduling include uncommitted development changes; older commits may lack these capabilities. Scheduled plans require the backend to remain running, and each model call may incur charges.

The current application uses **FastAPI + SQLite, Vue 3, and the Fish Audio API**. Local CUDA and Qwen TTS model downloads are not required. Hardware requirements for a local LLM depend on your chosen model.

## Requirements and installation

For local setup, use Python 3.12, Node.js 22 and npm to match the Docker build environment. Audio merging and video preparation require FFmpeg on PATH; Bilibili tools also require FFprobe. You need an accessible OpenAI-compatible LLM service and a Fish Audio API key.

Run these commands from the repository root. Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm --prefix frontend ci
ffmpeg -version
ffprobe -version
```

### Windows development launcher

```powershell
.\start-web.ps1
```

Open the [development UI](http://127.0.0.1:5173). The launcher starts both services; Ctrl+C stops them. It refuses to start if port 4200 or 5173 is already occupied.

Alternatively, use two terminals:

```powershell
# Terminal 1: backend
.\.venv\Scripts\python.exe -m backend.alexandria.main
```

```powershell
# Terminal 2: frontend
npm --prefix frontend run dev
```

On other systems, run the same backend module with your virtual environment's `python`. The development frontend runs on 5173 and proxies backend requests to 4200.

### Run with a built frontend

```powershell
npm --prefix frontend run build
.\.venv\Scripts\python.exe -m backend.alexandria.main
```

Open the [application](http://127.0.0.1:4200). The backend serves `frontend/dist` directly, so Vite is unnecessary. Rebuild after frontend changes.

### Docker

```sh
docker compose up --build -d
```

Open the [application](http://127.0.0.1:4200). The image includes the frontend build and FFmpeg, with no GPU passthrough required. Compose mounts host `./data` at `/alexandria/data`.

Inside Docker, `localhost` refers to the container. To connect to an LLM on the host, use an address reachable from the container, such as `host.docker.internal` on Docker Desktop.

The application follows a local, single-user model and has no multi-user login system. Docker publishes host port 4200; use it on a trusted network.

## Make your first audiobook

1. In Settings, enter the LLM base URL, API key and model name. The LLM handles script annotation, review and character analysis. Agent models must also support Chat Completions tool calling.
2. Configure the Fish Audio API key, endpoint, model and voice library. Web and CLI settings are independent and are not synchronized automatically.
3. Import an EPUB or TXT file, check chapter detection and selection, and choose narrator and rendering settings.
4. Submit preprocessing, inspect annotated and reviewed scripts, and confirm character voices. Wait for a task to finish pausing or complete before editing.
5. Submit rendering, follow progress and logs in the task panel, preview audio, and download chapter and full-book MP3 files. Script edits invalidate affected audio and full-book outputs, requiring regeneration.
6. For optional Bilibili publishing, sign in through Settings, supply book source and submission details, then prepare videos and choose publish, append or replace. Audio production does not require publishing.

LLM and Fish Audio requests may incur service charges. Starting the application does not mean these services are configured.

## Data, backups and troubleshooting

Data defaults to the repository's `data/` directory. Set `ALEXANDRIA_DATA_DIR` before startup to use another location; internal names remain compatible.

| Location relative to the data directory | Contents |
| --- | --- |
| `alexandria.db` | Projects, scripts, settings, voice metadata, tasks, events, Agent conversations and plans |
| `projects/<project ID>/` | Imported sources, chapter intermediates, audio, merged outputs and Bilibili working files |
| `voices/` | Voice preview and validation files |
| `secrets/bilibili-cookies.json` | Web Bilibili login credentials |
| `tools/biliup/` | Upload tool used by the web application |

Web service settings, including API keys, are stored in the local database. The data directory contains sensitive information; do not commit or share it directly. Standalone CLIs use their own settings and credential directories.

Before upgrading, stop the backend and back up the entire data directory. Then update dependencies, rebuild the frontend and restart. Database migrations run automatically at backend startup. Do not run two backend instances against the same database.

| Symptom | What to check |
| --- | --- |
| Port 4200 reports that the frontend is not built | Run `npm --prefix frontend run build`, or use 5173 during development |
| Port already occupied | Inspect existing processes and stop the old instance before restarting |
| Annotation, review or TTS fails | Inspect project task logs and backend terminal output; check endpoint, model, key and service errors |
| Audio merging or video preparation fails | Check FFmpeg/FFprobe availability, source audio and task logs |
| Task interrupted after a restart | Inspect its state and use the available resume or retry action; do not assume automatic completion |
| Scheduled plan does not run | Check backend availability, project archive state, plan enablement, latest result and pause reason |

Re-detecting chapters clears the project's existing scripts, characters, audio, outputs and task records. Permanently deleting a project also deletes its local project files. Back up anything you need before these operations.

## CLI and further documentation

- [Project Agent](docs/agent.md) (Chinese): conversations, business operations, scheduled plans, APIs and validation.
- [Standalone Bilibili CLI](tools/bilibili_cli/README.md) (Chinese): prepare and publish videos from existing chapter MP3 files, with separate login and checkpoints.
- [Whole-book rendering CLI source](tools/render_book.py): EPUB extraction, script generation and review, Fish rendering, chapter and full-book merging.

The whole-book CLI does not read web settings. Configure the LLM in `app/config.json` using the [example configuration](app/config.example.json), prepare the voice pool in `fish_adapter/voice_pool.json`, and supply the Fish key through `FISH_API_KEY` or `fish_adapter/config.local.json`. Its Python dependencies are in `requirements-cli.txt`; FFmpeg is also required.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-cli.txt
.\.venv\Scripts\python.exe tools/render_book.py --help
# List chapters without rendering audio
.\.venv\Scripts\python.exe tools/render_book.py --epub book.epub --list-chapters
# Render after configuration is complete
.\.venv\Scripts\python.exe tools/render_book.py --epub book.epub --render-start after-review-batch --release-batch-size 3
```

Use `--render-start after-all-reviews` to wait for all chapter reviews before rendering, and `--book-dir` to select a working directory. CLI intermediates and audio stay in that workspace and do not automatically become web projects.

## Origins and license

This project is derived from [Finrandojin/alexandria-audiobook](https://github.com/Finrandojin/alexandria-audiobook). Thanks to the original author and contributors. Legacy Qwen TTS, voice design, LoRA, dataset and Pinokio/Colab files remain in the repository, but are not features or installation methods of the current default application.

Licensed under the [MIT License](LICENSE), preserving the original copyright notice. This documentation introduces the Shusheng brand; application UI, package names, environment variables, database names and repository directory have not been renamed.
