# ComfyUI 接入替代 RunningHub 与 Colab 一键启停计划

状态：**已定稿待执行（暂不编码）**。等当前分支 `feat/prompt-optimization-and-grouped-cast` 的工作合并回 `main` 后，再从最新 `main` 切新分支（或独立 worktree `feat/comfyui-colab`）实施。

---

## 已确认的目标与默认参数

1. **视频生成后端与默认回退**：
   - 新增 `h3ref/comfy.py` 接入远端 ComfyUI，同时完整保留 RunningHub 作为可切换回退选项。
   - 代码级未配置回退保持 `VIDEO_PROVIDER="runninghub"`（确保现有离线回归测试 100% 兼容，不破坏既有单测夹具）；`.env.example` 与实际 `.env` 默认配置为 `VIDEO_PROVIDER=comfyui`。
2. **默认生成预设（`COMFYUI_PRESET`）**：
   - 将步数与加速 LoRA 合并为单一防呆枚举 `COMFYUI_PRESET=4step|8step|full20`（防止步数与 LoRA 权重错配导致画面崩坏）：
     - **`4step`（默认）**：使用多参考图专用蒸馏权重 `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`（`steps=4`，`enable_lora=true`），与 `MiniMaxH3ReferenceToVideo` 主干严格匹配；
     - **`8step`（可选）**：`minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`（`steps=8`，`enable_lora=true`）；
     - **`full20`（可选）**：关闭加速 LoRA（`steps=20`，`enable_lora=false`）。
3. **默认分辨率与画幅隔离**：
   - ComfyUI 使用独立分辨率表 `COMFY_SIZES`（768p 档、32 对齐：`9:16` → `768×1344`、`16:9` → `1344×768`、`1:1` → `992×992`），直接将确定性宽高整数写入 `MiniMaxH3ReferenceToVideo` 节点输入，避免 `ResolutionSelector` 浮点换算偏差；**不修改 RunningHub 原有的 `config.SIZES`（544p）**。
4. **参考图动态挂载与哈希命名上传**：
   - 按当前片段实际参考图数量（1～9 张）动态写入用到的 `LoadImage` 与 `ref_images.ref_image_0..N-1`，不传占位图。
   - 上传图片到 ComfyUI `/upload/image` 时，multipart 文件名统一使用纯 ASCII 内容摘要 `<sha256[:16]><ext>`，杜绝跨任务 `s1-1.png` 同名覆盖串图，同时规避 Windows 中文路径在 multipart 头中的编码问题。
5. **单卡并发与状态持久化**：
   - ComfyUI 默认并发 `1`（`COMFYUI_CONCURRENCY=1`）；完成记录写入 `cny=0.0` 与 `provider="comfyui"`，既兼容现有 `rec.get("cny") is not None` 完成判定，又防止中途切换 `VIDEO_PROVIDER` 时拿旧后端任务号跨后端轮询。
6. **Colab 按需启停、隧道鉴权与 1 小时空闲自动关机**：
   - **按需拉起（`COLAB_AUTO_START=0` 默认）**：打开 `workbench.py` 时仅做只读探活（`GET {COMFYUI_URL}/system_stats`），绝不在看历史任务或审分镜图阶段自动烧 A100；当用户点击工作台「一键启动」按钮，或真正进入视频生成阶段（`one` 提交 / `film` 分镜确认后 `continue_film` / 单段重生成）且配置允许时再触发拉起（保留 `COLAB_AUTO_START=1` 供需开工作台即预热的场景）。
   - **公网隧道强制鉴权**：固定 ngrok 域名严禁裸奔暴露挂载了 Google Drive 的 ComfyUI `8188` 端口。远端 `start_colab.py` 启动 ngrok 时必须配置鉴权（Bearer / Basic 校验对应 `COMFYUI_API_KEY`），本地所有请求统一携带鉴权头与 `ngrok-skip-browser-warning: 1`。
   - **ngrok 密钥零本地落盘传输**：`NGROK_AUTHTOKEN` 直接保存在 Google Drive 私密文件（如 `/content/drive/MyDrive/MiniMax_ComfyUI/.ngrok_token`）中由 `start_colab.py` 挂载后自读，不进入本机 `.env`、工作台表单和 `colab exec` 本地明文历史日志（`~/.config/colab-cli/history/*.jsonl`）。
   - **云端 1 小时空闲自动关机看门狗**：在 Drive 的 `start_colab.py` 内置后台看门狗线程，持续检查本地 `127.0.0.1:8188/queue` 与最近一次活跃时间；**连续闲置满 60 分钟（`COLAB_IDLE_TIMEOUT_MIN=60`）无运行或排队任务时，自动调用 `google.colab.runtime.unassign()` 释放 A100 实例**，防止本机休眠、断网或关终端后持续按 `6.73 compute units/hr` 扣费。

