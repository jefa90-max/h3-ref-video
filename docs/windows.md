# Windows 一键安装与运行

适用 Windows 10/11、Intel/AMD 64 位电脑，首次安装需要联网。无需预装 Python、FFmpeg 或管理员权限。

## 使用

1. 将整个 ZIP 解压到自己有写入权限的目录，例如 `D:\工具\H3 工作台`。请先解压，不能直接在压缩包里双击。
2. 双击 `start-windows.cmd`。首次会下载固定版本的 Python、FFmpeg、curl，校验 SHA-256，创建 `.venv` 并安装依赖。总下载量约 150 MB，请等待安装结束。
3. 脚本打开浏览器并启动 `http://127.0.0.1:7860`。使用期间保留控制台窗口；结束后按 Ctrl+C。之后仍双击同一个文件，已安装的环境会复用。
4. 在工作台配置自己的视频后端与 API Key。视频后端默认可选 **ComfyUI（Colab / 远端自建）** 或 **RunningHub 云端**；页面默认选中“真实生成”，如需无费用试用，请手动切换到“离线演示”。此发布包不含开发者的密钥、历史任务或生成素材。
5. 在输出目录区域选择电脑上的文件夹。设置保存在本机；每个新任务创建按时间命名的子目录，原任务续跑继续使用原目录。

英文安装日志：在该目录的命令提示符中运行 `start-windows.cmd -Language en`。界面语言仍由网页中的语言选择控制。

## ComfyUI / Colab 模式说明（Windows 零额外依赖出片）

- **直连远端或本机 ComfyUI（无需额外安装任何 CLI）**：只要远端 Colab 或自建机器上的 ComfyUI 隧道已开启，在工作台环境配置中选择 `ComfyUI`，填写 `COMFYUI_URL`（如 `https://xxx.ngrok-free.dev`）与 `COMFYUI_API_KEY`（公网 HTTPS 隧道必填；本机 `http://127.0.0.1:8188` 可留空），保存后即可直接上传参考图、生成 768p 视频（`4step` / `8step` / `full20`）并自动下载成片。发布包内置的 Python 与专用 curl 已完整支持中文/空格路径参考图上传（自动转为 ASCII 哈希文件名）及隧道鉴权下载。
- **可选：在 Windows 上启用 Colab 一键启停按钮**：如需直接在工作台顶部卡片点击「一键启动 Colab A100 / 终止实例」，可在项目虚拟环境中额外安装 `.venv\Scripts\pip install google-colab-cli` 并完成一次 `colab auth login`。若本机遇到 Windows 凭据管理器（Credential Locker）OAuth JSON 长度限制报错，可在 `.env` 中设置 `COLAB_AUTH=adc` 改用 Google Cloud Application Default Credentials，或直接在浏览器手动运行 Colab 中的 `scripts/start_colab.py` 后由 Windows 工作台直连隧道。

## 安装位置与失败处理

- `.runtime/`：项目专用 Python、FFmpeg、curl、下载缓存和安装记录。只修改当前进程的 PATH，不安装全局 Python、不写系统 PATH。
- `.venv/`：项目的 Python 虚拟环境，所有 Python 依赖均安装在这里。
- `.runtime/startup-时间-进程号.log`：每次启动各自保存 UTF-8 安装与启动日志。失败时窗口保留，显示具体错误；修正网络或权限问题后重新双击即可。
- `.env`：选择保存的 API Key。`.workbench/preferences.json`：本机默认输出目录。它们不会被发布打包。

下载仅使用 HTTPS，并验证脚本内固定的 SHA-256。不匹配的文件不会解压或执行。Python 使用完整的官方运行时 ZIP，包含 `venv`、`ensurepip` 和所需运行库；未使用缺少 pip 的 embeddable ZIP。

安装过程中不要移动解压目录。需要迁移已安装目录时，建议从发布 ZIP 重新解压到新位置后启动；旧任务、偏好和密钥按需另行迁移。不要将别人的 `.venv` 或 `.runtime` 当作跨电脑部署包。

## 固定版本和来源

下载 URL、版本和 SHA-256 均记录在 `scripts/windows-runtime.json`。

- Python 3.13.17 x64：[官方 Windows 发布清单](https://www.python.org/ftp/python/3.13.17/windows-3.13.17.json)。完整运行时约 31 MB；包含 `Lib/venv`、`Lib/ensurepip`、pip 和 `vcruntime140*.dll`。
- FFmpeg 9.0.2 essentials：[Gyan Windows 构建](https://www.gyan.dev/ffmpeg/builds/)及[对应 SHA-256](https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256)。Gyan 是 [FFmpeg 官网列出的 Windows 二进制提供方](https://ffmpeg.org/download.html)。解压时保留许可证文件。
- curl 8.23.0_8：[curl 官方 Windows 构建](https://curl.se/windows/)。项目的下载流程使用较新的 curl 参数，因此安装包使用项目专用 curl，不依赖旧 Windows 自带的版本。
- Python 依赖从 PyPI 获取，遵循仓库 `requirements-web.txt` 和 `requirements.txt`。当前 [Flask](https://pypi.org/project/Flask/) 和 [Pillow](https://pypi.org/project/Pillow/) 支持 Python 3.13；后续启动在依赖声明未变化时不会联网重新安装。

## 验证边界

本包在 macOS 开发环境制作。已核对三个运行时的真实下载摘要和归档内容，使用 PowerShell 解析安装脚本及目录选择脚本，并离线验证缓存安装、重复使用、坏缓存拒绝与命令失败传播。Python 接口测试与发布包检查也在本机执行。这些检查不等于 Windows PowerShell 5.1 或 Windows 真机运行验证。

Windows 首次验收：检查双击完成安装并打开网页、关闭后再次双击、中文/空格路径下启动、系统目录选择并重启恢复、运行一个离线演示。真实 API 生成另外需要有效余额。

开发者若安装了 macOS 版 `pwsh`，可用 PowerShell 的 `System.Management.Automation.Language.Parser.ParseFile` 解析 `.ps1`，无需执行安装命令。例如：

```powershell
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PWD 'scripts/windows-start.ps1'), [ref]$tokens, [ref]$parseErrors
) | Out-Null
if ($parseErrors.Count) { $parseErrors | Format-List; exit 1 }
```

PowerShell 7 的语法解析不能证明 Windows PowerShell 5.1、原生 EXE、文件权限或系统对话框在真机上的运行结果；仍需上述 Windows 验收。
