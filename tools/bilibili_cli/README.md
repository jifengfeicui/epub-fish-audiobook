# B站有声书分章投稿 CLI

独立命令行工具：读取 Alexandria 或普通 `output` 目录中的章节 MP3，为每章生成一张随机图片视频，并作为一个多P稿件上传到 B 站。源码与测试统一放在主仓库的 `tools/bilibili_cli/`；主程序网页也已集成 B 站投稿功能。

## 环境

- Python 3.11+
- PATH 中可用的 FFmpeg 和 FFprobe
- 网络可访问 Picsum、GitHub 和 B 站

`biliup v1.2.4` 会在首次登录或上传时下载到 `tools/`，下载包会验证固定 SHA-256。

## 使用

```powershell
# 从仓库根目录进入工具目录
cd tools/bilibili_cli

python cli.py prepare "<章节 MP3 的 output 目录>" `
  --author "麦克斯·班尼特" `
  --publisher "中译出版社"

python cli.py login

python cli.py upload ".\work\智能简史" `
  --source "《智能简史》，麦克斯·班尼特，中译出版社"

# 也可以先提交前18P，再逐批追加
python cli.py upload ".\work\智能简史" `
  --source "《智能简史》，麦克斯·班尼特，中译出版社" `
  --parts 18 --line cnbldsa

python cli.py append ".\work\智能简史" --line cnbldsa
```

投稿默认使用知识区“科学科普” (`tid=201`)、转载类型和仅自己可见。只有显式增加 `--public` 才会公开投稿。

可用覆盖参数：

```text
prepare: --prepared-dir --title --author --publisher
upload:  --title --tid --tag --desc --line --parts --public
append:  --vid --line --parts
```

若自动选择的上传节点证书异常，可用 `--line cnbldsa` 显式切换线路；现有 checkpoint 会继续生效。

运行测试：

```powershell
python -m unittest -v
```

生成文件、biliup、登录凭据和上传日志均由 `.gitignore` 排除。源音频目录不会被修改。

旧独立目录的工作文件与登录凭据不会自动迁移；已有投稿清单包含绝对路径，继续使用旧工作区可保留原上传 checkpoint。新位置使用自己的 `work/`、`tools/` 与 `secrets/`，首次投稿前需要登录。