---

## 跨平台（macOS / Linux / Windows）与关键架构约束

1. **生成层与控制层解耦，保证 Windows 100% 原生可用**：
   - **视频生成层（`h3ref/comfy.py`）全平台零新增依赖**：仅使用 Python 标准库与项目 Windows 一键包已内置的 `Python 3.13.17`、`FFmpeg`、`curl.exe`（见 `scripts/windows-runtime.json`）。即便某台 Windows 电脑未安装 `google-colab-cli`，只要远端 ComfyUI 已在运行（通过另一台设备或直接在 Windows 浏览器打开 Colab 运行 `start_colab.py`），Windows 工作台和 CLI 即可完整跑通上传、生成、轮询、下载与拼片。
   - **Colab 控制层（`h3ref/colab_runtime.py`）跨平台兼容与无 CLI 优雅降级**：
     - **不把 `google-colab-cli` 写入 `requirements-web.txt`**：避免引入 17 个额外依赖拖慢或破坏 `scripts/windows-start.ps1` 的 `--only-binary=:all:` 安装校验。
     - **多路径探测**：支持 `COLAB_BIN` 覆盖，自动按平台探测项目虚拟环境（POSIX `.venv/bin/colab`、Windows `.venv\Scripts\colab.exe`）、`~/.local/bin/colab`、`%USERPROFILE%\.local\bin\colab.exe`、`%APPDATA%\Python\Scripts\colab.exe` 及系统 `PATH`。未检测到 CLI 时，工作台状态卡正常显示 ComfyUI 在线/离线状态，仅禁用「一键启动 / 终止」按钮并提示安装命令或浏览器启动入口，不阻塞视频生成。
     - **Windows 凭据兼容（`COLAB_AUTH`）**：Windows Credential Locker（`WinVaultKeyring`）存在约 1280 字符长度上限，可能导致 `colab` 默认 `oauth2` 无法持久化长 token JSON 而反复要求重登；支持通过 `COLAB_AUTH=adc|oauth2` 透传 `--auth`，并在 `docs/windows.md` 说明 Windows 下使用 ADC 文件凭据或 WSL 的配置方法。
2. **子进程 TTY 脱离与 Windows 编码 / 防弹窗处理**：
   - **macOS / Linux 脱离 `/dev/tty`**：`colab_cli` 的 `drivemount` 在 POSIX 下会优先打开 `/dev/tty` 阻塞读回车（忽略 `stdin` 管道）。启动子进程时**必须传 `start_new_session=True`**，迫使其回退读取 `sys.stdin`，工作台网页上的「我已授权，继续」按钮才能真正推进挂载；同时关注 600 秒授权超时后的状态机回落。
   - **Windows 防黑框与 UTF-8 强制输出**：Windows 下启动 `colab.exe` 必须传 `creationflags=subprocess.CREATE_NO_WINDOW`（防止后台探活反复弹出控制台黑框），且全平台统一注入 `PYTHONUNBUFFERED=1`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（防止 `rich`/`typer` 输出 `╭─ ─╮` Unicode 字符在 Windows GBK 代码页触发 `UnicodeEncodeError`）。
3. **跨进程锁与工作台父子进程职责边界**：
   - 复用 `workbench/instance.py` 的双平台文件锁方案（Windows `msvcrt.locking` / POSIX `fcntl.flock`）对 `.workbench/colab-runtime.lock` 加锁，防止并发请求开出两台 A100；同时检测 `colab sessions` 中的 `[?]` 孤立会话并预警。
   - 工作台任务 worker 子进程（`H3_WORKBENCH_WORKER=1`）不直接运行 `colab drivemount` 或托管启动子进程（避免任务结束时 `stop_posix_group` / `WindowsJob` 把正在拷权重的进程一并强杀）；工作台模式下统一由 Flask 主进程管理 Colab 生命周期。
4. **`start_colab.py` 后台守护与幂等退出**：
   - `start_colab.py` 必须以独立会话后台拉起 ComfyUI 与 ngrok（及 60 分钟空闲看门狗），在本机 `127.0.0.1:8188/system_stats` 与隧道探活成功后立即 `exit 0`，避免长占 `colab exec` 与 Jupyter kernel；若检测到 8188 已就绪则幂等返回。
5. **ComfyUI 内存历史丢失快速失败**：
   - 原生 ComfyUI 的 `/queue` 与 `/history` 仅保存在进程内存中。`comfy.poll(prompt_id)` 若连续 2 次确认 `prompt_id` 既不在 `/history/{prompt_id}` 也不在 `/queue`（`queue_running` + `queue_pending`），立即判定为终态 `failed`（提示远端实例可能已重启），避免续跑时无效轮询 2 小时。
6. **终止实例前的在途任务保护**：
   - 点击工作台「终止实例」时，若本地有运行中的视频任务或远端 `/queue` 非空，前端弹出二次确认，后端接口需显式收到 `force=true` 才执行 `colab stop`。

---

## 实施步骤与验证方式

### 阶段 0：远端启动脚本加固与 API 工作流模板核对（动手编码前）
- 更新 Google Drive 上的 `/content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py`：
  1. 确保本地 SSD 预拷列表包含 4 步 `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`（及可选 8 步 LoRA）；
  2. 从 Drive 私密文件读取 `.ngrok_token`，并为 ngrok 隧道加上请求鉴权（对齐 `COMFYUI_API_KEY`）；
  3. 后台守护拉起 ComfyUI + ngrok + **60 分钟空闲自动关机看门狗（`runtime.unassign()`）**，探活成功后脚本立即 `exit 0`。
- 确认导出的工作流文件为 ComfyUI **API 格式**（`Save (API Format)` 扁平节点字典，非网页 UI `nodes/links` 格式），放入 `workflows/comfy-h3-ref2va.json`。

### 阶段 1：配置函数化与密钥脱敏注册（`h3ref/config.py`、`workflows/comfy-h3-ref2va.json`、`.env.example`、`workbench/jobs.py`、`workbench/server.py`）
- 新增 `workflows/comfy-h3-ref2va.json` 及节点类型启动自检。
- 在 `h3ref/config.py` 新增动态解析函数 `video_settings(environ=None)`（仿照 `llm_settings(environ=None)`，支持工作台内存 `env` 热更新与单测隔离，不写成模块导入期静态常量）：
  - 解析 `VIDEO_PROVIDER`（`runninghub` | `comfyui`）、`COMFYUI_URL`、`COMFYUI_API_KEY`、`COMFYUI_PRESET`（`4step` | `8step` | `full20`，默认 `4step`）、`COMFYUI_CONCURRENCY=1`、`COLAB_SESSION=comfy-a100`、`COLAB_AUTO_START=0`、`COLAB_BIN`、`COLAB_AUTH`。
  - 定义 `COMFY_SIZES = {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (992, 992)}`，保持 `config.SIZES`（RunningHub 544p）不变。
- 将 `"COMFYUI_API_KEY"` 加入 `workbench/jobs.py` 的 `SECRET_NAMES` 与 `workbench/server.py` 的 `KEYS`。
- **验证**：单测覆盖 `video_settings(environ)` 非法枚举/URL 边界快速报错（Fail Fast）、`COMFYUI_API_KEY` 日志脱敏、RunningHub 默认回退不受影响。

### 阶段 2：实现 `h3ref/comfy.py` 并接入双后端分发（`h3ref/http.py`、`h3ref/comfy.py`、`h3ref/film.py`、`h3ref.py`）
- 扩展 `h3ref/http.py`（保持 Windows `curl.exe --config -` 路径转义兼容）：
  - `upload()` 支持自定义表单文件字段名（ComfyUI 需 `image` 而非 `file`）、显式 ASCII `filename`（`<sha256[:16]><ext>`）、可选无 `Authorization` 或自定义 `extra_headers`；
  - `download()` 支持附加自定义请求头（鉴权头 + `ngrok-skip-browser-warning: 1`）。
- 实现与 `h3ref/rh.py` 同构的 `h3ref/comfy.py`：
  - `upload(path)`：上传图片并返回服务端文件名；
  - `graph(prompt, image_names, seconds, aspect, seed, *, preset=...)`：校验 1～9 张参考图，动态注入用到的 `LoadImage` 与 `ref_images.ref_image_0..N-1`，写入 `COMFY_SIZES[aspect]` 与 `4step`/`8step`/`full20` 对应的步数和 LoRA 开关；
  - `submit_with_retry(g, ...)`：`POST {COMFYUI_URL}/prompt`，记录 `prompt_id` 作为 `task`（保留未知提交保护）；
  - `poll(task)`：查 `/history/{prompt_id}` 与 `/queue`，兼容解析 `SaveVideo` 输出键（`videos` 或 `images`）拼装 `/view` 下载地址，返回 `{state, url, cny: 0.0, run_seconds, error}`；连续两次既不在 history 也不在 queue 时立即返回 `failed`；
  - `estimate_cny(seconds)` 返回 `0.0`。
- 改造 `h3ref/film.py` 与 `h3ref.py`：
  - 按 `video_settings()` 路由视频后端（续跑已提交任务时优先按 `rec.get("provider")` 路由），并发池大小按当前后端读取 `COMFYUI_CONCURRENCY` 或 `RH_CONCURRENCY`；
  - `h3ref.py account` 在 `comfyui` 模式下探活显示远端 GPU / 队列状态（或明确提示当前为 ComfyUI 自管算力）。
- **验证**：离线单测覆盖 1 / 2 / 9 张参考图动态构图、3 种 `COMFYUI_PRESET` 参数映射、Windows 反斜杠与中文路径上传构造、`videos`/`images` 双输出结构解析、实例重启丢失 `prompt_id` 快速失败、跨后端 `provider` 续跑隔离。

### 阶段 3：实现跨平台 Colab 启停控制层（`h3ref/colab_runtime.py`）
- 实现跨平台文件锁（Windows `msvcrt` / POSIX `fcntl`）与非阻塞后台状态机：
  - 状态集合：`online`（`/system_stats` 200）、`offline`、`cli_unavailable`（未装 `colab` 且 `/system_stats` 不通）、`provisioning`、`awaiting_drive_auth`（暴露授权 URL 与确认动作）、`starting_comfy`、`stopping`、`orphan_warning`（检测到 `[?]` 会话）、`error`。
  - `status()`：先探 `COMFYUI_URL/system_stats`；若已通直接返回 `online`（零 CLI 调用，Windows 无 CLI 同样直接就绪）；不通且有 CLI 时查 `colab sessions`。
  - `ensure_started()` / `start_async()`：`/system_stats` 已通则立即返回；否则串行执行会话复用或 `colab new -s <session> --gpu A100` → `colab drivemount`（POSIX `start_new_session=True` 捕获授权链接并支持 `submit_drive_authorized()` 写入换行继续）→ 通过 `colab exec --timeout 1200` 触发远端 `start_colab.py` 直至 `/system_stats` 返回 200。
  - `stop(force=False)`：检查本地活跃任务与远端 `/queue`，非空且 `force=False` 时拒绝并返回确认提示；确认后执行 `colab stop -s <session>`。
- **验证**：Mock `colab` / `colab.exe` 子进程与 `/system_stats`，覆盖 macOS/Linux 与 Windows 分支参数（`start_new_session` vs `CREATE_NO_WINDOW` + UTF-8 环境）、无 CLI 优雅降级、已在线零操作、Drive 授权暂停与续推、600s 超时恢复、并发点击锁保护、运行中终止拦截。

### 阶段 4：工作台设置、状态卡片、预检与 Windows 打包核验（`workbench/*`、`docs/windows.md`、`docs/workbench-cli-parity.md`）
- 预检改造（`workbench/inputs.py`、`workbench/segments.py`）：`VIDEO_PROVIDER=comfyui` 时校验 `COMFYUI_URL`（及配置了鉴权时的 `COMFYUI_API_KEY`），不再强制要求 `RUNNINGHUB_API_KEY`。
- 设置面板与状态卡片（`workbench/server.py`、`workbench/templates/base.html`、`workbench/static/common.js`、中英文语言包）：
  - 配置区支持切换 `VIDEO_PROVIDER`、`COMFYUI_URL`、`COMFYUI_API_KEY`（脱敏不回传明文）、`COMFYUI_PRESET`、`COLAB_AUTO_START`（使用 `<select>` 或显式布尔序列化，避免 `FormData` 漏提未勾选 checkbox），同步修正 `common.js` 顶部 `configReady` 判定逻辑；
  - 新增 `/api/runtime/status`、`/api/runtime/start`、`/api/runtime/drive-ack`、`/api/runtime/stop` 接口与顶部 **ComfyUI / Colab 运行状态卡片**（展示在线状态、启动阶段、Drive 授权链接+确认按钮、一键启动、带在途保护的终止按钮）；
  - 当进入视频生成操作（`create_job` 的 `one`、`continue_film`、`regenerate_segment`）且远端未就绪时，若本机具备 `colab` CLI 可按策略触发或提示拉起。
- 所有新增界面与报错文案完整写入 `zh-CN` / `en` 语言文件，严禁硬编码。
- 更新 `docs/windows.md`（说明 ComfyUI 模式在 Windows 下的零额外依赖用法及可选 `google-colab-cli` 安装/ADC 事项）与 `docs/workbench-cli-parity.md`。
- **验证**：
  - 跑通全套 `.venv/bin/python -m unittest discover -s tests -v`、Node 前端测试、四组 locale 键与占位符一致性校验、Python 编译与 JS 语法检查；
  - 干跑 `scripts/package_windows.py` 校验新模块与 `workflows/comfy-h3-ref2va.json` 完整进入 Windows 发布包且无非法路径/符号链接。

### 阶段 5：macOS 与 Windows 双端真机验收
- **macOS 真机**：验证开工作台只读探活不误开 A100 → 点击启动（含 Drive 授权确认与 SSD 权重预拷）→ 跑通 1～3 张参考图的 `4step` / `768p` 成片下载与播放 → 验证运行中点终止弹二次确认、空闲后终止释放 `colab sessions` → 临时调短看门狗阈值验证 `runtime.unassign()` 自动释放生效后再恢复为 **60 分钟**。
- **Windows 真机**：解压新打出的 Windows 发布包（放在含中文与空格路径下），验证配置 `COMFYUI_URL` + `COMFYUI_API_KEY` 后直连远端 ComfyUI 跑通图片上传、生成与成片下载（及安装 `colab.exe` 后的状态探活无黑框闪烁、无 GBK 编码报错）。
