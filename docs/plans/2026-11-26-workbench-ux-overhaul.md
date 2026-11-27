<!-- /autoplan restore point: "/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/main-autoplan-restore-20261126-222625.md" -->
## Implementation plan
# 本地工作台用户体验整体优化设计与实施计划

日期：2026-11-26  
状态：原型与核心决策已确认，待确认进入阶段 1 实施  
交互原型：[docs/design/workbench-ux-prototype.html](../design/workbench-ux-prototype.html)

---

## 1. 背景与目标

### 1.1 现状痛点

当前本地工作台（`workbench/`）经过多轮功能增量开发后，底层状态机与防重复扣费机制非常严谨，但在整体信息架构与高频操作体验上存在以下系统性问题：

1. **日志分散、查看不便**：Colab 启停日志藏在顶部卡片折叠里且双节点互相覆盖；任务日志默认折叠在结果页最底（限高 240px）；创建页看不到运行中任务日志；无搜索、错误筛选、复制功能。
2. **页面布局松散、操作按钮四处散落、高频滚屏**：每页顶部固定堆叠「大 Intro + Colab 算力卡 + 环境配置折叠条」；结果页主按钮分散在摘要卡、分镜 Tab 最底、片段 Tab 顶/中/底四处；成片播放器压在所有片段卡片和分页最下方；全局反馈条固定在页面最顶，滚到底部操作完全看不见；创建页角色参考图卡片过高，把运行模式与启动按钮顶出多屏。
3. **缺少任务暂停 / 停止能力**：任务一旦运行无法优雅暂停后续派发，也无法立即停止本地进程以释放工作台锁。
4. **高频创作与审片摩擦**：历史任务仅显示随机 8 位 ID 无剧本标题，结果页无法切换任务，无删除记录和一键打开本地文件夹；多节点/多段并发进度顶部只显示 1 段；分镜和改写阶段缺少实时进度条；分镜列表无状态筛选且点缩略图不能直达大图；参考图缺少拖放与按文件名自动匹配角色。

### 1.2 设计目标与非目标

**目标：**
- **零构建原生栈延续**：继续使用 Flask + Jinja2 + Vanilla JS + CSS Variables + 本地 Plyr，不引入 React/Vite 等前端构建器，保持 Windows 一键部署包与 `default-src 'self'` CSP 完全兼容。
- **操作常驻视口**：核心控制（暂停/停止、确认出片、续跑、重新拼接、创建页启动）统一收口到吸顶操作栏或右侧吸顶发车台，不再为了点一个按钮滚几屏。
- **唯一统一日志控制台**：全工作台共享底部可停靠、可拖拽调高的控制台抽屉，统一承载「当前任务日志 / Colab 主节点 / Colab 节点 2」，支持搜索、仅看警告/错误、一键复制与收起状态条。
- **安全可信的暂停与停止**：同时提供「暂停后续」和「立即停止」两个按钮，**绝不主动取消已在远端运行的任务，绝不清掉已获得的 `task_id`**，完全遵循“只停新派发、保护已付费成果”的既定原则。
- **中英双语一致**：全部新增/调整界面文案与后端提示均同步写入 `workbench/static/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json` 与 `h3ref/messages/{zh-CN,en}.json`，通过 `tests/test_locale_parity.py`。
- **CLI 零破坏**：`h3ref.py` 直接 CLI 运行行为保持 100% 不变。

**非目标：**
- 不接入 ComfyUI `/interrupt`、RunningHub cancel 或 APIMart cancel 等任何远端任务取消接口（用户明确拍板：正在运行的远端任务不用取消，只停止执行新任务）。
- 不允许多个生成任务同时并行运行（仍保持单活跃任务全局互斥，通过停止/暂停快速释放锁）。
- 不修改现有分镜提示词规则、视频生成工作流图与计费结算口径。
- 删除历史记录默认仅删除 `.workbench/jobs/<id>.{json,log}` 登记记录，**绝不自动删除磁盘上的输出目录与已付费素材**。
- 实时 SSE / WebSocket 日志与进度推流推迟至 `TODOS.md`（`CEO-E6`），本次继续沿用现有离线轮询。

---

## 2. 已确认的核心设计决策

| 决策项 | 结论 |
|---|---|
| 任务控制按钮 | 同时提供 **`⏸ 暂停后续`**（优雅等待在途项落盘后停）与 **`⏹ 立即停止`**（立即停本地进程、解锁工作台）两个按钮 |
| 远端在途任务处理 | **一律不取消远端正在执行的任务**（去掉 ComfyUI 可选中断勾选框），完整保留 `task_id`，续跑优先取回结果防重复扣费 |
| 页面整体外壳 | 固定 54px 顶栏 + 结果页吸顶指挥栏 / 创建页右吸顶发车台 + 底部可停靠统一控制台 + 右下角浮动 Toast |
| 低频面板归位 | Colab 算力收敛为顶栏状态胶囊 Popover（需授权/报错自动亮醒）；环境配置收敛为顶栏右侧分组滑出抽屉 |
| 高频扩展项 | 全部纳入：参考图拖放/按文件名自动匹配、JSON 格式化与摘要条、`sessionStorage` 草稿暂存（`?from=<job_id>` 优先并覆盖草稿，提交成功后清理）、`Esc` / `⌘/Ctrl+J` 快捷键与控制台 `localStorage` 状态记忆、历史记录带剧本名/快切/删除记录/一键打开本地文件夹/复刻新建、成片置顶折叠条、分镜状态筛选与直达大图、片段耗时费用展示、全阶段/多段并发进度聚合、动态浏览器标签页标题 |
| CSS 防折行约束 | 底部控制台工具栏 `input.console-search` 使用更高特异性覆盖全局 `input` 宽度，所有 `.cbtn` 强制 `white-space: nowrap; flex-shrink: 0; display: inline-flex;`，杜绝按钮文字换行溢出 |

---

## 3. 架构与详细设计

### 3.1 后端：任务暂停与停止引擎

#### 状态机扩充

在 `h3ref/control.py` 定义 `EXIT_CODE_PAUSED = 75`（供 `h3ref.py` 与 `workbench/*` 共同导入，避免 `h3ref/` 反向依赖 `workbench/`），在 `workbench/jobs.py` 中统一定义状态集合，替代散落各处的硬编码判断：

```python
# h3ref/control.py
EXIT_CODE_PAUSED = 75

# workbench/jobs.py
ACTIVE_STATUSES = ("queued", "running", "pausing", "stopping")
RESUMABLE_STATUSES = ("failed", "interrupted", "paused", "stopped")
SETTLED_STATUSES = ("succeeded", "failed", "interrupted", "paused", "stopped")
```

状态流转：
- `queued` / `running` → `POST /api/jobs/<id>/pause` → `pausing`（重复调用幂等返回 `200`；`stopping` 或终态返回 `400`）→ worker 收尾落盘后以 `EXIT_CODE_PAUSED` 退出 → `paused`（若恰好所有剩余项都已完成且无需进入 `media.join`，则正常结算为 `succeeded`；若进入 `media.join` 前已请求暂停则停在拼接前进入 `paused`）。
- `queued` / `running` / `pausing` → `POST /api/jobs/<id>/stop` → `stopping`（重复调用幂等不重复触发 `stop_child`；终态返回 `400`）→ 写入停止信号并在去重后台线程调用 `stop_child`（若抛出 `TimeoutError` 等异常，后台线程直接持 `self.lock` 校验 `operation_id` 后标记共享 `cleanup_failed=True`、转 `status="failed"` 保留 `pid`、`self.save(record)` 并 `run_event.set()`，杜绝 `child.stdout` 阻塞导致永久卡在 `stopping`），接口有界等待 `1.5s`（`operation_id` 作用域的 `Event` 在 `Jobs.launch()` 锁内启动线程前注册，并在 `self.save(record)` 之后才 `set()`），快速退出返回 `stopped`，慢退出返回 `stopping` 并由后台收尾。
- `paused` / `stopped` → `POST /api/jobs/<id>/resume` → 清理 `control.json` → `queued` → `running`，无缝复用所有已落盘产物与在途 `task_id`。
- 服务启动恢复（`Jobs.__init__`）：对磁盘上所有遗留的 `ACTIVE_STATUSES` 执行状态迁移——若 `not worker_locked(output_dir)`，`stopping` 置为 `stopped`（`error=None`）且其余置为 `interrupted` 并弹出 `pid`；若 `worker_locked(output_dir)` 为真，则全部（含 `stopping`）置为 `interrupted` 并保留 `pid`，防止重启后孤儿进程残留永久僵尸状态，同时保持 `available()` 互斥直到锁释放。

#### 协作控制文件协议 (`<output_dir>/inputs/control.json`)

- 格式：`{"action": "pause" | "stop", "requested_at": 1732680000.0, "operation_id": "..."}`。
- 写入采用原子替换（`write_json`）；`Jobs.create` 与 `Jobs.restart` 均生成 `operation_id=uuid.uuid4().hex`，`Jobs.launch()` 在 `self.lock` 内且 `thread.start()` 前清理 `control.json`（`unlink(missing_ok=True)`）并注册 `_run_events[identifier]` 与共享 `cleanup_failed` 标志，防止残留信号或 `queued` 竞态。
- 新增轻量工具模块 `h3ref/control.py`（零外部依赖，CLI 无该文件时零开销）：
  - `EXIT_CODE_PAUSED = 75`
  - `class JobPaused(Exception)`
  - `should_pause(job_dir: Path) -> bool`：检查 `inputs/control.json` 的 `action in ("pause", "stop")`。
  - `check_pause(job_dir: Path)`：满足时抛出 `JobPaused`。

#### Worker 协作暂停检查点（不取消已在途项，只停新派发）

| 模块 | 检查点位置 | 触发暂停时的行为 |
|---|---|---|
| `h3ref/film.py` `stage_frames` | 嵌套循环每个 shot 规划构图前后、提交 `pool.submit(draw, ...)` 前，**以及线程池内 `draw()` 调用 `frames.draw(...)` 前加 `if not task and should_pause(job.dir): return`**（用 `paused` 标志同时跳出内外双层循环，且显式透传 `JobPaused` 不被 `describe_composition` 的 `except Exception` 吞掉） | 阻止排队中的未提交镜头扣费提交；等待已提交的在途图下载落盘；**不把未开始镜头标为 `error`**；在写入 `frame_failures` 前若检测到暂停且仍有未完成目标镜则抛出 `JobPaused` |
| `workbench/storyboard.py` `regenerate` | `if not pending.get("downloaded"):` 内且仅当 `not pending.get("task")` 时，在 `demo.placeholder` / `frames.draw` 前调用 `check_pause(out)`（外层 `except (Exception, SystemExit)` 前显式透传 `JobPaused`） | 尚未提交远端任务时保留 `inputs/frame-edit.json` 原样并抛出 `JobPaused`（续跑继续完成该单镜）；已获得 `task` 的在途单镜继续轮询落盘 |
| `h3ref/film.py` `stage_prompts` | 每个 segment 改写 `try:` 外与派发前（`one()` 显式透传 `JobPaused`） | 跳过未开始段落，等待所有已在途改写落盘后抛出 `JobPaused` |
| `h3ref/film.py` `stage_generate` | 链式模式每段提交前；并行模式 `blocker` 分支与 `run_other` 获取信号量/调用 `gen_segment` 前；以及 `gen_segment` 中无 `task` 时在 upload / submit 前（`gen_segment`/`_generate_segment` 接受可选 `control_dir: Path | None = None`，默认 `job.dir`） | 立即设 `abort` 并唤醒 `node_pool.notify_all()` 阻止派发新段；**所有已提交的 `inflight_segs` 与 `blocker` 在跑段继续 poll→download→提取尾帧→写入 `state.json`**；所有在途段收尾完成后必须检查暂停信号并显式抛出 `JobPaused`，绝不落入 `media.join` |
| `workbench/segments.py` `_execute_rewrite_targets` | 每段改写派发前（外层显式透传 `JobPaused`） | 等待在途改写保存后抛出 `JobPaused` |
| `workbench/segments.py` `_regenerate` / `assemble` | `prepare_prompt` 前、`render_clip`/`gen_segment` 前（向 `gen_segment` 传入 `control_dir=out` 而非 staged 目录）、以及启动 ffmpeg 拼接前（外层 `regenerate` 显式透传 `JobPaused`） | 尚未提交/启动时直接抛出 `JobPaused`；已提交则等待落盘（若用户不等可直接点「立即停止」） |
| `workbench/demo.py` `run` | 每张占位图、每段演示视频生成前 | 检查暂停并抛出 `JobPaused`（同时确保离线演示可完整测试暂停/停止！） |
| `h3ref.py` `cmd_one` | `rewrite` 前、`submit_with_retry` 前 | 若尚未提交则抛出 `JobPaused`；已提交则等落盘退出 |

- 入口统一捕获 `JobPaused`（`h3ref.py`、`workbench/storyboard.py`、`workbench/segments.py`、`workbench/demo.py`）：打印一条国际化暂停完成日志，以 `sys.exit(EXIT_CODE_PAUSED)` 退出。
- `Jobs.run` 收到 `code == EXIT_CODE_PAUSED` 时：
  - 若 `record["status"] == "stopping"`（用户中途改点了立即停止）→ 置为 `stopped`；
  - 否则 → 置为 `paused`，`error = None`。
- `Jobs.stop(identifier)` 流程（`CEO-E4` 暂定默认，`d1` 留待 Phase 4 确认）：
  1. 校验任务存在且 `status in ACTIVE_STATUSES`（终态返回 `400`；若已在 `stopping` 则幂等等待同一收尾事件而不重复启动终止线程）；
  2. 写入 `control.json` (`action="stop"`)，设置 `record["status"] = "stopping"` 并持久化；
  3. 在锁外由去重后台线程调用 `stop_child(child)`（带 PID 守卫与 `child.terminate()/kill()` 回退；若 `stop_child` 抛出异常则立即持 `self.lock` 核对 `operation_id`，标记共享 `cleanup_failed=True`、转 `status="failed"` 保留 `pid`、`self.save(record)` 并 `run_event.set()`，杜绝 `child.stdout` 阻塞挂死），HTTP 接口有界等待 `Jobs.launch()` 预先注册的 `operation_id` 作用域 `Event` 最多 `1.5s` 后返回 `jobs.snapshot(identifier)`（快速退出为 `stopped`，慢退出为 `stopping`）；
  4. `Jobs.run` 在 `finally:` 且 `stop_child(child)` 完成后统一执行结算，在执行 `code == 0` 落盘后置变更（写 `report.md`/清 `dirty_segments`/`record_assembly`）前先检查 `stopping` 与本地/共享 `cleanup_failed`，严格按优先级结算：`self.closing` → `stopping` 且 `cleanup_failed=True`（`status="failed"` + 清理错误，锁存期间保留 `pid`，绝不被延迟解阻覆写为 `stopped`）→ `stopping` 且进程已退（`status="stopped", error=None`）→ `code == EXIT_CODE_PAUSED`（`status="paused", error=None`）→ `code == 0`（执行后置变更并置 `succeeded`）→ 其他（`failed`）；始终在 `self.save(record)` 之后再 `run_event.set()`，且仅当 `operation_id` 匹配时清理 `_run_events`；若在 `queued` 尚未 `Popen` 前收到 `pausing`/`stopping`，直接干净结算为 `paused`/`stopped` 且不走异常失败分支。
  5. **绝不修改 `state.json` 中已保存的 `task` / `submission_unknown` 字段**。

#### 关键续跑边界校验

- **分镜部分完成时暂停/停止**：`state.json` 里未画的镜头无 `file` 无 `error`；仅当 `record["status"] in SETTLED_STATUSES`、`bool(result["storyboard"])`、存在未完成/待取回镜头（`any(f["task_pending"] or (not f["url"] and not f.get("error")) for f in result["storyboard"])`，排除已有非 pending 错误的失败镜）、不存在进行中/状态未知的 `frame-edit.json`（`not frame_edit_task_pending(active_frame)` 且非 `submission_unknown and not task`），且非纯视频 `pipeline` 阶段或未应用 `operation=="frame"` 编辑任务时，`snapshot["can_resume_frames"]` 才返回 `True`；单镜重绘 `operation=="frame"` 在提交前暂停时**保留** `inputs/frame-edit.json` 原样不删，点击续跑时直接恢复 `operation=="frame"` 完成该单镜重绘，避免崩溃或误删已付费 `task_id`。
- **视频部分段在途时立即停止**：已提交的段在 `state.json` 中保有 `task`，`snapshot` 中显示 `task_pending = True`，`can_resume_video = True`；用户无法用新词覆盖该段（沿用现有 `segment_locked` 保护），但可以点「继续任务」先拉回该段结果并继续跑其余未完成段，也可以去启动别的任务。

---

### 3.2 后端：进度聚合、历史标题与管理 API

#### 新增 / 扩展 API 清单

| 路由 | 方法 | 用途与安全校验 |
|---|---|---|
| `/api/jobs/<id>/pause` | `POST` | 仅 `queued`/`running` 可转 `pausing`（`pausing` 幂等返回 `200`；`stopping` 与终态返回 `400`），成功返回 `200` 及 `jobs.snapshot(id)` |
| `/api/jobs/<id>/stop` | `POST` | `ACTIVE_STATUSES` 可转 `stopping`（`stopping` 幂等去重；终态返回 `400`），有界等待 `1.5s` 后返回 `200` 及 `jobs.snapshot(id)` |
| `/api/jobs/<id>` | `DELETE` | 持 `self.lock` 校验路径归属 `(storage/"jobs").resolve()` 且无活跃/探测/锁/存活进程（POSIX 下对 `interrupted`/`stopped` 且 `not worker_locked` 跳过 `process_alive(pid)` 防 PID 复用误拦），仅删除 `.workbench/jobs/<id>.{json,log}` 并淘汰内存与缓存（**不删输出目录**）；返回 `200` JSON `{"deleted": true, "id": id}`（未知 ID 返回 `404`，非法或活跃返回 `400`） |
| `/api/jobs/<id>/open-folder` | `POST` | 严禁前端传路径，校验已登记 `output_dir` 存在且为目录后按 OS 打开（macOS `subprocess.run(["open", "--", path], timeout=5, check=False)`；Windows `os.startfile`；Linux 检查 `shutil.which("xdg-open")` 缺失返回 `400`，存在则 `Popen(..., start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`），返回 `200` JSON |
| `/api/jobs/<id>/prefill` | `GET` | 基于白名单返回复刻配置（`kind`, 经 `describe_references()` 剥离全部本地路径的 `film`, `prompt`, `seconds`, `aspect`, `seed`, `check`, `frames`, `optimize_storyboard`, `demo`）；`request.json` 缺失或损坏返回 `400`/`404` `InputError` |

#### `record` 与 `snapshot` 扩展字段

1. **可读标题与汇总（用于顶栏任务快切 & 历史列表，`CEO-E2`）**：
   - `record["title"]`：创建任务时提取并持久化 `film.title`（为空则取输出目录名）或 `one` 的 `prompt` 首行前 32 字；终态结算时持久化 `progress_summary`（`frames_done`, `frames_total`, `clips_done`, `clips_total`, `cost_cny`）。
   - `summaries()` 在 `self.lock` 内仅快照目标 `record` 列表，随后在锁外（使用独立 `self._summary_cache_lock`）基于 `(record, request.json, state.json/demo-frames.json, clip.mp4, frames/, clips/)` 的 `mtime_ns/size` 缓存计算摘要，杜绝冷缓存磁盘 I/O 阻塞 `Jobs.run`；输出目录缺失时降级为已持久化摘要或零计数，`kind=="one"` 映射为 `clips_total=1`、`frames_total=0`。
2. **全阶段与多节点并发进度数组 `active_tasks`**：
   - 保留现有 `live_progress` 字段向后兼容，同时新增 `snapshot["active_tasks"]` 列表：
     - 视频并发生成时：收集所有带 `live_progress` 的在跑段，每项包含 `{kind: "segment", number, stage, step, max_steps, node_label, vram_used_gb, vram_total_gb, elapsed_s, queued, queue_position}`；
     - 分镜图阶段：新增轻量实时计数 `{kind: "frames", done, failed, total, stage}`（由 `state.json` 与已落盘图片计算，无需额外常驻线程）；
     - 提示词改写阶段：`{kind: "rewrite", done, total}`。
3. **成本与耗时明细**：
   - `snapshot["cost"]`：`{frames_cny, video_cny, total_cny, gpu_seconds}`（基于当前有效 `state.json` 实时累加）。
   - 每个 `storyboard` 卡片追加 `cny`；每个 `segment` 卡片追加 `cny`、`run_s`、`wall_s`、`provider`、`node_label`。
   - `snapshot["progress"]` 补齐总数：`{frames, frames_total, clips, clips_total}`。

---

### 3.3 前端：工作台外壳与页面重构

#### DOM 兼容性策略与测试同步

现有 `tests/test_workbench_ui.js`、`tests/test_results.js`、`tests/test_video_segment_prompts.js`、`tests/test_frame_character_picker.js` 共 3,531 行测试直接引用了一批核心元素 ID 及样式规则（如 `#config-form`、`#runtime-card`/`#runtime-start`、`#global-error`/`#global-message`、`#film-json`、`#storyboard-cards`、`#segment-cards`、`#continue-job`、`#assemble-job`、`#job-logs` 等）。
- **原则**：核心控件沿用既有 `id`，`showError`/`showMessage`/`clearNotices` 在触发 Toast 的同时继续同步更新 `#global-error`/`#global-message`；`style.css` 保留 `44px` 表单 `input`/`select`、`.header-tools select` `height: auto`、`.page-jump input`、`.artifact .plyr input`、`.checkbox-field input` `16px` 及 `max-width:68ch` 约束；阶段 2 同步更新少量因顶栏/抽屉结构变化而需调整的布局断言，并补充暂停/停止、统一控制台、批量匹配等新单测。

#### 模块拆分（保持零构建原生 JS）

在 `workbench/static/` 下合理组织模块，不把所有逻辑继续堆进 1000 行的单文件：
- `common.js`：全局状态、HTTP 请求封装、语言加载、**浮动 Toast 管理器**、**顶栏 Popover（历史快切 / Colab 算力）**、**右侧分组设置抽屉**、**底部统一可停靠日志控制台（含多 Tab、搜索、错误过滤、复制、拖拽调高、创建页活跃日志轮询、`Esc`/`⌘/Ctrl+J` 快捷键与 `localStorage` 高度/折叠/Tab 状态持久化）**。
- `create.js`：创建页表单编排、JSON 格式化与摘要条、`sessionStorage` 草稿暂存、**参考图拖放（按 `<= 9` 文件且 `<= 28 MiB` 分批上传）与按文件名自动匹配**、紧凑 2 列参考图与合图管理、右侧吸顶发车台、基于 `?from=<job_id>` 复刻填充。
- `results.js`：吸顶指挥栏渲染与动态主按钮状态机、**即时暂停 / 立即停止确认弹窗交互（经专用控制请求辅助函数绕开 `runJobAction` 的活跃态拦截）**、全阶段/多段并发进度条、成片置顶折叠条、分镜状态筛选与点击缩略图拦截直达大图、动态 `document.title` 更新。
- `video-segments.js` & `frame-references.js` & `pagination.js`：延续既有职责，保留 `selectResultTab`/`renderResultTabs` 在 `video-segments.js`（扩展为 3 Tab：`分镜图/剧本计划`、`视频片段`、`报告与文件`，并保持 null-safe 兼容既有单测），卡片上补充耗时/费用/节点胶囊展示。

#### 底部统一控制台 CSS 防回归硬规约

吸取静态原型截图暴露的特异性坑，正式样式中强制落实：
1. 控制台搜索框使用 `input.console-search`（特异性高于通用 `input[type="text"]`），显式设置 `width: 168px; flex: 0 1 168px; min-width: 100px; height: 26px;`。
2. 控制台工具区 `.console-tools` 使用 `display: flex; align-items: center; gap: 6px; flex: 0 0 auto; flex-wrap: nowrap;`。
3. 控制台所有按钮 `.cbtn` 强制 `display: inline-flex; align-items: center; justify-content: center; gap: 4px; white-space: nowrap; flex-shrink: 0; line-height: 1;`。
4. 窄屏（`<= 760px`）下自动隐藏非关键次级标签文字或折叠搜索框，绝不出现按钮文字垂直折行溢出。

---

## 4. 分阶段实施与验证计划

### 阶段 1：后端暂停/停止引擎 + 进度与历史管理 API
- **改动文件**：
  - 新增 `h3ref/control.py`
  - 修改 `h3ref/frames.py`（修复 `task_pending` 对无 `error` 强停在途分镜的判定）与 `h3ref/colab_runtime.py`（`MAX_SLOT_LOGS = 200` 统一用于缓冲、`_logs_tail` 与 `status()`）
  - 修改 `h3ref/film.py`、`h3ref.py`、`workbench/storyboard.py`、`workbench/segments.py`、`workbench/demo.py`（接入协作暂停检查点、`draw()` 队列守卫、`control_dir` 透传、`JobPaused` 显式透传与 `EXIT_CODE_PAUSED`）
  - 修改 `workbench/jobs.py`、`workbench/server.py`（`pause`/`stop`、`DELETE /api/jobs/<id>`、`open-folder`、`prefill`、`active_tasks`、`cost`、`title`/锁外缓存与 `stop_posix_group` PID 守卫）
  - 更新 `workbench/messages/{zh-CN,en}.json` 与 `h3ref/messages/{zh-CN,en}.json`；更新 `tests/test_colab_runtime.py`
- **验证方式**：
  - 新增后端单测 `tests/test_job_pause_stop.py`：覆盖分镜中途软暂停（等待在途落盘后进入 `paused`、未开始不提交、续跑接上）、立即停止（本地释放锁、保留 `task_id` 不删、续跑优先查询原任务不重复提交）、`control.json` 续跑自动清理、删除记录保留磁盘文件、打开目录越界防护、`prefill` 输出脱敏、`active_tasks` 多段并发聚合。
  - 运行全量 Python 离线测试 + `test_locale_parity.py` + `compileall`。

### 阶段 2：全局工作台外壳 + 统一底部日志控制台 + 算力/设置抽屉 + Toast
- **改动文件**：
  - 重构 `workbench/templates/base.html`、`workbench/static/style.css`、`workbench/static/common.js`
  - 更新 `workbench/static/messages/{zh-CN,en}.json`
- **验证方式**：
  - 更新/补充 `tests/test_workbench_ui.js`（顶栏历史快切、算力 Popover 授权切换、分组设置抽屉保留未保存草稿与隐藏 Tab 校验自动切换、底部控制台三 Tab 切换/搜索/错误筛选/收起展开、`Esc` 与 `⌘/Ctrl+J` 快捷键及 `localStorage` 持久化、Toast 与 `#global-error`/`#global-message` 同步）。
  - `node --check` 全部 JS、`node --test` 全部 JS 单测、Python 测试与双语键校验全绿。

### 阶段 3：创建页 & 结果页布局重构与交互提效
- **改动文件**：
  - 重构 `workbench/templates/index.html`、`workbench/templates/results.html`
  - 重构 `workbench/static/create.js`、`workbench/static/results.js`、`workbench/static/video-segments.js`
  - 完善 `workbench/static/style.css` 与 `workbench/static/messages/{zh-CN,en}.json`
- **验证方式**：
  - 补充前端单测覆盖：吸顶指挥栏 6 种状态按钮切换、暂停/停止确认流、分镜筛选与点击直达大图、成片置顶折叠、参考图按文件名自动匹配与冲突待分配、`sessionStorage` 草稿恢复、动态 `document.title`。
  - 跑全量 Python + JS 测试 + 语法/编译检查 + 双语一致性检查；提醒用户重启本地 Python 工作台并进行一轮 UI 实测反馈。

<!-- autoplan-accepted:ceo -->
- [CEO-H1] Fix `h3ref/frames.py` `task_pending(record)` to return `True` when `record.get("task")` is present, neither `task_failed` nor `moderation_error` is set, and either `record.get("error")` is truthy OR `not record.get("file")`, preventing hard-stopped in-flight APIMart frame tasks from unlocking frame edits and causing double billing; update `workbench/static/results.js` (lines 120-125) to render `frameTaskPending` / `frameSubmissionUnknown` notices whenever those flags are true even when `frame.error` is null ONLY while `!running(job)` (showing normal generating/waiting copy while `running(job)` is true); cover with regression tests in `tests/test_job_pause_stop.py` and `tests/test_results.js`.
- [CEO-H2] Complete pause/stop state-machine hardening across backend and frontend:
  - Define `EXIT_CODE_PAUSED = 75` in `h3ref/control.py` and import it in `workbench/jobs.py`, `h3ref.py`, `workbench/storyboard.py`, `workbench/segments.py`, and `workbench/demo.py` (keeping `h3ref/` free of `workbench/` imports).
  - Migrate all 6 active-status checks to `ACTIVE_STATUSES = ("queued", "running", "pausing", "stopping")`: `Jobs.__init__`, `has_active_work()` / `has_active_video_work()`, `Jobs.available()` (`workbench/jobs.py:456`), `Jobs._snapshot()` (`workbench/jobs.py:265,310` so `live_progress` stays visible while draining), `Jobs.summaries()` (`workbench/jobs.py:426` active sort), and `workbench/static/common.js:81` `running(job)`.
  - Migrate all 11 settled/resumable/stale-PID sites: `SETTLED_STATUSES` at `workbench/jobs.py:301` (`editable_base`), `584` (`edit_frame`), `793` (`start_video_operation`); `RESUMABLE_STATUSES` at `workbench/jobs.py:289` (`settled_dead_frame`), `352` (`pending_settled_dead`), `368` (`can_resume_pipeline`), `401` (`can_resume_video`), `867` (`Jobs.resume`), `workbench/storyboard.py:102` (`can_edit_frames`), `workbench/static/video-segments.js:85` (`reconcileSegmentSubmissions`); and POSIX stale-PID cleanup at `workbench/jobs.py:464` (`Jobs.available()`) covering `status in ("interrupted", "stopped")`. Keep failure display (`status in ("failed", "interrupted")` → `stageFailed`) separate from resumable actions (with dedicated `stagePaused` / `stageStopped` copy in `results.js`).
  - Explicitly catch and re-raise `JobPaused` before all 6 broad error handlers: `h3ref/film.py:474` (`stage_frames` around `describe_composition`), `h3ref/film.py:587` (`stage_prompts` `one()`), `h3ref/film.py:648` (`gen_segment`), `workbench/segments.py:1020` (`regenerate`, with pause checkpoints placed inside `_regenerate`), `workbench/segments.py:1303` (`_execute_rewrite_targets`), and `workbench/storyboard.py:255` (`regenerate`).
  - `stage_frames` (`h3ref/film.py:405-473`): add `if not task and should_pause(job.dir): return` inside `draw()` before `frames.draw(...)` so queued futures in `ThreadPoolExecutor(4)` never submit new paid APIMart requests after Pause; use a `paused` flag to exit BOTH nested segment/shot loops cleanly before/after `describe_composition()` and before `pool.submit`; after draining `futures`, if `should_pause(job.dir)` and any wanted frame is incomplete, raise `JobPaused` BEFORE writing `job.state["frame_failures"]`.
  - `workbench/storyboard.py:regenerate()`: inside `if not pending.get("downloaded"):`, call `check_pause(out)` ONLY when `not pending.get("task")` (before `demo.placeholder(...)` and `frames.draw(...)`), letting tasks with `pending.get("task")` finish polling/downloading; preserve `inputs/frame-edit.json` on pre-submit pause so `Jobs.resume` cleanly executes `operation == "frame"` without crashing at `storyboard.py:220`.
  - Add optional `control_dir: Path | None = None` (defaulting to `job.dir`) to `h3ref/film.py:gen_segment` and `_generate_segment`; pass `control_dir=out` from `workbench/segments.py:_regenerate` (lines 961, 984) and call `check_pause(out)` before `prepare_prompt` and before `render_clip`/`gen_segment`. Check `should_pause(job.dir)` before `upload_many`/`submit_with_retry` and in `stage_generate` `blocker` branch + `run_other`; wake `ComfyNodePool` via `abort.set()` + `node_pool.notify_all()`; wait for all in-flight rewrites/frames/segments to save, then explicitly raise `JobPaused` after draining in `stage_generate` before `media.join` (if all segments finish while paused, pause before `media.join` so resume goes straight to `assemble`).
  - Restrict `can_resume_frames` strictly to: `record["status"] in SETTLED_STATUSES` AND `bool(result["storyboard"])` AND `any(f["task_pending"] or (not f["url"] and not f.get("error")) for f in result["storyboard"])` (excluding non-pending errored frames) AND `not frame_edit_task_pending(active_frame)` AND `not (active_frame.get("submission_unknown") and not active_frame.get("task"))` AND never when `operation == "frame"` has an unapplied `frame-edit.json` (except existing `settled_dead_frame` / `status == "succeeded"` rules for other frames) or during `pipeline` video phase when `review.complete` is true.
  - Startup recovery (`Jobs.__init__`): transition ALL persisted `ACTIVE_STATUSES` — when `not worker_locked(output_dir)`, `stopping -> stopped` (`error=None`, pop `pid`) and others `-> interrupted` (pop `pid`); when `worker_locked(output_dir)`, transition ALL (including `stopping`) to `interrupted` while retaining `pid` so `available()` blocks until the orphan releases `worker.lock`.
  - `Jobs.create()` / `Jobs.launch()` / `Jobs.run()` lifecycle & settlement: set `operation_id = uuid.uuid4().hex` in `Jobs.create()` (matching `Jobs.restart()`); inside `Jobs.launch()` under `self.lock` BEFORE `thread.start()`, unlink `inputs/control.json` and register `_run_events[identifier] = (record["operation_id"], threading.Event())` + shared per-run `cleanup_failed` state; check `status in ("pausing", "stopping")` inside `self.lock` before `Popen` (settle directly to `paused`/`stopped` with `error=None` without entering `except Exception:`); in the background `stop_child` thread`s `except Exception`, acquire `self.lock` and (if `operation_id` matches and `status == "stopping"`) set shared `cleanup_failed=True`, transition `record.update(status="failed", error=localized_cleanup_error)` retaining `pid`, call `self.save(record)`, and `run_event.set()` immediately (preventing permanent `stopping` hangs if a descendant holds `child.stdout` open); in `Jobs.run()` `finally:`, finalize `stopping`/exit settlement AFTER `stop_child(child)` honoring both local and shared `cleanup_failed` (checking `stopping`/`cleanup_failed` BEFORE `code == 0` post-exit mutations `report.md`/`dirty_segments`/`record_assembly()`), with strict precedence: `self.closing` > `cleanup_failed` (`failed`, never overwritten by late unblock) > `stopping` clean exit (`stopped, error=None`) > `code == EXIT_CODE_PAUSED` (`paused, error=None`) > `code == 0` (post-exit mutations + `succeeded`) > `code != 0` (`failed`); call `self.save(record)` BEFORE `run_event.set()`, and clean up `_run_events` in `finally:` only when `operation_id` matches.
  - Guard `os.killpg` in `workbench/jobs.py:stop_posix_group` with `isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and pid != os.getpgrp()`, falling back to `child.terminate()` / `child.kill()` if `os.killpg` is skipped; add localized pause-complete message key to `h3ref/messages/{zh-CN,en}.json`; cover all transitions, races, and edge cases in `tests/test_job_pause_stop.py`.
- [CEO-H3] Complete API, derivation, and UI compatibility contracts:
  - HTTP contracts: `POST /api/jobs/<id>/pause` accepts `queued`/`running` (idempotent `200` on `pausing`, `400` on `stopping` or `SETTLED_STATUSES`) and returns `200` `jobs.snapshot(id)`; `POST /api/jobs/<id>/stop` accepts `ACTIVE_STATUSES` (idempotent `200` on `stopping` without spawning duplicate `stop_child`, `400` on `SETTLED_STATUSES`) and returns `200` `jobs.snapshot(id)`; `DELETE /api/jobs/<id>` acquires `self.lock`, verifies `(storage / "jobs").resolve()` parent containment and no `ACTIVE_STATUSES`/`_probing_launch`/`worker_locked`/`process_alive` (on POSIX skipping `process_alive(pid)` for `interrupted`/`stopped` when `not worker_locked(output_dir)` to prevent recycled-PID lockouts), deletes only `<id>.{json,log}` (never `output_dir`), evicts `self.records[id]` and `Jobs.summaries()` cache, returns `200` JSON `{"deleted": true, "id": id}` (`404` unknown, `400` invalid/active), and frontend stops polling + navigates to `/` when deleting the active page job; `POST /api/jobs/<id>/open-folder` rejects client paths, verifies `resolve().is_dir()`, uses macOS `subprocess.run(["open", "--", path], timeout=5, check=False)`, Windows `os.startfile(path)`, Linux `shutil.which("xdg-open")` (`400` if absent) + `subprocess.Popen(["xdg-open", path], start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`, returning `200` JSON `{"opened": true, "path": path}`; `GET /api/jobs/<id>/prefill` returns an explicit whitelist (`kind`, `film` via `workbench/references.py:describe_references()` with all local paths stripped, `prompt`, `seconds`, `aspect`, `seed`, `check`, `frames`, `optimize_storyboard`, `demo`) and returns clean `400`/`404` `InputError` if `inputs/request.json` is missing or corrupt.
  - `active_tasks` & `node_label` exact derivation: compute `node_label = comfy.endpoint_label(attempted.get("comfyui_url") or "") or None` inside `workbench/segments.py:view()` (safely handling RunningHub/legacy records without `comfyui_url` and staged `inputs/segment-<rev>/state.json`); return `active_tasks == []` whenever `status in SETTLED_STATUSES`; `frames` stage emits `{kind:"frames", done:0, failed:0, total:1, stage:"frame"}` for `operation == "frame"`, and `{kind:"frames", done, failed, total, stage}` (storyboard ready/failed/total counts via `state.json` or `demo-frames.json`/`frames/*.png`) for film `operation in ("frames", "pipeline")` before `review.ready`; `rewrite` stage (`rewrite_prompts`/`rewrite_segment` reading `prompt-rewrite.json`, or non-demo `pipeline` counting `.h3.txt` vs total segments ONLY while `done < total` and no segment has a `task` or `clip`) emits `{kind:"rewrite", done, total}`; `segment` stage builds entries from `result["segments"]` cards (each card with `live_progress` emits its ComfyUI progress + `node_label`, and each remaining `card["task_pending"]` emits `{kind:"segment", number, stage: provider_or_runninghub, step:null, max_steps:null, node_label}`, covering resumed RunningHub segments that retain transient `error` until download); `kind == "one"` emits a single `{kind:"segment", number:1, ...}` from `live_progress` or pending `task`.
  - Filename auto-matching & conflict tray (`create.js`): chunk drag-and-drop uploads sequentially into `POST /api/uploads` batches constrained by BOTH count (`<= 9` files) AND cumulative size (`<= 28 MiB`, headroom under Flask 32MB `MAX_CONTENT_LENGTH`), failing fast with a localized error Toast if any single file exceeds 28 MiB; normalize stem (lowercase, strip extension, trim `_- ` separators); exclude characters belonging to a multi-character cast group (`state.filmGroups`) from solo matching and match grouped slots ONLY at group level (normalized joined member names / group label, binding all `group.slotIds` identically per `workbench/references.py:87-92`); match `board` keywords (`board`/`storyboard`/`scene`/`场景`/`画板`), solo character `name`/`cast[].aliases`, and cast groups using exact-stem match first then unambiguous substring match; route ambiguities (1 file → >1 target, >1 file → 1 slot, unmatched file, or target slot already occupied) to `#upload-conflict-tray` with per-file target dropdown (`Assign` / `Replace` / `Dismiss`), never silently overwriting bound slots.
  - Sticky Command Bar 6 states, Pause/Stop confirmation, 3-tab visibility & storyboard filter/carousel (`results.js` + `video-segments.js`):
    - Pause triggers `POST /api/jobs/<id>/pause` immediately with Toast feedback; Stop opens a confirmation modal (closable via `Esc`, explaining remote tasks are not cancelled and `task_id` is preserved) before `POST /api/jobs/<id>/stop`. Both Pause and Stop use a dedicated control-action helper (with duplicate-click guard and poll rescheduling) rather than `runJobAction()` (which returns early whenever `running(state.selectedJob)` is true).
    - 6 Command Bar states: (1) `queued`/`running` → Pause + Stop; (2) `pausing`/`stopping` → disabled status badge (+ Stop during `pausing` only); (3) `paused`/`stopped`/`failed`/`interrupted` OR (`status === "succeeded"` && `(job.can_resume_frames || job.can_resume_pipeline)`) → primary `#resume-job` (kept disabled whenever `job.can_resume_video === false` while preserving `#resume-hint` for `terminal_failed`/`submission_unknown`/`moderation_failed`, matching `tests/test_workbench_ui.js:353-452`) + secondary Open Folder/Clone; (4) `Boolean(job.can_continue)` (i.e., `canGenerateVideo(job)`, never `kind == "one"`) → primary Confirm & Generate Video; (5) `Boolean(job.can_assemble && job.pending_assembly)` → primary Assemble Final Video (while keeping the Segments-tab `#assemble-job` button gated on `!locked && job.can_assemble` for manual re-assembly and test compatibility); (6) settled `succeeded` (final ready or `kind == "one"` done) → Open Folder/Download/Clone (or if `status === "succeeded"` && `frame_failures > 0` && `!job.can_resume_frames`, show repair-needed banner pointing to failed frame cards).
    - Keep `selectResultTab` / `renderResultTabs` in `workbench/static/video-segments.js` (extended to 3 tabs with null-safe DOM checks for `tests/test_video_segment_prompts.js:97`). Mode visibility: `film` (`frames != "none"`) shows `#tab-storyboard`, `#tab-segments`, `#tab-report` + pinned collapsible `#final-video-section` (`#final-video`); `frames == "none"` (`review.kind == "script"`) keeps `#tab-storyboard` visible (dynamically labeled via i18n as Script Plan) hosting `#script-section` (`#script-plan`) alongside `#tab-segments` + `#tab-report`; `kind == "one"` hides BOTH `#tab-storyboard` and `#tab-segments` and renders the single output video in the pinned `#final-video-section` above `#tab-report` (active by default).
    - Storyboard status filter + click-thumbnail-to-carousel: in list view, clicking a thumbnail calls `event.preventDefault()` on `<a class="storyboard-preview">` to switch into carousel mode at that frame (carousel view retains opening full image in a new tab); pagination and carousel prev/next operate on the active filtered subset and reset page/index to 0 whenever the filter changes.
  - Settings Drawer 3-tab mapping (`#config-form`): Tab 1 (`Video & Compute`): `VIDEO_PROVIDER`, `RUNNINGHUB_API_KEY`, `COMFYUI_URL`, `COMFYUI_API_KEY`, `COMFYUI_CONCURRENCY`, `COMFYUI_PRESET`, `COMFYUI_RESOLUTION`, `COMFYUI_UPSCALE`, `COLAB_GPU`, `COLAB_AUTO_START`; Tab 2 (`Image & LLM`): `APIMART_API_KEY`, `LLM_PROVIDER`, `LLM_API_KEY`, `GEMINI_API_KEY`, `LLM_COMPOSITION_MODEL`, `LLM_REWRITE_MODEL`, `LLM_BASE_URL`; Tab 3 (`Storage & Environment`): `#environment`, `#persist-config`, `#config-storage`. Attach a capture-phase `invalid` listener on `#config-form` to unhide the owning tab panel, activate its tab button, and call `reportValidity()`.
  - Create page active-log polling & test/DOM/CSS/CSP compatibility: on `/`, when `/api/bootstrap` reports an active job ID, poll `GET /api/jobs/<active_id>/logs?offset=<byte_offset>` at `1800ms` ONLY while the bottom console is expanded AND on the Job Log tab (stop immediately on collapse, tab switch, or job settlement); keep `#global-error`/`#global-message` updated in `showError`/`showMessage`/`clearNotices` alongside Toast; keep per-slot `managed_nodes[0/1].logs` separate for console tabs (mirroring active slot to `#runtime-logs` if present); preserve `style.css` `44px` `input`/`select`, `.header-tools select` `height: auto`, `.page-jump input`, `.artifact .plyr input`, `.checkbox-field input` `16px`, and `max-width:68ch` invariants; enforce zero inline JS / zero untrusted `innerHTML` under `script-src 'self'`; guard all browser globals (`window`, `document.body?.classList`, `sessionStorage`, `localStorage`, `document.addEventListener`, `document.querySelector`) for Node VM tests.
- [CEO-B1] Confirm create-page `JSON 格式化与摘要条` and `sessionStorage` draft (`h3.createDraft.v1` schema: `{kind, film_json, prompt, seconds, aspect, seed, check, frames, optimize_storyboard, demo, demo_placeholders}`, excluding file bindings; `restoreCreateDraft()` runs after `await langPromise` when no `?from=<job_id>` is present and overwrites `$("prompt").value` whenever `draft.prompt` is non-empty, leaving `t("samplePrompt")` only when no draft prompt exists; `?from=<job_id>` overrides draft; cleared on successful submit) as part of Stage 3 with unit tests in `tests/test_workbench_ui.js`.
- [CEO-E1] Define `MAX_SLOT_LOGS = 200` in `h3ref/colab_runtime.py` and apply it across all three sites: `_append_log` (line 1016), `_logs_tail` (line 1007), and `status()` (lines 1222-1223); audit hardcoded 15/80 assertions and add regression coverage in `tests/test_colab_runtime.py`.
- [CEO-E2] Persist `title` on creation and settled `progress_summary` (`frames_done`, `frames_total`, `clips_done`, `clips_total`, `cost_cny`) on settlement in job records; snapshot selected `record` dicts under `self.lock` then run mtime-keyed cache checks and disk reads OUTSIDE `self.lock` under `self._summary_cache_lock`, keyed by `(record, request.json, state.json/demo-frames.json, clip.mp4, frames/, clips/)` `(mtime_ns, size)` with graceful fallback when `output_dir` is missing on disk; map `kind == "one"` to `frames_total=0, clips_total=1`; verify in `tests/test_job_pause_stop.py`.
- [CEO-E3] Add `Esc` (close active popover/drawer/modal) and `⌘/Ctrl+J` (toggle bottom console when focus is outside inputs) shortcuts plus `localStorage` persistence for console height, collapsed state, and active tab in `workbench/static/common.js` (Stage 2); cover in `tests/test_workbench_ui.js`.
- [CEO-E4] (Provisional taste default, Phase 4 gate `d1`) `POST /api/jobs/<id>/stop` atomically persists `control.json` (`action="stop"`) and `status="stopping"`, dispatches `stop_child` in a deduplicated background thread outside `self.lock` (catching `Exception` to mark shared `cleanup_failed=True`, transition `stopping -> failed` retaining `pid`, `self.save(record)`, and `run_event.set()` immediately so blocked `child.stdout` cannot hang), waits up to `1.5s` on the `operation_id`-scoped per-run `Event` registered in `Jobs.launch()` before `thread.start()`, and returns `200` `jobs.snapshot(id)` (`stopped` on fast exit or `stopping` on slow exit); cover fast, slow, duplicate-stop, blocked-stdout `cleanup_failed`, and `queued` pre-spawn paths in `tests/test_job_pause_stop.py`.
- [CEO-E5] (Original default preserved; Dual-Model User Challenge, Phase 4 gate `uc2`) Deliver Stages 1, 2, and 3 as 3 sequential verified commits on a single feature branch / single PR (`pr_count=1`) so UI ships atomically while preserving granular rollback and test checkpoints.
- [CEO-H4] Block `DELETE /api/jobs/<id>` with HTTP `400` and localized message whenever any storyboard frame, video segment, `inputs/frame-edit.json`, or `inputs/segment-edit.json` has unsettled `task_pending` or `submission_unknown` (preventing history deletion from permanently orphaning the UI resume handle for paid in-flight tasks); warn explicitly in the Stop confirmation modal that stopping mid-HTTP-submit can leave a task in `submission_unknown` requiring provider dashboard verification; display the job's output folder path and an "Open Folder" shortcut inside the Delete confirmation modal; cover in `tests/test_job_pause_stop.py` and `tests/test_workbench_ui.js`.
- [CEO-H5] Keep billing semantics unchanged (Non-Goal #3) while labeling UI cost badges and tooltips accurately via i18n as "Known API cost (kept assets)" / "当前采用素材已知费用" and displaying ComfyUI `gpu_seconds` separately (e.g., `¥X.XX · Ym GPU`, noting discarded rerolls and self-hosted/Colab GPU compute are excluded from CNY) so creators do not under-budget; cover locale keys in `tests/test_locale_parity.py`.
- [CEO-H6] Add an auto-hiding cold-start setup banner at the top of `<main>` whenever `/api/bootstrap` reports missing required API keys or an unready video provider for the selected mode (1-click opens the owning Settings Drawer tab); render an inline 3-5 line recent log tail + "View full log in console" link inside the Colab Popover during `provisioning`/`starting`/`awaiting_auth`/`error`; default the unified bottom console to **collapsed (`32-36px` status bar)** on first visit (persisted in `localStorage` once toggled) with an unread warning/error badge so 85%+ of viewport height remains dedicated to frames and clips per `DESIGN.md`; cover in `tests/test_workbench_ui.js`.
- [CEO-E9] Add lightweight `GET /api/jobs/<id>/logs?offset=<byte_offset>` returning `{"status": ..., "offset": <next_byte_offset>, "chunk": <redacted_delta>, "truncated": <bool>}` (falling back to full redacted tail with `truncated: true` if file shrank/rotated, `404` on unknown ID) and use it for Create-page active-job console polling at `1800ms` (appending deltas without replacing DOM selection or re-reading/redacting 100KB snapshots); pass optional `should_stop: Callable[[], bool] | None` hook in `h3ref/control.py` / `h3ref/film.py` entrypoints (defaulting to `should_pause(control_dir or job.dir)`) to keep `h3ref/` model-agnostic; cover in `tests/test_job_pause_stop.py` and `tests/test_workbench_ui.js`.
<!-- /autoplan-accepted:ceo -->

<!-- autoplan-accepted:design -->
- [DES-H1] Compact Results Header, Single Primary CTA, Strict 6-State Priority & Paid-Action Framing:
  - Split `/jobs/<id>` top area into (a) a **single-line sticky command bar** (`<= 56px` desktop height: truncated title + tooltip, status badge, concise stage/cost summary e.g. `5/8 clips · 2 running` + `已知费用 ¥4.68 · 19.4m GPU` with info tooltip for full `CEO-H5` disclaimer, single primary CTA + consistent secondary `Open Folder` & `Clone` icon buttons across all loaded states) and (b) a **non-sticky scrollable telemetry/context strip** below it (collapsible per-node VRAM/step breakdown disclosure + `#ctx-notice` + `#job-error`).
  - Move the real `#resume-job` and `#continue-job` DOM nodes into the Sticky Command Bar (preserving existing IDs, eliminating duplicate primary CTAs); demote Segments-tab `#assemble-job` to secondary style when `pending_assembly` is true (keeping manual re-assemble as secondary when settled).
  - Lock strict `if / else if` Command Bar state priority: (1) `pausing`/`stopping` → (2) `queued`/`running` → (3) resumable (`paused`/`stopped`/`failed`/`interrupted` or `succeeded && (can_resume_frames || can_resume_pipeline)`, exposing explicit backend recovery action & blocker destination: `Retrieve pending results` when retrievable pending segments exist even if `can_resume_video === false` per `workbench/jobs.py:906`, `Resume Job` when resumable, or 1-click `Jump to blocking item → Seg X / Shot Y` scrolling/focusing the blocked card when `submission_unknown`/`terminal_failed` blocks resume) → (4) `Boolean(job.can_continue)` → (5) `Boolean(job.can_assemble && job.pending_assembly)` → (6) settled `succeeded` (`frame_failures > 0` repair banner vs finished).
  - Default pinned `#final-video-section` to expanded ONLY when `status === "succeeded" && !pending_assembly && !pending_video`; whenever `pending_assembly || pending_video`, collapse it to a `44px` banner AND preserve `"Download previous version"` / `"下载旧版成片"` across every download surface until new assembly succeeds.
  - Honor `DESIGN.md` paid-action framing: Live-mode primary CTAs carry explicit billing badge/copy (e.g., `Live · submits N segments` / `真实计费`) + amber accent framing; Demo mode shows explicit `(Free Demo)` / `(免费演示)`; ComfyUI segment cards display `GPU 142s · gpu1` (never `¥0.00`), while RunningHub cards display `¥X.XX · 142s`.
- [DES-H2] Responsive Breakpoint Contract (`1060px` / `760px` / `420px`), Hard Rejection #7 Fix & Console CSS Isolation:
  - Resolve Hard Rejection #7 on narrow Create page: at `<= 1060px`, render the Create launchpad as a **compact sticky bottom launch bar** docked above the collapsed console (preserving short product mark, Live/Demo billing badge, readiness summary, and primary Launch CTA so the action is never buried below long cast lists); collapse storyboard grid to 3 columns; keep a recognizable short product label (`H3 视频` / `H3 Video`) instead of bare `H3`.
  - At `<= 760px`: topbar History/Settings chips shrink to icon + count and Colab chip shrinks to status dot + `2/2`; ALL popovers use `position: fixed; left: 12px; right: 12px; width: auto; top: calc(var(--topbar-h) + 6px);` (preventing right-edge overflow); console toolbar groups Copy / Half-screen into an overflow trigger and moves search onto a `32px` sub-row when toggled.
  - At `<= 420px`: storyboard, segment, and cast grids collapse to 1 column; Toast stack spans `left: 12px; right: 12px; width: auto;`.
  - Fix CSS specificity: use `.console-tools input.console-search` (`(0,2,1)`, genuinely higher than `input[type="text"]` `(0,1,1)`); explicitly scope compact controls (`.console-tools input.console-search`, `.header-tools select`, `.conflict-tray select`) and align `.output-picker` button height to match `44px` input.
  - Split `--console-open-h` (clamped `clamp(120px, saved, 60vh)` on init and `window.resize`) from class-driven `--console-h` (`36px` collapsed, `var(--console-open-h, 230px)` open, `58vh` half-screen) so inline drag height never breaks `.console-collapsed`.
- [DES-H3] Truthful Collapsed-Console Unread Badge, Tab 1 Ownership, Plain-Text Traceback Filter & Scroll Lock:
  - Add lightweight per-job `log_alert_seq` / `log_alert_counts: {warn, error}` in `Jobs.summaries()` / `/api/bootstrap` (retained through settlement) and per-node `alert_seq` in `/api/runtime/status`, so the collapsed bottom console unread warning/error badge is truthful without downloading log bodies in the background; clear unread count per source ONLY when the console is expanded AND that tab is active (or on job switch).
  - Lock Tab 1 ownership: on `/jobs/<id>`, Tab 1 always binds to the currently viewed job — remove the legacy bottom `<details class="logs-section">` and move `#job-logs` directly into console Tab 1, updating via line-append diff on `job.logs` (preserving text selection); if another job A runs in background, surface it only via the topbar pulsing `Running: <A title>` chip without hijacking B's console. On `/`, Tab 1 binds to the active job via `GET /api/jobs/<active_id>/logs?offset=` (inserting a muted divider line when `truncated: true`).
  - Hide Colab console tabs when `VIDEO_PROVIDER !== "comfyui"`; show Colab Node 2 tab only when active, non-empty, or errored.
  - Implement a plain-text log level classifier that inherits `error` on indented Python traceback/stack continuation lines (`File "...", line ...`, indented frames) so `Warnings/Errors only` preserves full stack traces.
  - Keep bottom scroll-lock (`< 40px` from bottom auto-scrolls; scrolled-up shows floating `↓ Jump to latest` button) and distinguish all 4 console empty/fault states: `No active job`, `Waiting for first output`, `No matching lines`, and `Connection lost (last updated HH:MM:SS)`.
- [DES-H4] Topbar Navigation Clarity, `<select id="language">`, Local-Only Badge, Cold-Start Banner & Colab Popover:
  - Replace static `Current Job` nav link with contextual entry: hidden at 0 history; pulsing `Running: <short title>` whenever any active job exists; `Latest job` on `/` when idle with history.
  - Keep `<select id="language">` inside `.header-tools` (styled to match topbar chips) and keep `local-badge` ("Local only") visible in the topbar or Settings Drawer header/footer.
  - Auto-hide the cold-start setup banner whenever Create is in Demo mode or Results views a demo/settled job; in Live mode, offer primary `Open Settings tab` + secondary `Try Offline Demo`.
  - Settings Drawer: `Cancel` resets form values to `state.config` and clears dirty indicator; backdrop/`Esc`/`✕` preserves unsaved draft and marks dirty indicator on drawer footer + Settings chip; Settings topbar amber dot indicates missing Live config/ffmpeg or unsaved draft only.
  - Colab Popover (`#runtime-pop`): `width: 440px; max-height: calc(100vh - var(--topbar-h) - 56px); overflow-y: auto;`, preserving ALL existing `#runtime-*` IDs (CLI auth code form, Drive auth box, primary/secondary start/stop, force-stop confirm) plus the 3–5 line inline log preview; hide Colab chip (or show compact `RunningHub Cloud` settings link) when `VIDEO_PROVIDER !== "comfyui"`; auto-alert lights topbar dot + notice without stealing keyboard focus mid-typing.
- [DES-H5] Pause/Stop/Recovery Emotional Continuity, Honest Status Copy & Resilient Results States:
  - During `pausing`, keep all in-flight real step/percentage bars visible beneath an amber notice (`Stopped dispatching new items · waiting for N in-flight items to save`) — never replace live bars with an indeterminate bar.
  - Name Stop accurately (`Stop Local Worker` / `停止本地执行`): reconcile Stop modal copy between preserved `task_id` and the narrow mid-submit `submission_unknown` window (removing contradictory "never double-bills" wording); hide `Switch to Pause` when already `pausing`; persistently show post-stop local status PLUS explicit badges/links for `N retrievable remote tasks`, `Seg X needs provider verification`, or `Local process still holds lock` on `cleanup_failed`.
  - Add `has_unsettled_tasks: bool` to `Jobs.summaries()` so history list rows visibly disable Delete (`🗑`) with tooltip reason (matching `CEO-H4` backend 400) alongside a hover/focus `Clone` action; Delete modal explicitly states "History record only — output folder on disk is untouched", shows readonly monospace `output_dir` + `Open Folder`, and focuses `Cancel` by default.
  - Render `#job-loading` skeleton cards; render persistent inline error panels with `Back to Create` / `Retry now` on 404 or poll disconnect (never relying on auto-dismissing Toast); hide `Frames 0/0` metric when `kind === "one"` or `frames_total === 0`.
- [DES-H6] Batch Upload Partial-Failure Loop, Cast-Group Card Preservation & Clone/Draft Recovery Clarity:
  - Prevent accidental browser image navigation via global `window` `dragover`/`drop` listeners; style `.dropzone.drag-active` and per-card `.cast-card.drag-over` (direct single-slot drop bypassing filename match); wire drag-and-drop into `one` mode `.upload-zone`.
  - Show batch progress (`Uploading batch 1/2 (9/15)`); on partial failure, retain all succeeded bindings, display `Bound X / Needs assignment Y / Failed Z` counts, and retry ONLY failed/un-uploaded files.
  - Mount `#upload-conflict-tray` directly above `#cast-grid` (each row: `40x40` thumb, filename, conflict reason badge, target `<select>` prioritizing empty slots and labeling occupied slots `Replaces existing`, `Apply` / `Dismiss`, plus `Dismiss all`), syncing options whenever film JSON changes.
  - Preserve **multi-character cast group cards** (`state.filmGroups` spanning 2 cols or stacked avatars + member add/remove + group builder) and `peakFilmReferences` (>9 refs warning) in the 2-column compact grid.
  - Label Clone accurately (`Clone Script & Settings` / `复刻剧本与参数` under `d2` Option A); back up non-empty `h3.createDraft.v1` before applying `?from=<job_id>` (preserving original draft if clone fails, and showing a dismissible banner explaining references were not copied + `Restore previous draft` undo button); on draft restore, explicitly indicate how many references need attachment and add a `Clear / Reset` button with confirmation.
- [DES-H7] Frictionless Storyboard Review Loop (Draft Guard, Whole-Area Click, Key-Anchored Carousel, Full Filters & Grid Alignment):
  - When unapplied `frameDrafts` or `frameReferenceDrafts` exist, clicking `#continue-job` prompts a confirmation dialog (`N frames have unapplied prompt/reference edits — generate video with current saved images?`).
  - Make the entire `.storyboard-media` preview box (including waiting/failed placeholders without `<a>`) keyboard-focusable and clickable to enter carousel mode.
  - Anchor carousel state by `frame.key` (not array index): if repairing a failed frame removes it from the active `Failed` filter, keep viewing that frame with a top pill `Fixed · Next failed frame →`.
  - Expand storyboard filter pills to `All` / `Ready` / `Pending / In-Flight` (waiting, `task_pending`, `submission_unknown`, shown when count > 0) / `Failed` / `Drafts`, with an empty-filter state + `Show all frames` button.
  - Force `align-items: start` on `.frame-grid` and `.seg-grid` so expanding one card never stretches sibling cards.
- [DES-H8] Contrast (`>= 4.5:1`), Touch Targets, Top-Right Toast + Single Live Region, Focus Trap, Keyboard Console & Reduced Motion:
  - Replace prototype's `#74877c` (`3.82:1` FAIL) on `.frame-meta` and small metadata with `var(--muted)` (`#52655a`, `6.24:1` PASS on white); preserve `2px solid var(--accent)` focus rings on light surfaces and add high-contrast light focus ring (`#a9e5c6`) inside the dark `#263c30` log terminal.
  - Under `@media (pointer: coarse)`, provide `44px` minimum hit targets for filters, transport buttons, and expanded console actions; make collapsed console bar a single full-width clickable/keyboard trigger; provide keyboard height presets (`Collapsed / Compact / Half-screen`) alongside a focusable `role="separator"` drag handle (`ArrowUp`/`ArrowDown`).
  - Position `.toast-stack` at **top-right below topbar** (`top: calc(var(--topbar-h) + 12px); right: 18px; z-index: 95;`, above Modal `90` & Drawer `80`, never covering Settings Save); remove `$("global-error").scrollIntoView()` in `common.js:28`; style `#global-error`/`#global-message` as `.sr-only` (acting as the SINGLE screen-reader live region when `d3` Option A is active, with visual Toasts marked `aria-hidden="true"` to prevent double announcements); cap at 3 stacked Toasts, `4200ms` info/success (pause on hover), `8000ms` or manual close for error Toasts.
  - Stop/Delete modals require `aria-labelledby`, `Tab` focus trap, initial focus on `Cancel`, focus restoration to opener on close, and blocking `←/→` + `⌘/Ctrl+J` while open. Enforce LIFO `Esc` stack (`Modal -> Settings Drawer -> Popover`, never closing bottom console). `⌘/Ctrl+J` calls `event.preventDefault()`, checks `!event.isComposing`, and ignores form inputs/`contentEditable`. Add a re-entrancy guard (`validatingTabs`) on the capture-phase `invalid` listener so revealing a hidden tab + calling `focus()`/`reportValidity()` cannot loop across two invalid hidden tabs.
  - Remove competing continuous pulsing dots on idle/online indicators (reserve pulse strictly for `pausing`/`stopping` or indeterminate states) and honor `@media (prefers-reduced-motion: reduce)` across all new animations.
- [DES-H9] Prototype Authority Table, `document.title` / Tab 3 Spec & Bilingual Browser Acceptance Checklist:
  - Declare this plan (plus `CEO-*` / `DES-*` clauses) as single source of truth superseding `docs/design/workbench-ux-prototype.html` across 8 changed areas (collapsed console default, 3-tab Settings grouping, `<select id="language">`, reconciled Stop modal copy, Delete modal w/ path+Open Folder, `CEO-H5` cost+GPU labels, full Colab Popover flows, and Create `one`/cast-group/conflict-tray/cold-start banner).
  - Define exact localized `document.title` templates per job state and Tab 3 badge count (non-storyboard artifacts matching `results.js:243`) + pre-report empty state.
  - Add a bilingual (`zh-CN` & `en`) browser visual & keyboard acceptance pass across `1440px` / `1060px` / `760px` / `420px` verifying cold start (live/demo), loading/404/bootstrap fail, empty history, partial batch upload, keyboard-only hidden-tab validation + Stop cancel, and stale-film download label.
- [DES-T1] (Provisional taste default, Phase 4 gate `d4`) Keep desktop storyboard grid at 3 columns (`>= 1060px`) with `align-items: start` and expand `.frame-editor` full-row (`grid-column: 1 / -1`) to preserve spacious inline prompt + character picker editing and 100% compatibility with `tests/test_frame_character_picker.js` (alternative Option B: 4 columns clicking Edit into Carousel mode).
<!-- /autoplan-accepted:design -->

<!-- autoplan-accepted:dx -->
- [DX-H1] Run-Scoped Pause Boundary & Zero-CLI-Break `control.json`:
  - `Jobs.launch()` passes `H3_WORKBENCH_OPERATION_ID=<operation_id>` alongside `H3_WORKBENCH_WORKER=1` to every managed subprocess, and `POST /api/jobs/<id>/pause` & `/stop` persist `operation_id` inside `<output_dir>/inputs/control.json`.
  - `h3ref/control.py:should_pause(job_dir, should_pause_cb=None)` honors `inputs/control.json` ONLY when `os.environ.get("H3_WORKBENCH_WORKER") == "1"` AND `control.json.get("operation_id") == os.environ.get("H3_WORKBENCH_OPERATION_ID")` (or when an explicit `should_pause_cb` returns `True`), guaranteeing that direct CLI `python h3ref.py film <out>` / `one --out <out>` is never trapped at exit 75 by a leftover workbench `control.json` (while preserving direct CLI lock and pending-edit guards). Corrupt or unreadable `control.json` logs a redacted `stderr` warning (file path + exception class) and degrades safely instead of crashing an in-flight paid run.
  - Enforce the run-scoped pause check immediately before EVERY fresh outbound provider generation POST: (a) inside `h3ref/rh.py:submit_with_retry()` before each `submit(g)` AND using 1-second interruptible sleep slices during the 60-second `QueueFull` (421/415) backoff (raising `JobPaused` with zero new POST if paused during backoff), and (b) inside `h3ref/frames.py:draw()` after reference upload and immediately before the APIMart generation POST (preserving retrieval for any already-accepted `task_id`).
  - On `JobPaused` (`EXIT_CODE_PAUSED = 75`), emit a structured diagnostic to `sys.stderr` (never `stdout`) containing stable code `JOB_PAUSED`, triggering file and `operation_id`, `side_effects: partial` (completed assets and accepted `task_id`s saved to `state.json`), exact recovery action (`POST /api/jobs/<id>/resume` or direct CLI resume, noting automatic retry resumes paid work), and `docs/workbench.md#job-control-errors`, then exit 75 without prompting.
- [DX-H2] Eliminate False-Success HTTP 200 Responses + Bound Termination Waits + `operation_id` Precondition:
  - `POST /api/jobs/<id>/open-folder`: macOS runs `subprocess.run(["open", "--", path], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5, check=False)` and returns HTTP `500` RFC 9457 `job_problem` (`code="open_folder_failed"`, redacted stderr + exit code + `context.path` + manual open command in `fix`) when `proc.returncode != 0`; Linux checks `shutil.which("xdg-open")` AND `os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")` (missing -> HTTP `400` `code="open_folder_headless"` or `"open_folder_unavailable"` with `context.path` + copy-paste command), spawns `Popen` with `stdin=subprocess.DEVNULL`, polls up to `0.2s` to catch immediate non-zero exit -> HTTP `500`, and on async launch returns HTTP `200` `{"dispatched": true, "opened": true, "path": path}`; catch `subprocess.TimeoutExpired` and `OSError` (including Windows `os.startfile`) -> HTTP `500` RFC 9457 with `instance` trace and `context.path`; missing output directory -> HTTP `404` `job_output_dir_missing`; client-supplied `path` payload key -> HTTP `400` `open_folder_client_path_forbidden` (`field="path"`). Never return HTTP `200` on observed failure.
  - `POST /api/jobs/<id>/stop` & `stop_posix_group`: replace BOTH unbounded `child.wait()` calls in `workbench/jobs.py:132,134` with bounded `child.wait(timeout=5)` (raising `TimeoutError` on `TimeoutExpired` so `cleanup_failed` triggers deterministically and never hangs).
  - Accept optional `wait_seconds` (`0..15`, default `1.5`) and optional `operation_id` precondition on `/pause` and `/stop` (UI always sends observed `job.operation_id`). If `operation_id` is provided and does not match `record.get("operation_id")`, reject with HTTP `409 Conflict` RFC 9457 (`code="job_operation_mismatch"`, `context={expected_operation_id, current_operation_id, current_status}`, `fix="Re-read GET /api/jobs/<id> before acting"`).
  - After `run_event.wait(wait_seconds)`: (1) if `cleanup_failed` is observed (`status == "failed"` with `pid` retained), return HTTP `500` RFC 9457 `job_problem` (`code="job_stop_cleanup_failed"`, `instance`, `context={job_id, operation_id, pid, lock_path}`, safe OS inspection/termination `fix`, `recovery={action:"ask_human", side_effects:"unknown", remote_cancelled:false, max_retries:0}`, plus `job` snapshot extension) — never HTTP `200`; (2) if still `stopping`, return HTTP `202 Accepted` with `{status:"stopping", operation_id, poll_url:"/api/jobs/<id>", job:...}` (`common.js` `response.ok` accepts `200`/`202`); (3) if cleanly `stopped` (or idempotent replay on matching `operation_id`), return HTTP `200 OK`.
- [DX-H3] Agent-Ready RFC 9457 Error Contract Across All 6 New Routes + `/api/config` + `common.js`:
  - Add `job_problem(code, detail, status, *, fix, recovery=None, context=None, field=None, instance=None, **extensions)` in `workbench/server.py` wrapping `api_problem()` (`doc_anchor="docs/workbench.md#job-control-errors"`), and recursively redact every string inside `api_problem` `extensions` (nested dicts/lists) via `jobs.redact` so secrets never leak through extension fields.
  - Route `pause_job`, `stop_job`, `delete_job`, `open_job_folder`, `job_prefill`, `job_logs`, and `configure` (`/api/config`, including `field`) through `api_problem()` in `input_error`, `http_error`, `unexpected_error`, and `local_requests` (missing `X-Workbench: 1` / invalid `Origin` on these routes). Every unexpected `500` writes a redacted `correlation=<instance>` trace to `.workbench/jobs/<id>.log` (or `.workbench/server.log`) and returns `instance` + explicit `recovery.side_effects`.
  - `GET /api/jobs/<id>/prefill` distinguishes missing `inputs/request.json` (`404 job_prefill_missing`) from corrupt/invalid JSON (`400 job_prefill_corrupt` with file and parse line) and returns `references_omitted: true` + `omitted_reference_count`.
  - Upgrade `workbench/static/common.js:request()` to safely parse JSON (falling back to HTTP status + truncated response text for non-JSON 413/502 bodies instead of throwing `SyntaxError`), attach RFC 9457 metadata (`code`, `fix`, `field`, `recovery`, `instance`, `doc_anchor`) onto the thrown `Error`, and switch Settings Drawer to the tab owning `error.field` + focus that control on `POST /api/config` `400`.
- [DX-H4] Authoritative `unsettled_tasks()` Covering `kind=="one"`, Safe Delete Ordering, & `cleanup_failed` Ownership Check:
  - Implement a single authoritative `unsettled_tasks(job_dir)` helper shared by `Jobs.summaries()` (`has_unsettled_tasks`) and `DELETE /api/jobs/<id>`, scanning ALL 5 locations: (1) root `state.json` for `kind=="one"` (`task` without completed clip or `submission_unknown`), (2) storyboard `frames`, (3) `segments`, (4) `frame-edit.json`, and (5) `segment-edit.json`. On block, `DELETE` returns HTTP `400` RFC 9457 (`code="job_delete_unsettled_tasks"`, `context={job_id, status, blocking_items:[{kind, key_or_number, provider, state, task_id, file}]}`, concrete `fix` distinguishing `/resume` vs provider reconciliation, `recovery={action:"do_first", side_effects:"none", reconcile:"GET /api/jobs/<id>"}`).
  - `DELETE` removes `<id>.log` first (if Windows handle lock fails, returns `500 job_delete_io_error` with `side_effects: "none"`), then `<id>.json` (`side_effects: "partial"` on failure), then evicts memory cache last.
  - On startup (`Jobs.__init__`) and before active-job mutual-exclusion checks, verify worker ownership via `worker_locked(output_dir)` (not `os.kill(pid, 0)` alone). If `status == "failed"` with `cleanup_failed=True` and `not worker_locked(output_dir)`, clear stale `pid`, record `cleanup_resolved=True`, and persist so recycled OS PIDs never permanently block launches.
- [DX-H5] Generation-Tracked Bounded Incremental Logs + Consistent Field & Key Naming:
  - `GET /api/jobs/<id>/logs?offset=<n>&limit=<m>&generation=<g>`: validate `offset` as non-negative decimal integer (else `400 invalid_log_offset`, `field="offset"`, `fix="Pass offset=0"`); bound `limit` (`default=100000`, `max=262144`); emit a rotation-sensitive `generation` token (inode + first-line hash; equal-size replacement changes `generation`, triggering `reset: true` when mismatched or `offset > file_size`); redact complete lines BEFORE slicing and clamp `next_offset` to the last complete UTF-8 line boundary (`truncated: bool`, `reset: bool`, `generation`, `next_offset`).
  - `h3ref/colab_runtime.py`: allow `H3_COLAB_MAX_LOGS` env override (`default=200`, validated `20..2000`) and emit `logs_truncated: bool` per node in `/api/runtime/status`.
  - Unify naming across API & client: align `Jobs.summaries()` progress object (`progress: {frames, frames_total, clips, clips_total}` + `cost_cny`) with `Jobs.snapshot()`, unify `log_alert_seq` / `log_alert_counts` across jobs and runtime nodes, use `should_pause` consistently as the callback parameter in `h3ref/control.py`/`film.py`, name console persistence key `h3.consoleState.v1`, serialize absent optional `active_tasks` metrics as `null` (fixed schema), and support `?include_logs=0` on `GET /api/jobs/<id>` (default `1`).
- [DX-H6] 2-Click Offline Demo Onboarding + Short-Name Auto-Match Guard & Discoverability Hints:
  - Add 1-click **"Load Sample Script / 载入示例剧本"** beside `#film-json` (`fetch("/api/film/example")` directly into `#film-json` + auto-parse; retain Download Sample for offline editing). When `#film-json` is empty, clicking Cold-Start Banner's **"Try Offline Demo / 试离线演示"** switches to Demo, enables `demo-placeholders`, and loads the sample script so browser TTHW drops from ~9 steps to 2 clicks (`< 15s` post-launch, `< 2 min` end-to-end).
  - Filename auto-match restricts short cast names/aliases (`len(name) < 2`) to exact-stem or separator-boundary match (no arbitrary substring matching), provides an **"Undo auto-match / 撤销本次自动匹配"** action after batch drop, and includes `{filename}`, `{actual_mb} MiB / 28 MiB`, and compression hint in `> 28 MiB` Toasts.
  - Add in-UI discoverability hints: console toggle tooltip shows `⌘J / Ctrl+J`; dropzone placeholder shows filename match examples (`board.png`, `小林.jpg`).
- [DX-H7] Operator, Agent & Upgrade Documentation Deliverables + Automated Doc Smoke Test:
  - Stage 1 updates `docs/workbench.md` (`#first-offline-job` copy-paste runnable offline demo `curl` with `-H 'X-Workbench: 1'`, ID capture, bounded polling, expected artifact; `#job-control-errors` 6-route table + exit-75 table + paid-side-effect column; quiescent upgrade/rollback guide backing up BOTH `output_dir` AND `.workbench/jobs/*.json` and settling `pausing`/`stopping` before downgrade), `docs/workbench-cli-parity.md` (run-scoped `control.json` + exit 75), and `SKILL.md` (agent job-control & exit-75 rules).
  - Stage 3 updates `README.md` (new workbench layout + side-by-side 0.06s keyless `h3ref.py preflight` CLI and 2-click Workbench Offline Demo quickstart) and adds a supersession banner to `docs/design/workbench-ux-prototype.html`.
  - Add an automated test in `tests/test_workbench.py` executing the documented offline `First Offline Job` + job-control RFC 9457 error sequence end-to-end across both locales (`zh-CN`/`en`) so docs never drift.
- [DX-T1] Provisional Unsettled-Task Delete Escape Hatch (`d5`):
  - Keep `CEO-H4` `400` block by default for any unsettled task, plus an explicit non-active-only escape hatch `DELETE /api/jobs/<id>?force=1` requiring JSON body `{"acknowledge_unsettled": true}` (still NEVER deleting `output_dir` and still returning `400` for active/running jobs) for abandoned-account cleanup.
<!-- /autoplan-accepted:dx -->

<!-- autoplan-accepted:eng -->
- [ENG-H1] Worker Admission Boundary, Complete `JobPaused` Passthrough & Pre-Existing Task Drain:
  - Enforce `h3ref/control.py:check_pause(job_dir, should_pause_cb=None)` strictly BEFORE writing `submission_unknown=True` and immediately before every fresh outbound provider/LLM attempt across ALL 7 locations: (1) `h3ref/frames.py:draw()` post-upload / pre-POST, (2) `h3ref/frames.py:describe_composition()` (`h3ref/frames.py:132`) between retries, (3) `h3ref/rewrite.py` (`h3ref/rewrite.py:322`) between retries, (4) `h3ref/rh.py:submit_with_retry()` (`h3ref/rh.py:121`) before `record_submission(True)` + 1s interruptible sleep slices during 60s `QueueFull` backoff, (5) `h3ref/comfy.py:submit_with_retry()` (`h3ref/comfy.py:452`) before `record_submission(True)` + inside `ComfyNodePool.acquire()` (`h3ref/comfy.py:994-1010`) on each `0.5s` wait iteration via optional `should_pause` callback, (6) `h3ref/film.py:run()` (`h3ref/film.py:1426-1437`) between stages, and (7) `workbench/segments.py:_regenerate()` (`961,971,982`) ONLY when the staged attempt does NOT already carry an accepted `task` (allowing existing accepted staged rerolls to finish download/publication).
  - In `h3ref/film.py:stage_frames()`, enumerate and schedule all wanted shots that already have a saved accepted `task` (or `task_pending`) BEFORE admitting unsubmitted shots (and never skip past remaining accepted `task` shots when breaking on Pause), so remote tasks accepted by an earlier worker always drain to disk.
  - Add `except JobPaused:` passthrough BEFORE the 3 missed broad `except Exception:` handlers in addition to the 6 in `CEO-H2`: (a) `h3ref/film.py:413` (`stage_frames.draw`: `except JobPaused: return` without calling `failure(key, exc)`), (b) `h3ref/film.py:748` and `h3ref/film.py:772` (`_generate_segment` ComfyUI multi-node upload & submit failover: `except JobPaused: raise` before `node_pool.mark_unhealthy`, releasing `leased_endpoint` cleanly in `finally:`).
  - In parallel `stage_generate` and `stage_frames`, give real exceptions strict precedence over `JobPaused`: if `first_error` is `None` or `isinstance(first_error, JobPaused)` and a non-`JobPaused` exception arrives, replace `first_error = exc` so real in-flight failures are never masked as clean `status="paused", error=None`.
- [ENG-H2] Cross-Platform Process Ownership (POSIX vs Windows), `RunState` Immutable Settlement & `Jobs.run()` vs `T_stop` Serialization:
  - Gate `not worker_locked(output_dir)` stale-PID resolution strictly to POSIX (`os.name != "nt"`), and treat POSIX `worker_locked()` `OSError` as locked/unknown (fail closed) rather than returning `False`. On Windows (`os.name == "nt"`), NEVER clear `pid` via `worker_locked()` — verify emptiness via `WindowsJob` `ActiveProcesses == 0` (when in-process handle exists) and `windows_process_alive(pid)`, clearing stale `pid` only when verified empty or after server restart (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
  - Replace ad-hoc `_run_events` with a per-run `RunState(operation_id, event, cleanup_failed=False, settlement_snapshot=None, stop_started=False)` registered under `self.lock` BEFORE `thread.start()`; if `control.json` unlink, `RunState` registration, or `thread.start()` fails after persisting `queued`, roll back `status="failed"` + persist so the record cannot wedge `available()`.
  - Condition every finalizer write, child eviction, and Stop waiter response on run identity (`operation_id` / `self.children.get(identifier) is child`): capture immutable `run_state.settlement_snapshot` immediately after `self.save(record)` and BEFORE `run_state.event.set()`, and return that immutable settlement from `POST /api/jobs/<id>/stop` so concurrent `stop(A) -> resume(B) -> return stop(A)` never leaks run B's snapshot.
  - Commit control transitions in fixed order: persist registry status (`pausing`/`stopping`) under `self.lock` BEFORE writing `control.json` (rolling back registry status and returning `500` RFC 9457 if `control.json` write fails).
  - Serialize `Jobs.run()` vs `T_stop`: immediately after `code = child.wait()`, acquire `self.lock` and check `self.closing`, `run_state.cleanup_failed`, `record["status"] == "stopping"`, and `code == EXIT_CODE_PAUSED`; execute `report.md`/`dirty_segments = []`/`record_assembly()` ONLY when none hold and `code == 0`. In `except Exception:`, skip overwriting `record["error"]` and skip appending bogus error log lines if `run_state.cleanup_failed` or `record["status"] == "stopping"`, wrapping `log_path.open("a")` in `try/except OSError`. Include active `self.children` (non-demo / video) in `has_active_work()` and `has_active_video_work()`. Wrap `stop_posix_descendants_on_exit` (`jobs.py:149`) in `try/except Exception` recording completion on `child` so `T_stop` never double-waits 15s, and release ownership once the owned process group or Job Object is verified empty independently of leaked stdout EOF.
  - Designate `POST /api/jobs/<id>/resume` (Workbench Resume) as the primary supported recovery command while Workbench runs (documenting that direct CLI resume must not run concurrently with Workbench until `ENG-E1` adds a cross-platform CLI lifetime lease), and add a non-blocking POSIX `worker.lock` check at `h3ref.py:cmd_one()` entry matching `film.py:1345`.
- [ENG-H3] Authoritative `unsettled_tasks()` (Including Staged Rerolls & `one` Terminal Failures) + Server-Derived `recovery_action`:
  - Build `unsettled_tasks(job_dir, kind)` reusing `segments.task_unresolved`, `segments.pending_superseded`, and `frames.task_pending`: for unapplied `inputs/segment-edit.json`, read `inputs/segment-<revision>/state.json -> segs[str(number - 1)]` and treat `publishing`, `generated`, `task_unresolved(staged)`, or `(submission_unknown and not task)` as unsettled (ignoring superseded edits); for `kind == "one"`, treat root `state.json` as unsettled iff `(state.get("submission_unknown") and not state.get("task")) or (state.get("task") and not state.get("terminal_failed") and not ((job_dir / "clip.mp4").is_file() and state.get("cny") is not None))`.
  - Catch `(OSError, ValueError)` per file inside `unsettled_tasks()`: `Jobs.summaries()` degrades safely (`has_unsettled_tasks: False, state_unreadable: True`, never 500-bricking `/api/bootstrap`); `DELETE` re-reads under `self.lock` and fails closed (`400`) on unreadable state unless `d5` force-ack applies.
  - Emit server-derived `snapshot["recovery_action"]` (`"retrieve_only"` | `"resume_pipeline"` | `"resume_frames"` | `"jump_blocker"` | `None`) and `snapshot["blocking_target"]` (`{kind, key_or_number}`); reserve `"Retrieve pending results"` (`"retrieve_only"`) strictly for operations that dispatch zero fresh paid work, and label mixed resumes accurately (`"Resume Job (retrieves N + submits M)"`).
- [ENG-H4] Normalized `200`/`202` Stop Envelope, Strict Optional JSON Parser, Safe `open-folder` Reveal, Recursive `prefill` Projection & Route Security:
  - Return the flat `jobs.snapshot(id)` (or `run_state.settlement_snapshot`) on BOTH HTTP `200` and `202` from `POST /api/jobs/<id>/stop`, adding `"poll_url": f"/api/jobs/{identifier}"` as an additive top-level key on `202` (keeping `500 job_stop_cleanup_failed` as RFC 9457 with `job` extension and unwrapping `res.job ?? res` in `results.js`).
  - Add a shared `_optional_json_object()` helper for `/pause`, `/stop`, `/open-folder`, and `DELETE`: return `{}` when `not request.get_data()` (fixing HTTP `415` on bodyless `curl`), otherwise require valid JSON `dict` (returning RFC 9457 `415`/`400` on wrong media type, malformed JSON, or non-object); require literal `val is True` for `acknowledge_unsettled` and finite non-boolean numeric `0 <= wait_seconds <= 15` (rejecting `bool`/`NaN`/`Inf`).
  - Harden `POST /api/jobs/<id>/open-folder`: resolve `output_dir`, verify `resolved.is_dir()` and reject `.app`/`.bundle`/`.framework` (and symlink targets matching them); invoke `["open", "-a", "Finder", "--", str(resolved)]` on macOS and `os.startfile(str(resolved), "explore")` on Windows; truncate and redact stderr to `<= 300` chars.
  - Recursively project only supported keys inside `GET /api/jobs/<id>/prefill` (stripping unknown nested metadata and local paths), add `demo_placeholders` to the whitelist, compute `omitted_reference_count` for both `film` and `one` (`references_omitted = omitted_reference_count > 0`), and emit valid raw shot durations/boundaries so `prefill -> POST /api/jobs` round-trips cleanly including the `3600.4` timeline fixture (`tests/test_dialogue_budget.py:283`).
  - Validate `re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record["id"])` in `Jobs.__init__` and `DELETE`; wrap `workbench/server.py:345` correlation log append in `try/except OSError`; recursively redact all strings inside `api_problem` without mutating inputs; fall back to `request.path` prefix matching in `http_error` when `request.endpoint is None` (covering `405 Method Not Allowed`).
- [ENG-H5] Zero-Stall Byte-Cursor `/logs`, Unified Incremental Console Reader, Bounded Log Buffers & Single Golden Alert Classifier:
  - Harden `GET /api/jobs/<id>/logs`: validate `offset`/`limit` with strict ASCII regex `re.fullmatch(r"[0-9]{1,18}", raw)` (`1 <= limit <= 262144`, default `100000`); compute `next_offset = start_offset + raw_bytes_consumed` strictly from pre-redaction bytes; if incoming `offset > 0` lands mid-line (`byte[offset-1] != b"\n"`), realign to the enclosing line start (bounded `4096`-byte lookback) before decoding/redacting so secrets bisected by a cursor are fully redacted; guarantee forward progress on oversized lines (> `limit`) or settled EOF by advancing to the last valid UTF-8 codepoint boundary (holding back an incomplete tail ONLY when `offset + len(raw) == file_size` and active); emit `generation: null` when missing/empty and trigger `reset: true` ONLY when `offset > 0` and either `offset > file_size` or `generation` (`st_ino:st_ctime_ns:first_line_sha256[:16]:prefix_4k_sha256[:16]`) mismatches.
  - Update `results.js:refreshJob()` to poll `GET /api/jobs/<id>?include_logs=0` (0 log reads when collapsed; eliminates `100KB` sliding-window diff breakage & `0.71 MB/s` 10-view I/O), and reuse ONE incremental `/api/jobs/<id>/logs` poller for Console Tab 1 on BOTH `/` and `/jobs/<id>` (keeping `renderJob(job)` backward-compatible when unit tests supply explicit `typeof job.logs === "string"`).
  - Cap browser retained console DOM lines (`<= 2000` lines / `<= 512 KB`), cap `h3ref/colab_runtime.py` reader internal `lines` list and unterminated-line buffer (`<= 64 KB`), and parse `H3_COLAB_MAX_LOGS` safely (fallback/clamp `20..2000`, default `200`, never crashing at import).
  - Track `log_alert_seq` / `log_alert_counts: {warn, error}` incrementally in `Jobs.run()` and `ColabRuntimeManager._append_log()` (counting traceback entry once as 1 error event while continuation lines inherit `error` display class), verified against a single shared golden fixture `tests/fixtures/log_alerts_golden.json` run in BOTH Python and JS tests.
- [ENG-H6] Zero-Stat Warm `summaries()`, Orphaned `output_dir` Degradation, Accurate Cost Labels & Windows `DELETE` Retry:
  - Persist `title`, `frames_total`, and `clips_total` at `Jobs.create()`, and persist `progress`, `cost_cny`, `log_alert_seq`, `log_alert_counts`, and `has_unsettled_tasks` at settlement (re-persisting on any `edit_frame`/`regenerate_segment`/`assemble`/`rewrite`/`resume` mutation) so warm settled `summaries()` performs `0` filesystem `stat()` calls per poll. For active rows, include `frame-edit.json`, `segment-edit.json`, and staged `segment-<rev>/state.json` in the cache stamp, release `_summary_cache_lock` during disk I/O, and verify record membership/generation before storing back into cache (preventing post-delete resurrection).
  - In `Jobs._snapshot()` (`workbench/jobs.py:274`), catch `(FileNotFoundError, ValueError)` on `inputs/request.json` and return a clean degraded snapshot (`output_dir_missing: True`, preserved `.log`, empty storyboard/segments, Delete enabled) instead of HTTP `500`.
  - Compute `snapshot["cost"]` separately for `kind=="one"` (root `state.json`, `frames_cny=0.0`) vs `kind=="film"`; read `cny`/`run_s`/`wall_s` on segment cards from active `record` (never empty `staged`); align cost tooltip copy with actual recorded accounting (`workbench/storyboard.py:279-281` / `tests/test_workbench.py:223,246`: "Recorded API cost in job state (includes saved frame rerolls; excludes external/Colab compute)") and label ComfyUI `run_s` as "Reported execution time" (preserving `null` when unknown).
  - Retry `<id>.log` / `<id>.json` unlinks on Windows (`WinError 5/32/33`) with brief backoff (matching `h3ref/config.py:atomic_replace`) before returning `500 job_delete_io_error`.
- [ENG-H7] Zero-Build Frontend Module Split, Legacy Node VM Resilience, `.frame-card.is-editing` Grid Fix & Control/Upload Races:
  - Keep `workbench/static/common.js` focused on `state`, `request()`, `t()`, `renderConfig()`, and `renderRuntimeStatus()`, extracting two zero-build `<script defer>` modules loaded in `base.html`: `workbench/static/console.js` (unified dockable log console, cursor poller, traceback classifier, resize/presets/`localStorage`) and `workbench/static/shell.js` (Toast stack, topbar popovers, 3-tab Settings drawer, focus-trapped modals, LIFO `Esc`/`⌘J` dispatcher), wired through safe `typeof ... === "function"` hooks.
  - Protect existing 3,531-line Node VM tests (`tests/test_workbench_ui.js:33,93,98-100`): feature-detect `typeof response.text === "function"` before falling back to `response.json()` in `common.js:request()`; wrap every `localStorage`/`sessionStorage` `JSON.parse()` in `try/catch` + plain-object guard (`localStorage.getItem: () => "en"` safe); guard all browser globals (`window`, `document.addEventListener`, `document.querySelector`, `document.body?.classList`, `el?.closest`, `el?.contains`) and update `makeDom()` to store listener arrays without overwriting prior handlers.
  - Apply `grid-column: 1 / -1` to the owning **`.frame-card.is-editing`** (direct child of `.frame-grid`), NOT nested `.frame-editor`.
  - In `results.js`, give `runControlAction()` its own `state.controlSubmitting` guard, clear `state.pollTimer`, bump `state.jobRevision`, and recover `409 job_operation_mismatch` via `refreshJob()` + Toast; keep existing `failed_only` rule for paused mid-storyboard frames and add a clear disabled tooltip on finished frames while paused/stopped.
  - In `create.js`, enforce `state.images.length + files.length <= 9` BEFORE batching in `one` mode, re-sync `#upload-conflict-tray` inside `renderFilmReferences()` (covering cast-group edits), and clear Undo auto-match state on any subsequent manual binding, group edit, or script edit.
- [ENG-H8] Atomic Demo Writes, Safe Downgrade Helper & Real Rendered-Template Browser Smoke Gate:
  - Update `workbench/demo.py` to write PNGs, FFmpeg clips, and final copies to `.tmp` files and publish via `config.atomic_replace()` (matching `h3ref/media.py:74`) so Hard Stop mid-demo never corrupts assets or destroys previous downloadable `film.mp4`.
  - Add an idempotent offline downgrade script (`scripts/prepare_downgrade.py`, documented in `docs/workbench.md`) that verifies zero active workers and converts quiescent `paused`/`stopped` records in `.workbench/jobs/*.json` to `interrupted` without touching `state.json` (preserving all paid `task_id`s), backed by a prior-reader compatibility fixture test for paused `film` and stopped `one` records.
  - Require a real rendered-template headless browser smoke pass across `1440px`/`1060px`/`760px`/`420px` in both `zh-CN` and `en` (verifying real DOM IDs, `.frame-card.is-editing` span, narrow-Create sticky launch bar, `760px` fixed popovers, hidden-tab validation jump, modal Tab focus trap, and delayed `202` Stop responses) plus Windows CI execution of `tests/test_windows_processes.py` + `tests/test_job_pause_stop.py` prior to packaging.
- [UC2A] Sequential 3-PR Delivery Plan (Approved User Challenge `uc2a`, Superseding Single-PR `CEO-E5`):
  - Deliver the overhaul as **3 sequential PRs merged to `main` in strict order** (queue cost: +2 CI/review merge gates at ~5–10 min each; explicit cross-PR UI, API, and doc coherence guarantees on every PR boundary):
    - **PR 1 (Backend Safe Pause/Stop Engine, Management APIs, `CEO-H1` Fix & Operator API Docs)**: ships `h3ref/control.py`, `h3ref/{frames,film,comfy,rh,rewrite,colab_runtime}.py`, `h3ref.py`, `workbench/{jobs,server,storyboard,segments,demo,processes}.py`, `scripts/prepare_downgrade.py`, backend i18n (`workbench/messages/`, `h3ref/messages/`), `tests/test_job_pause_stop.py`, `tests/test_colab_runtime.py`, `tests/fixtures/log_alerts_golden.json`, `docs/workbench.md` (`#first-offline-job`, `#job-control-errors`, upgrade/rollback), `docs/workbench-cli-parity.md`, and `SKILL.md`. Cross-PR compatibility: zero edits to `workbench/templates/*` or `workbench/static/*`; `GET /api/jobs/<id>` keeps `?include_logs=1` default so legacy `results.js` remains intact; `docs/workbench.md` distinguishes currently-shipped API/CLI controls from UI controls landing in PR 2/3; `README.md` UI layout update stays deferred to PR 3; CI requires green Python suite, `test_locale_parity.py`, `compileall`, existing 3,531-line JS tests, and native Windows `test_windows_processes.py` + `test_job_pause_stop.py`.
    - **PR 2 (Global Workbench Shell, Unified Dockable Console, Popovers & 3-Tab Settings Drawer — stacked on merged PR 1)**: ships `workbench/templates/base.html`, refactored `workbench/static/common.js` + new zero-build `workbench/static/console.js` & `workbench/static/shell.js`, shell/console/drawer/toast CSS in `workbench/static/style.css`, frontend i18n for shell/console, and updated `tests/test_workbench_ui.js` (incl. shared golden alert-classifier test). Cross-PR compatibility: existing `index.html`/`create.js` and `results.html`/`results.js` remain fully functional inside the new shell (`base.html` retains all legacy IDs `#global-error`/`#global-message`/`#config-form`/`#runtime-*`, and legacy `/jobs/<id>` `#job-logs` stays readable without duplicate-ID collision until PR 3 moves Results into Console Tab 1); zero dead links or missing DOM targets on `/` or `/jobs/<id>`.
    - **PR 3 (Create & Results Page Refactor, Interaction Polish, `README.md` Sync & Real Browser Smoke Gate — stacked on merged PR 2)**: ships `workbench/templates/{index,results}.html`, `workbench/static/{create,results,video-segments}.js`, page-layout & responsive CSS (`<=1060px` sticky Create launch bar, `.frame-card.is-editing` full-row span, `760px`/`420px` grids), remaining frontend i18n, updated `README.md` (final UI layout + 2-click Demo + CLI preflight pairing), `docs/design/workbench-ux-prototype.html` supersession banner, `tests/test_results.js`, `tests/test_workbench_ui.js`, and the real rendered-template 4-viewport×2-locale browser smoke gate (`ENG-H8`).
- [ENG2-H1] Process-Tree Settlement Authority, Bounded Log Draining, Verified Ownership & Restart-Safe Orphan Guard:
  - Decouple `child.stdout` draining into a dedicated daemon `log_reader` thread (matching `h3ref/colab_runtime.py:1046-1095`). The single process-tree waiter (`Jobs.run`) waits on `code = child.wait()`, executes `stop_child(child)` FIRST, calls `log_reader.join(timeout=2.0)` (abandoning leaked FD readers without stalling), and ONLY AFTER verified process-tree cleanup commits success bookkeeping (`report.md`/`dirty_segments = []`/`record_assembly()`), persists the final `record`, captures a minimal immutable `RunOutcome` (`{id, status, operation_id, error, cleanup_failed}` — never calling full `snapshot()` before `run_state.event.set()` so artifact/consistency I/O cannot strand waiters), evicts `self.children` strictly by child object identity, and calls `run_state.event.set()`. `POST /stop` builds the response snapshot after waking (falling back to `RunOutcome` if the job was concurrently resumed or deleted).
  - Wrap `WindowsJob.wait_for_exit` (`workbench/processes.py:122-126`) in `try/except Exception` recording completion state matching POSIX `stop_posix_descendants_on_exit`; apply the PID guard to BOTH `os.killpg` sites (`workbench/jobs.py:125` and `138-145`) and treat `PermissionError` during POSIX group cleanup as an unverified cleanup failure (never silently treating it as `ProcessLookupError`).
  - Fix POSIX `worker_locked()` (`workbench/jobs.py:99`) to open existing locks read-only (`"rb"`), explicitly returning `False` on `FileNotFoundError`, `True` on `BlockingIOError`, and distinguishing inaccessible permission errors from active locks. Share ONE verified ownership predicate across `Jobs.available()`, `has_active_work()`, and `has_active_video_work()` that probes ONLY records with `status in ACTIVE_STATUSES`, non-empty `pid` (preserving Colab runtime-stop protection for locked `interrupted` orphans surviving server restart), `cleanup_failed=True`, or active membership in `self.children` — eliminating read-only archive deadlocks and O(N) lock-held history scans.
- [ENG2-H2] Pause/Failure Precedence (`CancelledError` Safe), Inner Retry Admission, Two-Pass Frame Snapshot & Enforced Retrieval-Only Consent:
  - In `stage_generate`, `stage_prompts` (`h3ref/film.py:612`), and `_execute_rewrite_targets` (`workbench/segments.py:1389`), collect ALL admitted future outcomes, explicitly ignore `concurrent.futures.CancelledError` and `comfy.DispatchAbortedError` (never letting deliberate cancellation override `JobPaused`), and give genuine in-flight failures strict precedence over `JobPaused`.
  - In `h3ref/film.py:stage_frames`, snapshot `frames_snapshot = copy.deepcopy(job.state["frames"])` under `job.lock` before entering `ThreadPoolExecutor(4)`; Pass 1 schedules eligible pending tasks (`frames.task_pending(rec)` and not already complete on disk) into `scheduled_keys: set[str]`; Pass 2 walks shots in order against `frames_snapshot` to preserve `known_scenes`/`locked_looks`/`previous` continuity while skipping `scheduled_keys` (zero double-scheduling, zero lockless dict races).
  - Check `should_pause` inside `h3ref/rewrite.py:_llm()`'s 3-attempt HTTP retry loop (`rewrite.py:274`) and `h3ref/http.py:560,597` retry backoff before every fresh outbound POST; at paused stage boundaries (`h3ref/film.py:1430`), route through retrieval-only drain when accepted tasks already exist instead of skipping retrieval; retry transient Windows `PermissionError` 3×20ms inside `h3ref/control.py:should_pause()`.
  - Add `"frames"` to `workbench/jobs.py:911`'s `can_resume_video is False` gate; unify `results.js` `#resume-job` disabled rule as `locked || (job.can_resume_video === false && job.recovery_action !== "retrieve_only")` (resolving the `CEO-H3` vs `DES-H1`/`ENG-H3` conflict); require `"Retrieve pending results"` to send `{"intent": "retrieve_only", "operation_id": ...}` and enforce a backend `drain_only=True` flag inside `stage_frames`/`stage_generate` that strictly forbids fresh provider submissions.
- [ENG2-H3] Append-Stable `/logs` Generation, Monotonic Redacted Cursor, Symlink Containment & Bounded Colab Chunk Reader:
  - Replace `ENG-H5`'s append-breaking `generation` formula (which used POSIX `st_ctime_ns` and `prefix_4k_sha256`) with strictly append-stable `generation = f"{st.st_ino}:{first_complete_line_sha256[:16]}"` (SHA-256 of the first `\n`-terminated line up to 512 bytes; `null` + `next_offset: 0` until the first `\n` is flushed). Open the log via a single FD (`O_NOFOLLOW` on POSIX after asserting `not raw_path.is_symlink()` and `resolved.parent == (storage / "jobs").resolve()`), detecting truncation/replacement on that FD.
  - Enforce `limit` bounds `4096 <= limit <= 262144` (default `100000`, preventing `limit=1` multibyte stalls). Keep `next_offset` strictly monotonic without duplicating prefix text: if an incoming cursor lands mid-line, decode/redact the enclosing line internally and emit only text strictly from `offset` forward; for any single logical line exceeding `limit`, consume the full oversized line through its `\n`/EOF and emit a bounded notice `"[line truncated: N bytes]\n"` while advancing `next_offset` past it (zero stalls, zero bisected-secret leaks).
  - Route all direct server-side job log appends (`Jobs.run()` `except` at `jobs.py:1083` and `server.py:345`) through a unified `append_job_log()` helper that increments `log_alert_seq`/`log_alert_counts`; replace `for line in proc.stdout:` in `h3ref/colab_runtime.py:1050` with `4096`-byte chunk reads enforcing a hard `64 KB` line cap, a `2 MiB` head/tail cap on `_run_command` `collected`, and dynamic `_max_slot_logs()`.
- [ENG2-H4] Private Demo Staging, Dot-File Artifact Filter, State Shape Validation & OS Security Hardening:
  - Stage all `workbench/demo.py` PNGs, FFmpeg clips, tail JPEGs (`h3ref/media.py:33`), and final copies inside a private same-volume directory (`out / "inputs" / ".staging"`) using canonical media extensions (`.png`, `.mp4`, `.jpg`) so Pillow and FFmpeg infer formats natively, then publish via `config.atomic_replace()`; additionally filter `path.name.startswith(".")` in `workbench/jobs.py:214` and `workbench/segments.py:1082` so temporary files never enter listings, progress counts, or `/files/` downloads.
  - Validate dict/list shapes on `state.json`/`request.json` before `read_context()`/`_snapshot()` enrichment (preventing `AttributeError`/`TypeError` on malformed JSON); include unapplied `frame-edit.json` `downloaded=True` in `unsettled_tasks()`, validate `segment-edit.json` `revision` (`[A-Za-z0-9_-]{1,64}`) and non-bool positive int `number`, and fail closed on `DELETE` whenever state is malformed/unreadable (never enabling Delete on corrupt state unless `d5` force-ack applies); add a 10s TTL + explicit invalidation (`GET /api/jobs/<id>` & Refresh button) to settled `summaries()` cache; emit `active_tasks` `rewrite` progress whenever `operation == "pipeline"`, no segment is `task_pending`, and at least one unfinished segment lacks valid `h3`.
  - In `GET /api/jobs/<id>/prefill`, if `any(float(s["end"]) > 3600 for s in shots)`, emit `start=0, end=round(float(s["end"]) - float(s["start"]), 2)` per shot (preserving exact durations/segments/dialogue budget under `[0, 3600]`); in `POST /api/jobs/<id>/open-folder`, use `stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL` + daemon reaper `proc.wait()` on Linux `xdg-open`, expand macOS rejected bundle suffixes (`.app`, `.bundle`, `.framework`, `.pkg`, `.workflow`, `.action`, `.prefPane`, `.command`, `.appex`), and reject Windows UNC paths (`\\...`).
- [ENG2-H5] Canonical Stop Contract Table & Lock-Safe Packaged Downgrade:
  - Lock ONE authoritative `POST /api/jobs/<id>/stop` response table across code and docs: fast exit -> `200` flat snapshot (`stopped`); slow exit -> `202` flat snapshot + additive top-level `poll_url` (`stopping`); observed cleanup failure -> `500` RFC 9457 `job_stop_cleanup_failed` (+ `job` extension); already `stopped` on matching `operation_id` -> `200` idempotent flat snapshot; mismatched `operation_id` -> `409` RFC 9457 `job_operation_mismatch`; any other settled status (or `stopped` without matching `operation_id`) -> `400` RFC 9457 `job_not_active`.
  - Have `scripts/prepare_downgrade.py` acquire the workbench single-instance lock (`workbench/instance.py:7`) throughout conversion, refuse conversion if any job has an errorless `task_pending` frame that the prior release (`h3ref/frames.py:77`) cannot recover (instructing operator to retrieve/settle first), add `scripts/prepare_downgrade.py` to `scripts/package_windows.py`, and test stopped-`film`, paused-`film`, and stopped-`one` downgrade fixtures (including extracted Windows ZIP verification).
- [ENG2-H6] Intermediate-`main` Compatibility Contracts Across PR 1, PR 2 & PR 3 (`UC2A` Hardening):
  - **PR 1 legacy-UI compatibility adapter**: include a minimal ~25-line adapter in `workbench/static/{common,results}.js` and `workbench/static/messages/{zh-CN,en}.json` so existing pages cleanly handle API-driven `pausing`/`stopping`/`paused`/`stopped` statuses (continue polling while `pausing`/`stopping`, keep Create locked while active, enable Resume for `paused`/`stopped` `one`/`film`, show `frameTaskPending` notice when `!running(job)`, localize all 4 new statuses). Keep PR 1 `docs/workbench.md` scoped strictly to shipped API/CLI contracts without referencing unmerged PR 2/3 UI changes.
  - **PR 2 zero-collision shell & complete Results console handoff**: use distinct shell IDs in `base.html` (`#topbar-history-list`, `#topbar-history-empty`, `#console-output`, `#console-panel`) preventing duplicate DOM IDs alongside legacy `index.html:55` and `results.html:75`; share `/api/bootstrap` `state.jobs` hydration and lightweight history refresh across both `/` and settled `/jobs/<id>` in `common.js`/`shell.js`; migrate BOTH `results.js:390` and `results.js:478` to `?include_logs=0` and wire `console.js` Tab 1 to `$("results-page")?.dataset?.jobId` via `/api/jobs/<id>/logs` while retiring legacy `<details class="logs-section">` in PR 2 (one log owner, zero collapsed log reads); hold the History `Clone` button until PR 3 ships `?from=<job_id>` consumption; update `docs/workbench.md` navigation sections in PR 2; gate PR 2 on a rendered-template test verifying zero duplicate IDs, live `/jobs/<id>` topbar history, and Console Tab 1 output.
  - **PR 2/3 storage & drag-drop safety**: invoke `refreshFilmReferences()` inside `restoreCreateDraft()` after restoring non-empty `film_json`; wrap ALL `sessionStorage`/`localStorage` `getItem`/`setItem` calls in safe helpers catching `QuotaExceededError`/`SecurityError`; restrict global `window` `dragover`/`drop` `preventDefault()` to `event.dataTransfer?.types?.includes("Files")`; cap single drop batches at `<= 36` files and document `.workbench/uploads/` cleanup.
<!-- /autoplan-accepted:eng -->
## Review record

<!-- autoplan-baseline-edits:ceo {"sourceSha256":"9e1c406c6061fc75b87e4c44c966879784d1621d48690302d871dbd20a3aa1d1","replacements":[{"oldText":"- **中英双语一致**：全部新增/调整界面文案与后端提示均同步写入 `workbench/static/messages/{zh-CN,en}.json` 与 `workbench/messages/{zh-CN,en}.json`，通过 `tests/test_locale_parity.py`。","newText":"- **中英双语一致**：全部新增/调整界面文案与后端提示均同步写入 `workbench/static/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json` 与 `h3ref/messages/{zh-CN,en}.json`，通过 `tests/test_locale_parity.py`。"},{"oldText":"- 删除历史记录默认仅删除 `.workbench/jobs/<id>.{json,log}` 登记记录，**绝不自动删除磁盘上的输出目录与已付费素材**。","newText":"- 删除历史记录默认仅删除 `.workbench/jobs/<id>.{json,log}` 登记记录，**绝不自动删除磁盘上的输出目录与已付费素材**。\n- 实时 SSE / WebSocket 日志与进度推流推迟至 `TODOS.md`（`CEO-E6`），本次继续沿用现有离线轮询。"},{"oldText":"| 高频扩展项 | 全部纳入：参考图拖放/按文件名自动匹配、历史记录带剧本名/快切/删除记录/一键打开本地文件夹/复刻新建、成片置顶折叠条、分镜状态筛选与直达大图、片段耗时费用展示、全阶段/多段并发进度聚合、动态浏览器标签页标题 |","newText":"| 高频扩展项 | 全部纳入：参考图拖放/按文件名自动匹配、JSON 格式化与摘要条、`sessionStorage` 草稿暂存（`?from=<job_id>` 优先并覆盖草稿，提交成功后清理）、`Esc` / `⌘/Ctrl+J` 快捷键与控制台 `localStorage` 状态记忆、历史记录带剧本名/快切/删除记录/一键打开本地文件夹/复刻新建、成片置顶折叠条、分镜状态筛选与直达大图、片段耗时费用展示、全阶段/多段并发进度聚合、动态浏览器标签页标题 |"},{"oldText":"在 `workbench/jobs.py` 中统一定义状态集合，替代散落各处的硬编码判断：\n\n```python\nACTIVE_STATUSES = (\"queued\", \"running\", \"pausing\", \"stopping\")\nRESUMABLE_STATUSES = (\"failed\", \"interrupted\", \"paused\", \"stopped\")\nSETTLED_STATUSES = (\"succeeded\", \"failed\", \"interrupted\", \"paused\", \"stopped\")\nEXIT_CODE_PAUSED = 75\n```\n\n状态流转：\n- `queued` / `running` → `POST /api/jobs/<id>/pause` → `pausing` → worker 收尾落盘后以 `EXIT_CODE_PAUSED` 退出 → `paused`（若恰好所有剩余项都已完成，则正常结算为 `succeeded`）。\n- `queued` / `running` / `pausing` → `POST /api/jobs/<id>/stop` → `stopping` → 写入停止信号后终止本地子进程树（`stop_child`）→ `stopped`（`error = None`，不视为失败）。\n- `paused` / `stopped` → `POST /api/jobs/<id>/resume` → 清理 `control.json` → `queued` → `running`，无缝复用所有已落盘产物与在途 `task_id`。\n- 服务启动恢复（`Jobs.__init__`）：磁盘上遗留的 `ACTIVE_STATUSES` 且无存活锁时，若为 `stopping` 置为 `stopped`，其余置为 `interrupted`。","newText":"在 `h3ref/control.py` 定义 `EXIT_CODE_PAUSED = 75`（供 `h3ref.py` 与 `workbench/*` 共同导入，避免 `h3ref/` 反向依赖 `workbench/`），在 `workbench/jobs.py` 中统一定义状态集合，替代散落各处的硬编码判断：\n\n```python\n# h3ref/control.py\nEXIT_CODE_PAUSED = 75\n\n# workbench/jobs.py\nACTIVE_STATUSES = (\"queued\", \"running\", \"pausing\", \"stopping\")\nRESUMABLE_STATUSES = (\"failed\", \"interrupted\", \"paused\", \"stopped\")\nSETTLED_STATUSES = (\"succeeded\", \"failed\", \"interrupted\", \"paused\", \"stopped\")\n```\n\n状态流转：\n- `queued` / `running` → `POST /api/jobs/<id>/pause` → `pausing`（重复调用幂等返回 `200`；`stopping` 或终态返回 `400`）→ worker 收尾落盘后以 `EXIT_CODE_PAUSED` 退出 → `paused`（若恰好所有剩余项都已完成且无需进入 `media.join`，则正常结算为 `succeeded`；若进入 `media.join` 前已请求暂停则停在拼接前进入 `paused`）。\n- `queued` / `running` / `pausing` → `POST /api/jobs/<id>/stop` → `stopping`（重复调用幂等不重复触发 `stop_child`；终态返回 `400`）→ 写入停止信号并在去重后台线程调用 `stop_child`（若抛出 `TimeoutError` 等异常，后台线程直接持 `self.lock` 校验 `operation_id` 后标记共享 `cleanup_failed=True`、转 `status=\"failed\"` 保留 `pid`、`self.save(record)` 并 `run_event.set()`，杜绝 `child.stdout` 阻塞导致永久卡在 `stopping`），接口有界等待 `1.5s`（`operation_id` 作用域的 `Event` 在 `Jobs.launch()` 锁内启动线程前注册，并在 `self.save(record)` 之后才 `set()`），快速退出返回 `stopped`，慢退出返回 `stopping` 并由后台收尾。\n- `paused` / `stopped` → `POST /api/jobs/<id>/resume` → 清理 `control.json` → `queued` → `running`，无缝复用所有已落盘产物与在途 `task_id`。\n- 服务启动恢复（`Jobs.__init__`）：对磁盘上所有遗留的 `ACTIVE_STATUSES` 执行状态迁移——若 `not worker_locked(output_dir)`，`stopping` 置为 `stopped`（`error=None`）且其余置为 `interrupted` 并弹出 `pid`；若 `worker_locked(output_dir)` 为真，则全部（含 `stopping`）置为 `interrupted` 并保留 `pid`，防止重启后孤儿进程残留永久僵尸状态，同时保持 `available()` 互斥直到锁释放。"},{"oldText":"- 写入采用原子替换（`write_json`）；每次 `Jobs.create` / `Jobs.restart` 启动新子进程前强制 `unlink(missing_ok=True)`，防止残留信号污染续跑。\n- 新增轻量工具模块 `h3ref/control.py`（零外部依赖，CLI 无该文件时零开销）：\n  - `class JobPaused(Exception)`\n  - `should_pause(job_dir: Path) -> bool`：检查 `inputs/control.json` 的 `action in (\"pause\", \"stop\")`。\n  - `check_pause(job_dir: Path)`：满足时抛出 `JobPaused`。","newText":"- 写入采用原子替换（`write_json`）；`Jobs.create` 与 `Jobs.restart` 均生成 `operation_id=uuid.uuid4().hex`，`Jobs.launch()` 在 `self.lock` 内且 `thread.start()` 前清理 `control.json`（`unlink(missing_ok=True)`）并注册 `_run_events[identifier]` 与共享 `cleanup_failed` 标志，防止残留信号或 `queued` 竞态。\n- 新增轻量工具模块 `h3ref/control.py`（零外部依赖，CLI 无该文件时零开销）：\n  - `EXIT_CODE_PAUSED = 75`\n  - `class JobPaused(Exception)`\n  - `should_pause(job_dir: Path) -> bool`：检查 `inputs/control.json` 的 `action in (\"pause\", \"stop\")`。\n  - `check_pause(job_dir: Path)`：满足时抛出 `JobPaused`。"},{"oldText":"| `h3ref/film.py` `stage_frames` | 主循环每个 shot 开始规划构图/提交 `pool.submit(draw, ...)` 前 | `break` 停止规划和派发后续镜头；等待 `futures` 中已提交的 1~4 张在途图全部下载落盘；**不把未开始镜头标为 `error`**；若全部目标镜已齐则正常返回，否则抛出 `JobPaused` |\n| `h3ref/film.py` `stage_prompts` | 每个 segment 改写前 | 跳过未开始段落，等待已在途改写落盘后抛出 `JobPaused` |\n| `h3ref/film.py` `stage_generate` | 链式模式每段提交前；并行模式 `run_other` 获取信号量与调用 `gen_segment` 前；以及 `gen_segment` 中无 `task` 时在 upload / submit 前 | 立即设 `abort` 阻止派发任何新段；**所有已提交的 `inflight_segs` 和正在跑的段继续 poll→download→提取尾帧→写入 `state.json`**；收尾完成后不进入 `media.join`，抛出 `JobPaused` |\n| `workbench/segments.py` `_execute_rewrite_targets` | 每段改写派发前 | 等待在途改写保存后抛出 `JobPaused` |\n| `workbench/segments.py` `_regenerate` / `assemble` | 提交新任务 / 启动 ffmpeg 拼接前 | 尚未提交/启动时直接抛出 `JobPaused`；已提交则等待落盘（若用户不等可直接点「立即停止」） |","newText":"| `h3ref/film.py` `stage_frames` | 嵌套循环每个 shot 规划构图前后、提交 `pool.submit(draw, ...)` 前，**以及线程池内 `draw()` 调用 `frames.draw(...)` 前加 `if not task and should_pause(job.dir): return`**（用 `paused` 标志同时跳出内外双层循环，且显式透传 `JobPaused` 不被 `describe_composition` 的 `except Exception` 吞掉） | 阻止排队中的未提交镜头扣费提交；等待已提交的在途图下载落盘；**不把未开始镜头标为 `error`**；在写入 `frame_failures` 前若检测到暂停且仍有未完成目标镜则抛出 `JobPaused` |\n| `workbench/storyboard.py` `regenerate` | `if not pending.get(\"downloaded\"):` 内且仅当 `not pending.get(\"task\")` 时，在 `demo.placeholder` / `frames.draw` 前调用 `check_pause(out)`（外层 `except (Exception, SystemExit)` 前显式透传 `JobPaused`） | 尚未提交远端任务时保留 `inputs/frame-edit.json` 原样并抛出 `JobPaused`（续跑继续完成该单镜）；已获得 `task` 的在途单镜继续轮询落盘 |\n| `h3ref/film.py` `stage_prompts` | 每个 segment 改写 `try:` 外与派发前（`one()` 显式透传 `JobPaused`） | 跳过未开始段落，等待所有已在途改写落盘后抛出 `JobPaused` |\n| `h3ref/film.py` `stage_generate` | 链式模式每段提交前；并行模式 `blocker` 分支与 `run_other` 获取信号量/调用 `gen_segment` 前；以及 `gen_segment` 中无 `task` 时在 upload / submit 前（`gen_segment`/`_generate_segment` 接受可选 `control_dir: Path | None = None`，默认 `job.dir`） | 立即设 `abort` 并唤醒 `node_pool.notify_all()` 阻止派发新段；**所有已提交的 `inflight_segs` 与 `blocker` 在跑段继续 poll→download→提取尾帧→写入 `state.json`**；所有在途段收尾完成后必须检查暂停信号并显式抛出 `JobPaused`，绝不落入 `media.join` |\n| `workbench/segments.py` `_execute_rewrite_targets` | 每段改写派发前（外层显式透传 `JobPaused`） | 等待在途改写保存后抛出 `JobPaused` |\n| `workbench/segments.py` `_regenerate` / `assemble` | `prepare_prompt` 前、`render_clip`/`gen_segment` 前（向 `gen_segment` 传入 `control_dir=out` 而非 staged 目录）、以及启动 ffmpeg 拼接前（外层 `regenerate` 显式透传 `JobPaused`） | 尚未提交/启动时直接抛出 `JobPaused`；已提交则等待落盘（若用户不等可直接点「立即停止」） |"},{"oldText":"- `Jobs.stop(identifier)` 流程：\n  1. 校验任务存在且 `status in ACTIVE_STATUSES`；\n  2. 写入 `control.json` (`action=\"stop\"`)，设置 `record[\"status\"] = \"stopping\"` 并持久化；\n  3. 对当前 `child` 在锁外调用 `stop_child(child)`（已有的 POSIX 进程组 SIGTERM→SIGKILL / Windows JobObject 终止）；\n  4. `Jobs.run` 退出清理时检测到 `stop_requested`（或 `status == \"stopping\"`），将 `record` 干净更新为 `status=\"stopped\", error=None`，追加日志，释放 `self.children` 与锁。\n  5. **绝不修改 `state.json` 中已保存的 `task` / `submission_unknown` 字段**。","newText":"- `Jobs.stop(identifier)` 流程（`CEO-E4` 暂定默认，`d1` 留待 Phase 4 确认）：\n  1. 校验任务存在且 `status in ACTIVE_STATUSES`（终态返回 `400`；若已在 `stopping` 则幂等等待同一收尾事件而不重复启动终止线程）；\n  2. 写入 `control.json` (`action=\"stop\"`)，设置 `record[\"status\"] = \"stopping\"` 并持久化；\n  3. 在锁外由去重后台线程调用 `stop_child(child)`（带 PID 守卫与 `child.terminate()/kill()` 回退；若 `stop_child` 抛出异常则立即持 `self.lock` 核对 `operation_id`，标记共享 `cleanup_failed=True`、转 `status=\"failed\"` 保留 `pid`、`self.save(record)` 并 `run_event.set()`，杜绝 `child.stdout` 阻塞挂死），HTTP 接口有界等待 `Jobs.launch()` 预先注册的 `operation_id` 作用域 `Event` 最多 `1.5s` 后返回 `jobs.snapshot(identifier)`（快速退出为 `stopped`，慢退出为 `stopping`）；\n  4. `Jobs.run` 在 `finally:` 且 `stop_child(child)` 完成后统一执行结算，在执行 `code == 0` 落盘后置变更（写 `report.md`/清 `dirty_segments`/`record_assembly`）前先检查 `stopping` 与本地/共享 `cleanup_failed`，严格按优先级结算：`self.closing` → `stopping` 且 `cleanup_failed=True`（`status=\"failed\"` + 清理错误，锁存期间保留 `pid`，绝不被延迟解阻覆写为 `stopped`）→ `stopping` 且进程已退（`status=\"stopped\", error=None`）→ `code == EXIT_CODE_PAUSED`（`status=\"paused\", error=None`）→ `code == 0`（执行后置变更并置 `succeeded`）→ 其他（`failed`）；始终在 `self.save(record)` 之后再 `run_event.set()`，且仅当 `operation_id` 匹配时清理 `_run_events`；若在 `queued` 尚未 `Popen` 前收到 `pausing`/`stopping`，直接干净结算为 `paused`/`stopped` 且不走异常失败分支。\n  5. **绝不修改 `state.json` 中已保存的 `task` / `submission_unknown` 字段**。"},{"oldText":"- **分镜部分完成时暂停/停止**：`state.json` 里未画的镜头无 `file` 无 `error`；`snapshot[\"can_resume_frames\"]` 在 `paused` / `stopped` 时返回 `True`；点「继续任务」后继续画剩余未完成分镜。","newText":"- **分镜部分完成时暂停/停止**：`state.json` 里未画的镜头无 `file` 无 `error`；仅当 `record[\"status\"] in SETTLED_STATUSES`、`bool(result[\"storyboard\"])`、存在未完成/待取回镜头（`any(f[\"task_pending\"] or (not f[\"url\"] and not f.get(\"error\")) for f in result[\"storyboard\"])`，排除已有非 pending 错误的失败镜）、不存在进行中/状态未知的 `frame-edit.json`（`not frame_edit_task_pending(active_frame)` 且非 `submission_unknown and not task`），且非纯视频 `pipeline` 阶段或未应用 `operation==\"frame\"` 编辑任务时，`snapshot[\"can_resume_frames\"]` 才返回 `True`；单镜重绘 `operation==\"frame\"` 在提交前暂停时**保留** `inputs/frame-edit.json` 原样不删，点击续跑时直接恢复 `operation==\"frame\"` 完成该单镜重绘，避免崩溃或误删已付费 `task_id`。"},{"oldText":"| `/api/jobs/<id>/pause` | `POST` | 优雅暂停后续派发；非活跃状态返回 400 |\n| `/api/jobs/<id>/stop` | `POST` | 立即停止本地任务；非活跃状态返回 400 |\n| `/api/jobs/<id>` | `DELETE` | 仅删除 `.workbench/jobs/<id>.{json,log}` 记录（**不删磁盘输出目录**）；禁止删除活跃任务或存在存活锁/PID 的任务 |\n| `/api/jobs/<id>/open-folder` | `POST` | 在本机系统文件管理器打开已登记的 `record[\"output_dir\"]`（严禁前端传路径；仅校验目录存在后调用 `open` / `os.startfile` / `xdg-open`） |\n| `/api/jobs/<id>/prefill` | `GET` | 返回该任务的输入配置摘要（`kind`, `film` 剥离绝对路径后的脚本对象 / `prompt`, `seconds`, `aspect`, `frames`, `optimize_storyboard`, `demo`），支持“基于此任务复刻新建” |","newText":"| `/api/jobs/<id>/pause` | `POST` | 仅 `queued`/`running` 可转 `pausing`（`pausing` 幂等返回 `200`；`stopping` 与终态返回 `400`），成功返回 `200` 及 `jobs.snapshot(id)` |\n| `/api/jobs/<id>/stop` | `POST` | `ACTIVE_STATUSES` 可转 `stopping`（`stopping` 幂等去重；终态返回 `400`），有界等待 `1.5s` 后返回 `200` 及 `jobs.snapshot(id)` |\n| `/api/jobs/<id>` | `DELETE` | 持 `self.lock` 校验路径归属 `(storage/\"jobs\").resolve()` 且无活跃/探测/锁/存活进程（POSIX 下对 `interrupted`/`stopped` 且 `not worker_locked` 跳过 `process_alive(pid)` 防 PID 复用误拦），仅删除 `.workbench/jobs/<id>.{json,log}` 并淘汰内存与缓存（**不删输出目录**）；返回 `200` JSON `{\"deleted\": true, \"id\": id}`（未知 ID 返回 `404`，非法或活跃返回 `400`） |\n| `/api/jobs/<id>/open-folder` | `POST` | 严禁前端传路径，校验已登记 `output_dir` 存在且为目录后按 OS 打开（macOS `subprocess.run([\"open\", \"--\", path], timeout=5, check=False)`；Windows `os.startfile`；Linux 检查 `shutil.which(\"xdg-open\")` 缺失返回 `400`，存在则 `Popen(..., start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`），返回 `200` JSON |\n| `/api/jobs/<id>/prefill` | `GET` | 基于白名单返回复刻配置（`kind`, 经 `describe_references()` 剥离全部本地路径的 `film`, `prompt`, `seconds`, `aspect`, `seed`, `check`, `frames`, `optimize_storyboard`, `demo`）；`request.json` 缺失或损坏返回 `400`/`404` `InputError` |"},{"oldText":"1. **可读标题与汇总（用于顶栏任务快切 & 历史列表）**：\n   - `record[\"title\"]`：创建任务时提取 `film.title`（为空则取输出目录名）或 `one` 的 `prompt` 首行前 32 字。对旧历史记录（无 `title` 字段）在 `summaries()` 中做只读回退（懒读取 `inputs/request.json` 并缓存到内存），老任务也立刻可读！\n   - `summaries()` 返回轻量 `progress_summary`（如 `frames_done`, `frames_total`, `clips_done`, `clips_total`, `cost_cny`）。","newText":"1. **可读标题与汇总（用于顶栏任务快切 & 历史列表，`CEO-E2`）**：\n   - `record[\"title\"]`：创建任务时提取并持久化 `film.title`（为空则取输出目录名）或 `one` 的 `prompt` 首行前 32 字；终态结算时持久化 `progress_summary`（`frames_done`, `frames_total`, `clips_done`, `clips_total`, `cost_cny`）。\n   - `summaries()` 在 `self.lock` 内仅快照目标 `record` 列表，随后在锁外（使用独立 `self._summary_cache_lock`）基于 `(record, request.json, state.json/demo-frames.json, clip.mp4, frames/, clips/)` 的 `mtime_ns/size` 缓存计算摘要，杜绝冷缓存磁盘 I/O 阻塞 `Jobs.run`；输出目录缺失时降级为已持久化摘要或零计数，`kind==\"one\"` 映射为 `clips_total=1`、`frames_total=0`。"},{"oldText":"现有 `tests/test_workbench_ui.js`、`tests/test_results.js`、`tests/test_video_segment_prompts.js`、`tests/test_frame_character_picker.js` 共 2500+ 行测试直接引用了一批核心元素 ID（如 `#config-form`、`#runtime-card`/`#runtime-start`、`#film-json`、`#storyboard-cards`、`#segment-cards`、`#continue-job`、`#assemble-job`、`#job-logs` 等）。\n- **原则**：凡功能语义一致的核心控件，尽量沿用既有 `id`（仅调整其容器位置与样式，如把 `#config-form` 放进右侧设置抽屉、把 `#job-logs` / `#runtime-logs` 放进底部统一控制台、把 `#continue-job` / `#resume-job` / `#assemble-job` 聚合到吸顶指挥栏），**最大程度保持既有回归测试语义完整，同时补充覆盖新布局、暂停/停止、统一控制台、批量匹配的新单测**。","newText":"现有 `tests/test_workbench_ui.js`、`tests/test_results.js`、`tests/test_video_segment_prompts.js`、`tests/test_frame_character_picker.js` 共 3,531 行测试直接引用了一批核心元素 ID 及样式规则（如 `#config-form`、`#runtime-card`/`#runtime-start`、`#global-error`/`#global-message`、`#film-json`、`#storyboard-cards`、`#segment-cards`、`#continue-job`、`#assemble-job`、`#job-logs` 等）。\n- **原则**：核心控件沿用既有 `id`，`showError`/`showMessage`/`clearNotices` 在触发 Toast 的同时继续同步更新 `#global-error`/`#global-message`；`style.css` 保留 `44px` 表单 `input`/`select`、`.header-tools select` `height: auto`、`.page-jump input`、`.artifact .plyr input`、`.checkbox-field input` `16px` 及 `max-width:68ch` 约束；阶段 2 同步更新少量因顶栏/抽屉结构变化而需调整的布局断言，并补充暂停/停止、统一控制台、批量匹配等新单测。"},{"oldText":"- `common.js`：全局状态、HTTP 请求封装、语言加载、**浮动 Toast 管理器**、**顶栏 Popover（历史快切 / Colab 算力）**、**右侧分组设置抽屉**、**底部统一可停靠日志控制台（含多 Tab、搜索、错误过滤、复制、拖拽调高、创建页活跃日志轮询）**。\n- `create.js`：创建页表单编排、JSON 格式化与摘要条、`sessionStorage` 草稿暂存、**参考图拖放与按文件名自动匹配**、紧凑 2 列参考图与合图管理、右侧吸顶发车台、基于 `?from=<job_id>` 复刻填充。\n- `results.js`：吸顶指挥栏渲染与动态主按钮状态机、**暂停 / 停止（含确认弹窗）交互**、全阶段/多段并发进度条、成片置顶折叠条、3 Tab 切换（`分镜图` / `视频片段` / `报告与文件`）、分镜状态筛选与点击缩略图直达大图、动态 `document.title` 更新。\n- `video-segments.js` & `frame-references.js` & `pagination.js`：延续既有职责，卡片上补充耗时/费用/节点胶囊展示。","newText":"- `common.js`：全局状态、HTTP 请求封装、语言加载、**浮动 Toast 管理器**、**顶栏 Popover（历史快切 / Colab 算力）**、**右侧分组设置抽屉**、**底部统一可停靠日志控制台（含多 Tab、搜索、错误过滤、复制、拖拽调高、创建页活跃日志轮询、`Esc`/`⌘/Ctrl+J` 快捷键与 `localStorage` 高度/折叠/Tab 状态持久化）**。\n- `create.js`：创建页表单编排、JSON 格式化与摘要条、`sessionStorage` 草稿暂存、**参考图拖放（按 `<= 9` 文件且 `<= 28 MiB` 分批上传）与按文件名自动匹配**、紧凑 2 列参考图与合图管理、右侧吸顶发车台、基于 `?from=<job_id>` 复刻填充。\n- `results.js`：吸顶指挥栏渲染与动态主按钮状态机、**即时暂停 / 立即停止确认弹窗交互（经专用控制请求辅助函数绕开 `runJobAction` 的活跃态拦截）**、全阶段/多段并发进度条、成片置顶折叠条、分镜状态筛选与点击缩略图拦截直达大图、动态 `document.title` 更新。\n- `video-segments.js` & `frame-references.js` & `pagination.js`：延续既有职责，保留 `selectResultTab`/`renderResultTabs` 在 `video-segments.js`（扩展为 3 Tab：`分镜图/剧本计划`、`视频片段`、`报告与文件`，并保持 null-safe 兼容既有单测），卡片上补充耗时/费用/节点胶囊展示。"},{"oldText":"  - 修改 `h3ref/film.py`、`h3ref.py`、`workbench/storyboard.py`、`workbench/segments.py`、`workbench/demo.py`（接入协作暂停检查点与 `EXIT_CODE_PAUSED`）\n  - 修改 `workbench/jobs.py`、`workbench/server.py`（`pause`/`stop`、`DELETE /api/jobs/<id>`、`open-folder`、`prefill`、`active_tasks`、`cost`、`title` 与总数统计）\n  - 更新 `workbench/messages/{zh-CN,en}.json`","newText":"  - 修改 `h3ref/frames.py`（修复 `task_pending` 对无 `error` 强停在途分镜的判定）与 `h3ref/colab_runtime.py`（`MAX_SLOT_LOGS = 200` 统一用于缓冲、`_logs_tail` 与 `status()`）\n  - 修改 `h3ref/film.py`、`h3ref.py`、`workbench/storyboard.py`、`workbench/segments.py`、`workbench/demo.py`（接入协作暂停检查点、`draw()` 队列守卫、`control_dir` 透传、`JobPaused` 显式透传与 `EXIT_CODE_PAUSED`）\n  - 修改 `workbench/jobs.py`、`workbench/server.py`（`pause`/`stop`、`DELETE /api/jobs/<id>`、`open-folder`、`prefill`、`active_tasks`、`cost`、`title`/锁外缓存与 `stop_posix_group` PID 守卫）\n  - 更新 `workbench/messages/{zh-CN,en}.json` 与 `h3ref/messages/{zh-CN,en}.json`；更新 `tests/test_colab_runtime.py`"},{"oldText":"  - 更新/补充 `tests/test_workbench_ui.js`（顶栏历史快切、算力 Popover 授权切换、分组设置抽屉保留未保存草稿、底部控制台三 Tab 切换/搜索/错误筛选/收起展开、Toast 提示）。","newText":"  - 更新/补充 `tests/test_workbench_ui.js`（顶栏历史快切、算力 Popover 授权切换、分组设置抽屉保留未保存草稿与隐藏 Tab 校验自动切换、底部控制台三 Tab 切换/搜索/错误筛选/收起展开、`Esc` 与 `⌘/Ctrl+J` 快捷键及 `localStorage` 持久化、Toast 与 `#global-error`/`#global-message` 同步）。"}]} -->

<!-- autoplan-accepted:ceo -->
- [CEO-H1] Fix `h3ref/frames.py` `task_pending(record)` to return `True` when `record.get("task")` is present, neither `task_failed` nor `moderation_error` is set, and either `record.get("error")` is truthy OR `not record.get("file")`, preventing hard-stopped in-flight APIMart frame tasks from unlocking frame edits and causing double billing; update `workbench/static/results.js` (lines 120-125) to render `frameTaskPending` / `frameSubmissionUnknown` notices whenever those flags are true even when `frame.error` is null ONLY while `!running(job)` (showing normal generating/waiting copy while `running(job)` is true); cover with regression tests in `tests/test_job_pause_stop.py` and `tests/test_results.js`.
- [CEO-H2] Complete pause/stop state-machine hardening across backend and frontend:
  - Define `EXIT_CODE_PAUSED = 75` in `h3ref/control.py` and import it in `workbench/jobs.py`, `h3ref.py`, `workbench/storyboard.py`, `workbench/segments.py`, and `workbench/demo.py` (keeping `h3ref/` free of `workbench/` imports).
  - Migrate all 6 active-status checks to `ACTIVE_STATUSES = ("queued", "running", "pausing", "stopping")`: `Jobs.__init__`, `has_active_work()` / `has_active_video_work()`, `Jobs.available()` (`workbench/jobs.py:456`), `Jobs._snapshot()` (`workbench/jobs.py:265,310` so `live_progress` stays visible while draining), `Jobs.summaries()` (`workbench/jobs.py:426` active sort), and `workbench/static/common.js:81` `running(job)`.
  - Migrate all 11 settled/resumable/stale-PID sites: `SETTLED_STATUSES` at `workbench/jobs.py:301` (`editable_base`), `584` (`edit_frame`), `793` (`start_video_operation`); `RESUMABLE_STATUSES` at `workbench/jobs.py:289` (`settled_dead_frame`), `352` (`pending_settled_dead`), `368` (`can_resume_pipeline`), `401` (`can_resume_video`), `867` (`Jobs.resume`), `workbench/storyboard.py:102` (`can_edit_frames`), `workbench/static/video-segments.js:85` (`reconcileSegmentSubmissions`); and POSIX stale-PID cleanup at `workbench/jobs.py:464` (`Jobs.available()`) covering `status in ("interrupted", "stopped")`. Keep failure display (`status in ("failed", "interrupted")` → `stageFailed`) separate from resumable actions (with dedicated `stagePaused` / `stageStopped` copy in `results.js`).
  - Explicitly catch and re-raise `JobPaused` before all 6 broad error handlers: `h3ref/film.py:474` (`stage_frames` around `describe_composition`), `h3ref/film.py:587` (`stage_prompts` `one()`), `h3ref/film.py:648` (`gen_segment`), `workbench/segments.py:1020` (`regenerate`, with pause checkpoints placed inside `_regenerate`), `workbench/segments.py:1303` (`_execute_rewrite_targets`), and `workbench/storyboard.py:255` (`regenerate`).
  - `stage_frames` (`h3ref/film.py:405-473`): add `if not task and should_pause(job.dir): return` inside `draw()` before `frames.draw(...)` so queued futures in `ThreadPoolExecutor(4)` never submit new paid APIMart requests after Pause; use a `paused` flag to exit BOTH nested segment/shot loops cleanly before/after `describe_composition()` and before `pool.submit`; after draining `futures`, if `should_pause(job.dir)` and any wanted frame is incomplete, raise `JobPaused` BEFORE writing `job.state["frame_failures"]`.
  - `workbench/storyboard.py:regenerate()`: inside `if not pending.get("downloaded"):`, call `check_pause(out)` ONLY when `not pending.get("task")` (before `demo.placeholder(...)` and `frames.draw(...)`), letting tasks with `pending.get("task")` finish polling/downloading; preserve `inputs/frame-edit.json` on pre-submit pause so `Jobs.resume` cleanly executes `operation == "frame"` without crashing at `storyboard.py:220`.
  - Add optional `control_dir: Path | None = None` (defaulting to `job.dir`) to `h3ref/film.py:gen_segment` and `_generate_segment`; pass `control_dir=out` from `workbench/segments.py:_regenerate` (lines 961, 984) and call `check_pause(out)` before `prepare_prompt` and before `render_clip`/`gen_segment`. Check `should_pause(job.dir)` before `upload_many`/`submit_with_retry` and in `stage_generate` `blocker` branch + `run_other`; wake `ComfyNodePool` via `abort.set()` + `node_pool.notify_all()`; wait for all in-flight rewrites/frames/segments to save, then explicitly raise `JobPaused` after draining in `stage_generate` before `media.join` (if all segments finish while paused, pause before `media.join` so resume goes straight to `assemble`).
  - Restrict `can_resume_frames` strictly to: `record["status"] in SETTLED_STATUSES` AND `bool(result["storyboard"])` AND `any(f["task_pending"] or (not f["url"] and not f.get("error")) for f in result["storyboard"])` (excluding non-pending errored frames) AND `not frame_edit_task_pending(active_frame)` AND `not (active_frame.get("submission_unknown") and not active_frame.get("task"))` AND never when `operation == "frame"` has an unapplied `frame-edit.json` (except existing `settled_dead_frame` / `status == "succeeded"` rules for other frames) or during `pipeline` video phase when `review.complete` is true.
  - Startup recovery (`Jobs.__init__`): transition ALL persisted `ACTIVE_STATUSES` — when `not worker_locked(output_dir)`, `stopping -> stopped` (`error=None`, pop `pid`) and others `-> interrupted` (pop `pid`); when `worker_locked(output_dir)`, transition ALL (including `stopping`) to `interrupted` while retaining `pid` so `available()` blocks until the orphan releases `worker.lock`.
  - `Jobs.create()` / `Jobs.launch()` / `Jobs.run()` lifecycle & settlement: set `operation_id = uuid.uuid4().hex` in `Jobs.create()` (matching `Jobs.restart()`); inside `Jobs.launch()` under `self.lock` BEFORE `thread.start()`, unlink `inputs/control.json` and register `_run_events[identifier] = (record["operation_id"], threading.Event())` + shared per-run `cleanup_failed` state; check `status in ("pausing", "stopping")` inside `self.lock` before `Popen` (settle directly to `paused`/`stopped` with `error=None` without entering `except Exception:`); in the background `stop_child` thread`s `except Exception`, acquire `self.lock` and (if `operation_id` matches and `status == "stopping"`) set shared `cleanup_failed=True`, transition `record.update(status="failed", error=localized_cleanup_error)` retaining `pid`, call `self.save(record)`, and `run_event.set()` immediately (preventing permanent `stopping` hangs if a descendant holds `child.stdout` open); in `Jobs.run()` `finally:`, finalize `stopping`/exit settlement AFTER `stop_child(child)` honoring both local and shared `cleanup_failed` (checking `stopping`/`cleanup_failed` BEFORE `code == 0` post-exit mutations `report.md`/`dirty_segments`/`record_assembly()`), with strict precedence: `self.closing` > `cleanup_failed` (`failed`, never overwritten by late unblock) > `stopping` clean exit (`stopped, error=None`) > `code == EXIT_CODE_PAUSED` (`paused, error=None`) > `code == 0` (post-exit mutations + `succeeded`) > `code != 0` (`failed`); call `self.save(record)` BEFORE `run_event.set()`, and clean up `_run_events` in `finally:` only when `operation_id` matches.
  - Guard `os.killpg` in `workbench/jobs.py:stop_posix_group` with `isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and pid != os.getpgrp()`, falling back to `child.terminate()` / `child.kill()` if `os.killpg` is skipped; add localized pause-complete message key to `h3ref/messages/{zh-CN,en}.json`; cover all transitions, races, and edge cases in `tests/test_job_pause_stop.py`.
- [CEO-H3] Complete API, derivation, and UI compatibility contracts:
  - HTTP contracts: `POST /api/jobs/<id>/pause` accepts `queued`/`running` (idempotent `200` on `pausing`, `400` on `stopping` or `SETTLED_STATUSES`) and returns `200` `jobs.snapshot(id)`; `POST /api/jobs/<id>/stop` accepts `ACTIVE_STATUSES` (idempotent `200` on `stopping` without spawning duplicate `stop_child`, `400` on `SETTLED_STATUSES`) and returns `200` `jobs.snapshot(id)`; `DELETE /api/jobs/<id>` acquires `self.lock`, verifies `(storage / "jobs").resolve()` parent containment and no `ACTIVE_STATUSES`/`_probing_launch`/`worker_locked`/`process_alive` (on POSIX skipping `process_alive(pid)` for `interrupted`/`stopped` when `not worker_locked(output_dir)` to prevent recycled-PID lockouts), deletes only `<id>.{json,log}` (never `output_dir`), evicts `self.records[id]` and `Jobs.summaries()` cache, returns `200` JSON `{"deleted": true, "id": id}` (`404` unknown, `400` invalid/active), and frontend stops polling + navigates to `/` when deleting the active page job; `POST /api/jobs/<id>/open-folder` rejects client paths, verifies `resolve().is_dir()`, uses macOS `subprocess.run(["open", "--", path], timeout=5, check=False)`, Windows `os.startfile(path)`, Linux `shutil.which("xdg-open")` (`400` if absent) + `subprocess.Popen(["xdg-open", path], start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`, returning `200` JSON `{"opened": true, "path": path}`; `GET /api/jobs/<id>/prefill` returns an explicit whitelist (`kind`, `film` via `workbench/references.py:describe_references()` with all local paths stripped, `prompt`, `seconds`, `aspect`, `seed`, `check`, `frames`, `optimize_storyboard`, `demo`) and returns clean `400`/`404` `InputError` if `inputs/request.json` is missing or corrupt.
  - `active_tasks` & `node_label` exact derivation: compute `node_label = comfy.endpoint_label(attempted.get("comfyui_url") or "") or None` inside `workbench/segments.py:view()` (safely handling RunningHub/legacy records without `comfyui_url` and staged `inputs/segment-<rev>/state.json`); return `active_tasks == []` whenever `status in SETTLED_STATUSES`; `frames` stage emits `{kind:"frames", done:0, failed:0, total:1, stage:"frame"}` for `operation == "frame"`, and `{kind:"frames", done, failed, total, stage}` (storyboard ready/failed/total counts via `state.json` or `demo-frames.json`/`frames/*.png`) for film `operation in ("frames", "pipeline")` before `review.ready`; `rewrite` stage (`rewrite_prompts`/`rewrite_segment` reading `prompt-rewrite.json`, or non-demo `pipeline` counting `.h3.txt` vs total segments ONLY while `done < total` and no segment has a `task` or `clip`) emits `{kind:"rewrite", done, total}`; `segment` stage builds entries from `result["segments"]` cards (each card with `live_progress` emits its ComfyUI progress + `node_label`, and each remaining `card["task_pending"]` emits `{kind:"segment", number, stage: provider_or_runninghub, step:null, max_steps:null, node_label}`, covering resumed RunningHub segments that retain transient `error` until download); `kind == "one"` emits a single `{kind:"segment", number:1, ...}` from `live_progress` or pending `task`.
  - Filename auto-matching & conflict tray (`create.js`): chunk drag-and-drop uploads sequentially into `POST /api/uploads` batches constrained by BOTH count (`<= 9` files) AND cumulative size (`<= 28 MiB`, headroom under Flask 32MB `MAX_CONTENT_LENGTH`), failing fast with a localized error Toast if any single file exceeds 28 MiB; normalize stem (lowercase, strip extension, trim `_- ` separators); exclude characters belonging to a multi-character cast group (`state.filmGroups`) from solo matching and match grouped slots ONLY at group level (normalized joined member names / group label, binding all `group.slotIds` identically per `workbench/references.py:87-92`); match `board` keywords (`board`/`storyboard`/`scene`/`场景`/`画板`), solo character `name`/`cast[].aliases`, and cast groups using exact-stem match first then unambiguous substring match; route ambiguities (1 file → >1 target, >1 file → 1 slot, unmatched file, or target slot already occupied) to `#upload-conflict-tray` with per-file target dropdown (`Assign` / `Replace` / `Dismiss`), never silently overwriting bound slots.
  - Sticky Command Bar 6 states, Pause/Stop confirmation, 3-tab visibility & storyboard filter/carousel (`results.js` + `video-segments.js`):
    - Pause triggers `POST /api/jobs/<id>/pause` immediately with Toast feedback; Stop opens a confirmation modal (closable via `Esc`, explaining remote tasks are not cancelled and `task_id` is preserved) before `POST /api/jobs/<id>/stop`. Both Pause and Stop use a dedicated control-action helper (with duplicate-click guard and poll rescheduling) rather than `runJobAction()` (which returns early whenever `running(state.selectedJob)` is true).
    - 6 Command Bar states: (1) `queued`/`running` → Pause + Stop; (2) `pausing`/`stopping` → disabled status badge (+ Stop during `pausing` only); (3) `paused`/`stopped`/`failed`/`interrupted` OR (`status === "succeeded"` && `(job.can_resume_frames || job.can_resume_pipeline)`) → primary `#resume-job` (kept disabled whenever `job.can_resume_video === false` while preserving `#resume-hint` for `terminal_failed`/`submission_unknown`/`moderation_failed`, matching `tests/test_workbench_ui.js:353-452`) + secondary Open Folder/Clone; (4) `Boolean(job.can_continue)` (i.e., `canGenerateVideo(job)`, never `kind == "one"`) → primary Confirm & Generate Video; (5) `Boolean(job.can_assemble && job.pending_assembly)` → primary Assemble Final Video (while keeping the Segments-tab `#assemble-job` button gated on `!locked && job.can_assemble` for manual re-assembly and test compatibility); (6) settled `succeeded` (final ready or `kind == "one"` done) → Open Folder/Download/Clone (or if `status === "succeeded"` && `frame_failures > 0` && `!job.can_resume_frames`, show repair-needed banner pointing to failed frame cards).
    - Keep `selectResultTab` / `renderResultTabs` in `workbench/static/video-segments.js` (extended to 3 tabs with null-safe DOM checks for `tests/test_video_segment_prompts.js:97`). Mode visibility: `film` (`frames != "none"`) shows `#tab-storyboard`, `#tab-segments`, `#tab-report` + pinned collapsible `#final-video-section` (`#final-video`); `frames == "none"` (`review.kind == "script"`) keeps `#tab-storyboard` visible (dynamically labeled via i18n as Script Plan) hosting `#script-section` (`#script-plan`) alongside `#tab-segments` + `#tab-report`; `kind == "one"` hides BOTH `#tab-storyboard` and `#tab-segments` and renders the single output video in the pinned `#final-video-section` above `#tab-report` (active by default).
    - Storyboard status filter + click-thumbnail-to-carousel: in list view, clicking a thumbnail calls `event.preventDefault()` on `<a class="storyboard-preview">` to switch into carousel mode at that frame (carousel view retains opening full image in a new tab); pagination and carousel prev/next operate on the active filtered subset and reset page/index to 0 whenever the filter changes.
  - Settings Drawer 3-tab mapping (`#config-form`): Tab 1 (`Video & Compute`): `VIDEO_PROVIDER`, `RUNNINGHUB_API_KEY`, `COMFYUI_URL`, `COMFYUI_API_KEY`, `COMFYUI_CONCURRENCY`, `COMFYUI_PRESET`, `COMFYUI_RESOLUTION`, `COMFYUI_UPSCALE`, `COLAB_GPU`, `COLAB_AUTO_START`; Tab 2 (`Image & LLM`): `APIMART_API_KEY`, `LLM_PROVIDER`, `LLM_API_KEY`, `GEMINI_API_KEY`, `LLM_COMPOSITION_MODEL`, `LLM_REWRITE_MODEL`, `LLM_BASE_URL`; Tab 3 (`Storage & Environment`): `#environment`, `#persist-config`, `#config-storage`. Attach a capture-phase `invalid` listener on `#config-form` to unhide the owning tab panel, activate its tab button, and call `reportValidity()`.
  - Create page active-log polling & test/DOM/CSS/CSP compatibility: on `/`, when `/api/bootstrap` reports an active job ID, poll `GET /api/jobs/<active_id>/logs?offset=<byte_offset>` at `1800ms` ONLY while the bottom console is expanded AND on the Job Log tab (stop immediately on collapse, tab switch, or job settlement); keep `#global-error`/`#global-message` updated in `showError`/`showMessage`/`clearNotices` alongside Toast; keep per-slot `managed_nodes[0/1].logs` separate for console tabs (mirroring active slot to `#runtime-logs` if present); preserve `style.css` `44px` `input`/`select`, `.header-tools select` `height: auto`, `.page-jump input`, `.artifact .plyr input`, `.checkbox-field input` `16px`, and `max-width:68ch` invariants; enforce zero inline JS / zero untrusted `innerHTML` under `script-src 'self'`; guard all browser globals (`window`, `document.body?.classList`, `sessionStorage`, `localStorage`, `document.addEventListener`, `document.querySelector`) for Node VM tests.
- [CEO-B1] Confirm create-page `JSON 格式化与摘要条` and `sessionStorage` draft (`h3.createDraft.v1` schema: `{kind, film_json, prompt, seconds, aspect, seed, check, frames, optimize_storyboard, demo, demo_placeholders}`, excluding file bindings; `restoreCreateDraft()` runs after `await langPromise` when no `?from=<job_id>` is present and overwrites `$("prompt").value` whenever `draft.prompt` is non-empty, leaving `t("samplePrompt")` only when no draft prompt exists; `?from=<job_id>` overrides draft; cleared on successful submit) as part of Stage 3 with unit tests in `tests/test_workbench_ui.js`.
- [CEO-E1] Define `MAX_SLOT_LOGS = 200` in `h3ref/colab_runtime.py` and apply it across all three sites: `_append_log` (line 1016), `_logs_tail` (line 1007), and `status()` (lines 1222-1223); audit hardcoded 15/80 assertions and add regression coverage in `tests/test_colab_runtime.py`.
- [CEO-E2] Persist `title` on creation and settled `progress_summary` (`frames_done`, `frames_total`, `clips_done`, `clips_total`, `cost_cny`) on settlement in job records; snapshot selected `record` dicts under `self.lock` then run mtime-keyed cache checks and disk reads OUTSIDE `self.lock` under `self._summary_cache_lock`, keyed by `(record, request.json, state.json/demo-frames.json, clip.mp4, frames/, clips/)` `(mtime_ns, size)` with graceful fallback when `output_dir` is missing on disk; map `kind == "one"` to `frames_total=0, clips_total=1`; verify in `tests/test_job_pause_stop.py`.
- [CEO-E3] Add `Esc` (close active popover/drawer/modal) and `⌘/Ctrl+J` (toggle bottom console when focus is outside inputs) shortcuts plus `localStorage` persistence for console height, collapsed state, and active tab in `workbench/static/common.js` (Stage 2); cover in `tests/test_workbench_ui.js`.
- [CEO-E4] (Provisional taste default, Phase 4 gate `d1`) `POST /api/jobs/<id>/stop` atomically persists `control.json` (`action="stop"`) and `status="stopping"`, dispatches `stop_child` in a deduplicated background thread outside `self.lock` (catching `Exception` to mark shared `cleanup_failed=True`, transition `stopping -> failed` retaining `pid`, `self.save(record)`, and `run_event.set()` immediately so blocked `child.stdout` cannot hang), waits up to `1.5s` on the `operation_id`-scoped per-run `Event` registered in `Jobs.launch()` before `thread.start()`, and returns `200` `jobs.snapshot(id)` (`stopped` on fast exit or `stopping` on slow exit); cover fast, slow, duplicate-stop, blocked-stdout `cleanup_failed`, and `queued` pre-spawn paths in `tests/test_job_pause_stop.py`.
- [CEO-E5] (Original default preserved; Dual-Model User Challenge, Phase 4 gate `uc2`) Deliver Stages 1, 2, and 3 as 3 sequential verified commits on a single feature branch / single PR (`pr_count=1`) so UI ships atomically while preserving granular rollback and test checkpoints.
- [CEO-H4] Block `DELETE /api/jobs/<id>` with HTTP `400` and localized message whenever any storyboard frame, video segment, `inputs/frame-edit.json`, or `inputs/segment-edit.json` has unsettled `task_pending` or `submission_unknown` (preventing history deletion from permanently orphaning the UI resume handle for paid in-flight tasks); warn explicitly in the Stop confirmation modal that stopping mid-HTTP-submit can leave a task in `submission_unknown` requiring provider dashboard verification; display the job's output folder path and an "Open Folder" shortcut inside the Delete confirmation modal; cover in `tests/test_job_pause_stop.py` and `tests/test_workbench_ui.js`.
- [CEO-H5] Keep billing semantics unchanged (Non-Goal #3) while labeling UI cost badges and tooltips accurately via i18n as "Known API cost (kept assets)" / "当前采用素材已知费用" and displaying ComfyUI `gpu_seconds` separately (e.g., `¥X.XX · Ym GPU`, noting discarded rerolls and self-hosted/Colab GPU compute are excluded from CNY) so creators do not under-budget; cover locale keys in `tests/test_locale_parity.py`.
- [CEO-H6] Add an auto-hiding cold-start setup banner at the top of `<main>` whenever `/api/bootstrap` reports missing required API keys or an unready video provider for the selected mode (1-click opens the owning Settings Drawer tab); render an inline 3-5 line recent log tail + "View full log in console" link inside the Colab Popover during `provisioning`/`starting`/`awaiting_auth`/`error`; default the unified bottom console to **collapsed (`32-36px` status bar)** on first visit (persisted in `localStorage` once toggled) with an unread warning/error badge so 85%+ of viewport height remains dedicated to frames and clips per `DESIGN.md`; cover in `tests/test_workbench_ui.js`.
- [CEO-E9] Add lightweight `GET /api/jobs/<id>/logs?offset=<byte_offset>` returning `{"status": ..., "offset": <next_byte_offset>, "chunk": <redacted_delta>, "truncated": <bool>}` (falling back to full redacted tail with `truncated: true` if file shrank/rotated, `404` on unknown ID) and use it for Create-page active-job console polling at `1800ms` (appending deltas without replacing DOM selection or re-reading/redacting 100KB snapshots); pass optional `should_stop: Callable[[], bool] | None` hook in `h3ref/control.py` / `h3ref/film.py` entrypoints (defaulting to `should_pause(control_dir or job.dir)`) to keep `h3ref/` model-agnostic; cover in `tests/test_job_pause_stop.py` and `tests/test_workbench_ui.js`.
<!-- /autoplan-accepted:ceo -->

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | CEO (Step 0) | CEO-H1: 修复 `h3ref/frames.py:task_pending` 对强停无 `error` 在途分镜的漏判 | Mechanical | P1 Completeness | 否则点击「立即停止」后分镜可被重新编辑提交，直接触发 APIMart 重复扣费 | 保留旧逻辑仅靠 `error` 判断（会破坏防重复扣费红线） |
| 2 | CEO (Step 0) | CEO-H2: `JobPaused` 显式透传 + `queued` 窗口竞态保护 + `has_active_*` 含 `pausing`/`stopping` + `stop_posix_group` PID 守卫 | Mechanical | P1+P2 Boil Lakes | 现有 4 处宽泛 `except` 会把软暂停污染成失败状态；`os.killpg` 无 PID 守卫曾导致 Linux CI runner 被杀 | 仅按原计划在循环首部加检查点（会引入静默状态破坏与 CI 炸弹） |
| 3 | CEO (Step 0) | CEO-H3: 复用 `describe_references`、补齐 `kind=="one"`/`frames=="none"`/合图/隐藏 Tab 校验/删除当前页防 404 轮询/CSP 与 Node VM 兼容守卫 | Mechanical | P1+P4+P5 | 复用已有去路径逻辑避免双份实现；原计划未写明单段任务、无图脚本、合图及 3500 行 VM 单测兼容约束 | 实施期临时踩坑修补（返工成本高且易破坏已有测试） |
| 4 | CEO (Step 0) | CEO-E1: `colab_runtime.py` 单槽日志上限与 `managed_nodes[].logs` 返回条数扩至 200 行 | Mechanical | P1+P2 Boil Lakes | 现状仅返回 15 行，统一控制台的搜索与错误过滤形同虚设；1 个文件 <10 分钟 CC 工作量 | 维持 15 行（让新控制台 Colab Tab 几乎不可用） |
| 5 | CEO (Step 0) | CEO-E2: 终态持久化 `title`/`progress_summary` + `mtime` 缓存 | Mechanical | P1+P3 Pragmatic | `summaries(50)` 每 1.8s 轮询若逐任务读磁盘将产生 100+ 次 I/O | 每次轮询全量读盘 |
| 6 | CEO (Step 0) | CEO-E3: 增加 `Esc`/`⌘+J` 快捷键与底部控制台 `localStorage` 状态记忆 | Mechanical | P2 Boil Lakes | 跨创建/结果多页跳转时保持控制台展开高度与 Tab，消除频繁重拖摩擦；同文件改动 | 每次换页重置控制台为默认折叠/高度 |
| 7 | CEO (Step 0) | CEO-E4 (`d1`): `POST /api/jobs/<id>/stop` 锁外执行 `stop_child`，有界等待 1.5s 后未退出转后台收尾并返回 `stopping` | Taste | P1+P3 | 95% 场景 <150ms 即返回 `stopped`，同时杜绝异常卡死时阻塞 Flask 线程 10s；备选纯异步立即返回 `stopping` 留待 Phase 4 确认 | 同步阻塞最长 10s（卡死并发轮询） |
| 8 | CEO (Step 0) | CEO-E5 (`d2`): 单 PR 内按阶段 1/2/3 分 3 个独立验证提交顺序落地 | Taste | P1+P6 | 遵循默认 `pr_count=1` 且用户看到完整一致的界面；备选先单独合入阶段 1 后端 PR 留待 Phase 4 确认 | 单一大提交混合前后端（回滚与排查困难） |
| 9 | CEO (Step 0) | CEO-E6: SSE / WebSocket 实时日志流推迟至 `TODOS.md` | Mechanical | P3+P5 Explicit | 现有 1.4s/2.5s 轮询零基础设施且已稳定测试；引入流式连接会冲击 Flask 开发服并发线程 | 本次同步引入 SSE（超出爆炸半径） |
| 10 | CEO (Step 0) | CEO-E7/E8: 拒绝多任务排队与删除记录时连带删输出目录 | Mechanical | User Sovereignty | 严格遵守计划第 1.2 节明确声明的非目标红线 | 突破用户明确边界 |
| 11 | CEO (Step 0H) | 0H Spec Review 1轮全部 25 项修复（6处 ACTIVE_STATUSES、6处 JobPaused 透传、stage_generate/storyboard 边界、HTTP 幂等与后台 stop_child、active_tasks/自动匹配/6态指挥栏/3Tab设置抽屉精确契约、CEO-B1 确认） | Mechanical | P1+P2+P5 | 消除基线与增量条款矛盾，补齐所有实现模糊点与 Node VM/style.css 回归风险 | 留到实现阶段踩坑或跳过 0H 复检 |
| 12 | CEO (Step 0H Iter 2) | 0H Spec Review 2轮全部 17 项深度代码级修复（`control_dir` 透传、保留 `frame-edit.json` 与 `can_resume_frames` 守卫、11处 `RESUMABLE`/`SETTLED` 迁移、`EXIT_CODE_PAUSED` 移至 `h3ref/control.py`、`running(job)` 门控、`frames==none`/`kind==one` Tab 与 `#script-section` 归属、`<=28 MiB` 分批、`_summary_cache_lock` 锁外缓存） | Mechanical | P1+P2+P5 | 消除真实运行期崩溃、单镜付费任务误删与锁竞争风险 | 放任至编码阶段排查 |
| 13 | CEO (Step 0H Iter 3 Cap) | 0H 3轮封顶收口 8 项最终代码级修复（`stage_frames` 池内 `draw()` 守卫 + 双层循环 `paused` 标志、`storyboard.regenerate` 无 `task` 暂停点、`can_resume_frames` 终态+非错谓词、`Jobs.create` `operation_id` + `Jobs.launch` 预注册 + `child.stdout` 阻塞破局、State 5 门控 `can_assemble && pending_assembly`、State 3 `#resume-job` 禁用保 `#resume-hint` + 专用控制助手、`operation=="frame"` 0/1 计数、`restoreCreateDraft` 时序） | Mechanical | P1+P2+P5 | 达 3 轮 Spec Review 上限（8/10），8/8 修复均已直接核验写入计划并在 Step 0.5 双声评审中复检 | 超过 3 轮上限继续循环 |
| 14 | CEO (Step 0.5) | CEO-H4: `DELETE /api/jobs/<id>` 拦截存在未结 `task_pending`/`submission_unknown` 的任务 + 强停弹窗提示 `submission_unknown` 窗口 + 删除弹窗展示目录路径与打开按钮 | Mechanical | P1 Completeness | Codex P0 指出强停后删历史会丢失唯一 UI 续跑入口导致已付费在途素材无法取回；同时缓解已删记录磁盘孤儿膨胀 | 允许直接删除带未结付费任务的记录 |
| 15 | CEO (Step 0.5) | CEO-H5: 费用文案精确标注「当前采用素材已知费用」并将 ComfyUI `gpu_seconds` 分列展示（不改计费口径） | Mechanical | P1+P5 | Codex High 指出仅汇总活动记录且 ComfyUI 为 `0.0` 会系统性低估反复抽卡成本；不违反非目标 #3 | 沿用误导性的「总费用」标签 |
| 16 | CEO (Step 0.5) | CEO-H6: 未配置冷启动引导条 + Colab Popover 内联 3-5 行日志预览 + 底部控制台默认收起（32-36px）与未读报错角标 | Mechanical | P1+P2 | Native High 指出藏起配置与算力会伤及 Windows 新手与 Colab 授权流，且 230px 常驻底部栏违反 `DESIGN.md` 画面优先原则 | 首次进入默认 230px 展开且无新手引导 |
| 17 | CEO (Step 0.5) | CEO-E9: 新增轻量增量日志接口 `GET /api/jobs/<id>/logs?offset=` 替代创建页 1.8s 全量快照轮询，并在 `h3ref/film.py` 入口提供轻量 `should_stop` 回调封装 | Mechanical | P2+P5 | 消除每 1.8s 重读脱敏 100KB 日志与破坏日志选区，同时保持 `h3ref/` 核心引擎轻量解耦 | 在 `/` 每 1.8s 轮询全量 `GET /api/jobs/<id>` |
| 18 | CEO (Step 0.5) | `uc1`（停止策略双模型挑战）与 `uc2`（单 PR 拆 3 PR 双模型挑战，升级自 `CEO-E5`）保留原需求并提交 Phase 4 Gate | User Challenge | P6 User Sovereignty | Native 与 Codex 均独立挑战不取消远端的「立即停止」及单 Mega-PR 交付；按规则绝不自动覆盖用户拍板，交由 Phase 4 最终决策 | 擅自删掉「立即停止」或强行更改 `pr_count` |
| 19 | CEO (Step 0.5) | `d2`（`prefill` 剥离路径 vs 服务端复用绑定）与 `d3`（DOM 双写保旧测 vs 迁移测试删镜像）标为 Taste；`CEO-E10..E12`（脚本向导/单段试拍与多 Take 对比/待办排队）推迟至 `TODOS.md` | Taste / Deferred | P3+P5 | 保留当前范围聚焦同时将 10x 路线图与边界取舍透明沉淀 | 当期强塞大特性或静默丢弃双声洞见 |
| 20 | Design (Pass 1) | DES-H1: 结果页拆为单行紧凑吸顶指挥栏（<=56px）+ 非吸顶可滚遥测区；#resume-job/#continue-job 迁入指挥栏单主 CTA；严格 6 态 if/else 优先级含可取回在途与一键定位阻塞项；待重拼时成片条默认折叠 44px 并保留「下载旧版」；Live 主按钮保留付费文案与琥珀边线 | Mechanical | P1+P5 | 修复 39%-58% 视口被 Chrome 吞噬、同屏双主 CTA、合法在途取回入口被封及违背 DESIGN.md 付费区分规约 | 保持 240-310px 吸顶堆叠或盲目禁用续跑 |
| 21 | Design (Pass 1/6) | DES-H2: 修复窄屏 Hard Rejection #7（<=1060px 创建页底吸顶紧凑发车条）、补齐 1060/760/420px 断点（760px 弹层 fixed 防溢出）、修正 .console-tools input.console-search 特异性、拆分 --console-open-h 与 --console-h 限幅 | Mechanical | P1+P5 | 原型在 1100px 把发车台打回文档流且 410px Popover 在 420px 溢屏 170px；input.console-search 与 input[type=text] 特异性相同会不稳定 | 仅写抽象窄屏描述或用同特异性选择器 |
| 22 | Design (Pass 2) | DES-H3: 轻量状态接口补充 log_alert_seq/counts 支撑折叠态未读徽标；/jobs/<id> Tab 1 永久绑定当前页任务并迁入 #job-logs 做按行增量 diff；纯文本错误筛选继承 Python Traceback 缩进续行；保留 <40px 锁底判定与「跳至最新」悬浮按钮 | Mechanical | P1+P5 | 解决折叠态未读徽标无数据源、看旧任务被活跃任务劫持日志槽、报错筛选丢堆栈、无条件滚底打断阅读 4 大实现陷阱 | 仅靠展开态增量接口喂未读徽标或全量替换日志 DOM |
| 23 | Design (Pass 1/2) | DES-H4: 顶栏上下文任务入口替代含糊「当前任务」；保留 .header-tools <select id="language"> 与本机运行标识；Demo 模式/已结任务隐藏冷启动横幅；设置抽屉区分取消回滚与关闭留草稿；Colab Popover 440px 可滚全量容纳既有 #runtime-* 流且不抢焦点 | Mechanical | P1+P4 | 消除原型与 base.html 测试冲突，修复 768px 屏高下 Colab Popover 裁切及冷启动误导 Demo 用户 | 静默删掉本机标识或用 <button> 替换语言 <select> |
| 24 | Design (Pass 3) | DES-H5: pausing 保留真实步骤/百分比进度条；停止动作更名「停止本地执行」并在结果页持续区分本地已停/可取回 N 项/待核对/cleanup_failed；历史列表凭 has_unsettled_tasks 提前禁用删除；补齐 404/断联持久错误卡与隐藏 0/0 分镜指标 | Mechanical | P1+P5 | 消除 pausing 进度黑盒焦虑、停止文案自相矛盾与 404 白屏问题 | pausing 换黄色模糊进度条或靠自动消失 Toast 报 404 |
| 25 | Design (Pass 2/3) | DES-H6: 全局拦截误拖图片跳转、单卡直拖绑定、分批上传进度与部分失败保留已绑仅重传失败项；明确 #upload-conflict-tray 挂载与多角色合图组卡；复刻前备份旧草稿并显示缺失参考图提示条与撤销恢复、创建页补清空按钮 | Mechanical | P1+P5 | 闭环批量拖放部分失败、多角色合图回归与复刻静默覆盖草稿三大断点 | 拖放失败重传全部或静默覆盖未保存草稿 |
| 26 | Design (Pass 3) | DES-H7: #continue-job 检测未应用分镜草稿弹确认；整个 .storyboard-media（含失败/等待占位）可点击进大图；大图按 frame.key 锚定不跳飞；分镜筛选补「待处理/在途」与空筛选引导；网格强设 align-items:start 且 ComfyUI 卡不刷 ¥0.00 | Mechanical | P1+P5 | 防止带未应用改词误点付费出片，解决失败卡点不进大图及修完画面跳飞 | 仅拦截有图 <a> 或按数组下标索引大图 |
| 27 | Design (Pass 4/5/6) | DES-H8: 元信息用 --muted (6.24:1) 替代 #74877c (3.82:1)；粗指针 44px 触控命中区；.toast-stack 迁至右上角 z:95 防盖保存按钮/被 Modal 压住，移除 scrollIntoView 并将 #global-error/#global-message 设为 .sr-only 单一播报源；Modal 焦点陷阱默认聚焦「取消」+ LIFO Esc + 键盘控制台预设高度 + invalid 防重入锁 + 去除竞争性常亮脉冲并支持 reduced-motion | Mechanical | P1+P5 | 满足 WCAG AA 对比度、键盘/读屏闭环、避免双读屏播报与多隐藏页签校验死循环 | 保留右下角 z:85 Toast 盖住抽屉保存按钮与 3.82:1 灰字 |
| 28 | Design (Pass 1/7) | DES-H9: 增补「原型差异权威覆盖表」（8 处覆盖原型）、中英双语动态 document.title 模板、Tab 3 计数与空态，以及双语 4 断点浏览器视觉/键盘验收清单 | Mechanical | P1+P5 | 杜绝实现者照抄原型过期 DOM/CSS，补足 Node VM 无法覆盖的真实浏览器视觉与焦点验收 | 依赖原型作为隐式规范 |
| 29 | Design (Pass 7) | d4 (Taste): 分镜网格默认 3 列 + align-items:start + .frame-editor 展开跨全行 (grid-column:1/-1) 保 100% 既有角色选择器单测兼容（备选 4 列点编辑切大图改单测，留待 Phase 4 Gate） | Taste | P1+P5 | 避免 4 列 315px 窄卡内嵌 6 行编辑器拥挤，同时不破坏 test_frame_character_picker.js | 4 列强塞内嵌编辑器导致挤压错位 |

| 30 | DX (Step 0.5/Pass 2) | DX-H1: `control.json` 绑定 `H3_WORKBENCH_OPERATION_ID` + `H3_WORKBENCH_WORKER=1`（零 CLI 破坏）、RunningHub 60s 退避内与 APIMart 上传后逐次查暂停、`JobPaused` 退出码 75 输出结构化 `stderr` | Mechanical | P1+P5 | Codex Critical 发现 60s 退避/上传后仍会发出新付费 POST；双声均指出遗留 `control.json` 会把 CLI 直接续跑卡死在退出码 75 | 仅在循环首部检查且按全局 action 生效 |
| 31 | DX (Step 0.5/Pass 3) | DX-H2: 消除两项 `CAP4` 假成功（`/open-folder` 查 macOS 退出码、Linux `DISPLAY`/`WAYLAND_DISPLAY` + `stdin=DEVNULL` + `0.2s` 轮询返回 `dispatched`；`/stop` 限幅所有 `child.wait()`，`cleanup_failed` 返 HTTP `500` RFC 9457、`stopping` 返 `202`）并支持 `/pause` `/stop` 可选 `operation_id` `409` 前置校验 | Mechanical | P1 Completeness | 双声独立抓出两处 `CAP4`（失败返 HTTP 200）；`jobs.py:132,134` 无界 `child.wait()` 可永久卡死清理线程；延迟重试可能停掉新续跑任务 | `check=False` 恒返 200 或 `cleanup_failed` 返 200 snapshot |
| 32 | DX (Pass 3) | DX-H3: 全部 6 个新接口与 `/api/config` 接入 RFC 9457 `api_problem()`（`job_problem` 封装、递归脱敏扩展字段、500 写脱敏关联 trace），升级 `common.js:request()` 安全 JSON 解析与设置抽屉服务端 `field` 跨 Tab 定位 | Mechanical | P1+P5 | 消除 5 项 `CAP6` 缺口，复用已有 `api_problem()` 达成 `agent-ready-v1` 契约，修复非 JSON 413/502 抛 `SyntaxError` | 6 个新端点落回旧 `{error}` |
| 33 | DX (Pass 3/5) | DX-H4: 统一 `unsettled_tasks()` 覆盖 `kind=="one"` 根 `state.json` + 分镜/片段/编辑文件并按 `.log -> .json -> 缓存` 顺序删除；`cleanup_failed` 启动/互斥按 `worker_locked()` 核验归属防止复用 PID 永久锁死 | Mechanical | P1+P5 | Codex High 指出仅扫 frames/segments 会漏拦最简单的 `one` 付费任务，且 POSIX 复用 PID 会永久卡死工作台 | 分散扫描未决任务或仅靠 `os.kill(pid,0)` 判活 |
| 34 | DX (Pass 2/6) | DX-H5: `/logs` 增加 `generation` 轮转标识、`offset>=0` 校验、有界 `limit` 与脱敏后行/UTF-8 边界对齐；支持 `H3_COLAB_MAX_LOGS`（`20..2000`）与 `logs_truncated`；统一 `progress`/`log_alert_seq`/`should_pause`/`h3.consoleState.v1`、`active_tasks` `null` 契约与 `?include_logs=0` | Mechanical | P1+P5 | 解决等长日志轮转静默丢行、中文截断与跨块敏感串漏脱敏，消除同类指标双命名与硬编码 | 仅靠裸 `offset` 切片与硬编码常量 |
| 35 | DX (Pass 1/6) | DX-H6: 新增 1 键「载入示例剧本」并让冷启动「试离线演示」2 次点击直达首张分镜；短角色名（<2 字）禁任意子串误匹配 + 「撤销自动匹配」+ 超限 Toast 带文件名/体积；补快捷键与文件名示例提示 | Mechanical | P2+P5 | 双声确认 Demo 仍需「下载再导入」9 步往返（TTHW 5-8 分钟）；2 步直达将浏览内 TTHW 压至 <15 秒 | 保持下载 JSON 再手动上传往返 |
| 36 | DX (Pass 4/5/7/8) | DX-H7: 阶段 1/3 纳入 `docs/workbench.md`（`#first-offline-job` 可运行 `curl`、`#job-control-errors` 6 路由/退出码 75 表、静默升级与 `.workbench/jobs/*.json` 备份回滚指南）、`docs/workbench-cli-parity.md`、`README.md`、`SKILL.md`、原型过期提示及文档冒烟单测 | Mechanical | P1+P5 | 双声确认 3 个阶段零文档交付；补齐可运行离线示例与回滚指引避免用户与 Agent 迷路 | 把文档更新留到上线后口头补写 |
| 37 | DX (Pass 5) | DX-T1 (`d5` Taste): `DELETE /api/jobs/<id>` 保留 `CEO-H4` 默认 400 拦截，暂定增加非活跃任务专用 `?force=1` + `{"acknowledge_unsettled":true}` 逃生口（仍绝不删 `output_dir`）；备选 Codex 严格无覆盖版交 Phase 4 Gate | Taste | P3+P6 | 防止 Colab/RunningHub 失效后历史列表永久死锁，同时严守绝不删磁盘文件与绝不删活跃任务边界 | 既无逃生口又未提供核对工具，或直接取消默认拦截 |
| 38 | Eng (Sec 1/2) | ENG-H1: 补齐 3 处漏抓 `except Exception`（`film.py:413` 池内 `draw()`、`film.py:748/772` ComfyUI 故障切换），将暂停准入前置到 `submission_unknown=True` 与 ComfyUI/RunningHub/APIMart/LLM 内部重试前 + `ComfyNodePool.acquire()` 0.5s 轮询，先排空已有 `task` 再准入新项，且并发异常优先于 `JobPaused` | Mechanical | P1 Completeness | 双声在源码中直接验证 `film.py:413` 会把 `JobPaused` 写成永久分镜错误、`748/772` 会把健康节点标离线、并行 `first_error` 会吞掉真实报错 | 仅保留原 6 处 `except` 透传 |
| 39 | Eng (Sec 1/2) | ENG-H2: `worker_locked()` 清理严格限定 POSIX（修复 Windows `worker_locked()` 恒 `False` 绕过互斥锁的 P0 漏洞），引入 `RunState` 固化 `settlement_snapshot` 与按子进程对象身份清理 `children`，`Jobs.run()` `try:` 守卫后置变更、`has_active_work()` 含 `self.children`、注册表先落盘再写 `control.json`、`launch` 失败回滚、明确 Workbench Resume 为主恢复入口并给 `cmd_one` 加 POSIX 锁校验 | Mechanical | P1+P5 | 双声抓出 Windows 互斥击穿、`stop(A)->resume(B)->return stop(A)` 串包、`Jobs.run()` 覆盖 `cleanup_failed` 及 CLI/浏览器竞态 4 类高危并发缺陷 | 在全平台直接按 `not worker_locked` 清 PID 或直接回传实时 `snapshot(id)` |
| 40 | Eng (Sec 2/4) | ENG-H3: `unsettled_tasks()` 复用 `segments.task_unresolved`/`pending_superseded` 扫描 `inputs/segment-<rev>/state.json` 并豁免 `kind=="one"` 的 `terminal_failed`，`summaries()` 坏 JSON 安全降级而 `DELETE` 失败关闭；后端统一给出 `recovery_action`/`blocking_target` 避免「仅取回」文案暗含新付费派发 | Mechanical | P1 Completeness | 双声验证 `segment-edit.json` 不含 `task`（在 staged `state.json`）且 `terminal_failed` 单段会变永久删不掉；`summaries()` 异常会 500 拖垮整个工作台 | 仅扫 5 个顶层文件或仅凭前端推断恢复动作 |
| 41 | Eng (Sec 1/3) | ENG-H4: `POST /stop` 的 `200`/`202` 统一为扁平 snapshot（`202` 顶层加 `poll_url`）；新增 `_optional_json_object()` 解决空体 `curl` 415 与严格 bool/有限 `wait_seconds` 校验；`open-folder` 拒绝 macOS `.app`/`.bundle`/`.framework` 与符号链接并显式 `open -a Finder --`；`prefill` 递归白名单裁剪并修复 `3600.4` 复刻回环；补 `record["id"]` 正则校验、`server.py:345` `OSError` 守卫与 `405` RFC 9457 回退 | Mechanical | P1+P5 | 消除 `202` 嵌套信封打爆 `results.js`、`open --` 启动 `.app` 安全漏洞、空体 `curl` 415 与嵌套字段泄露 | `202` 嵌套 `.job` 或继续 `open --` 裸调 |
| 42 | Eng (Sec 1/4) | ENG-H5: 修复 `/logs` 超长行死循环、脱敏偏移漂移、跨游标切半密钥、新任务首行误 `reset:true` 与宽松整数校验；创建页与结果页统一复用 `?include_logs=0` + `/logs?offset=` 消除 100KB/1.4s 滑窗 diff 与 0.71MB/s 读盘风暴；限制浏览器与 Colab 行缓冲，安全解析 `H3_COLAB_MAX_LOGS`，并用共享黄金夹具对齐 Python/JS 告警分类器 | Mechanical | P1+P3+P5 | 双声抓出超长行 `next_offset==offset` 死循环与 `100KB` 滑窗失效；单读取器同时降载 10x 轮询并保证折叠态 0 日志读盘 | 结果页继续轮询 100KB `job.logs` 做前端 diff |
| 43 | Eng (Sec 2/4) | ENG-H6: 创建与终态持久化摘要使温热历史列表 0 次 `stat()` 零盘读，活跃缓存含 `frame-edit.json`/`segment-edit.json`/staged 邮戳且释放锁执行 I/O 并防删除复活；`_snapshot()` 对缺失 `output_dir` 优雅降级不报 500；修正费用标签与现有 `tests/test_workbench.py:223` 累加语义一致；Windows `DELETE` 增加 WinError 5/32/33 退避重试 | Mechanical | P1+P3 | 避免 50 条历史每 1.8s 350 次 `stat()`、消除缺失目录 500 死页与文案自相矛盾 | 每次轮询全量 `stat()` 并在文案声称排除所有重绘 |
| 44 | Eng (Sec 1/2/3) | ENG-H7: 从 925 行 `common.js` 拆出零构建 `console.js` 与 `shell.js`；为既有 3,531 行 Node VM 单测保护 `response.text()` 与 `JSON.parse(localStorage)`；将 `grid-column:1/-1` 移至父级 `.frame-card.is-editing`；补 `results.js` `controlSubmitting`/`jobRevision` 防轮询覆盖与暂停分镜只读提示；补 `create.js` `one` 9 图前置校验、合图同步冲突托盘与撤销失效 | Mechanical | P1+P4 | 直接读取 `test_workbench_ui.js:33,93,98` 证实未守卫会在加载期立即崩溃；`.frame-editor` 是孙节点无法跨外层网格 | 把全部外壳塞进 2000 行 `common.js` 或对孙节点设 `grid-column` |
| 45 | Eng (Sec 3) | ENG-H8: `workbench/demo.py` 改用 `.tmp` + `atomic_replace` 原子发布防强停毁坏演示产物；新增离线降级脚本 `scripts/prepare_downgrade.py` 与旧版读取器回归夹具（保留最新 `state.json` 不丢付费 `task_id`）；将真实渲染模板 4 视口×双语浏览器冒烟与 Windows CI 进程树测试列为发版门禁 | Mechanical | P1+P5 | 弥补 Node VM 无法测几何/焦点及旧版读不了 `paused`/`stopped` 单段记录的发布回滚风险 | 仅依赖 VM 桩单测与手工回滚文档 |
| 46 | Gate rev 1 (`rec`) | UC2A (Accepted User Challenge `uc2a`): 采纳用户裁决 `rec`（`uc1a` + `uc2a` + `d1a–d5a` + `p1b`），将交付结构从单 PR 3 提交（`CEO-E5`）更新为 3 个严格顺序合并的 PR（PR 1 后端引擎/API/文档 → PR 2 全局外壳与统一控制台 → PR 3 创建/结果页与真实浏览器门禁），明确各 PR 跨边界兼容契约并重跑 Eng Cycle 2 复审 | User Challenge (Approved) | User Sovereignty | 用户在 Phase 4 Gate 回复 `rec` 正式拍板采纳三方建议的 3-PR 顺序交付，降低 27 文件变更的评审与回归爆炸半径 | 保留单 PR 3 提交 |
| 47 | Eng Cycle 2 | ENG2-H1: `child.stdout` 拆独立 daemon `log_reader` 线程、`stop_child` 核验清理后才做 `report.md`/`record_assembly` 与 `RunOutcome` 不可变结算（不阻塞 `event.set()`），`WindowsJob.wait_for_exit` 补异常保护、`stop_posix_group` 两处 PID 守卫且 `PermissionError` 视作清理失败，POSIX `worker_locked()` 改 `"rb"` 只读并共享统一归属谓词覆盖重启后孤儿进程 | Mechanical | P1 Completeness | 双声同时抓出泄漏 FD 永久卡 `stopping`、只读归档目录 `a+b` 锁死全工作台、重启后孤儿丧失 Colab 停机保护与全量快照阻塞唤醒 4 项高危并发 bug | 主线程读 stdout、全量历史 `a+b` 探测、锁内生成完整 snapshot |
| 48 | Eng Cycle 2 | ENG2-H2: `stage_generate`/`stage_prompts`/`_execute_rewrite_targets` 忽略 `CancelledError`/`DispatchAbortedError` 且真实异常高于 `JobPaused`；`stage_frames` 持锁快照 `frames_snapshot` + `scheduled_keys` 防双派发保构图连续；暂停准入深入 `rewrite._llm()`/`http.py` 内部重试与 Windows `control.json` 3×20ms 重试；`jobs.py:911` 补 `"frames"` 拦截、统一 `#resume-job` 禁用规则并强制 `intent="retrieve_only"` 后端 `drain_only=True` | Mechanical | P1 Completeness | 双声验证 `CancelledError` 会把多段暂停全变 `failed`、两遍扫描存在双派发竞态、`CEO-H3` 与 `DES-H1` 禁用逻辑直接打架且 `/resume` 缺少无新增付费保证 | `first_error` 无差别覆盖或仅靠前端文案承诺不新派发 |
| 49 | Eng Cycle 2 | ENG2-H3: `/logs` `generation` 移除会随写入变化的 POSIX `st_ctime_ns` 与 `prefix_4k_sha256`，改为严格追加不变的 `st_ino:first_complete_line_sha256[:16]` + `O_NOFOLLOW` 符号链接防穿越；`limit` 下限 `4096` 与严格单调游标（超长单行整行消费输出截断提示、零重复拼接零密钥泄露）；统一 `append_job_log()` 同步告警计数；Colab 改 `4096B` 定长分块读限 `64KB` 行长 | Mechanical | P1+P5 | 双声同时证实 `st_ctime_ns` 与 `prefix_4k_sha256` 会让每次写入都触发 `reset:true` 彻底击毁增量日志，`limit=1` 会卡死多字节字符 | 在 `generation` 中包含 `st_ctime_ns` 或 `prefix_4k` |
| 50 | Eng Cycle 2 | ENG2-H4: `demo.py` 与尾帧暂存迁入同分区私有 `inputs/.staging/` 保留合法扩展名并原子替换、`jobs.py:214`/`segments.py:1082` 过滤 `.` 点文件；校验 `state.json`/`request.json` 容器类型防 `AttributeError`、`unsettled_tasks()` 补 `frame-edit.json` `downloaded=True` 与 `revision`/`number` 校验、10s TTL 缓存失效、放宽局部重跑 `rewrite` 判定；明确 `prefill` `3600.4` 归零算法与 `open-folder` Linux 无管道 FD + 扩展 macOS 拒绝后缀 + 拒 Windows UNC | Mechanical | P1+P5 | 双声验证裸 `.tmp` 会让 PIL/FFmpeg 报错且点文件会落入 `Path.glob("*")` 公开下载，Windows UNC 与未校验状态类型存在安全/崩溃风险 | 在 `frames/`/`clips/` 内直接生成 `.tmp` 文件 |
| 51 | Eng Cycle 2 | ENG2-H5: 锁定唯一权威 `POST /stop` 响应与幂等回放表；`scripts/prepare_downgrade.py` 持有单实例锁 `workbench/instance.py:7`、拒绝旧版无法恢复的无 `error` 在途分镜、纳入 `scripts/package_windows.py` 打包清单并补 stopped-film / Windows ZIP 回归 | Mechanical | P1+P5 | 消除跨段落 `200`/`202`/`400` 契约矛盾、防止降级工具与空闲服务竞态并确保 Windows 发布包内实际包含该工具 | 无锁转换或遗漏 Windows 打包清单 |
| 52 | Eng Cycle 2 | ENG2-H6: 补齐 `UC2A` 3-PR 顺序合并的中间态 `main` 兼容契约（PR 1 带 ~25 行旧 UI 状态/轮询/i18n 适配；PR 2 独立壳层 DOM ID、共享 `state.jobs` 同步、一次性迁完 Results 双拉取为 `?include_logs=0` + Console Tab 1、Clone 按钮留 PR 3、同步导航文档并设渲染模板零重 ID 门禁；PR 2/3 补 `restoreCreateDraft` 调 `refreshFilmReferences`、`QuotaExceededError` 安全存储封装与 Files-only 拖放） | Mechanical | P1+P5 | 双声抓出 PR 1 合并后 API 暂停导致旧 UI 停轮询/漏文案、PR 2 合并后首页重 ID/结果页历史为空/Console Tab 1 空白三大中间态断层 | 假定 PR 1/2 完全不碰旧前端即可安全独立合入 `main` |
### Phase 1 Step 0 — Ledger, Audit & Reality Rows

#### Pre-Review System Audit & Taste Calibration
- **Git / In-flight state**: Branch `main` at `1366481` ("Upload media without curl and sign in to Colab from the workbench (#5)"), clean working tree except untracked `docs/plans/2026-11-26-workbench-ux-overhaul.md` and `docs/design/workbench-ux-prototype.html`; zero stashes; `TODOS.md` has zero open items.
- **Retrospective / Recurring risk areas**: Recent 30-day churn is heavily concentrated in `workbench/server.py` (40), `h3ref/film.py` (40), `workbench/jobs.py` (31), `workbench/segments.py` (29), `h3ref/colab_runtime.py` (24), `tests/test_workbench_ui.js` (23). Prior learnings directly applicable:
  - Prior learning applied: `linux-killpg-magicmock-pid-1-kills-ci-runner` (confidence 10, from 2026-11-27) → mandatory PID guard in `workbench/jobs.py:stop_posix_group`.
  - Prior learning applied: `ab-reservation-actual-submit-boundary` (confidence 10, from 2026-11-26) → pause checkpoints must fire BEFORE `submission_unknown=True` is written so pausing never locks resume.
  - Prior learning applied: `preview-reconcile-pure-in-memory` (confidence 10, from 2026-11-17) → `resume`/`continue_film` preflight reconciliation must remain side-effect-free on disk until commit.
  - Prior learning applied: `python-310-pep701-fstring-ast-gate` (confidence 10, from 2026-11-16) → new Python code in `h3ref/control.py` and `workbench/` must remain Python 3.10 AST-clean.
- **Taste calibration**:
  - *Good references*: `workbench/processes.py` (clean isolated OS boundary with explicit locking and timeout), `workbench/static/pagination.js` (small focused zero-build module with VM-safe DOM handling), `workbench/references.py` (single source of truth for stripping local paths).
  - *Patterns to avoid*: `workbench/jobs.py:_snapshot` (190-line monolithic method with inline status literals) and `workbench/static/common.js:renderRuntimeStatus` (175-line god-function mixing primary/secondary log streams).

#### Step 0 Decision Ledger

| ID and owner | Contract and evidence | Current | Proposed | Status | Exact approval and scope |
|---|---|---|---|---|---|
| CEO-H1 (Step 0B/Sec 2) | `h3ref/frames.py:77-79` `task_pending` misses hard-stopped in-flight APIMart frames without `error` | Requires `record.get("error")` | Check `bool(record.get("error")) or not record.get("file")` | approved | Auto-decided (P1, Mechanical): fix `h3ref/frames.py` + regression in `tests/test_job_pause_stop.py` |
| CEO-H2 (Step 0B/Sec 1-2) | `film.py:648`, `segments.py:1020,1303`, `storyboard.py:255`, `jobs.py:125,433,1017-1098` broad catches, queued race, active-status & killpg PID guard | Unaware of `JobPaused`/`pausing`/`stopping`; unguarded `os.killpg` | Explicit `JobPaused` passthrough, pre-spawn status check, `ACTIVE_STATUSES`/`RESUMABLE_STATUSES`, PID guard | approved | Auto-decided (P1+P2, Mechanical): apply across Stage 1 backend files + tests |
| CEO-H3 (Step 0B/Sec 3,11) | Multi-page routing, `kind=="one"`, `frames=="none"`, cast grouping, hidden-tab validation, delete-current-job poll stop, CSP & VM DOM guards | Unspecified in draft plan | Explicit Stage 1–3 compatibility & security contracts | approved | Auto-decided (P1+P4+P5, Mechanical): apply across Stages 1–3 |
| CEO-E1 (Step 0G) | `h3ref/colab_runtime.py:1007,1016,1222-1223` caps returned logs at 15 lines | Buffer 80 / return 15 | Buffer 200 / return 200 per slot | approved | Auto-decided (P1+P2, Mechanical): 1 file + unit test |
| CEO-E2 (Step 0G) | `workbench/jobs.py:424-428` `summaries()` polled every 1.8s | Re-reads metadata dynamically | Persist `title`+settled summary + mtime cache | approved | Auto-decided (P1+P3, Mechanical): in `workbench/jobs.py` + unit test |
| CEO-E3 (Step 0G) | Console height/collapse/tab resets across `/` ↔ `/jobs/<id>` | No persistence or shortcuts | `Esc` + `⌘/Ctrl+J` + `localStorage` console prefs | approved | Auto-decided (P2, Mechanical): in `workbench/static/common.js` + UI test |
| CEO-E4 / d1 (Step 0G) | `POST /api/jobs/<id>/stop` sync vs async HTTP return | Unspecified wait semantics | A) 1.5s bounded wait then async `stopping` (rec) vs B) immediate `stopping` | approved (provisional taste) | Auto-decided provisional Taste (`d1`, P1+P3); surfaced at Phase 4 Gate |
| CEO-E5 / d2 (Step 0G) | PR split vs 3 verified commits in 1 PR | 3 implementation stages | A) 3 commits in 1 PR (`pr_count=1`, rec) vs B) Stage 1 separate PR | approved (provisional taste) | Auto-decided provisional Taste (`d2`, P1+P6); surfaced at Phase 4 Gate |
| CEO-E6 (Step 0G) | Real-time SSE/WebSocket log stream | HTTP polling (1.4s/2.5s) | Defer to `TODOS.md` | deferred | Auto-decided (P3+P5): defer to `TODOS.md` |
| CEO-E7/E8 (Step 0G) | Multi-job queue & output-folder deletion | Explicitly excluded in Sec 1.2 | Skip / keep in NOT in scope | declined | User Sovereignty: stated non-goals |

#### Step 0A–0C Premise Table & Reality Rows

| Premise | Claim | Receipt | Verdict |
|---|---|---|---|
| P1 | Colab primary/secondary logs overwrite each other in UI | `workbench/static/common.js:416-421` | verified |
| P2 | Job logs collapse at bottom of results page with no search/filter | `workbench/templates/results.html:75`, `workbench/static/results.js:365-370` | verified |
| P3 | Top of every page stacks intro + runtime card + config details | `workbench/templates/base.html:18-103` | verified |
| P4 | Final video sits below segment cards & pagination | `workbench/templates/results.html:66-70` | verified |
| P5 | No pause/stop endpoint exists for running jobs | `workbench/server.py:740-889`, `workbench/jobs.py:1007-1098` | verified |
| P6 | History list shows only 8-char hex ID without title | `workbench/static/create.js:842` | verified |
| P7 | Existing JS test suite is ~2500+ lines referencing core DOM IDs | `tests/test_workbench_ui.js:1-2490` (total 3531 lines across 4 JS test files) | verified |
| P8 | `frames.task_pending` safely protects hard-stopped frames | `h3ref/frames.py:77-79` (requires `error` truthy — FALSE on hard stop!) | finding (fixed by CEO-H1) |
| P9 | `colab_runtime` returns enough log history for console search | `h3ref/colab_runtime.py:1222-1223` (truncated to `[-15:]`) | finding (fixed by CEO-E1) |

- Premise verification count: `unverified=0`

REALITY: premise-table finding 9 premises checked with 0 unverified; 2 codebase gaps surfaced (frames.task_pending requires error and colab_runtime caps logs to 15 lines) h3ref/frames.py:77-79
REALITY: already-done pass Checked git log origin/main since 2026-11-26; no pause/stop or UX overhaul commits landed yet workbench/jobs.py:1
REALITY: surface-check pass Verified existing routes (/api/bootstrap, /api/config, /api/runtime/*, /api/jobs/*) and CLI entrypoints (h3ref.py one/film/preflight/rewrite/account) match codebase workbench/server.py:350-936
REALITY: binding-decisions pass Checked gstack-decision-search (empty) and plan Section 2 confirmed decisions; all 4 non-goals preserved docs/plans/2026-11-26-workbench-ux-overhaul.md:30-48
REALITY: numbers pass Verified measured counts: test_workbench_ui.js has 2490 lines (tests/test_workbench_ui.js:2490), test_results.js has 393 lines (tests/test_results.js:393), test_video_segment_prompts.js has 509 lines (tests/test_video_segment_prompts.js:509), test_frame_character_picker.js has 139 lines (tests/test_frame_character_picker.js:139), colab_runtime keeps 80 log lines (h3ref/colab_runtime.py:1016) and returns 15 lines (h3ref/colab_runtime.py:1222), results polling uses 1400ms (workbench/static/results.js:381) and runtime polling uses 2500ms (workbench/static/common.js:147)
REALITY: deferred-asks pass No linked external issue exists; all user-confirmed items in plan Section 2 and prototype are retained docs/plans/2026-11-26-workbench-ux-overhaul.md:38-48
REALITY: pr-count pass No .gstack/ship-policy.json present; default pr_count=1 with 3 sequential verified commits (taste option d2 to split Stage 1) docs/plans/2026-11-26-workbench-ux-overhaul.md:165-192

#### Dream State Delta (0C)
```
CURRENT STATE                          THIS PLAN (Stages 1-3 + CEO Step 0)            12-MONTH IDEAL
-------------------------------------  ---------------------------------------------  ----------------------------------------
- Scattered actions across 4 screens   - Fixed 54px topbar + sticky command bar /     - Multi-project workspace & templates
- Mixed/hidden logs (15-line Colab)      right sticky launchpad + pinned final video  - Optional SSE streaming & cost budgets
- No pause/stop without killing Flask  - Unified 3-tab resizable bottom console       - Shot-level visual diff & version tree
- Opaque 8-char job IDs, manual refs     (200-line Colab logs, search, err filter)    - Background job queue (opt-in)
- Hard-stop frame task_pending gap     - Safe Pause + Stop preserving task_id &       - Cross-device sync / remote worker view
                                         fixing frames.task_pending + killpg guard
                                       - Titled history, quick switch, prefill clone,
                                         drag-drop filename auto-match, safe delete
```

#### Step 0H Spec Review Loop Outcome
- **Iterations**: 3 (cap reached) | **Scores**: Iter 1 = 6/10 (25 issues) → Iter 2 = 7/10 (17 issues; Scope PASS) → Iter 3 = 8/10 (8 issues; Scope PASS).
- **Status**: `DONE_WITH_CONCERNS` (`spec not re-verified` after 3-launch cap; `issues verified: 8 of 8` applied directly to `ACTIVE_PLAN` and `CEO_SUMMARY_PATH` and verified in full readback of `autoplan-ceo-AqWKCg/ceo-implementation.md`). Metrics persisted to `~/.gstack/analytics/spec-review.jsonl`.
- **Auto-decision (0H Document Approval)**: `A) Approve these documents and continue to 0I` (autonomous `/autoplan` pipeline; all 8 remaining cap items are verified in plan text and immediately reviewed by Step 0.5 Dual Voices + Phase 3 Eng Review).

#### Step 0I Temporal Interrogation (Implementation Sequencing & Effort)
- **HOUR 1 (Foundations & Invariants)**: Implement `h3ref/control.py` (`EXIT_CODE_PAUSED = 75`, `JobPaused`, `should_pause`, `check_pause`), fix `h3ref/frames.py:task_pending()`, widen `h3ref/colab_runtime.py` (`MAX_SLOT_LOGS = 200` across `_append_log`, `_logs_tail`, `status()`), and add `stop_posix_group` PID guard + `terminate()`/`kill()` fallback in `workbench/jobs.py`. *Key invariant*: `h3ref/` must remain zero-import from `workbench/`, and `python3 -m py_compile` must pass Python 3.10 AST.
- **HOUR 2-3 (Core Pause/Stop Engine & APIs)**: Wire pause checkpoints + `JobPaused` passthrough across `h3ref/film.py` (`stage_frames` inner `draw()` guard + nested-loop `paused` flag, `stage_prompts`, `stage_generate` + `control_dir`), `h3ref.py`, `workbench/storyboard.py`, `workbench/segments.py`, `workbench/demo.py`; migrate all 6 `ACTIVE_STATUSES` + 11 `RESUMABLE`/`SETTLED`/stale-PID sites in `workbench/jobs.py`; implement `Jobs.create()` `operation_id`, `Jobs.launch()` `_run_events`/`cleanup_failed`, `Jobs.pause`/`Jobs.stop` (1.5s bounded wait + blocked-stdout recovery), `DELETE`/`open-folder`/`prefill`/`active_tasks`/`cost`/`title` + `_summary_cache_lock`, `workbench/server.py` routes, locale files, and `tests/test_job_pause_stop.py` + `tests/test_colab_runtime.py`.
- **HOUR 4-5 (Workbench Shell & Unified Bottom Console — Stage 2)**: Refactor `workbench/templates/base.html`, `workbench/static/style.css` (preserving 44px `input`/`select`, `.header-tools select` `height: auto`, `max-width:68ch`, console CSS specificity rules), and `workbench/static/common.js` (54px topbar, History & Colab Popovers, 3-tab Settings Drawer with capture-phase `invalid` tab switch, 3-tab resizable bottom console with `localStorage` & `Esc`/`⌘/Ctrl+J`, Toast synced with `#global-error`/`#global-message`, Node VM guards). Run full `tests/test_workbench_ui.js` + `tests/test_locale_parity.py`.
- **HOUR 6+ (Create & Results Overhaul — Stage 3)**: Refactor `index.html`, `results.html`, `create.js` (JSON format/summary bar, `h3.createDraft.v1` post-`langPromise` restore, `<=9` files & `<=28 MiB` chunked upload, cast-group-aware auto-match + `#upload-conflict-tray`, sticky right launchpad), `results.js` (6-state Sticky Command Bar with State 5 `can_assemble && pending_assembly` + dedicated Pause/Stop helper + Stop modal, pinned collapsible final video, `!running(job)` gate for `frameTaskPending`, storyboard filter + list-thumbnail click-to-carousel), and `video-segments.js` (3-tab `selectResultTab`/`renderResultTabs` covering `film`/`frames=="none"`/`kind=="one"`). Run full Python + JS test suite.
- **Effort Estimate**: Human ~4–5 days across 3 stages; CC ~2.5–3.5 hours total across 3 verified commits in 1 PR (`CEO-E5`/`d2`).


### Phase 1 Step 0.5 — Dual Voices & Consensus

#### Native CEO Voice (`agy` subagent `189c79cc-7044-4123-8cfc-d82c2fa2b765`, 343s)
- **Verified input**: `INPUT: ceo 3bbf356b9c32c7c8386263a204d2491af99ef43d42170221a6b7b02c1ef21808` (exact match).
- **7 findings** (2 Critical, 3 High, 2 Medium):
  1. [CRITICAL] Local hard stop without cancelling ComfyUI leaves 3-6 min ghost GPU jobs blocking Task B while injecting ~60% of the state-machine complexity -> Pause-first v1 or provider-aware ComfyUI `/interrupt` option.
  2. [CRITICAL] Framing around IDE shell / 230px docked console misses 10x creator leverage (Chinese story -> `film.json` wizard, FIFO queue / pool decoupling, multi-take comparison).
  3. [HIGH] Global-scope Vanilla JS + dual DOM mirroring (`#global-error`, `#runtime-logs`) risks 6-month maintenance drag vs ES Modules / CSS `@layer` + clean test updates.
  4. [HIGH] Single Mega-PR (`pr_count=1`) bundles 100% backward-compatible Stage 1 backend + `CEO-H1` safety fix with large UI rewrite -> split into 3 independent PRs.
  5. [HIGH] Hiding Config & Colab in topbar hurts cold-start Windows users and splits Colab auth/logs diagonally; 230px docked console eats 35-45% of 13-14" laptop viewport -> add cold-start banner, inline 3-5 line Colab preview, collapse console by default.
  6. [MEDIUM] Competitive moat is cost/Colab orchestration + director consistency + local sovereignty, not bespoke UI chrome -> keep `h3ref/` decoupled via lightweight `should_stop` callback hook.
  7. [MEDIUM] 1800ms full-snapshot polling on `/` re-reads/redacts 100KB logs & breaks selection; history delete hides orphaned 200-500MB folders -> add `GET /api/jobs/<id>/logs?offset=` + show folder path/open shortcut in delete modal.

#### Codex CEO Voice (`gpt-6.2-astra`, `OUTSIDE_STATUS: completed provider=codex host=agy`, 177s)
```tool-output
**1. Severity: Critical｜“立即停止”可能把一笔已受理的付费任务留在无人跟进的状态。** 供应商受理请求和本地保存 `task_id` 是两个不同的时刻。强停落在两者之间时，续跑可能只看到 `submission_unknown`。即使 ID 已保存，方案也允许用户停止后删除历史登记，而工作台的续跑入口依赖这条登记。输出目录还在，用户却可能失去从界面取回结果的入口。计划里的“保留已获得的 ID”只覆盖了其中一种情况。[提交与落盘代码](/Users/qiaoyu/projects/h3-ref-video/h3ref/film.py:815) · [续跑入口](/Users/qiaoyu/projects/h3-ref-video/workbench/jobs.py:861)
Recommendation: 把“停止后仍能找回每笔未结任务”设为强停上线门槛，提供只取回已提交结果的操作，对无回执任务给出核对路径，未结清前保留可恢复的历史入口，because 用户有充分理由停止后续消费，同时拿回已经付费的素材。

**2. Severity: High｜计划没有证明哪类用户最值得优先服务。** 首次使用者要导入剧本 JSON、绑定参考图、选择演示或真实运行；熟练操作者关心双节点、并发进度和日志。两者的瓶颈完全可能不同。原型确认和单测全绿，都回答不了一个决定投入的问题：今天究竟有多少人卡在第一条可用片段之前，又有多少人每天因找日志而损失时间？[现有创建流程](/Users/qiaoyu/projects/h3-ref-video/workbench/templates/index.html:27)
Recommendation: 实施前分别观察首次用户和重复用户完成一项真实任务，记录首次可用片段用时、放弃位置和人工介入次数，再选一类用户作为首发对象，because 两类人需要的第一项改动很可能不同，整套上线会让团队失去识别主因的机会。

**3. Severity: High｜最值得验证的替代方案是缩短一次付费创作决策。** 仓库已经能单段重做并替换成片中的片段，计划却主要优化如何看到任务、日志和最终播放器。创作者也可能更愿意先拿最难的一段验证人物一致性和镜头风格，再决定是否为整片付费。竞争上真正需要检验的是，用户面对同一个剧本，会选择这套工作台、直接在生成平台逐段尝试，还是沿用现有剪辑流程。计划没有给出选择本产品的可测理由。[单段生成与替换](/Users/qiaoyu/projects/h3-ref-video/workbench/segments.py:961)
Recommendation: 用同一份真实剧本对照试验“先生成一段，认可后再扩展”和现有整片流程，同时让创作者用其惯用工具完成同一任务，比较得到首个认可镜头的时间、花费与返工次数，because 这能检验产品是否有足以改变工具选择的优势。

**4. Severity: High｜“总费用”会系统性低估反复创作的代价。** 当前报告汇总有效帧和片段，单段重做成功会替换活动记录；ComfyUI 的结果还直接返回 `cny: 0.0`。用户先后付费生成三版、最终保留一版时，按有效状态求和只能反映保留版本的部分费用。若再把它持久化到历史列表，用户会据此低估下一部片的预算。[当前费用汇总](/Users/qiaoyu/projects/h3-ref-video/h3ref/film.py:1276) · [版本替换](/Users/qiaoyu/projects/h3-ref-video/workbench/segments.py:1009) · [ComfyUI 金额来源](/Users/qiaoyu/projects/h3-ref-video/h3ref/comfy.py:748)
Recommendation: 首版把数字明确标为“当前采用素材的已知费用”，把未计价算力单列；若要支持预算决策，再按生成尝试累计，并用同一段重做两次的账单验收，because 创作者要为被丢弃的版本和实际使用的算力一起买单。

**5. Severity: High｜方案假定“想开始下一项工作”就需要暂停当前任务。** 当前工作台在创建任务时检查全局可用性。计划保留单任务执行，又把强停、释放锁、切换任务当作改善吞吐量的主要路径。但如果用户只是希望在 A 渲染时准备 B，强停会额外制造远端在途任务和恢复工作；`sessionStorage` 中的一份草稿也无法表达多个待办项目。[任务创建与互斥检查](/Users/qiaoyu/projects/h3-ref-video/workbench/jobs.py:555)
Recommendation: 在决定强停按钮的优先级前，让高频用户用“保存下一份待运行项目，稍后手动启动”的原型完成一次任务切换，记录他们是否仍需要中断 A，because 准备多个项目可以与单任务执行并存，也能检验复杂的抢占流程是否解决了真实需求。

**6. Severity: Medium｜“复刻新建”会在重复创作最费力的一步丢失上下文。** 计划复制剧本和参数，却从预填结果剥离本地素材路径，草稿也排除文件绑定。现有代码明确要求检查角色、合图及场景的素材绑定。对固定角色的系列创作者，打开上一集后还要重新选图、匹配、核对，按钮节省的可能只有复制 JSON 的几秒钟。[参考图绑定与必填检查](/Users/qiaoyu/projects/h3-ref-video/workbench/references.py:98)
Recommendation: 用“沿用上一集角色和场景，只改一段剧情”验收复刻，在服务端复用仍可读取的素材绑定，仅提示补齐失效项，because 连续创作的收益取决于能否保留已经核对过的资产关系。

**7. Severity: Medium｜三个提交进一个 PR，留下了代码检查点，却没有留下产品取舍点。** 测试可以证明暂停状态、日志抽屉和拖放匹配各自按设计工作。三阶段一起交付后，即使用户更快出片，也很难判断贡献来自哪个改动；如果没有改善，团队仍需长期维护所有控件、快捷键、双语文案和兼容契约。六个月后最难撤回的是这些已形成使用习惯的交互承诺。
Recommendation: 先独立交付一个可验收的用户结果，例如“中断后能找回已付费片段”，下一批功能以实测瓶颈决定是否进入，而非以三个阶段是否写完决定发布，because 分批验证才能让团队在收益不足时真正删减范围。

VERDICT: FINDINGS
OUTSIDE_STATUS: completed provider=codex host=agy
```

#### CEO DUAL VOICES — CONSENSUS TABLE
```
CEO DUAL VOICES — CONSENSUS TABLE:
  Dimension                            agy (in-host)                           Codex (gpt-6.2-astra)                      Consensus
  1. Premises valid?                   Challenged (no-cancel ComfyUI Stop;     Challenged (Stop submission_unknown +       CONFIRMED (uc1 + CEO-H4/H5/H6)
                                       Colab/Config cold-start & viewport)     DELETE orphans handle; cost understatement)
  2. Right problem to solve?           Challenged (IDE console vs 10x wizard/  Challenged (unverified primary persona;    CONFIRMED (CEO-H6 collapse;
                                       FIFO/multi-take leverage)               single-segment pilot tests value faster)   CEO-E10..E12 deferred)
  3. Scope calibration correct?        Challenged (split Mega-PR into 3 PRs;   Challenged (1 PR drops validation gates;   CONFIRMED (uc2 PR split +
                                       consider Pause-first v1)                deliver independent user outcomes)         uc1 Pause-first option)
  4. Alternatives sufficiently explored? Challenged (ES Modules, clean tests,  Challenged (save-next-job prep, server-    CONFIRMED (CEO-E9 accepted;
                                       incremental /logs?offset= endpoint)     side ref binding reuse in /prefill)        d2 & d3 -> Phase 4 Gate)
  5. Competitive/market risks covered? Challenged (cannot out-UI cloud apps;   Challenged (prove advantage vs direct      CONFIRMED (should_stop hook;
                                       keep h3ref/ lean & model-agnostic)      generation platforms on first good clip)   10x pilot/wizard in TODOS)
  6. 6-month trajectory sound?         Challenged (dual DOM debt, 1.8s 100KB   Challenged (hard-to-revoke UI promises;    CONFIRMED (CEO-H4/H5/E9 +
                                       polling, silent deleted disk folders)   understated cost misleads budgeting)       d3 close 6-month risks)
```

---

### Phase 1 Sections 1-11 (Full-Depth Review)

#### Section 1: System Architecture & Diagrams

```
SYSTEM ARCHITECTURE
===================
[Browser (Zero-Build Jinja2 + Vanilla JS + Local Plyr, CSP: script-src 'self')]
  |-- 54px Fixed Topbar (Brand, History Popover, Colab Popover w/ 3-5 line preview, Settings Drawer, Lang)
  |-- Auto-hiding Cold-Start Setup Banner (when required keys/provider missing)
  |-- Page A: Create (/)               |-- Page B: Results (/jobs/<id>)
  |   - Left: Script JSON + summary    |   - Sticky 6-State Command Bar (Pause / Stop modal / Resume / Confirm / Assemble)
  |   - Cast Refs (<=9 & <=28MiB drop) |   - Pinned Collapsible Final Video Banner
  |   - Right Sticky Launchpad         |   - Multi-Node Active Tasks Bar + 3 Tabs (Storyboard|Script / Segments / Report)
  |-- Bottom Unified Resizable Console (Collapsed 32-36px default; Job Log | Colab Primary | Colab Secondary)
          |
          | HTTP JSON Polling: /api/bootstrap, /api/jobs/<id>, /api/jobs/<id>/logs?offset=, /api/runtime/status
          v
[Flask Local Server (workbench/server.py, 127.0.0.1)]
  |-- Jobs Manager (workbench/jobs.py)
  |   |-- self.lock (state transitions, _run_events[id]=(op_id, Event), shared cleanup_failed)
  |   |-- self._summary_cache_lock (lock-free disk stat/json summaries cache)
  |   |-- Background stop_child thread (POSIX stop_posix_group w/ PID guard / Windows JobObject)
  |-- Colab Runtime Manager (h3ref/colab_runtime.py, MAX_SLOT_LOGS=200 across buffer/_logs_tail/status)
          |
          | Subprocess (worker.lock + <output_dir>/inputs/control.json)
          v
[Worker Process (h3ref.py / workbench/{storyboard,segments,demo}.py)]
  |-- h3ref/control.py (EXIT_CODE_PAUSED=75, JobPaused, should_pause, check_pause, should_stop hook)
  |-- h3ref/frames.py (task_pending fixed for errorless in-flight APIMart tasks)
  |-- h3ref/film.py (stage_frames draw() guard + nested paused flag, stage_prompts, stage_generate w/ control_dir)
  `-- Remote Providers (APIMart / RunningHub / ComfyUI) -- NEVER remotely cancelled (unless uc1 Option C chosen)
```

```
PAUSE / STOP STATE MACHINE & SETTLEMENT PRECEDENCE
==================================================
                  +--- POST /pause (queued|running) ---> [pausing] ---(worker drains & exits 75)---> [paused]
                  |      (pausing: idempotent 200)          |                                           |
 [queued] --> [running]                                 POST /stop                                 POST /resume
    |             |                                         v                                      (unlink control.json)
    |             +--- POST /stop (ACTIVE_STATUSES) ---> [stopping] --(bg stop_child <=1.5s fast)--> [stopped]
    |                    (stopping: dedup 200)              |                                           |
    +-- pre-spawn pause/stop (in self.lock) --------+       +--(slow >1.5s: returns stopping)-----------+
                                                    |       +--(TimeoutError cleanup_failed)-------> [failed (keep pid)]
                                                    v
                              clean paused / stopped (no Popen, no fake error)

Settlement Precedence in Jobs.run finally (after stop_child finishes):
  1. self.closing         -> interrupted (preserve server shutdown contract)
  2. cleanup_failed==True -> failed + localized cleanup error (keep pid while worker_locked; never overwrite!)
  3. status=="stopping"   -> stopped, error=None (checked BEFORE code==0 report/assemble mutations)
  4. code==EXIT_CODE_PAUSED(75) -> paused, error=None
  5. code==0              -> run post-exit report/dirty_segments/assemble mutations -> succeeded
  6. code!=0              -> failed + error message
```

```
DATA FLOW & SHADOW PATHS
========================
1. Create -> Prefill/Draft -> Upload -> Launch:
   [GET /prefill] --(whitelist + describe_references, strip local paths)--> Create Form (overrides h3.createDraft.v1)
   [Drop N files] --(chunk <=9 files & <=28 MiB; single >28MiB fast fail)--> POST /api/uploads
      |-- exact stem / alias / cast-group match (slot empty) --> auto-bind slot
      `-- ambiguous / duplicate / occupied / unmatched       --> #upload-conflict-tray (Assign / Replace / Dismiss)
2. Active Log Console on / (shadow path: rotated/shrunk log):
   [Expanded + Job Log tab] --(1800ms)--> GET /api/jobs/<id>/logs?offset=N
      |-- file_size >= N --> append redacted delta (preserves text selection)
      `-- file_size < N  --> truncated=true, replace with redacted tail
3. Delete History Record (shadow path: unsettled paid task):
   [DELETE /api/jobs/<id>] --> check lock + parent containment + !ACTIVE + !worker_locked + !unsettled task_pending/submission_unknown
      |-- unsettled paid task exists --> 400 (instruct user to Resume & settle first)
      `-- safe & settled             --> delete <id>.{json,log} ONLY, evict records & summary cache, 200 JSON (output_dir untouched)
```

#### Section 2: Error & Rescue Registry

| Failure Mode / Codepath | Trigger / Exception | Rescued? | User-Facing Behavior | Recovery Path |
|---|---|---|---|---|
| Hard-stop mid-poll APIMart frame (`h3ref/frames.py:77`) | `task` saved, `file=None`, `error=None` | Yes (`CEO-H1`) | Card shows `frameTaskPending` notice when `!running(job)`, frame edit locked | Click Resume to poll existing `task_id` without double billing |
| Pause during `stage_frames` `ThreadPoolExecutor(4)` | Queued futures 5..N after Pause | Yes (`CEO-H2`) | Queued unstarted shots return early in `draw()` without error; in-flight 1-4 finish; job enters `paused` | Click Resume to draw remaining shots |
| `JobPaused` inside broad `except` (6 handlers) | `JobPaused(Exception)` raised mid-stage | Yes (`CEO-H2`) | Explicitly re-raised before `except Exception`/`BaseException`; no fake `error` written | Clean `EXIT_CODE_PAUSED = 75` -> `paused` |
| `stop_child` `TimeoutError` + blocked `child.stdout` | Unkillable child holds stdout pipe | Yes (`CEO-H2`/`E4`) | Bg thread acquires `self.lock`, marks `cleanup_failed=True`, `status="failed"`, saves, sets `Event` | UI shows cleanup error; `available()` blocks new launch until lock clears |
| `os.killpg` invalid/mock/own-group PID | `pid <= 1`, `bool`, `MagicMock`, `== os.getpgrp()` | Yes (`CEO-H2`) | Guard skips `os.killpg`, falls back to `child.terminate()`/`kill()` | Prevents killing Flask server or CI runner |
| `DELETE` job with unsettled `task_pending` / `submission_unknown` | User clicks Delete after Stop | Yes (`CEO-H4`) | HTTP 400 with localized error; record preserved | User resumes/reconciles paid task before deleting |
| `POST /open-folder` missing dir or missing `xdg-open` | Deleted folder or headless Linux | Yes (`CEO-H3`) | HTTP 400 localized Toast; no 500 or zombie process | User inspects output path directly |
| `GET /prefill` missing/corrupt `inputs/request.json` | Manually edited/deleted output dir | Yes (`CEO-H3`) | HTTP 400/404 `InputError` Toast | User creates job fresh |
| Drag-drop >9 files or >28 MiB payload | Multi-sheet batch drop | Yes (`CEO-H3`) | Sequential `<=9` & `<=28 MiB` chunking; single file >28 MiB fails fast w/ Toast | All valid sheets upload without HTTP 400/413 |
| Hidden required input inside inactive Settings Drawer tab | User submits `#config-form` | Yes (`CEO-H3`) | Capture-phase `invalid` listener switches to owning tab + `reportValidity()` | User sees exact missing field |
| Server restart while worker alive (`worker_locked=True`) | Crash/restart during `running`/`stopping` | Yes (`CEO-H2`) | All `ACTIVE_STATUSES` -> `interrupted`, retaining `pid` until lock releases | No permanent zombie `ACTIVE_STATUSES` record |

#### Section 3: Security & Threat Model
- **Local-only bind**: Flask binds `127.0.0.1` only (unchanged).
- **CSP & XSS**: Strict `default-src 'self'; script-src 'self'` maintained; zero inline `onclick` attributes, zero untrusted `innerHTML` (all dynamic text rendered via `textContent` / `document.createElement`).
- **Filesystem & command injection boundaries**:
  - `POST /api/jobs/<id>/open-folder` accepts ZERO path inputs from the client, resolves registered `record["output_dir"]`, checks `is_dir()`, and invokes `["open", "--", str(out)]` (macOS argument terminator `--` prevents flag injection), `os.startfile` (Windows), or `Popen(["xdg-open", str(out)], start_new_session=True, ...)` (Linux after `shutil.which`).
  - `DELETE /api/jobs/<id>` validates job ID and checks `(storage / "jobs").resolve()` parent containment so traversal IDs (`../`) cannot delete arbitrary files; never touches `output_dir`.
  - `GET /api/jobs/<id>/prefill` uses an explicit whitelist + `workbench/references.py:describe_references()` to guarantee zero host filesystem paths leak into JSON responses.
  - `GET /api/jobs/<id>/logs?offset=` runs `redact()` on returned delta slices so API keys never leak to the browser console.

#### Section 4: Data Flow, Async Ordering & Interaction Edge Cases
- Verified all critical async races: (1) `queued -> pausing/stopping` pre-spawn cancellation registered via `Jobs.launch()` `_run_events` before `thread.start()`; (2) `operation_id`-scoped `_run_events` cleanup so a resumed run cannot have its event deleted by a stale run's `finally:`; (3) `self.save(record)` strictly before `run_event.set()`; (4) `operation == "frame"` preserving `inputs/frame-edit.json` on pre-submit pause while `can_resume_frames` excludes unapplied/pending `frame-edit.json`; (5) `operation == "segment"` passing `control_dir=out` instead of `staged.dir`; (6) `DELETE` on currently viewed job stopping poll timer and redirecting to `/`; (7) Pause/Stop using dedicated control-action helper instead of `runJobAction()`; (8) `restoreCreateDraft()` running after `await langPromise` when no `?from=` is present.

#### Section 5: Code Quality & Maintainability Review
- **Finding**: `workbench/jobs.py` is already 1,107 lines (with `_snapshot()` at ~190 lines) and `workbench/static/common.js` is 925 lines. Adding pause/stop + summaries cache + console + popovers inline would degrade readability.
- **Decision (Mechanical, P5)**: Keep pause/control protocol isolated in `h3ref/control.py` (with `should_stop` hook); decompose new `Jobs` helpers cleanly (`_settle_run`, `_derive_active_tasks`, `_job_summary_cached`, `_has_unsettled_paid_tasks`); keep `selectResultTab`/`renderResultTabs` in `video-segments.js`; keep all Python code Python 3.10 AST-clean (no PEP 701 nested f-string quotes).

#### Section 6: Test Review
- **Backend (`tests/test_job_pause_stop.py`, `tests/test_colab_runtime.py`, `tests/test_locale_parity.py`)**: Must cover (a) `frames.task_pending` errorless hard-stop regression (`CEO-H1`), (b) `stage_frames` queued futures pause via `draw()` guard + nested loop, (c) `storyboard.regenerate` pre-submit pause preserving `frame-edit.json` + Resume, (d) `stage_generate` blocker + post-drain `JobPaused` before `media.join` + `control_dir=out` for staged `operation=="segment"`, (e) `Jobs.run` pre-spawn `queued` cancel, settlement precedence (`stopping` before `code==0`), blocked-`stdout` `cleanup_failed`, duplicate `/stop`, `stopping` rejecting `/pause`, (f) `stop_posix_group` PID guard (`pid=1`, `MagicMock`, `own pgrp`), (g) `DELETE` blocking unsettled `task_pending`/`submission_unknown` + POSIX recycled-PID handling + preserving `output_dir`, (h) `open-folder` OS guards, `prefill` whitelist & missing `request.json`, `logs?offset=` incremental + truncation, `summaries()` `_summary_cache_lock` + `clip.mp4`, (i) `colab_runtime.py` `MAX_SLOT_LOGS=200` in `_append_log`, `_logs_tail`, and `status()`.
- **Frontend (`tests/test_workbench_ui.js`, `tests/test_results.js`, `tests/test_video_segment_prompts.js`, `tests/test_frame_character_picker.js`)**: Must cover all 6 Command Bar states (including State 5 `can_assemble && pending_assembly` vs State 6, State 3 disabled `#resume-job` + `#resume-hint`), `!running(job)` gate for `frameTaskPending`, Pause immediate + Stop confirmation modal, 3 tabs across `film`/`frames=="none"`/`kind=="one"`, storyboard filter + thumbnail click intercept, cold-start setup banner, Colab Popover inline preview, collapsed-by-default console + `Esc`/`⌘/Ctrl+J` + `localStorage`, hidden-tab `invalid` auto-switch, `<=9` & `<=28 MiB` chunked upload + cast-group auto-match + `#upload-conflict-tray`, `h3.createDraft.v1` vs `?from=`, and `#global-error`/`#global-message` sync.

#### Section 7: Performance Review
- Eliminated two severe polling bottlenecks: (1) `Jobs.summaries()` cold-cache disk I/O moved outside `self.lock` under `_summary_cache_lock` keyed by mtime/size (`CEO-E2`), preventing lock contention with `Jobs.run`; (2) Create-page active-job console polling switched from full 100KB `GET /api/jobs/<id>` to incremental `GET /api/jobs/<id>/logs?offset=` (`CEO-E9`), reducing per-tick I/O and redaction work by ~100x. Colab 200-line per-slot ring buffer (`CEO-E1`) uses <40KB RAM across 2 slots.

#### Section 8: Observability & Debuggability Review
- Examined logging, error surfaces, and live progress visibility: unified 3-tab bottom console (default collapsed with unread warning/error badge), Colab Popover 3-5 line inline preview (`CEO-H6`), localized `EXIT_CODE_PAUSED` worker log (`h3ref/messages`), explicit `cleanup_failed` error logging in background `stop_child`, and `active_tasks` exposing concurrent ComfyUI/RunningHub/frames/rewrite progress. No remaining observability gaps.

#### Section 9: Deployment & Rollout Review
- Examined zero-build packaging (`start-windows.cmd`, macOS/Linux local Python launch), startup recovery (`Jobs.__init__` transitioning all persisted `ACTIVE_STATUSES` safely across both locked and unlocked orphans), and backward compatibility of legacy `.workbench/jobs/*.json` (lazy read-only title fallback + missing-`output_dir` summary degradation). Delivery structure (1 PR vs 3 PRs) is surfaced cleanly to user via `uc2`.

#### Section 10: Long-Term Trajectory Review
- Examined 6-month evolution: isolating pause control behind `h3ref/control.py` + `should_stop` hook keeps `h3ref/` ready for future video backends; honest cost labeling (`CEO-H5`) prevents budget drift; Delete unsettled-task guard (`CEO-H4`) protects paid assets; high-leverage 10x product features (`CEO-E10` GUI script wizard, `CEO-E11` single-segment pilot & multi-take comparison, `CEO-E12` saved next-job queue / pool decoupling) are explicitly queued in `TODOS.md` without bloating this overhaul.

#### Section 11: Design & UX Review (UI scope = yes)
- Aligned layout with `DESIGN.md` ("quiet studio desk where frames and clips hold visual priority"): collapsing bottom console to 32-36px by default (`CEO-H6`) restores 85%+ viewport on 13-14" laptops; cold-start setup banner + Colab Popover 3-5 line preview close the onboarding and diagonal-eye-jump regressions; 6-state Sticky Command Bar, pinned collapsible final video, 3-tab visibility across `film`/`frames=="none"`/`kind=="one"`, and CSS specificity guardrails (`input.console-search`, `.cbtn`, `44px` input/select parity) are locked in; detailed visual/interaction/a11y critique continues in Phase 2 Design Review.

---

### Phase 1 Mandatory Outputs

#### NOT in scope
- **Remote task cancellation** (ComfyUI `/interrupt`, RunningHub cancel, APIMart cancel) -- excluded by Non-Goal #1 (unless user opts into Option C under `uc1` at Phase 4 Gate).
- **Multi-job concurrent execution** -- excluded by Non-Goal #2 (`CEO-E7`).
- **Storyboard prompt / workflow graph / billing semantics changes** -- excluded by Non-Goal #3.
- **Deleting output folders or paid assets on history delete** -- excluded by Non-Goal #4 (`CEO-E8`).
- **Deferred to `TODOS.md`**: `CEO-E6` (SSE/WebSocket streaming), `CEO-E10` (GUI Chinese-story -> `film.json` wizard), `CEO-E11` (single-segment pilot & multi-take comparison), `CEO-E12` (saved next-job queue / image-vs-video pool decoupling).

#### What already exists
| Sub-problem | Existing Codebase Module | How Plan Reuses It |
|---|---|---|
| Subprocess group / Windows JobObject termination | `workbench/jobs.py:110-146`, `workbench/processes.py` | Reused inside deduplicated background `stop_child` (adding PID guard + fallback) |
| Worker mutex file lock & live PID detection | `workbench/processes.py:worker_locked`, `process_alive` | Reused in `Jobs.__init__`, `Jobs.available()`, `DELETE /api/jobs/<id>` |
| Stripping local paths from reference metadata | `workbench/references.py:describe_references()` | Reused in `GET /api/jobs/<id>/prefill` (zero duplicate sanitizer) |
| ComfyUI endpoint short labels | `h3ref/comfy.py:endpoint_label()` | Reused in `workbench/segments.py:view()` for `node_label` |
| Paid-task lock & resume recovery | `h3ref/frames.py:task_pending`, `workbench/segments.py:view` | Hardened (`CEO-H1`, `CEO-H4`) without changing `state.json` schema |
| 2-tab result switching & pagination | `workbench/static/video-segments.js`, `pagination.js` | Extended in-place to 3 tabs (null-safe) & filtered storyboard pagination |
| Dual-node Colab runtime manager | `h3ref/colab_runtime.py` | Widened `MAX_SLOT_LOGS = 200` in-place across 3 sites |
| Locale parity enforcement | `tests/test_locale_parity.py` | Extended automatically across `h3ref/messages` & `workbench/{,static/}messages` |

#### Failure Modes Registry
| # | Failure Mode | Severity | Mitigation in Plan | Critical Gap? |
|---|---|---|---|---|
| 1 | Hard-stop mid-poll APIMart frame unlocks re-edit & double-bills | Critical | `CEO-H1` fixes `frames.task_pending` + `!running(job)` UI notice | None (mitigated) |
| 2 | Pause during `stage_frames` submits queued futures 5..N to APIMart | Critical | `CEO-H2` inner `draw()` `if not task and should_pause: return` + nested `paused` flag | None (mitigated) |
| 3 | `DELETE` on stopped job orphans UI resume handle for paid `task_id` | Critical | `CEO-H4` blocks `DELETE` (400) while any `task_pending`/`submission_unknown` exists | None (mitigated) |
| 4 | Unlinking `frame-edit.json` or overbroad `can_resume_frames` deletes paid `task_id` / crashes Resume | Critical | `CEO-H2` preserves `frame-edit.json`, gates `can_resume_frames` strictly | None (mitigated) |
| 5 | Unguarded `os.killpg` kills Flask server or Linux CI runner | Critical | `CEO-H2` strict integer PID guard (`> 1`, `!= os.getpgrp()`) + `terminate/kill` fallback | None (mitigated) |
| 6 | Broad `except` writes fake error on `JobPaused` | High | `CEO-H2` explicit `except JobPaused: raise` across all 6 handlers | None (mitigated) |
| 7 | `staged.dir` mismatch breaks pause in `operation == "segment"` | High | `CEO-H2` `control_dir=out` passed into `gen_segment`/`_generate_segment` | None (mitigated) |
| 8 | Blocked `child.stdout` hangs job in `stopping` forever after `TimeoutError` | High | `CEO-H2`/`E4` bg thread transitions `stopping -> failed` + `run_event.set()` | None (mitigated) |
| 9 | `can_assemble` staying `True` makes Command Bar State 6 unreachable | High | `CEO-H3` gates State 5 on `can_assemble && pending_assembly` | None (mitigated) |
| 10 | ComfyUI ghost GPU job after local Stop without `/interrupt` | Critical (Strategic) | Surfaced to user as Dual-Model User Challenge `uc1` at Phase 4 Gate | None (gated for user) |

#### Stale Diagram Audit
- Checked `README.md`, `DESIGN.md`, and `docs/plans/` -- no stale architecture or state-machine diagrams exist in the repository that conflict with this plan.

#### Phase 1 CEO Completion Summary
```
+======================================================================+
|                    CEO REVIEW — COMPLETION SUMMARY                   |
+======================================================================+
| Mode                  | SELECTIVE EXPANSION                          |
| Premises Checked      | 9 verified (0 unverified; 2 gaps fixed)      |
| Reality Check         | PLAN_REALITY: phase=ceo 7/7 verdict=complete |
| 0H Spec Review Loop   | 3 iterations (6/10 -> 7/10 -> 8/10, 8/8 cap  |
|                       | fixes verified; spec-review.jsonl recorded)  |
| Dual Voices           | Native (343s, 7 findings) + Codex (177s, 7   |
|                       | findings, P0) -- 6/6 dimensions CONFIRMED    |
| Accepted Additions    | CEO-H1..H6, CEO-B1, CEO-E1..E4, CEO-E9       |
| User Challenges       | uc1 (Stop scope), uc2 (1 PR vs 3 PRs)        |
| Taste Decisions       | d1 (1.5s Stop wait), d2 (prefill refs),      |
|                       | d3 (dual DOM mirror vs clean test cutover)   |
| Deferred to TODOS.md  | CEO-E6 (SSE), CEO-E10..E12 (10x roadmap)     |
| Critical Unmitigated  | 0 (all code gaps fixed; strategic uc1 gated) |
+======================================================================+
```


---

## Phase 2: Design Review (UI Scope = Yes)


<!-- autoplan-accepted:design -->
- [DES-H1] Compact Results Header, Single Primary CTA, Strict 6-State Priority & Paid-Action Framing:
  - Split `/jobs/<id>` top area into (a) a **single-line sticky command bar** (`<= 56px` desktop height: truncated title + tooltip, status badge, concise stage/cost summary e.g. `5/8 clips · 2 running` + `已知费用 ¥4.68 · 19.4m GPU` with info tooltip for full `CEO-H5` disclaimer, single primary CTA + consistent secondary `Open Folder` & `Clone` icon buttons across all loaded states) and (b) a **non-sticky scrollable telemetry/context strip** below it (collapsible per-node VRAM/step breakdown disclosure + `#ctx-notice` + `#job-error`).
  - Move the real `#resume-job` and `#continue-job` DOM nodes into the Sticky Command Bar (preserving existing IDs, eliminating duplicate primary CTAs); demote Segments-tab `#assemble-job` to secondary style when `pending_assembly` is true (keeping manual re-assemble as secondary when settled).
  - Lock strict `if / else if` Command Bar state priority: (1) `pausing`/`stopping` → (2) `queued`/`running` → (3) resumable (`paused`/`stopped`/`failed`/`interrupted` or `succeeded && (can_resume_frames || can_resume_pipeline)`, exposing explicit backend recovery action & blocker destination: `Retrieve pending results` when retrievable pending segments exist even if `can_resume_video === false` per `workbench/jobs.py:906`, `Resume Job` when resumable, or 1-click `Jump to blocking item → Seg X / Shot Y` scrolling/focusing the blocked card when `submission_unknown`/`terminal_failed` blocks resume) → (4) `Boolean(job.can_continue)` → (5) `Boolean(job.can_assemble && job.pending_assembly)` → (6) settled `succeeded` (`frame_failures > 0` repair banner vs finished).
  - Default pinned `#final-video-section` to expanded ONLY when `status === "succeeded" && !pending_assembly && !pending_video`; whenever `pending_assembly || pending_video`, collapse it to a `44px` banner AND preserve `"Download previous version"` / `"下载旧版成片"` across every download surface until new assembly succeeds.
  - Honor `DESIGN.md` paid-action framing: Live-mode primary CTAs carry explicit billing badge/copy (e.g., `Live · submits N segments` / `真实计费`) + amber accent framing; Demo mode shows explicit `(Free Demo)` / `(免费演示)`; ComfyUI segment cards display `GPU 142s · gpu1` (never `¥0.00`), while RunningHub cards display `¥X.XX · 142s`.
- [DES-H2] Responsive Breakpoint Contract (`1060px` / `760px` / `420px`), Hard Rejection #7 Fix & Console CSS Isolation:
  - Resolve Hard Rejection #7 on narrow Create page: at `<= 1060px`, render the Create launchpad as a **compact sticky bottom launch bar** docked above the collapsed console (preserving short product mark, Live/Demo billing badge, readiness summary, and primary Launch CTA so the action is never buried below long cast lists); collapse storyboard grid to 3 columns; keep a recognizable short product label (`H3 视频` / `H3 Video`) instead of bare `H3`.
  - At `<= 760px`: topbar History/Settings chips shrink to icon + count and Colab chip shrinks to status dot + `2/2`; ALL popovers use `position: fixed; left: 12px; right: 12px; width: auto; top: calc(var(--topbar-h) + 6px);` (preventing right-edge overflow); console toolbar groups Copy / Half-screen into an overflow trigger and moves search onto a `32px` sub-row when toggled.
  - At `<= 420px`: storyboard, segment, and cast grids collapse to 1 column; Toast stack spans `left: 12px; right: 12px; width: auto;`.
  - Fix CSS specificity: use `.console-tools input.console-search` (`(0,2,1)`, genuinely higher than `input[type="text"]` `(0,1,1)`); explicitly scope compact controls (`.console-tools input.console-search`, `.header-tools select`, `.conflict-tray select`) and align `.output-picker` button height to match `44px` input.
  - Split `--console-open-h` (clamped `clamp(120px, saved, 60vh)` on init and `window.resize`) from class-driven `--console-h` (`36px` collapsed, `var(--console-open-h, 230px)` open, `58vh` half-screen) so inline drag height never breaks `.console-collapsed`.
- [DES-H3] Truthful Collapsed-Console Unread Badge, Tab 1 Ownership, Plain-Text Traceback Filter & Scroll Lock:
  - Add lightweight per-job `log_alert_seq` / `log_alert_counts: {warn, error}` in `Jobs.summaries()` / `/api/bootstrap` (retained through settlement) and per-node `alert_seq` in `/api/runtime/status`, so the collapsed bottom console unread warning/error badge is truthful without downloading log bodies in the background; clear unread count per source ONLY when the console is expanded AND that tab is active (or on job switch).
  - Lock Tab 1 ownership: on `/jobs/<id>`, Tab 1 always binds to the currently viewed job — remove the legacy bottom `<details class="logs-section">` and move `#job-logs` directly into console Tab 1, updating via line-append diff on `job.logs` (preserving text selection); if another job A runs in background, surface it only via the topbar pulsing `Running: <A title>` chip without hijacking B's console. On `/`, Tab 1 binds to the active job via `GET /api/jobs/<active_id>/logs?offset=` (inserting a muted divider line when `truncated: true`).
  - Hide Colab console tabs when `VIDEO_PROVIDER !== "comfyui"`; show Colab Node 2 tab only when active, non-empty, or errored.
  - Implement a plain-text log level classifier that inherits `error` on indented Python traceback/stack continuation lines (`File "...", line ...`, indented frames) so `Warnings/Errors only` preserves full stack traces.
  - Keep bottom scroll-lock (`< 40px` from bottom auto-scrolls; scrolled-up shows floating `↓ Jump to latest` button) and distinguish all 4 console empty/fault states: `No active job`, `Waiting for first output`, `No matching lines`, and `Connection lost (last updated HH:MM:SS)`.
- [DES-H4] Topbar Navigation Clarity, `<select id="language">`, Local-Only Badge, Cold-Start Banner & Colab Popover:
  - Replace static `Current Job` nav link with contextual entry: hidden at 0 history; pulsing `Running: <short title>` whenever any active job exists; `Latest job` on `/` when idle with history.
  - Keep `<select id="language">` inside `.header-tools` (styled to match topbar chips) and keep `local-badge` ("Local only") visible in the topbar or Settings Drawer header/footer.
  - Auto-hide the cold-start setup banner whenever Create is in Demo mode or Results views a demo/settled job; in Live mode, offer primary `Open Settings tab` + secondary `Try Offline Demo`.
  - Settings Drawer: `Cancel` resets form values to `state.config` and clears dirty indicator; backdrop/`Esc`/`✕` preserves unsaved draft and marks dirty indicator on drawer footer + Settings chip; Settings topbar amber dot indicates missing Live config/ffmpeg or unsaved draft only.
  - Colab Popover (`#runtime-pop`): `width: 440px; max-height: calc(100vh - var(--topbar-h) - 56px); overflow-y: auto;`, preserving ALL existing `#runtime-*` IDs (CLI auth code form, Drive auth box, primary/secondary start/stop, force-stop confirm) plus the 3–5 line inline log preview; hide Colab chip (or show compact `RunningHub Cloud` settings link) when `VIDEO_PROVIDER !== "comfyui"`; auto-alert lights topbar dot + notice without stealing keyboard focus mid-typing.
- [DES-H5] Pause/Stop/Recovery Emotional Continuity, Honest Status Copy & Resilient Results States:
  - During `pausing`, keep all in-flight real step/percentage bars visible beneath an amber notice (`Stopped dispatching new items · waiting for N in-flight items to save`) — never replace live bars with an indeterminate bar.
  - Name Stop accurately (`Stop Local Worker` / `停止本地执行`): reconcile Stop modal copy between preserved `task_id` and the narrow mid-submit `submission_unknown` window (removing contradictory "never double-bills" wording); hide `Switch to Pause` when already `pausing`; persistently show post-stop local status PLUS explicit badges/links for `N retrievable remote tasks`, `Seg X needs provider verification`, or `Local process still holds lock` on `cleanup_failed`.
  - Add `has_unsettled_tasks: bool` to `Jobs.summaries()` so history list rows visibly disable Delete (`🗑`) with tooltip reason (matching `CEO-H4` backend 400) alongside a hover/focus `Clone` action; Delete modal explicitly states "History record only — output folder on disk is untouched", shows readonly monospace `output_dir` + `Open Folder`, and focuses `Cancel` by default.
  - Render `#job-loading` skeleton cards; render persistent inline error panels with `Back to Create` / `Retry now` on 404 or poll disconnect (never relying on auto-dismissing Toast); hide `Frames 0/0` metric when `kind === "one"` or `frames_total === 0`.
- [DES-H6] Batch Upload Partial-Failure Loop, Cast-Group Card Preservation & Clone/Draft Recovery Clarity:
  - Prevent accidental browser image navigation via global `window` `dragover`/`drop` listeners; style `.dropzone.drag-active` and per-card `.cast-card.drag-over` (direct single-slot drop bypassing filename match); wire drag-and-drop into `one` mode `.upload-zone`.
  - Show batch progress (`Uploading batch 1/2 (9/15)`); on partial failure, retain all succeeded bindings, display `Bound X / Needs assignment Y / Failed Z` counts, and retry ONLY failed/un-uploaded files.
  - Mount `#upload-conflict-tray` directly above `#cast-grid` (each row: `40x40` thumb, filename, conflict reason badge, target `<select>` prioritizing empty slots and labeling occupied slots `Replaces existing`, `Apply` / `Dismiss`, plus `Dismiss all`), syncing options whenever film JSON changes.
  - Preserve **multi-character cast group cards** (`state.filmGroups` spanning 2 cols or stacked avatars + member add/remove + group builder) and `peakFilmReferences` (>9 refs warning) in the 2-column compact grid.
  - Label Clone accurately (`Clone Script & Settings` / `复刻剧本与参数` under `d2` Option A); back up non-empty `h3.createDraft.v1` before applying `?from=<job_id>` (preserving original draft if clone fails, and showing a dismissible banner explaining references were not copied + `Restore previous draft` undo button); on draft restore, explicitly indicate how many references need attachment and add a `Clear / Reset` button with confirmation.
- [DES-H7] Frictionless Storyboard Review Loop (Draft Guard, Whole-Area Click, Key-Anchored Carousel, Full Filters & Grid Alignment):
  - When unapplied `frameDrafts` or `frameReferenceDrafts` exist, clicking `#continue-job` prompts a confirmation dialog (`N frames have unapplied prompt/reference edits — generate video with current saved images?`).
  - Make the entire `.storyboard-media` preview box (including waiting/failed placeholders without `<a>`) keyboard-focusable and clickable to enter carousel mode.
  - Anchor carousel state by `frame.key` (not array index): if repairing a failed frame removes it from the active `Failed` filter, keep viewing that frame with a top pill `Fixed · Next failed frame →`.
  - Expand storyboard filter pills to `All` / `Ready` / `Pending / In-Flight` (waiting, `task_pending`, `submission_unknown`, shown when count > 0) / `Failed` / `Drafts`, with an empty-filter state + `Show all frames` button.
  - Force `align-items: start` on `.frame-grid` and `.seg-grid` so expanding one card never stretches sibling cards.
- [DES-H8] Contrast (`>= 4.5:1`), Touch Targets, Top-Right Toast + Single Live Region, Focus Trap, Keyboard Console & Reduced Motion:
  - Replace prototype's `#74877c` (`3.82:1` FAIL) on `.frame-meta` and small metadata with `var(--muted)` (`#52655a`, `6.24:1` PASS on white); preserve `2px solid var(--accent)` focus rings on light surfaces and add high-contrast light focus ring (`#a9e5c6`) inside the dark `#263c30` log terminal.
  - Under `@media (pointer: coarse)`, provide `44px` minimum hit targets for filters, transport buttons, and expanded console actions; make collapsed console bar a single full-width clickable/keyboard trigger; provide keyboard height presets (`Collapsed / Compact / Half-screen`) alongside a focusable `role="separator"` drag handle (`ArrowUp`/`ArrowDown`).
  - Position `.toast-stack` at **top-right below topbar** (`top: calc(var(--topbar-h) + 12px); right: 18px; z-index: 95;`, above Modal `90` & Drawer `80`, never covering Settings Save); remove `$("global-error").scrollIntoView()` in `common.js:28`; style `#global-error`/`#global-message` as `.sr-only` (acting as the SINGLE screen-reader live region when `d3` Option A is active, with visual Toasts marked `aria-hidden="true"` to prevent double announcements); cap at 3 stacked Toasts, `4200ms` info/success (pause on hover), `8000ms` or manual close for error Toasts.
  - Stop/Delete modals require `aria-labelledby`, `Tab` focus trap, initial focus on `Cancel`, focus restoration to opener on close, and blocking `←/→` + `⌘/Ctrl+J` while open. Enforce LIFO `Esc` stack (`Modal -> Settings Drawer -> Popover`, never closing bottom console). `⌘/Ctrl+J` calls `event.preventDefault()`, checks `!event.isComposing`, and ignores form inputs/`contentEditable`. Add a re-entrancy guard (`validatingTabs`) on the capture-phase `invalid` listener so revealing a hidden tab + calling `focus()`/`reportValidity()` cannot loop across two invalid hidden tabs.
  - Remove competing continuous pulsing dots on idle/online indicators (reserve pulse strictly for `pausing`/`stopping` or indeterminate states) and honor `@media (prefers-reduced-motion: reduce)` across all new animations.
- [DES-H9] Prototype Authority Table, `document.title` / Tab 3 Spec & Bilingual Browser Acceptance Checklist:
  - Declare this plan (plus `CEO-*` / `DES-*` clauses) as single source of truth superseding `docs/design/workbench-ux-prototype.html` across 8 changed areas (collapsed console default, 3-tab Settings grouping, `<select id="language">`, reconciled Stop modal copy, Delete modal w/ path+Open Folder, `CEO-H5` cost+GPU labels, full Colab Popover flows, and Create `one`/cast-group/conflict-tray/cold-start banner).
  - Define exact localized `document.title` templates per job state and Tab 3 badge count (non-storyboard artifacts matching `results.js:243`) + pre-report empty state.
  - Add a bilingual (`zh-CN` & `en`) browser visual & keyboard acceptance pass across `1440px` / `1060px` / `760px` / `420px` verifying cold start (live/demo), loading/404/bootstrap fail, empty history, partial batch upload, keyboard-only hidden-tab validation + Stop cancel, and stale-film download label.
- [DES-T1] (Provisional taste default, Phase 4 gate `d4`) Keep desktop storyboard grid at 3 columns (`>= 1060px`) with `align-items: start` and expand `.frame-editor` full-row (`grid-column: 1 / -1`) to preserve spacious inline prompt + character picker editing and 100% compatibility with `tests/test_frame_character_picker.js` (alternative Option B: 4 columns clicking Edit into Carousel mode).
<!-- /autoplan-accepted:design -->

### Step 0 — Design Scope Assessment
- **0A. Initial Design Rating**: **5/10** — Plan + interactive prototype establish strong high-level direction (sticky controls, unified bottom console, titled history, batch auto-match), but leave major viewport-budget contradictions (sticky chrome eating 30-58% of 800px height), Hard Rejection #7 on narrow Create viewports, missing collapsed-console unread data source, missing partial-upload/404/recovery states, WCAG contrast regression (`3.82:1`), z-index Toast collisions, and 8 direct contradictions with the linked prototype. A **10/10** plan specifies exact viewport budgets, strict recovery state priority, truthful paid-task & stale-film indicators, complete narrow-screen reflow, keyboard/SR closure, and zero prototype drift.
- **0B. DESIGN.md Status**: `DESIGN.md` exists (33 lines). All design decisions calibrated directly against its tokens (`--ink: #203831`, `--muted: #52655a`, `--accent: #237b60`, `--border: #e2e7e1`, `--surface: #ffffff`, `--wash: #edf5ef`, `--mono`), breakpoints (`1060px`, `760px`, `420px`), `prefers-reduced-motion`, and paid-action framing.
- **0C. Existing Design Leverage**: Reuse `--muted` (`6.24:1`), existing `44px` control height parity, `2px solid var(--accent)` focus treatment, `style.css:252` reduced-motion rule, existing core DOM IDs (`#runtime-*`, `#config-form`, `#resume-job`, `#continue-job`, `#job-logs`), and `video-segments.js` tab plumbing.
- **0D. Focus Areas**: Auto-decided all 7 dimensions (P1 Completeness).

### Step 0.5 — Dual Voices & Design Litmus Scorecard

#### AGY (IN-HOST) SUBAGENT (design completeness — `50392c0b-cdc4-44e3-a92a-2cb6512b2406`, 369s)
- **Verified input**: `INPUT: design fc309c2d9085612c1d907683711d4beeaee5651d4a225518827626e4fba8ec9c` (exact match).
- **21 findings** (4 Critical: D1 sticky viewport budget, D5 batch upload/cast-group states, D10 pause/stop/blocker emotional breaks, D18 viewing Job B while Job A runs / `#job-logs` source conflict; 12 High: D2, D3, D4, D6, D7, D8, D11, D12, D14, D15, D19, D20; 5 Medium: D9, D13, D16, D17, D21).

#### CODEX SAYS (design critique — `gpt-6.2-astra`, `OUTSIDE_STATUS: completed provider=codex host=agy`, 235s)
```tool-output
**Classification: APP UI**

**Decision: Revise before implementation.** Moving Colab into the header, collapsing the console by default, and bringing the finished film forward all serve the creator. Three decisions still block approval. “Stop” overstates what happens to remote work, the narrow layout loses the promised persistent action, and the collapsed console promises an unread badge without a source for new job errors.

This is a source review of the on-disk plan, prototype, templates, and CSS. The contrast measurement below is calculated from the declared colors. Viewport and keyboard behavior still need browser verification.

### Hard rejection and applicable rules

**Hard rejection #7 applies to Create at narrow widths.** The prototype turns the editor, reference manager, launch controls, and recent history into a stack of bordered panels. The launch action becomes static and follows the reference list. This recreates the scrolling problem the redesign is supposed to solve. [Prototype layout](/Users/qiaoyu/projects/h3-ref-video/docs/design/workbench-ux-prototype.html:462)

| Hard rejection | Applies? | Judgment |
|---|---|---|
| 1. Generic SaaS card grid as first impression | NO | The storyboard grid contains the actual shots under review. |
| 2. Beautiful image with weak brand | NO | The images are work products. Compact product identification is a separate issue. |
| 3. Strong headline with no clear action | NO | The job title and command bar have an explicit operational purpose. |
| 4. Busy imagery behind text | NO | Controls and editing copy have their own surfaces. |
| 5. Sections repeating the same mood statement | NO | The sections perform distinct production tasks. |
| 6. Carousel with no narrative purpose | NO | Reviewing adjacent shots in order is a concrete continuity check. |
| 7. App UI made of stacked cards instead of layout | **YES** | Narrow Create loses the relationship between inputs, readiness, and launch. |

**App UI rule violations:** primary-action visibility, media-first hierarchy, and a clear next action in partial states. **Universal rule violations:** responsive reflow, focus management, contrast, and feedback that accurately reflects system state. The repository’s own design brief explicitly requires all three breakpoints, visible focus, reduced motion, and in-place recovery for ambiguous paid work. [Design rules](/Users/qiaoyu/projects/h3-ref-video/DESIGN.md:29)

### Seven litmus checks

| # | Check | Answer | Reason |
|---|---|---|---|
| 1 | Brand/product unmistakable in first screen? | **NO** | Desktop names the video workbench. At 780px the prototype hides that name and leaves “H3.” |
| 2 | One strong visual anchor present? | **NO** | The completed film has one. During generation, the sticky telemetry and shot grid compete for attention. |
| 3 | Page understandable by scanning headlines only? | **NO** | The title identifies the project and the tabs identify asset types. The next decision lives in badges, hints, and button states. |
| 4 | Each section has one job? | **NO** | The command bar combines navigation context, billing, GPU monitoring, progress, and execution controls. |
| 5 | Are cards actually necessary? | **YES** | Each shot or clip needs a bounded preview with its own status and edit action. |
| 6 | Does motion improve hierarchy or atmosphere? | **NO** | The running badge and log-tab indicator can pulse while a progress bar also animates. The three signals compete. |
| 7 | Would the design feel premium with all decorative shadows removed? | **YES** | Film imagery, restrained color, type, spacing, and dividers carry the studio identity. |

### Findings

**1. “Stop immediately” gives the wrong answer to “is the GPU still running?”**  
Severity: High

H4 correctly warns about an unacknowledged submission and protects the history record. That warning disappears with the modal. A creator can then see “Stopped,” launch another job, and discover the previous job still occupies the node. There are three distinct outcomes to communicate: local work ended, a known remote task can be retrieved, or provider acceptance needs verification. A single status badge cannot communicate all three. [Plan, Stop lifecycle](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:103)

Recommendation: Resolve **uc1** by retaining local-only Stop, label it “Stop local worker,” and resolve **d1** with immediate acknowledgment after the stop request is saved because users need to distinguish request acceptance, worker exit, and remote completion. Keep “Stopping…” visible until exit is confirmed. Afterward, show the affected shots or segments with “Retrieve result” or “Check provider” as appropriate. If Delete is blocked, take the user to those same items.

**2. The breakpoint contract breaks the core Create interaction.**  
Severity: High

At 1100px the prototype puts the launchpad below the form and sets it to `position: static`. At 780px it hides the product name. It has no 420px layout. The plan specifies a nonshrinking console toolbar and a 100px minimum search field, but leaves the overflow decision to the implementer. Its claim that `input.console-search` outranks `input[type="text"]` is also incorrect. Both have equal specificity. [Prototype breakpoints](/Users/qiaoyu/projects/h3-ref-video/docs/design/workbench-ux-prototype.html:462) · [Plan, console CSS](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:164) · [Production breakpoints](/Users/qiaoyu/projects/h3-ref-video/workbench/static/style.css:258)

Recommendation: Specify a compact launch row at 1060px, group secondary header and console actions at 760px, and retain a short product name, billing mode, and one primary action at 420px because those are the facts creators need while scrolling a long cast list. Verify both languages just above and below each breakpoint, with the console open, a long job title, and keyboard focus on the last form field.

**3. Sticky telemetry can displace the work being reviewed.**  
Severity: High

The running prototype puts sampling steps, VRAM, node names, and elapsed time in the sticky command area. The plan extends that pattern to every active segment without a height limit. H6 reclaims space from the console but sets no corresponding limit on the command bar. H5’s “Known API cost (kept assets)” and separate GPU duration are accurate. Neither needs to be repeated at every level of the page. [Prototype, running state](/Users/qiaoyu/projects/h3-ref-video/docs/design/workbench-ux-prototype.html:1134) · [Design purpose](/Users/qiaoyu/projects/h3-ref-video/DESIGN.md:5)

Recommendation: Keep the sticky area to the job title, one stage summary such as “Generating clips · 5 of 8 ready,” and the current action because those remain relevant while inspecting any frame. Put per-node detail behind a disclosure, retain H5’s exact cost semantics in the cost detail, and reserve animation for an active stage whose progress cannot be measured.

**4. The six command-bar states specify buttons more precisely than recovery.**  
Severity: High

The plan groups paused, stopped, failed, and interrupted jobs under Resume, then disables Resume for some of the cases that most need guidance. It also promotes finished-video actions while the current renderer explicitly distinguishes a previous cut from a current cut. A creator with seven usable clips and one blocked submission needs to know which clip to inspect and whether the available download includes the latest edits. [Plan, command-bar states](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:220) · [Current version and download labels](/Users/qiaoyu/projects/h3-ref-video/workbench/static/results.js:213)

Recommendation: Define the headline, primary action, and destination for each recovery case because a disabled Resume button cannot take the user to the item requiring attention. Include acceptance cases for one blocked clip among ready clips, pause immediately before assembly, and a previous film available during regeneration. Preserve “Download previous version” on every download surface until the new assembly succeeds.

**5. The collapsed console cannot substantiate its unread badge.**  
Severity: High

H6 promises unread warnings and errors. E9 fetches incremental job logs on Create only while the console is expanded on the Job Log tab. A warning emitted while the console is collapsed therefore has no specified route to the badge. If the job settles while collapsed, the final error can also be missed by an active-job-only poller. An empty badge would imply a clean run. [Plan, Create polling](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:224) · [H6 and E9](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:233)

Recommendation: Include a per-job warning/error sequence or count in the lightweight status response, retain it through settlement, and mark it read when its source is viewed because the badge must remain truthful without downloading log bodies in the background. Distinguish “No active job,” “Waiting for first output,” “No matching lines,” and “Connection lost” in the expanded console.

**6. Escape support leaves the keyboard and screen-reader workflow incomplete.**  
Severity: High

The current templates already have linked tabs and panels, a live error alert, and a status region. The proposed drawer, popovers, Stop/Delete dialogs, and draggable console introduce new focus boundaries. The plan specifies Escape but leaves entry focus, focus containment, return focus, keyboard resizing, and announcement ownership open. Its capture-phase `invalid` handler also calls `reportValidity()`, which can fire `invalid` again. [Current notice regions](/Users/qiaoyu/projects/h3-ref-video/workbench/templates/base.html:19) · [Current result tabs](/Users/qiaoyu/projects/h3-ref-video/workbench/templates/results.html:24) · [Plan, drawer validation](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:223)

Recommendation: Resolve **d3** with one authoritative live region and update the tests because mirrored alerts can announce the same failure twice. Specify safe initial focus for destructive dialogs, modal focus containment, topmost-only Escape, focus restoration, named keyboard resize controls, and focus on the first invalid settings field after revealing its tab. Test the entire sequence in a browser, including validation across two hidden tabs.

**7. Small metadata and compact controls regress the existing visual treatment.**  
Severity: Medium

The prototype renders 11px frame metadata in `#74877c`, approximately **3.82:1** against white. Console buttons are 26px high and small actions are 28px high. Preserving 44px form fields alone will not make adjacent touch actions easy to select. The current stylesheet already supplies a stronger muted color, visible focus, and a global reduced-motion rule. [Prototype metadata](/Users/qiaoyu/projects/h3-ref-video/docs/design/workbench-ux-prototype.html:275) · [Console controls](/Users/qiaoyu/projects/h3-ref-video/docs/design/workbench-ux-prototype.html:421) · [Production motion rule](/Users/qiaoyu/projects/h3-ref-video/workbench/static/style.css:252)

Recommendation: Use the existing `--muted` token for small metadata, preserve the 2px focus treatment on both light and terminal surfaces, and give frequently used touch actions a 44px hit area because shot status, filters, and transport controls must remain legible and operable during repeated review. Keep the collapsed console a single generous trigger on touch screens.

**8. Clone and draft restoration hide how much work was recovered.**  
Severity: Medium

The proposed clone strips local paths, drafts exclude file bindings, and `?from=` takes precedence over the current draft. A valid script can therefore reopen with its cast references missing. The prototype illustrates a populated script and successful matching, while the plan leaves the experience after a later upload batch fails unspecified. The conflict tray solves ambiguous matches but does not explain which earlier batches succeeded. [Plan, clone endpoint](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:127) · [Current reference checklist](/Users/qiaoyu/projects/h3-ref-video/workbench/templates/index.html:37)

Recommendation: Resolve **d2** in favor of server-validated opaque reference reuse for Clone because the creator’s goal is to start from the same cast. Show reused and missing references by name, preserve the prior draft until the clone loads successfully, and retain completed uploads when a later batch fails. For draft restoration, say explicitly how many references need reattachment.

**9. The verification plan cannot establish visual or interaction acceptance.**  
Severity: Medium

The three phases verify syntax, DOM behavior, locale parity, and backend transitions. A passing suite can still ship a launch button below 100 references, a modal that loses focus, or a stale film presented as current. The populated prototype also skips the first interaction on an empty Create page, where the creator needs a clear script-import action and an explanation of when the setup banner applies. [Plan, phase verification](/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/autoplan-design-4dRkiq/design-implementation.md:184) · [Current script import and empty editor](/Users/qiaoyu/projects/h3-ref-video/workbench/templates/index.html:27)

Recommendation: Resolve **uc2** with three sequential reviewable PRs and a final integrated browser acceptance pass because the backend contract, shared shell, and creator workflows have different failure modes. Exercise cold start in live and demo modes, loading and failed bootstrap, empty history, partial uploads, keyboard-only recovery, and stale-film download in both languages. Make primary-action visibility and correct recovery destinations explicit pass conditions.

VERDICT: FINDINGS
```

#### DESIGN OUTSIDE VOICES — LITMUS SCORECARD
```
DESIGN OUTSIDE VOICES — LITMUS SCORECARD:
=========================================================================
  Check                                    agy (in-host)  Codex  Consensus
  ---------------------------------------  -------------  -----  ---------
  1. Brand unmistakable in first screen?   NO (narrow)    NO     CONFIRMED (fixed by DES-H2/H4)
  2. One strong visual anchor?             NO (running)   NO     CONFIRMED (fixed by DES-H1)
  3. Scannable by headlines only?          NO (recovery)  NO     CONFIRMED (fixed by DES-H1/H5)
  4. Each section has one job?             NO (cmd bar)   NO     CONFIRMED (fixed by DES-H1)
  5. Cards actually necessary?             YES            YES    CONFIRMED (media/shot cards earn existence)
  6. Motion improves hierarchy?            NO (pulses)    NO     CONFIRMED (fixed by DES-H8)
  7. Premium without decorative shadows?   YES            YES    CONFIRMED (quiet studio desk holds)
  ---------------------------------------  -------------  -----  ---------
  Hard rejections triggered:               #7 (narrow)    #7 YES CONFIRMED [HARD REJECTION] (fixed by DES-H2)
=========================================================================
```

### Passes 1-7 (All 7 Passes Executed)

#### Pass 1: Information Architecture — `5/10 -> 9/10`
- **[HARD REJECTION #7 — CONFIRMED]**: Prototype collapses Create page at 1100px into vertically stacked bordered panels with static launch button at bottom. **Auto-fixed (`DES-H2`)**: `<= 1060px` keeps a compact sticky bottom launch bar docked above the collapsed console.
- **Litmus #1/#2/#3/#4 CONFIRMED failures**: Topbar hid brand name at 780px, Command Bar mixed sticky telemetry/VRAM/cost/buttons eating 30-58% of 800px height, duplicate primary CTAs (`#assemble-job`), and missing Live billing amber framing per `DESIGN.md`. **Auto-fixed (`DES-H1`, `DES-H4`, `DES-H9`)**:
  - Constraint-worship 3-item priority per screen: Create = (1) Script/Input, (2) Cast References, (3) Sticky Launch CTA + Billing Mode; Results = (1) Single-line Sticky Command Bar (`<= 56px`), (2) Pinned Final Video (when current) OR Media Grid/Carousel, (3) Tab Switcher.
```
SCREEN HIERARCHY (RESULTS /jobs/<id>)
+-------------------------------------------------------------------------+
| [54px Fixed Topbar] H3 Video | New | [Running: A / Latest] | Hist | GPU | Gear | Lang |
+-------------------------------------------------------------------------+
| [Auto-hiding Cold-Start Setup Banner -- Live unconfigured only]         |
+-------------------------------------------------------------------------+
| [<=56px Sticky Command Bar] Title | Status | 5/8 clips · ¥4.68 · 19m GPU| [Primary CTA] 📂 📋 |
+-------------------------------------------------------------------------+
| [Scrollable Telemetry & Context] Collapsible Per-Node Bars | Recovery/Error Notice |
| [Pinned Final Video] Expanded if current; 44px collapsed strip + "Old Version" if stale |
| [3 Tabs] Storyboard/Script | Video Segments | Report & Files            |
| [Media Grid / Key-Anchored Carousel -- 3 cols desktop, align-items:start]|
+-------------------------------------------------------------------------+
| [36px Collapsed Bottom Console (default)] Status · Unread Badge · ⌘/Ctrl+J |
+-------------------------------------------------------------------------+
```

#### Pass 2: Interaction State Coverage — `4/10 -> 9/10`
- **Gaps fixed (`DES-H3`, `DES-H5`, `DES-H6`, `DES-H7`)**: Collapsed console unread badge lacked server cursor (`log_alert_seq`/`log_alert_counts` added), partial batch upload retry was undefined, 404/disconnect left blank screen after Toast faded, `stopping`/`cleanup_failed` lacked visuals, and storyboard filter omitted `Pending / In-Flight` & empty filter state.
```
FEATURE               | LOADING                 | EMPTY                        | ERROR                          | SUCCESS                        | PARTIAL
----------------------+-------------------------+------------------------------+--------------------------------+--------------------------------+-------------------------------
Results Page          | #job-loading skeletons  | N/A (404 inline card -> /)   | Inline 404/disconnect + Retry  | Sticky bar + current player    | Old cut 44px strip + Pending
Bottom Console        | "Waiting for output..." | "No active job" / "No match" | "Connection lost (HH:MM:SS)"   | Appended lines + scroll-lock   | "Earlier logs truncated" line
Batch Cast Upload     | "Batch 1/2 (9/15)" bar  | Dashed dropzone + sample cue | Toast naming failed files      | Auto-bound slots + count pill  | Keep bound; tray + retry failed
Storyboard Grid       | Waiting frame shimmer   | Filter empty + "Show all"    | Failed card clickable -> edit  | Ready thumbs + click carousel  | Pending filter + draft badge
Settings Drawer       | Disabled Save spinner   | N/A (env defaults loaded)    | Auto-switch tab + field focus  | Toast + clear dirty badge      | Unsaved draft badge on close
Colab Popover         | 3-5 line inline log     | "Idle · Start primary node"  | Inline error + console link    | Ready dot + node list          | Auth URL/code form inline
History Popover       | Loading rows            | "No jobs yet · Create first" | Inline fetch retry             | Titled rows + Clone/Delete     | Unsettled task -> Delete disabled
```

#### Pass 3: User Journey & Emotional Arc — `5/10 -> 9/10`
- **Gaps fixed (`DES-H5`, `DES-H6`, `DES-H7`)**: Preserved real step/percent bars during `pausing`, reconciled Stop copy (`Stop Local Worker` + persistent retrievable/verify/lock-held notices), added 1-click `Jump to blocking item`, added `?from=` draft backup + undo banner, added unapplied-draft confirm on `#continue-job`, made whole `.storyboard-media` clickable, and anchored carousel by `frame.key`.
```
STEP | USER DOES                       | USER FEELS                  | PLAN SPECIFIES?
-----+---------------------------------+-----------------------------+-------------------------------------------------------------
1    | Opens / (first time, no keys)   | Orienting, cautious (5-sec) | Auto-hiding Live setup banner + "Try Offline Demo" (DES-H4)
2    | Drops 12 cast sheets            | Expects fast setup (5-min)  | Chunked <=9/<=28MiB progress, cast-group match, conflict tray (DES-H6)
3    | Clicks Launch (Live mode)       | Cost-aware                  | Explicit Live billing badge + amber framing (DES-H1)
4    | Watches multi-node render       | Wants calm progress         | <=56px sticky bar + collapsible per-node detail + 36px console (DES-H1/H3)
5    | Clicks Pause After Current      | Anxious about in-flight     | Real step bars kept + amber draining summary (DES-H5)
6    | Clicks Stop Local Worker        | Worried about paid tasks    | Cancel-focused modal w/ submission_unknown truth + post-stop badges (DES-H5/H8)
7    | Repairs failed shot in carousel | Focused on continuity       | Whole-box click, key-anchored carousel, "Fixed -> Next failed" (DES-H7)
8    | Clones series Ep 2 months later | Returning pro (5-year)      | Titled history, Clone banner w/ draft backup & undo (DES-H6)
```

#### Pass 4: AI Slop Risk & Design Hard Rules — `6/10 -> 9/10`
- **Surface Classifier**: **OPERATE (APP UI)**.
- **Hard Rejections**: #1–#6 = NO; **#7 = YES** on narrow Create page -> **resolved by `DES-H2`** (sticky bottom launch bar at `<= 1060px`).
- **AI Slop & Reflex Check**: Zero purple gradients, zero 3-col feature grids, zero decorative blobs, zero generic hero copy. **Litmus #6 fixed (`DES-H8`)**: removed competing continuous pulsing status dots on idle/online indicators. **Browser surfaces themed (`DES-H8`)**: `2px solid var(--accent)` focus ring on light surfaces + `#a9e5c6` focus ring inside dark `#263c30` terminal; `tabular-nums` / `--mono` for counters and costs. Native OS CJK + Latin font stack explicitly justified in `DESIGN.md:23-24` for offline zero-network Windows/macOS CSP compliance.

#### Pass 5: Design System Alignment — `6/10 -> 9/10`
- Aligned strictly with `DESIGN.md`:
  - Replaced prototype's failing `#74877c` (`3.82:1`) with `var(--muted)` (`#52655a`, `6.24:1` on `var(--surface)`) (`DES-H8`).
  - Restored `DESIGN.md:33` amber Live-mode paid-action framing and explicit billing copy on Create & Results primary CTAs (`DES-H1`).
  - Preserved `DESIGN.md:29-32` `1060px`/`760px`/`420px` breakpoints, `44px` controls, `max-width: 68ch`, and `prefers-reduced-motion: reduce` (`DES-H2`, `DES-H8`).
  - Added 8-item Prototype Supersession Table (`DES-H9`) so plan + `DESIGN.md` override outdated prototype markup.

#### Pass 6: Responsive & Accessibility — `4/10 -> 9/10`
- **Responsive (`DES-H2`)**: Concrete per-breakpoint layout for `1060px`, `760px`, `420px` (sticky narrow launch bar, `position: fixed` popovers at `760px`, 1-col grids & full-width Toasts at `420px`, `.console-tools input.console-search` specificity fix, `--console-open-h` clamping).
- **Accessibility (`DES-H8`)**: `6.24:1` muted text contrast; `44px` coarse-pointer touch targets; single screen-reader live region (`#global-error`/`#global-message` as `.sr-only`, visual Toasts `aria-hidden="true"`, top-right `z:95` never covering Drawer Save or blocked by Modal `z:90`, `8000ms` error Toast); Stop/Delete modal `aria-labelledby`, `Tab` focus trap, initial focus on `Cancel`, return focus on close; LIFO `Esc`; keyboard height presets + `role="separator"` drag handle; re-entrancy-guarded capture-phase `invalid` tab switcher; `prefers-reduced-motion: reduce`.

#### Pass 7: Unresolved Design Decisions (Unscored Register)
| Decision Needed | Disposition | If Deferred / Tradeoff |
|---|---|---|
| Desktop storyboard grid 3 cols full-row inline editor vs 4 cols click-to-carousel (`d4`) | Provisional Taste (`DES-T1`, Phase 4 Gate `d4`) | 3 cols preserves 100% of `test_frame_character_picker.js` spaciously; 4 cols trades inline edit for denser overview |
| Dual DOM mirror vs clean test cutover (`d3`) | Cross-referenced to CEO Taste `d3` (Codex Design strongly prefers single live region + test cutover; `DES-H8` eliminates double SR announcement under both options) | Under Option A, `.sr-only` mirror stays single live region; under Option B, mirror nodes deleted |
| Clone reference reuse (`d2`) | Cross-referenced to CEO Taste `d2` (Codex Design recommends opaque server-side reuse; `DES-H6` handles clear missing-ref banner + draft undo under Option A) | Series creators re-drop sheets under A vs automatic binding under B |
| Local Stop scope (`uc1`) & 3 PRs (`uc2`) | Preserved for Phase 4 Gate (Codex Design recommends local Stop labeled "Stop local worker" + immediate async `d1` + 3 sequential PRs `uc2`) | User decides at Phase 4 Gate |

### Phase 2 Required Outputs

#### NOT in scope (Design)
- Dark mode / theme switcher (quiet light studio desk per `DESIGN.md`).
- Drag-to-reorder shots or visual node/timeline editor (deferred with 10x script wizard `CEO-E10`).
- Mobile-native bottom sheet navigation (desktop/laptop local studio primary; `420px` graceful usability only).

#### What already exists (Design)
- `DESIGN.md` color tokens (`--ink`, `--muted`, `--accent`, `--border`, `--surface`, `--wash`, `--mono`), `68ch` measure, `1060px`/`760px`/`420px` breakpoints, and `prefers-reduced-motion` (`workbench/static/style.css:252`).
- Existing accessible markup: linked tabs/panels (`results.html:24`), live notice regions (`base.html:19-20`), `44px` input/select scale (`style.css:15`), `2px solid var(--accent)` focus ring (`style.css:11`).

#### Design Implementation Tasks
- [ ] **DES-T1 (P0, human: ~1d / CC: ~30min)** — `workbench/results+jobs` — Single-line `<=56px` sticky command bar, scrollable telemetry disclosure, strict 6-state priority + retrievable-pending & jump-to-blocker CTA, collapsed stale-film banner + previous-version label, Live billing amber framing.
- [ ] **DES-T2 (P0, human: ~1d / CC: ~30min)** — `workbench/shell+console` — Lightweight `log_alert_seq`/counts for truthful collapsed-console badge, Tab 1 viewed-job binding (`#job-logs` moved to console w/ line-append diff), traceback error inheritance, scroll-lock + jump-to-latest, `--console-open-h` clamp.
- [ ] **DES-T3 (P1, human: ~1d / CC: ~30min)** — `workbench/responsive+a11y` — Narrow-Create sticky bottom launch bar (`<=1060px`), `760px` fixed popovers, `420px` 1-col, `.console-tools input.console-search`, `--muted` `6.24:1` contrast, `44px` coarse targets, top-right `z:95` Toast + `.sr-only` live region, LIFO `Esc` & modal focus trap, re-entrancy-safe `invalid` tab switch, reduced-motion.
- [ ] **DES-T4 (P1, human: ~1d / CC: ~30min)** — `workbench/create+storyboard` — Partial batch upload retry + cast-group grid cards + `#upload-conflict-tray` + `?from=` draft backup/undo banner, storyboard whole-area click + key-anchored carousel + unapplied-draft confirm + pending filter + `align-items: start`.
- [ ] **DES-T5 (P2, human: ~4h / CC: ~15min)** — `workbench/history+qa` — `has_unsettled_tasks` disabled delete state in history list + Delete modal path/Open Folder, localized `document.title` templates, Tab 3 empty state, bilingual 4-viewport browser acceptance checklist.

#### Design Review Completion Summary
```
  +====================================================================+
  |         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
  +====================================================================+
  | System Audit         | DESIGN.md present; full workbench UI scope  |
  | Step 0               | Initial 5/10; all 7 passes selected         |
  | Pass 1  (Info Arch)  | 5/10 -> 9/10 after fixes (DES-H1/H2/H4/H9)  |
  | Pass 2  (States)     | 4/10 -> 9/10 after fixes (DES-H3/H5/H6/H7)  |
  | Pass 3  (Journey)    | 5/10 -> 9/10 after fixes (DES-H5/H6/H7)     |
  | Pass 4  (AI Slop)    | 6/10 -> 9/10 after fixes (HR #7 fixed, H8)  |
  | Pass 5  (Design Sys) | 6/10 -> 9/10 after fixes (DESIGN.md synced) |
  | Pass 6  (Responsive) | 4/10 -> 9/10 after fixes (DES-H2/H8)        |
  | Pass 7  (Decisions)  | 9 resolved (DES-H1..H9), 1 taste (d4)       |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (3 items)                           |
  | What already exists  | written                                     |
  | TODOS.md updates     | 0 new design TODOs (all fixed in-plan)      |
  | Approved Mockups     | 0 generated (existing HTML prototype audit) |
  | Decisions made       | 10 added to plan (DES-H1..H9 + DES-T1/d4)   |
  | Decisions deferred   | 0 unresolved blocking (d4 queued for Gate)  |
  | Overall design score | 4/10 -> 9/10                                |
  +====================================================================+
```

---

## Phase 2.5: DX Review (DX Scope = Yes, Mode = DX POLISH)

<!-- autoplan-accepted:dx -->
- [DX-H1] Run-Scoped Pause Boundary & Zero-CLI-Break `control.json`:
  - `Jobs.launch()` passes `H3_WORKBENCH_OPERATION_ID=<operation_id>` alongside `H3_WORKBENCH_WORKER=1` to every managed subprocess, and `POST /api/jobs/<id>/pause` & `/stop` persist `operation_id` inside `<output_dir>/inputs/control.json`.
  - `h3ref/control.py:should_pause(job_dir, should_pause_cb=None)` honors `inputs/control.json` ONLY when `os.environ.get("H3_WORKBENCH_WORKER") == "1"` AND `control.json.get("operation_id") == os.environ.get("H3_WORKBENCH_OPERATION_ID")` (or when an explicit `should_pause_cb` returns `True`), guaranteeing that direct CLI `python h3ref.py film <out>` / `one --out <out>` is never trapped at exit 75 by a leftover workbench `control.json` (while preserving direct CLI lock and pending-edit guards). Corrupt or unreadable `control.json` logs a redacted `stderr` warning (file path + exception class) and degrades safely instead of crashing an in-flight paid run.
  - Enforce the run-scoped pause check immediately before EVERY fresh outbound provider generation POST: (a) inside `h3ref/rh.py:submit_with_retry()` before each `submit(g)` AND using 1-second interruptible sleep slices during the 60-second `QueueFull` (421/415) backoff (raising `JobPaused` with zero new POST if paused during backoff), and (b) inside `h3ref/frames.py:draw()` after reference upload and immediately before the APIMart generation POST (preserving retrieval for any already-accepted `task_id`).
  - On `JobPaused` (`EXIT_CODE_PAUSED = 75`), emit a structured diagnostic to `sys.stderr` (never `stdout`) containing stable code `JOB_PAUSED`, triggering file and `operation_id`, `side_effects: partial` (completed assets and accepted `task_id`s saved to `state.json`), exact recovery action (`POST /api/jobs/<id>/resume` or direct CLI resume, noting automatic retry resumes paid work), and `docs/workbench.md#job-control-errors`, then exit 75 without prompting.
- [DX-H2] Eliminate False-Success HTTP 200 Responses + Bound Termination Waits + `operation_id` Precondition:
  - `POST /api/jobs/<id>/open-folder`: macOS runs `subprocess.run(["open", "--", path], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5, check=False)` and returns HTTP `500` RFC 9457 `job_problem` (`code="open_folder_failed"`, redacted stderr + exit code + `context.path` + manual open command in `fix`) when `proc.returncode != 0`; Linux checks `shutil.which("xdg-open")` AND `os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")` (missing -> HTTP `400` `code="open_folder_headless"` or `"open_folder_unavailable"` with `context.path` + copy-paste command), spawns `Popen` with `stdin=subprocess.DEVNULL`, polls up to `0.2s` to catch immediate non-zero exit -> HTTP `500`, and on async launch returns HTTP `200` `{"dispatched": true, "opened": true, "path": path}`; catch `subprocess.TimeoutExpired` and `OSError` (including Windows `os.startfile`) -> HTTP `500` RFC 9457 with `instance` trace and `context.path`; missing output directory -> HTTP `404` `job_output_dir_missing`; client-supplied `path` payload key -> HTTP `400` `open_folder_client_path_forbidden` (`field="path"`). Never return HTTP `200` on observed failure.
  - `POST /api/jobs/<id>/stop` & `stop_posix_group`: replace BOTH unbounded `child.wait()` calls in `workbench/jobs.py:132,134` with bounded `child.wait(timeout=5)` (raising `TimeoutError` on `TimeoutExpired` so `cleanup_failed` triggers deterministically and never hangs).
  - Accept optional `wait_seconds` (`0..15`, default `1.5`) and optional `operation_id` precondition on `/pause` and `/stop` (UI always sends observed `job.operation_id`). If `operation_id` is provided and does not match `record.get("operation_id")`, reject with HTTP `409 Conflict` RFC 9457 (`code="job_operation_mismatch"`, `context={expected_operation_id, current_operation_id, current_status}`, `fix="Re-read GET /api/jobs/<id> before acting"`).
  - After `run_event.wait(wait_seconds)`: (1) if `cleanup_failed` is observed (`status == "failed"` with `pid` retained), return HTTP `500` RFC 9457 `job_problem` (`code="job_stop_cleanup_failed"`, `instance`, `context={job_id, operation_id, pid, lock_path}`, safe OS inspection/termination `fix`, `recovery={action:"ask_human", side_effects:"unknown", remote_cancelled:false, max_retries:0}`, plus `job` snapshot extension) — never HTTP `200`; (2) if still `stopping`, return HTTP `202 Accepted` with `{status:"stopping", operation_id, poll_url:"/api/jobs/<id>", job:...}` (`common.js` `response.ok` accepts `200`/`202`); (3) if cleanly `stopped` (or idempotent replay on matching `operation_id`), return HTTP `200 OK`.
- [DX-H3] Agent-Ready RFC 9457 Error Contract Across All 6 New Routes + `/api/config` + `common.js`:
  - Add `job_problem(code, detail, status, *, fix, recovery=None, context=None, field=None, instance=None, **extensions)` in `workbench/server.py` wrapping `api_problem()` (`doc_anchor="docs/workbench.md#job-control-errors"`), and recursively redact every string inside `api_problem` `extensions` (nested dicts/lists) via `jobs.redact` so secrets never leak through extension fields.
  - Route `pause_job`, `stop_job`, `delete_job`, `open_job_folder`, `job_prefill`, `job_logs`, and `configure` (`/api/config`, including `field`) through `api_problem()` in `input_error`, `http_error`, `unexpected_error`, and `local_requests` (missing `X-Workbench: 1` / invalid `Origin` on these routes). Every unexpected `500` writes a redacted `correlation=<instance>` trace to `.workbench/jobs/<id>.log` (or `.workbench/server.log`) and returns `instance` + explicit `recovery.side_effects`.
  - `GET /api/jobs/<id>/prefill` distinguishes missing `inputs/request.json` (`404 job_prefill_missing`) from corrupt/invalid JSON (`400 job_prefill_corrupt` with file and parse line) and returns `references_omitted: true` + `omitted_reference_count`.
  - Upgrade `workbench/static/common.js:request()` to safely parse JSON (falling back to HTTP status + truncated response text for non-JSON 413/502 bodies instead of throwing `SyntaxError`), attach RFC 9457 metadata (`code`, `fix`, `field`, `recovery`, `instance`, `doc_anchor`) onto the thrown `Error`, and switch Settings Drawer to the tab owning `error.field` + focus that control on `POST /api/config` `400`.
- [DX-H4] Authoritative `unsettled_tasks()` Covering `kind=="one"`, Safe Delete Ordering, & `cleanup_failed` Ownership Check:
  - Implement a single authoritative `unsettled_tasks(job_dir)` helper shared by `Jobs.summaries()` (`has_unsettled_tasks`) and `DELETE /api/jobs/<id>`, scanning ALL 5 locations: (1) root `state.json` for `kind=="one"` (`task` without completed clip or `submission_unknown`), (2) storyboard `frames`, (3) `segments`, (4) `frame-edit.json`, and (5) `segment-edit.json`. On block, `DELETE` returns HTTP `400` RFC 9457 (`code="job_delete_unsettled_tasks"`, `context={job_id, status, blocking_items:[{kind, key_or_number, provider, state, task_id, file}]}`, concrete `fix` distinguishing `/resume` vs provider reconciliation, `recovery={action:"do_first", side_effects:"none", reconcile:"GET /api/jobs/<id>"}`).
  - `DELETE` removes `<id>.log` first (if Windows handle lock fails, returns `500 job_delete_io_error` with `side_effects: "none"`), then `<id>.json` (`side_effects: "partial"` on failure), then evicts memory cache last.
  - On startup (`Jobs.__init__`) and before active-job mutual-exclusion checks, verify worker ownership via `worker_locked(output_dir)` (not `os.kill(pid, 0)` alone). If `status == "failed"` with `cleanup_failed=True` and `not worker_locked(output_dir)`, clear stale `pid`, record `cleanup_resolved=True`, and persist so recycled OS PIDs never permanently block launches.
- [DX-H5] Generation-Tracked Bounded Incremental Logs + Consistent Field & Key Naming:
  - `GET /api/jobs/<id>/logs?offset=<n>&limit=<m>&generation=<g>`: validate `offset` as non-negative decimal integer (else `400 invalid_log_offset`, `field="offset"`, `fix="Pass offset=0"`); bound `limit` (`default=100000`, `max=262144`); emit a rotation-sensitive `generation` token (inode + first-line hash; equal-size replacement changes `generation`, triggering `reset: true` when mismatched or `offset > file_size`); redact complete lines BEFORE slicing and clamp `next_offset` to the last complete UTF-8 line boundary (`truncated: bool`, `reset: bool`, `generation`, `next_offset`).
  - `h3ref/colab_runtime.py`: allow `H3_COLAB_MAX_LOGS` env override (`default=200`, validated `20..2000`) and emit `logs_truncated: bool` per node in `/api/runtime/status`.
  - Unify naming across API & client: align `Jobs.summaries()` progress object (`progress: {frames, frames_total, clips, clips_total}` + `cost_cny`) with `Jobs.snapshot()`, unify `log_alert_seq` / `log_alert_counts` across jobs and runtime nodes, use `should_pause` consistently as the callback parameter in `h3ref/control.py`/`film.py`, name console persistence key `h3.consoleState.v1`, serialize absent optional `active_tasks` metrics as `null` (fixed schema), and support `?include_logs=0` on `GET /api/jobs/<id>` (default `1`).
- [DX-H6] 2-Click Offline Demo Onboarding + Short-Name Auto-Match Guard & Discoverability Hints:
  - Add 1-click **"Load Sample Script / 载入示例剧本"** beside `#film-json` (`fetch("/api/film/example")` directly into `#film-json` + auto-parse; retain Download Sample for offline editing). When `#film-json` is empty, clicking Cold-Start Banner's **"Try Offline Demo / 试离线演示"** switches to Demo, enables `demo-placeholders`, and loads the sample script so browser TTHW drops from ~9 steps to 2 clicks (`< 15s` post-launch, `< 2 min` end-to-end).
  - Filename auto-match restricts short cast names/aliases (`len(name) < 2`) to exact-stem or separator-boundary match (no arbitrary substring matching), provides an **"Undo auto-match / 撤销本次自动匹配"** action after batch drop, and includes `{filename}`, `{actual_mb} MiB / 28 MiB`, and compression hint in `> 28 MiB` Toasts.
  - Add in-UI discoverability hints: console toggle tooltip shows `⌘J / Ctrl+J`; dropzone placeholder shows filename match examples (`board.png`, `小林.jpg`).
- [DX-H7] Operator, Agent & Upgrade Documentation Deliverables + Automated Doc Smoke Test:
  - Stage 1 updates `docs/workbench.md` (`#first-offline-job` copy-paste runnable offline demo `curl` with `-H 'X-Workbench: 1'`, ID capture, bounded polling, expected artifact; `#job-control-errors` 6-route table + exit-75 table + paid-side-effect column; quiescent upgrade/rollback guide backing up BOTH `output_dir` AND `.workbench/jobs/*.json` and settling `pausing`/`stopping` before downgrade), `docs/workbench-cli-parity.md` (run-scoped `control.json` + exit 75), and `SKILL.md` (agent job-control & exit-75 rules).
  - Stage 3 updates `README.md` (new workbench layout + side-by-side 0.06s keyless `h3ref.py preflight` CLI and 2-click Workbench Offline Demo quickstart) and adds a supersession banner to `docs/design/workbench-ux-prototype.html`.
  - Add an automated test in `tests/test_workbench.py` executing the documented offline `First Offline Job` + job-control RFC 9457 error sequence end-to-end across both locales (`zh-CN`/`en`) so docs never drift.
- [DX-T1] Provisional Unsettled-Task Delete Escape Hatch (`d5`):
  - Keep `CEO-H4` `400` block by default for any unsettled task, plus an explicit non-active-only escape hatch `DELETE /api/jobs/<id>?force=1` requiring JSON body `{"acknowledge_unsettled": true}` (still NEVER deleting `output_dir` and still returning `400` for active/running jobs) for abandoned-account cleanup.
<!-- /autoplan-accepted:dx -->

### Step 0 — DX Investigation (0A–0G)

#### 0A. Product Type Classification
- **Primary**: Local Web Workbench HTTP API (`workbench/server.py`, Flask localhost `127.0.0.1:7860`) + Python CLI Tool (`h3ref.py`, `h3ref/*`).
- **Secondary**: Coding Agent Skill (`SKILL.md`, invoked by Claude Code / Codex / Antigravity to orchestrate `film.json` preflight, job creation, polling, and recovery).

#### 0B. Developer Persona Card & Developer Empathy Narrative

| Attribute | Primary Persona: Technical Creator / Indie AI Filmmaker | Secondary Persona: Coding Agent (Claude Code / Codex) |
|---|---|---|
| Role | Solo video creator / technical director running H3 locally or on Colab GPU | Automated agent operating `h3ref.py` CLI and `127.0.0.1:7860/api/*` via `SKILL.md` |
| Stack | Python 3.10+, Flask local web UI, ffmpeg, Google Colab / RunningHub / APIMart | `curl` / Python scripts parsing JSON, HTTP status codes, stderr, and exit codes |
| Primary goal | Go from a Chinese storyboard script to a zero-double-billing, inspectable `film.mp4` fast | Execute repeatable preflight, offline demo smoke runs, pause/stop/resume, and diagnose errors without human guesswork |
| Top anxiety | Accidental duplicate paid API charges, losing an in-flight paid `task_id`, or getting wedged by stale locks/processes | False-success HTTP 200 on failed commands, localized prose with no machine code/fix, or silent log truncation |

**Developer Empathy Narrative (First-Person):**
> I just cloned `h3-ref-video` because MiniMax H3 with per-shot storyboards costs a tenth of Seedance. I set up `.venv`, launched `workbench.py`, and landed on `http://127.0.0.1:7860`. Before putting my APIMart or RunningHub credit card on the line, I want to verify the pipeline offline in two minutes. I click "Try Offline Demo", expecting to hit Start immediately — instead the Launch button stays greyed out because the script textarea is empty, forcing me to download `film.example.json` to `~/Downloads` and upload it right back. Once I run a real 10-shot film and realize shot 4 has the wrong wardrobe, I hit "Pause" — trusting no new paid clip will fire — only to worry whether a RunningHub capacity retry sleeping for 60 seconds is about to submit anyway, or whether dropping back to the CLI (`python h3ref.py film jobs/my-film`) will immediately abort with exit 75 because of a leftover `inputs/control.json`. When my automation script calls `POST /api/jobs/<id>/open-folder` on a headless box or `POST /api/jobs/<id>/stop` during a cleanup failure, getting HTTP `200 OK` back makes my script blindly proceed as if the action succeeded. I want zero-guesswork 2-click onboarding, explicit run-scoped control boundaries, and RFC 9457 errors that tell me the exact `code`, `fix`, `recovery`, and billing side effects on the first read.

#### 0C. Competitive DX Benchmark & TTHW Assessment

| Product | Install / Setup | Time to First Result (TTHW) | Local Control & Error DX | Source / Benchmark Basis |
|---|---|---|---|---|
| **ComfyUI Desktop** | Native installer (`.app`/`.exe`/AppImage), sandboxed Python | 5–10 min (mostly model weights); instant on bundled templates | Graph UI; raw JSON node tracebacks on failures | Live web benchmark (`comfy.org` / community reports, 2026) |
| **Automatic1111 SD WebUI Forge** | Git clone + Python 3.10 + `webui-user` venv script | 10–20+ min (venv/torch/CUDA pitfalls); immediate form submit once up | Classic tabbed Gradio UI; unstructured console tracebacks | Live web benchmark (`openjourney.art` / `fedoramagazine.org`, 2026) |
| **RunningHub / Replicate CLI** | API key + hosted web/CLI call | 2–4 min (requires account + paid credits immediately) | Structured JSON API errors; zero local offline sandbox | Repo `README.md` + standard hosted API DX |
| **`h3-ref-video` (Current)** | `.venv` + `pip install -r requirements-web.txt` (Windows: 1-click `start-windows.cmd`) | **CLI preflight: 0.06s** (verified); **Workbench Offline Demo: ~5–8 min (~9 steps)** due to download/re-import loop | `api_problem()` RFC 9457 exists on 4 routes, absent on 6 new routes | Observed in repo (`README.md:60-88`, `workbench/server.py:283-348`) |
| **`h3-ref-video` (Target after DX fixes)** | Same `.venv` / Windows 1-click | **CLI preflight: 0.06s**; **Workbench Offline Demo: < 2 min end-to-end (< 15s / 2 clicks post-launch)**; **Live first clip: < 5 min** | Full `agent-ready-v1` RFC 9457 across all job control routes + run-scoped exit 75 | `DX-H1..DX-H7` |

- **TTHW Assessment**: Observed **`8 min`** (clean checkout + manual venv + 9-step film demo download/re-import) → Target **`< 2 min`** (offline demo; `< 15s` / 2 clicks once server is open) and **`< 5 min`** (first live generation with existing API keys). Time to playable `film.mp4` (requires `ffmpeg` + storyboard review click) is tracked separately from time to first storyboard/report.

#### 0D. Magical Moment Specification
- **Lowest-effort delivery vehicle**:
  1. **Creator web flow**: Cold-start banner's **"Try Offline Demo / 试离线演示"** auto-enables Demo + placeholders + loads `examples/film.example.json` in one click (`DX-H6`), so click #2 ("Start") lands the user on the live Results page with real storyboard cards, logs, and pause/resume controls in `< 15s` — zero keys, zero billing, zero file round-trip.
  2. **Agent / CLI flow**: Side-by-side README quickstart pairing `python h3ref.py preflight --film examples/dialogue-budget.json --json` (`0.06s`, zero server/keys) with the runnable offline Workbench `curl` job-control recipe (`DX-H7`).

#### 0E. Mode Selection
- **Selected Mode**: `DX POLISH` (per Autoplan Phase 2.5 override rule; existing local workbench & CLI are mature with RFC 9457 foundations in `workbench/server.py`, requiring surgical hardening of error contracts, run-scoped pause/CLI safety, onboarding, and docs without over-engineering).

#### 0F. Developer Journey Map (6-Stage Table)

| Stage | Current Plan Experience | Friction / Failure Mode | DX Fix (`DX-H*`) | Target Experience |
|---|---|---|---|---|
| 1. **Discover** | `README.md` describes old single-page workbench layout; new controls & APIs undocumented | Dev cannot tell how offline demo, pause/stop, or agent APIs work in < 2 min | `DX-H7` | `README.md` pairs 0.06s CLI `preflight` + 2-click Workbench Demo + links `#first-offline-job` & `#job-control-errors` |
| 2. **Install** | `.venv` + `requirements-web.txt` (or `start-windows.cmd` on Windows); zero-key demo exists | Clean-install timing unmeasured; missing `ffmpeg` only noticed late at assembly | `DX-H7` | Documented install + explicit distinction between report/storyboard TTHW (`<2m`) and `ffmpeg` playable video |
| 3. **Hello World** | Click "Try Offline Demo" → `#start-job` still disabled → download JSON → re-import JSON → launch (~9 steps) | Download-then-upload round-trip breaks momentum and hides magical moment | `DX-H6` | 1-click "Try Offline Demo" loads bundled sample script → 2 clicks to live storyboard results (`<15s`) |
| 4. **Real Usage** | Batch drop auto-match, Pause/Stop, Clone, Open Folder | Short Chinese names (`林`) mis-match substrings; pause during 60s RunningHub backoff can still POST; delayed retry can stop newer run | `DX-H1`, `DX-H2`, `DX-H6` | Safe exact/boundary match + Undo; zero paid POSTs after pause; `operation_id` `409` guard; tooltips |
| 5. **Debug** | 6 new HTTP routes return bare `{error}`; `/open-folder` & `/stop` return HTTP 200 on failure; `/logs` lacks generation/bounds; leftover `control.json` traps CLI at exit 75 | `CAP4` false-success breaks agents; `CAP6` lacks fix/recovery/trace; CLI resume broken after workbench pause | `DX-H1`, `DX-H2`, `DX-H3`, `DX-H5` | Full `agent-ready-v1` RFC 9457 with `instance` traces; run-scoped `control.json`; generation-tracked `/logs` |
| 6. **Upgrade** | Docs only mention backing up `output_dir`; `CEO-H4` misses `kind=="one"` and has no escape hatch; `cleanup_failed` PID reuse blocks launches | Rollback strands `pausing`/`stopping` records in `.workbench/jobs/*.json`; `one` paid tasks deletable; recycled PID wedges workbench | `DX-H4`, `DX-H7`, `DX-T1` | Quiescent backup/rollback guide (incl. `.workbench/jobs/*.json`); unified `unsettled_tasks()`; lock-verified PID cleanup; `d5` escape hatch |

#### 0G. First-Time Developer Confusion Report (`T+0:00` – `T+3:00`)
- **`T+0:00` (Open `README.md`)**: Reads old description of top-of-page Settings & Colab cards; sees no mention of Pause/Stop, `/api/jobs/<id>/logs`, or exit code `75`.
- **`T+0:45` (Click "Try Offline Demo")**: Mode flips to Demo, yet Launch button remains disabled with empty `#film-json`. Developer asks: *"Why do I have to download a JSON file just to upload it back to the same page?"* (`DX-H6` eliminates this completely).
- **`T+1:30` (Test `curl -X POST /api/jobs/<id>/pause` from terminal)**: Forgets `-H 'X-Workbench: 1'` or calls on a settled job and receives bare `{"error": "..."}` with no `code`, no `fix`, and no link to `docs/workbench.md`. (`DX-H3` fixes via `job_problem` RFC 9457).
- **`T+2:15` (Pause a job in Workbench, then resume via CLI `python h3ref.py film jobs/demo`)**: CLI immediately aborts with exit code `75` because `inputs/control.json` from the workbench run was left on disk. (`DX-H1` scopes `control.json` to `H3_WORKBENCH_WORKER=1` + `H3_WORKBENCH_OPERATION_ID`).
- **`T+2:45` (Call `POST /api/jobs/<id>/open-folder` over SSH / headless Linux)**: Gets HTTP `200 {"opened": true}` even though no desktop session exists and nothing opened. (`DX-H2` eliminates `CAP4` false-success and returns `400 open_folder_headless` with `context.path`).

---

### Step 0.5 — Dual Voices (DX)

#### agy (in-host) SUBAGENT (DX — independent review)
- **Conversation ID**: `7ef2bf24-7076-47ae-b1a5-52fa64c89cfe` (`native_s = 481`, verified header `INPUT: dx cf502530250ab56e1f823eca3956ade9807fc823115fa01df03e1c144909060a`).
- **Scores**: Getting Started `6/10`, API/CLI Ergonomics `6/10`, Error Handling **`4/10 (CAP4)`**, Documentation `4/10`, Escape Hatches `6/10`.
- **Key findings (15 total: 2 Critical `CAP4`, 8 High incl. 5 `CAP6`, 5 Medium)**:
  1. `CAP4`: `POST /api/jobs/<id>/open-folder` uses macOS `check=False` and Linux `Popen` without `DISPLAY`/`WAYLAND_DISPLAY` or `stdin=DEVNULL`, returning HTTP `200 {"opened": true}` on failure.
  2. `CAP4`: `POST /api/jobs/<id>/stop` returns HTTP `200` snapshot even when `stop_child` throws inside the 1.5s wait (`cleanup_failed=True`).
  3. `CAP6` (#3–#6): All 6 new endpoints (`/pause`, `/stop`, `DELETE`, `/open-folder`, `/prefill`, `/logs`) bypass existing `workbench/server.py:283` RFC 9457 `api_problem()`, lacking `code`, `doc_anchor`, `fix`, `recovery`, `context`, and `500` correlation `instance` traces; `/logs` lacks `offset` validation, byte limit, and UTF-8/line-aligned redaction; `/prefill` conflates 400/404.
  4. `CAP6` (#7): Leftover `inputs/control.json` traps direct CLI resume (`python h3ref.py film <out>`) at exit `75`; exit 75 logs to `stdout` instead of `stderr` without structured recovery diagnostics; corrupt `control.json` unhandled.
  5. High (#8–#10): "Try Offline Demo" still requires download-then-reimport (~9 steps); Stage 1–3 omit all documentation files (`docs/workbench.md`, `README.md`, `docs/workbench-cli-parity.md`); `CEO-H4` hard delete block lacks a `?force=1` escape hatch for abandoned/dead provider accounts.
  6. Medium (#11–#15): Field/key drift (`progress_summary` vs `progress`, `alert_seq` vs `log_alert_seq`, `should_stop` vs `should_pause`, unnamed console `localStorage` key); hardcoded `1.5s` stop wait & `MAX_SLOT_LOGS=200` & mandatory 100KB `logs` in `GET /api/jobs/<id>`; `common.js:55` raw `response.json()` crash & hidden-tab server `400` not switching tabs; short-name `<2` char substring auto-match false positives; `active_tasks` null schema & `/prefill` missing `omitted_reference_count`.

#### Codex SAYS (DX — developer experience challenge)
- **Execution**: `OUTSIDE_STATUS: completed provider=codex host=agy` (`model: gpt-6.2-astra`, `outside_s = 288`, `VERDICT: findings`, `FINDINGS: P0`).

```tool-output
**DX scores for the proposed release**

| Dimension | Score | Assessment |
|---|---:|---|
| Time to hello world | 6/10 | The keyless CLI example works. The documented film demo still requires a download and re-import before the first storyboard. |
| Agent-ready errors | 4/10 | Two control endpoints can report known failure as HTTP success. The six new routes lack a common recovery contract. |
| API and CLI design | 5/10 | The route names are guessable. A stale control file can break direct CLI resume, and a delayed HTTP retry can affect a newer run. |
| Documentation and discovery | 4/10 | Existing preflight documentation has a runnable example. The implementation stages omit the user and agent guide for the new controls. |
| Upgrade and escape hatches | 4/10 | The plan preserves output files, but single-clip recovery, failed cleanup, and rollback with new states need explicit handling. |

**CAP4 applies** to error handling. `/open-folder` can return `opened:true` after an opener failure, and `/stop` can return 200 after observing cleanup failure. **CAP6 also applies independently** to missing applicable fix, recovery, context, and trace information. The lower ceiling determines the score. Exit 75 itself is a valid nonzero signal. A successful GET reporting a failed job is also valid. The false-success cases below concern the command that just failed.

### 1. Pause can still dispatch new paid work

Severity: Critical

CEO-H2 places the check before `submit_with_retry` and before `frames.draw`. RunningHub can reject a submission for capacity, clear the uncertainty marker, sleep for 60 seconds, then issue another POST. A pause during that sleep leaves **no accepted task to drain**, yet the retry can create one after the UI says dispatch has stopped. APIMart likewise uploads references before reaching its actual generation POST. See `h3ref/rh.py:118–137`, `h3ref.py:89–103`, and `h3ref/frames.py:267–280`.

Recommendation: Check the run-scoped pause signal immediately before every fresh provider submission, including capacity retries and the post-upload APIMart callback, because the billing boundary is the outbound generation request. Test a pause during backoff and during upload, asserting zero subsequent generation POSTs and continued retrieval for an already accepted task.

### 2. Stop can report success on cleanup failure or never reach a terminal state

Severity: High

CEO-E4 wakes the HTTP waiter when cleanup fails and persists `status="failed"`, yet specifies `200` plus the snapshot. An agent using `curl --fail-with-body` would take its success branch even when the server already knows Stop failed. Separately, the 1.5-second wait bounds only the event wait. The existing cleanup helper has unbounded `child.wait()` calls, including after a signal permission error. A helper that never returns never enters the planned exception handler. Building the full response snapshot also adds disk I/O after the wait. See `workbench/jobs.py:123–145`, `workbench/jobs.py:227–252`, and `workbench/jobs.py:1049–1055`.

Recommendation: Return a non-2xx problem for cleanup failure already known to the POST, bound each termination wait, and return a lightweight acknowledgement with the operation ID and polling URL for ongoing cleanup, because agents need to distinguish accepted, stopped, and failed without waiting indefinitely. Test denied termination with a child still alive and a client polling with a deadline.

### 3. `open-folder` promises more than the OS confirms

Severity: High

CEO-H3 specifies `check=False` on macOS followed by `{"opened":true}`. A nonzero `open` exit is observable failure. On headless Linux, finding and spawning `xdg-open` establishes only that the opener started. The repository’s existing folder picker already checks timeout and return code explicitly at `workbench/directories.py:94–103`.

Recommendation: Return a non-2xx problem on observed opener failure, describe asynchronous success as `dispatched`, and give headless clients the registered output path as recovery, because an agent cannot verify that a desktop window appeared. Test a nonzero macOS exit and a Linux host with an opener but no desktop session.

### 4. The six endpoints do not inherit agent-ready errors automatically

Severity: High

`api_problem()` already supports `code`, `doc_anchor`, `instance`, and extensions. Routing determines who gets it. Most `InputError`s fall back to localized `{"error":…}`, and unexpected failures get correlation traces only for explicitly named endpoints. The helper redacts `detail`; arbitrary extension values pass through. Missing `X-Workbench: 1` can also fail before the handler runs. See `workbench/server.py:258–264`, `workbench/server.py:283–319`, and `workbench/server.py:321–348`.

| Proposed route | Failure an agent must distinguish | Required fix or recovery |
|---|---|---|
| `POST /pause` | Already settled vs. another run now active | Read the current operation and state before acting again |
| `POST /stop` | Cleanup pending vs. cleanup failed with worker ownership unresolved | Poll the operation or inspect the correlated cleanup failure |
| `DELETE /jobs/<id>` | Active worker vs. retrievable task vs. unknown submission | Identify the blocking item and its safe recovery action |
| `POST /open-folder` | Output missing vs. desktop opener unavailable | Return the appropriate filesystem or headless fallback |
| `GET /prefill` | Unknown job vs. missing or corrupt saved request | Identify the affected input and whether a draft can be recovered |
| `GET /logs` | Invalid cursor vs. unknown job vs. rotated log | Reject malformed input and specify how to reset a valid stale cursor |

Recommendation: Route all six endpoints and their request-validation failures through `application/problem+json`, because localized prose cannot support reliable branching. Preserve `error=detail`, include a stable code and working documentation anchor, supply an actionable fix, retry and billing consequences, and allowlisted job/item/operation context. Correlate unexpected failures with a redacted local trace. Test the HTTP status, content type, both locales, and secret removal from every returned field.

### 5. A stopped workbench job can trap direct CLI resume at exit 75

Severity: High

The proposed `should_pause(job_dir)` accepts any pause or stop file by action alone. Only `Jobs.launch()` clears it. A creator following the documented direct `film JOB_DIR` resume path can therefore hit the previous run’s signal repeatedly. Blindly unlinking the file could erase a legitimate worker’s request. The repository already distinguishes workbench workers from direct CLI invocations and preserves separate pending-edit guards. See `h3ref.py:4–7`, `h3ref.py:200–207`, `h3ref/film.py:1344–1368`, and `workbench/jobs.py:1035–1040`.

Recommendation: Pass the active operation ID to each managed worker and honor only its matching control file, because a historical stop belongs to one execution. Preserve direct CLI lock and pending-edit checks. For a genuine cooperative pause, emit a stable `job_paused` diagnostic and recovery location on stderr, exit 75 without prompting, and document that automatic retry can resume paid work. Test both managed exit 75 and standalone same-directory resume with a stale file, capturing stdout, stderr, and exit status separately.

### 6. A delayed retry can stop the wrong run

Severity: High

The plan scopes the internal event to `operation_id`, but the public pause and stop commands target only the persistent job ID. If a client loses the response for run A and a creator resumes the job as run B, retrying A’s Stop can terminate B. Idempotence while the record says `stopping` does not cover this sequence. The current restart path already issues a fresh operation ID at `workbench/jobs.py:776–785`, and snapshots expose record fields at `workbench/jobs.py:234–239`.

Recommendation: Have clients send the observed operation ID as a precondition and reject a stale command with a conflict problem, because a transport retry must not acquire authority over a later run. Test a lost Stop response, resume, and replay of the original Stop.

### 7. The paid-task delete guard can miss the simplest job type

Severity: High

CEO-H4 names storyboard frames, segments, and edit files. A `kind=="one"` job has empty storyboard and segment arrays. Its paid task and `submission_unknown` live at the root of `state.json`. Scanning the named collections can enable Delete on precisely the record needed for recovery. The existing docs already describe provider verification and backups, but the proposed 400 does not direct an agent to the specific blocked item. See `workbench/jobs.py:260–271`, `h3ref.py:89–111`, and `docs/workbench.md:124–138`.

Recommendation: Use one authoritative unsettled-task check for the history summary and DELETE, including root single-clip state and staged edits, because disabling a button cannot protect direct HTTP callers. Return the provider, item, known task ID if available, and verification instructions. Test both a known task without a downloaded clip and an unknown submission. Keep the guard until retrieval or verified reconciliation.

### 8. Under-five-minute onboarding is demonstrated only for the CLI check

Severity: Medium

I ran `.venv/bin/python -B h3ref.py preflight --film examples/dialogue-budget.json --json` in the existing environment. It returned the documented `partial` result with two warnings in about **0.06 seconds**. That verifies a useful one-command CLI result after setup. From a source checkout with Python available, the documented film path takes roughly **nine actions** to first storyboard: create the venv, install dependencies, start Flask, open the URL, select Demo, download the sample, import it, launch, and inspect the result. A playable film adds review confirmation and requires ffmpeg/ffprobe. No clean-install time was measured. See `README.md:60–71`, `README.md:82–88`, `docs/workbench.md:8–27`, and `workbench/demo.py:106–110`.

Recommendation: Make “Try Offline Demo” launch or populate a bundled example and put the existing CLI preflight command beside it in the README, because both creators and agents need a recognizable first result before configuring a provider. The existing `one` demo accepts a prompt with zero images at `workbench/inputs.py:78–88`. Time a clean install to the first report and record time to playable media separately.

### 9. The plan updates implementation and UI tests but leaves the operator guide behind

Severity: Medium

None of the three implementation stages lists `README.md`, `docs/workbench.md`, or the CLI parity guide as a changed deliverable. The current guide sends users to controls that will move. Its preflight curl example is runnable, while its segment examples deliberately use placeholder IDs and versions and warn that the POST incurs charges. Neither teaches an agent to create a free job, capture its ID, poll through the new terminal states, or interpret exit 75. See `docs/workbench.md:22–31`, `docs/workbench.md:106–114`, `docs/workbench.md:183–213`, and `workbench/server.py:740–770`.

Recommendation: Ship a README-linked “First offline job” and “Job control and recovery” section with the backend change, because the two-minute discovery target depends on published user documentation. Include a copy-paste demo request with the required header, captured ID, bounded polling, expected artifact, six-route table, exit-code table, and a paid-side-effect column. Execute the example as a documentation check.

### 10. A byte offset alone cannot reliably identify a rotated log

Severity: Medium

CEO-E9 promises rotation recovery using only `offset`. If an old log is replaced by a different file of equal or greater length, the offset still looks valid and the client silently skips new diagnostics. The contract also leaves first-read size and arbitrary offsets inside UTF-8 characters or redaction matches unspecified. The existing snapshot bounds reads at 100,000 bytes, the normal worker writer redacts child output line by line, and response-time redaction relies on whole-string replacement. See `workbench/jobs.py:182–187`, `workbench/jobs.py:243–247`, and `workbench/jobs.py:1049–1052`.

Recommendation: Return a log generation identifier with a bounded next cursor, define reset and truncation behavior, and redact complete records before slicing, because clients need loss-detectable, UTF-8-safe replay. Test equal-size rotation, a multibyte line, a legacy sensitive line, invalid offsets, and backlog larger than one response. Expose truncation explicitly for the 200-line node history as well.

### 11. Upgrade and rollback omit the job registry and failed-cleanup lock

Severity: High

The guide tells operators to back up output directories. The separately stored `.workbench/jobs/*.json` records carry the IDs needed to reach those outputs through the API. Older code recovers and recognizes only `queued` and `running` as active, so rolling back with a `pausing` or `stopping` record can leave an old client unable to interpret it. In the forward direction, the plan retains a PID on `cleanup_failed` but exempts only `interrupted` and `stopped` from stale-PID checks on POSIX. After the worker lock releases, a recycled PID can keep unrelated work blocked. See `docs/workbench.md:211–213`, `workbench/jobs.py:166–179`, and `workbench/jobs.py:451–471`.

Recommendation: Document a quiescent upgrade and rollback that backs up the registry alongside outputs, and provide a cleanup diagnostic that verifies worker ownership before releasing a stale record, because process existence alone cannot establish ownership. Test an old registry without operation IDs, a downgrade with a transient state, and a failed cleanup followed by lock release and PID reuse.

VERDICT: FINDINGS
```

#### DX DUAL VOICES — CONSENSUS TABLE

```
DX DUAL VOICES — CONSENSUS TABLE:
  Dimension                           agy (in-host)                  Codex                          Consensus
  1. Getting started < 5 min?         6/10 (Demo download/re-import) 6/10 (9-step film demo)        CONFIRMED (fixed by DX-H6)
  2. API/CLI naming guessable?        6/10 (CLI control.json trap)   5/10 (CLI trap + pause retry + op_id) CONFIRMED (fixed by DX-H1/H2/H5)
  3. Error messages actionable?       4/10 CAP4 (/open-folder,/stop) 4/10 CAP4 (/open-folder,/stop) CONFIRMED P0 CAP4 (fixed by DX-H2/H3)
  4. Docs findable & complete?        4/10 (0 docs in Stages 1-3)    4/10 (operator/agent docs omitted) CONFIRMED (fixed by DX-H7)
  5. Upgrade path safe?               6/10 (needs DELETE ?force=1)   4/10 (registry rollback, one-guard, NO force) DISAGREE -> taste (d5); bugs fixed by DX-H4/H7
  6. Dev environment friction-free?   6/10 (common.js JSON/hardcode) 5/10 (child.wait hang, log gen) CONFIRMED (fixed by DX-H2/H3/H5)
CONFIRMED = native + outside agree; primary cannot replace outside. DISAGREE -> taste.
Single-voice critical finding flagged: Codex #1 (Pause during RunningHub 60s backoff / APIMart upload can still POST paid work) -> verified in h3ref/rh.py:118-137 & fixed in DX-H1.
```

---

### Passes 1–8 (Scored Before -> After Fixes)

#### Pass 1: Getting Started & Onboarding (`6/10 -> 9/10`)
- **Gap**: Cold-start banner added "Try Offline Demo", yet `#film-json` starts empty and `#start-job` disabled (`README.md:71`), forcing a 9-step download-then-reupload loop before the first storyboard (`~5–8 min`).
- **Fix (`DX-H6`, `DX-H7`)**: Add 1-click **"Load Sample Script"** button (`fetch("/api/film/example")` into `#film-json`), auto-load bundled sample script when "Try Offline Demo" is clicked with an empty script (2 clicks / `< 15s` post-launch), and pair the verified `0.06s` keyless CLI `h3ref.py preflight` beside Workbench Demo in `README.md`.

#### Pass 2: API / CLI / SDK Ergonomics (`5/10 -> 9/10`)
- **Gaps**:
  1. Single-voice Critical (`Codex #1`, verified at `h3ref/rh.py:118-137`): `submit_with_retry()` sleeps `60s` on `QueueFull` (421/415) and retries `submit(g)` without re-checking pause; APIMart uploads references before POSTing generation.
  2. Dual-voice High (`Native #7`, `Codex #5`): `inputs/control.json` is un-scoped, trapping direct CLI `python h3ref.py film <out>` resume at exit `75`.
  3. High (`Codex #6`): delayed HTTP retry of `/pause` or `/stop` after a user resumes as run B can terminate run B.
  4. Medium (`Native #11, #12, #15`, `Codex #10`): naming drift (`progress_summary` vs `progress`, `alert_seq` vs `log_alert_seq`, `should_stop` vs `should_pause`, unnamed console `localStorage` key), missing `generation` token & bounds on `/logs`, absent `null` schema on `active_tasks` & `omitted_reference_count` on `/prefill`, hardcoded `wait_seconds`/`MAX_SLOT_LOGS` & mandatory 100KB `logs` in `GET /api/jobs/<id>`.
- **Fix (`DX-H1`, `DX-H2`, `DX-H5`)**: Scope `control.json` to `H3_WORKBENCH_WORKER=1` + `H3_WORKBENCH_OPERATION_ID`; check pause inside `submit_with_retry()` (1s sleep slices) and post-upload APIMart; support optional `operation_id` (`409` conflict) and `wait_seconds` (`0..15`) on `/pause` and `/stop`; add `generation` token + bounded line-aligned `/logs`; unify naming, `active_tasks` `null` schema, `omitted_reference_count`, `H3_COLAB_MAX_LOGS`, and `?include_logs=0`.

#### Pass 3: Error Messages & Debugging — `agent-ready-v1` (`4/10 [CAP4] -> 9/10`)

**Pre-Fix Caps Identified (Both Voices Confirmed):**
- **`CAP4:POST /api/jobs/<id>/open-folder:channel` (Critical)**: Plan specified macOS `check=False` and Linux `Popen` without checking `DISPLAY`/`WAYLAND_DISPLAY` or exit code, returning HTTP `200 {"opened": true}` on failure.
  - *Corrected error response (HTTP 400 headless Linux, `application/problem+json`)*:
    `{"type":"urn:workbench:error:open_folder_headless","title":"Cannot open output folder","status":400,"detail":"No desktop session (DISPLAY/WAYLAND_DISPLAY unset) to open /abs/jobs/20261126-demo.","error":"No desktop session (DISPLAY/WAYLAND_DISPLAY unset) to open /abs/jobs/20261126-demo.","code":"open_folder_headless","doc_anchor":"docs/workbench.md#job-control-errors","fix":"Open the folder locally or run: ls -la '/abs/jobs/20261126-demo'","recovery":{"action":"ask_human","side_effects":"none","max_retries":0},"context":{"job_id":"job_123","os":"linux","path":"/abs/jobs/20261126-demo","has_display":false}}`
- **`CAP4:POST /api/jobs/<id>/stop:channel` (Critical)**: Plan returned HTTP `200` snapshot when `stop_child` failed within 1.5s (`cleanup_failed=True`).
  - *Corrected error response (HTTP 500 `cleanup_failed`, `application/problem+json`)*:
    `{"type":"urn:workbench:error:job_stop_cleanup_failed","title":"Local worker cleanup failed","status":500,"detail":"Could not terminate local worker pid=48210 for job job_123 (correlation=9f2c...); remote provider tasks were not cancelled.","error":"...","code":"job_stop_cleanup_failed","doc_anchor":"docs/workbench.md#job-control-errors","instance":"9f2c8a1e...","fix":"Verify pid 48210 and lock /abs/jobs/demo/inputs/worker.lock locally (ps -p 48210), terminate if still running, then poll GET /api/jobs/job_123.","recovery":{"action":"ask_human","side_effects":"unknown","remote_cancelled":false,"reconcile":"GET /api/jobs/job_123","max_retries":0},"context":{"job_id":"job_123","operation_id":"op_7","pid":48210,"lock_path":"/abs/jobs/demo/inputs/worker.lock"}}`
- **`CAP6` (5 High paths)**: All 6 new endpoints (`/pause`, `/stop`, `DELETE`, `/open-folder`, `/prefill`, `/logs`) bypassed `workbench/server.py:283` `api_problem()`, missing `fix`, `recovery`, `context`, and `500` `trace` (`instance`); `h3ref.py` exit `75` logged to `stdout` without structured `code`/`fix`/`recovery`/`context`.

**ERROR PATH MATRIX (`agent-ready-v1`, Post-Fix Verification via `DX-H1..DX-H5`):**

| Traced Error Path | Class | what | fix | recovery | code+anchor | context | trace | redaction | channel | Post-Fix Status |
|---|---|---|---|---|---|---|---|---|---|---|
| 1. `POST /api/jobs/<id>/open-folder` (macOS non-zero exit / headless Linux / missing dir) | Env/Dep | PASS (`os`, `path`, exit code) | PASS (copy-paste local open cmd) | PASS (`ask_human`, `none`, `max_retries:0`) | PASS (`open_folder_*`, `#job-control-errors`) | PASS (`job_id`, `os`, `path`, `has_display`) | PASS (`instance` -> `server.log` on 500) | PASS (recursive `jobs.redact`) | PASS (HTTP 400/404/500 `problem+json`) | **PASS (CAP4 cleared)** |
| 2. `POST /api/jobs/<id>/stop` (`cleanup_failed` / `409` stale `operation_id` / `202` pending) | Partial/Unexpected | PASS (`job_id`, `pid`, `operation_id`) | PASS (OS check `ps -p` / poll URL) | PASS (`side_effects: unknown`, `remote_cancelled: false`, `reconcile`) | PASS (`job_stop_cleanup_failed`, `job_operation_mismatch`) | PASS (`job_id`, `operation_id`, `pid`, `lock_path`) | PASS (`instance` -> `jobs/<id>.log`) | PASS (recursive `jobs.redact`) | PASS (HTTP 500/409 `problem+json`; `202` pending) | **PASS (CAP4 cleared)** |
| 3. `DELETE /api/jobs/<id>` (`400` unsettled tasks incl. `kind=="one"` / `500` partial I/O) | Validation/Unexpected | PASS (lists exact `blocking_items`) | PASS (`/resume` vs provider verify) | PASS (`do_first`, `none` on 400 / `partial` on `.json` I/O fail) | PASS (`job_delete_unsettled_tasks`, `job_delete_io_error`) | PASS (`job_id`, `status`, `blocking_items`) | PASS (`instance` -> `server.log` on 500) | PASS (recursive `jobs.redact`) | PASS (HTTP 400/404/500 `problem+json`) | **PASS (CAP6 cleared)** |
| 4. `GET /api/jobs/<id>/logs` (`400` invalid `offset`) & `GET /prefill` (`404`/`400`) | Validation/Unexpected | PASS (bad `offset` / missing vs parse line) | PASS (`offset=0` / re-create or repair JSON) | N/A on 400 validation; PASS on 500 (`none`) | PASS (`invalid_log_offset`, `job_prefill_*`) | PASS (`field`, `job_id`, `path`) | PASS (`instance` on 500) | PASS (redact full lines before slice) | PASS (HTTP 400/404/500 `problem+json`) | **PASS (CAP6 cleared)** |
| 5. `h3ref.py` managed worker `JobPaused` (`EXIT_CODE_PAUSED = 75`) & corrupt `control.json` | CLI Partial | PASS (control path + `operation_id`) | PASS (exact `/resume` or CLI cmd, paid note) | PASS (`side_effects: partial`, `task_id` saved) | PASS (`JOB_PAUSED`, `#job-control-errors`) | PASS (`job_dir`, `operation_id`, stage) | N/A (expected pause); warning+exc on corrupt JSON | PASS (no secrets) | PASS (`sys.stderr` + exit `75`, no prompt) | **PASS (CAP6 cleared)** |

#### Pass 4: Documentation & Learning (`4/10 -> 9/10`)
- **Gap**: Zero documentation files appeared in Stages 1–3; `README.md`, `docs/workbench.md`, `docs/workbench-cli-parity.md`, and `SKILL.md` would become stale immediately, and no runnable offline curl walkthrough or `#job-control-errors` anchor existed.
- **Fix (`DX-H7`, `DX-H6`)**: Ship `docs/workbench.md` (`#first-offline-job`, `#job-control-errors`, 6-route & exit-75 tables with paid-side-effect column), `docs/workbench-cli-parity.md`, `README.md`, `SKILL.md`, prototype supersession note, in-UI shortcut/filename tooltips, and an automated test executing the documented offline workflow.

#### Pass 5: Upgrade & Migration Path (`5/10 -> 9/10`)
- **Gaps**:
  1. `docs/workbench.md:211-213` only warns to back up `output_dir`, omitting `.workbench/jobs/*.json` whose new transient statuses (`pausing`, `stopping`) older code cannot recover on rollback (`Codex #11`).
  2. `CEO-H4` missed `kind=="one"` root `state.json` (`Codex #7`).
  3. `cleanup_failed` retained `pid` without lock-based ownership check on POSIX, risking permanent wedge upon PID reuse (`Codex #11`).
  4. **Consensus Disagreement -> Taste Decision (`d5`)**: Native DX (`#10`) recommends a non-active-only `DELETE /api/jobs/<id>?force=1` + `{"acknowledge_unsettled": true}` escape hatch (never deleting `output_dir`) so abandoned-account records never deadlock the history list; Codex DX (`#7`) recommends keeping the guard strictly non-bypassable until `state.json` is reconciled.
- **Fix (`DX-H4`, `DX-H7`, `DX-T1`)**: Document quiescent upgrade & rollback backing up both `output_dir` and `.workbench/jobs/*.json`; unify `unsettled_tasks()` across all 5 state locations including `kind=="one"`; clear stale `cleanup_failed` `pid` whenever `not worker_locked(output_dir)`; adopt Option A (`?force=1` + explicit ack, non-active only, zero disk deletion) provisionally as `d5` and present both perspectives at Phase 4 Gate.

#### Pass 6: Developer Environment & Tooling (`6/10 -> 9/10`)
- **Gaps**: Unbounded `child.wait()` in `workbench/jobs.py:132,134` could hang tests and local dev workers indefinitely; `common.js:55` crashed with `SyntaxError` on non-JSON HTTP errors and didn't switch tabs on server-side `/api/config` `400`; `MAX_SLOT_LOGS=200` and `1.5s` stop wait were hardcoded; `/api/jobs/<id>` always shipped up to 100KB `logs`.
- **Fix (`DX-H2`, `DX-H3`, `DX-H5`)**: Bound every `child.wait(timeout=5)`; upgrade `common.js:request()` with safe JSON parsing + RFC 9457 error properties + Settings Drawer `field` tab jump; add `H3_COLAB_MAX_LOGS` (`20..2000`), `wait_seconds` (`0..15`), and `?include_logs=0`.

#### Pass 7: Community, Ecosystem & Agent Skill Integration (`7/10 -> 9/10`)
- **Claude Code / Agent Skill DX Checklist Audit (`SKILL.md`)**:
  - `[x]` **Agent-readable errors**: All 6 new routes + CLI exit `75` meet `agent-ready-v1` (`DX-H1..DX-H3`).
  - `[x]` **Bounded autonomy & paid-work guard**: Pause checked before every RunningHub capacity retry and post-upload APIMart POST (`DX-H1`); `operation_id` `409` prevents stale retries from killing newer runs (`DX-H2`); paid-side-effect column explicitly documented (`DX-H7`).
  - `[x]` **Error recovery & session continuity**: Partial assets & accepted `task_id`s preserved on exit `75`; generation-tracked incremental logs survive equal-size rotation (`DX-H5`).
  - `[x]` **Skill docs synced**: `SKILL.md` updated in Stage 1 alongside `docs/workbench.md` (`DX-H7`).

#### Pass 8: DX Measurement (`5/10 -> 8/10`)
- **SPACE / DevEx Instrumentation (Zero External Telemetry, Local-First)**:
  - **Efficiency / TTHW**: Measure and record two separate benchmarks in `docs/workbench-verification.md`: (1) clean-venv install to first offline report/storyboard (`target < 2 min`; `< 15s` post-launch), and (2) time to playable `film.mp4` with `ffmpeg`.
  - **Feedback Loops**: Automated regression in `tests/test_workbench.py` runs the exact documented `#first-offline-job` sequence and verifies RFC 9457 `content-type`, `code`, `doc_anchor`, `fix`, `recovery`, and secret redaction across both locales (`zh-CN`/`en`).
  - **Cognitive Load**: Track number of clicks from `/` to first offline storyboard (`<= 2` clicks) and zero false-200 / zero raw-`SyntaxError` incidents across all control APIs.

---

### Phase 2.5 Required Outputs

#### NOT in scope (DX)
- Remote SaaS telemetry / analytics phone-home (local-first workbench).
- Interactive TUI wizard or separate Python SDK package (`h3ref.py` CLI + localhost HTTP API + `SKILL.md` cover all workflows).
- Live paid-API key verification probe inside Settings Drawer on page load (would cause unexpected outbound calls; users can run `python h3ref.py account` documented in `docs/workbench.md`).

#### What already exists (DX)
- `workbench/server.py:283-348` already implements RFC 9457 `api_problem()`, `segment_problem()`, `film_problem()`, and redacted `correlation=<instance>` trace logging for 4 routes — `DX-H3` extends this exact mechanism rather than inventing a second error format.
- `h3ref.py preflight --film examples/dialogue-budget.json --json` already delivers a `0.06s` keyless CLI hello-world.
- `workbench/directories.py:94-103` already models explicit OS command return-code + timeout checking.
- `h3ref/film.py:1345` already uses `H3_WORKBENCH_WORKER == "1"` to distinguish managed workbench workers from direct CLI invocations.

#### DX Review Scorecard & Trend

| Dimension | Pre-Fix Score | Post-Fix Score | Delta | Key Drivers |
|---|---:|---:|---:|---|
| 1. Getting Started & Onboarding | 6/10 | **9/10** | +3 | `DX-H6` 2-click offline demo + Load Sample Script; `DX-H7` README pairing |
| 2. API / CLI / SDK Ergonomics | 5/10 | **9/10** | +4 | `DX-H1` run-scoped `control.json` & retry-boundary pause; `DX-H2` `operation_id` `409`; `DX-H5` unified naming |
| 3. Error Messages & Debugging (`agent-ready-v1`) | **4/10 (CAP4)** | **9/10** | +5 | `DX-H2` cleared both `CAP4` false-200s; `DX-H3` cleared 5 `CAP6` routes via `api_problem()` |
| 4. Documentation & Learning | 4/10 | **9/10** | +5 | `DX-H7` `#first-offline-job`, `#job-control-errors`, CLI parity, `README.md`, `SKILL.md`, doc smoke test |
| 5. Upgrade & Migration Path | 5/10 | **9/10** | +4 | `DX-H4` `kind=="one"` delete guard + lock-verified PID cleanup; `DX-H7` `.workbench/jobs/*.json` rollback guide; `d5` |
| 6. Developer Environment & Tooling | 6/10 | **9/10** | +3 | `DX-H2` bounded `child.wait(5)`; `DX-H3` `common.js` safe JSON & tab jump; `DX-H5` `H3_COLAB_MAX_LOGS` & `?include_logs=0` |
| 7. Community, Ecosystem & Agent Skill | 7/10 | **9/10** | +2 | `SKILL.md` updated with job-control & exit-75 rules; paid-side-effect boundary explicit |
| 8. DX Measurement | 5/10 | **8/10** | +3 | Split TTHW tracking + automated doc-curl & RFC 9457 contract test |
| **Overall DX Score** | **5.3/10** (Pass 3 capped **4/10**) | **8.9/10** | **+3.6** | All `CAP4` & `CAP6` caps resolved; **Trend vs 2026-11-25 (`agent-ready-v1`, `5.1 -> 8.6`)**: **`+0.3` post-fix improvement** |

#### DX Implementation Checklist
- [ ] **DX-T1 (P0, human: 4h / CC: 20min)** — `h3ref/control.py`, `h3ref/rh.py`, `h3ref/frames.py`, `h3ref.py` — Run-scoped `control.json` (`H3_WORKBENCH_OPERATION_ID` + `H3_WORKBENCH_WORKER=1`), pause check inside RunningHub 60s backoff & post-upload APIMart POST, `stderr` `JOB_PAUSED` diagnostic on exit `75`, corrupt `control.json` guard.
- [ ] **DX-T2 (P0, human: 4h / CC: 20min)** — `workbench/server.py`, `workbench/jobs.py` — Fix `CAP4` false-200 on `/open-folder` (macOS rc, Linux `DISPLAY`/`WAYLAND_DISPLAY` + `stdin=DEVNULL` + `0.2s` poll, `dispatched`+`opened`) and `/stop` (bound `child.wait(5)`, HTTP `500` on `cleanup_failed`, `202` while `stopping`, optional `operation_id` `409` & `wait_seconds` `0..15`).
- [ ] **DX-T3 (P0, human: 6h / CC: 30min)** — `workbench/server.py`, `workbench/jobs.py`, `workbench/static/common.js` — Route all 6 new job endpoints + `/api/config` through RFC 9457 `api_problem()` (`job_problem`, recursive extension redaction, `500` correlation traces), unified `unsettled_tasks()` incl. `kind=="one"`, safe `.log -> .json -> cache` delete order, lock-verified `cleanup_failed` PID cleanup, `common.js:request()` safe JSON + Settings tab error focus, provisional `?force=1` (`d5`).
- [ ] **DX-T4 (P1, human: 4h / CC: 20min)** — `workbench/server.py`, `workbench/jobs.py`, `h3ref/colab_runtime.py` — `/logs` `generation` token + `offset>=0` validation + bounded `limit` + line/UTF-8 redaction alignment; `H3_COLAB_MAX_LOGS` (`20..2000`) & `logs_truncated`; unified `progress`/`log_alert_seq`/`should_pause`/`h3.consoleState.v1`, fixed `active_tasks` `null` schema, `/prefill` `omitted_reference_count`, `?include_logs=0`.
- [ ] **DX-T5 (P1, human: 3h / CC: 15min)** — `workbench/static/create.js`, `workbench/templates/index.html`, `workbench/static/common.js` — 1-click Load Sample Script + 2-click Try Offline Demo auto-populate; `<2` char substring auto-match guard + Undo auto-match + filename/MiB `>28 MiB` Toast; UI shortcut & filename match tooltips.
- [ ] **DX-T6 (P1, human: 3h / CC: 15min)** — `docs/workbench.md`, `README.md`, `docs/workbench-cli-parity.md`, `SKILL.md`, `tests/test_workbench.py` — `#first-offline-job` runnable `curl`, `#job-control-errors` 6-route & exit-75 table, quiescent upgrade/rollback registry backup guide, README quickstart + CLI preflight pairing, prototype supersession banner, automated doc-curl smoke check.

---

## Phase 3: Eng Review + Dual Voices (Final Required Gate)

<!-- autoplan-accepted:eng -->
- [ENG-H1] Worker Admission Boundary, Complete `JobPaused` Passthrough & Pre-Existing Task Drain:
  - Enforce `h3ref/control.py:check_pause(job_dir, should_pause_cb=None)` strictly BEFORE writing `submission_unknown=True` and immediately before every fresh outbound provider/LLM attempt across ALL 7 locations: (1) `h3ref/frames.py:draw()` post-upload / pre-POST, (2) `h3ref/frames.py:describe_composition()` (`h3ref/frames.py:132`) between retries, (3) `h3ref/rewrite.py` (`h3ref/rewrite.py:322`) between retries, (4) `h3ref/rh.py:submit_with_retry()` (`h3ref/rh.py:121`) before `record_submission(True)` + 1s interruptible sleep slices during 60s `QueueFull` backoff, (5) `h3ref/comfy.py:submit_with_retry()` (`h3ref/comfy.py:452`) before `record_submission(True)` + inside `ComfyNodePool.acquire()` (`h3ref/comfy.py:994-1010`) on each `0.5s` wait iteration via optional `should_pause` callback, (6) `h3ref/film.py:run()` (`h3ref/film.py:1426-1437`) between stages, and (7) `workbench/segments.py:_regenerate()` (`961,971,982`) ONLY when the staged attempt does NOT already carry an accepted `task` (allowing existing accepted staged rerolls to finish download/publication).
  - In `h3ref/film.py:stage_frames()`, enumerate and schedule all wanted shots that already have a saved accepted `task` (or `task_pending`) BEFORE admitting unsubmitted shots (and never skip past remaining accepted `task` shots when breaking on Pause), so remote tasks accepted by an earlier worker always drain to disk.
  - Add `except JobPaused:` passthrough BEFORE the 3 missed broad `except Exception:` handlers in addition to the 6 in `CEO-H2`: (a) `h3ref/film.py:413` (`stage_frames.draw`: `except JobPaused: return` without calling `failure(key, exc)`), (b) `h3ref/film.py:748` and `h3ref/film.py:772` (`_generate_segment` ComfyUI multi-node upload & submit failover: `except JobPaused: raise` before `node_pool.mark_unhealthy`, releasing `leased_endpoint` cleanly in `finally:`).
  - In parallel `stage_generate` and `stage_frames`, give real exceptions strict precedence over `JobPaused`: if `first_error` is `None` or `isinstance(first_error, JobPaused)` and a non-`JobPaused` exception arrives, replace `first_error = exc` so real in-flight failures are never masked as clean `status="paused", error=None`.
- [ENG-H2] Cross-Platform Process Ownership (POSIX vs Windows), `RunState` Immutable Settlement & `Jobs.run()` vs `T_stop` Serialization:
  - Gate `not worker_locked(output_dir)` stale-PID resolution strictly to POSIX (`os.name != "nt"`), and treat POSIX `worker_locked()` `OSError` as locked/unknown (fail closed) rather than returning `False`. On Windows (`os.name == "nt"`), NEVER clear `pid` via `worker_locked()` — verify emptiness via `WindowsJob` `ActiveProcesses == 0` (when in-process handle exists) and `windows_process_alive(pid)`, clearing stale `pid` only when verified empty or after server restart (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
  - Replace ad-hoc `_run_events` with a per-run `RunState(operation_id, event, cleanup_failed=False, settlement_snapshot=None, stop_started=False)` registered under `self.lock` BEFORE `thread.start()`; if `control.json` unlink, `RunState` registration, or `thread.start()` fails after persisting `queued`, roll back `status="failed"` + persist so the record cannot wedge `available()`.
  - Condition every finalizer write, child eviction, and Stop waiter response on run identity (`operation_id` / `self.children.get(identifier) is child`): capture immutable `run_state.settlement_snapshot` immediately after `self.save(record)` and BEFORE `run_state.event.set()`, and return that immutable settlement from `POST /api/jobs/<id>/stop` so concurrent `stop(A) -> resume(B) -> return stop(A)` never leaks run B's snapshot.
  - Commit control transitions in fixed order: persist registry status (`pausing`/`stopping`) under `self.lock` BEFORE writing `control.json` (rolling back registry status and returning `500` RFC 9457 if `control.json` write fails).
  - Serialize `Jobs.run()` vs `T_stop`: immediately after `code = child.wait()`, acquire `self.lock` and check `self.closing`, `run_state.cleanup_failed`, `record["status"] == "stopping"`, and `code == EXIT_CODE_PAUSED`; execute `report.md`/`dirty_segments = []`/`record_assembly()` ONLY when none hold and `code == 0`. In `except Exception:`, skip overwriting `record["error"]` and skip appending bogus error log lines if `run_state.cleanup_failed` or `record["status"] == "stopping"`, wrapping `log_path.open("a")` in `try/except OSError`. Include active `self.children` (non-demo / video) in `has_active_work()` and `has_active_video_work()`. Wrap `stop_posix_descendants_on_exit` (`jobs.py:149`) in `try/except Exception` recording completion on `child` so `T_stop` never double-waits 15s, and release ownership once the owned process group or Job Object is verified empty independently of leaked stdout EOF.
  - Designate `POST /api/jobs/<id>/resume` (Workbench Resume) as the primary supported recovery command while Workbench runs (documenting that direct CLI resume must not run concurrently with Workbench until `ENG-E1` adds a cross-platform CLI lifetime lease), and add a non-blocking POSIX `worker.lock` check at `h3ref.py:cmd_one()` entry matching `film.py:1345`.
- [ENG-H3] Authoritative `unsettled_tasks()` (Including Staged Rerolls & `one` Terminal Failures) + Server-Derived `recovery_action`:
  - Build `unsettled_tasks(job_dir, kind)` reusing `segments.task_unresolved`, `segments.pending_superseded`, and `frames.task_pending`: for unapplied `inputs/segment-edit.json`, read `inputs/segment-<revision>/state.json -> segs[str(number - 1)]` and treat `publishing`, `generated`, `task_unresolved(staged)`, or `(submission_unknown and not task)` as unsettled (ignoring superseded edits); for `kind == "one"`, treat root `state.json` as unsettled iff `(state.get("submission_unknown") and not state.get("task")) or (state.get("task") and not state.get("terminal_failed") and not ((job_dir / "clip.mp4").is_file() and state.get("cny") is not None))`.
  - Catch `(OSError, ValueError)` per file inside `unsettled_tasks()`: `Jobs.summaries()` degrades safely (`has_unsettled_tasks: False, state_unreadable: True`, never 500-bricking `/api/bootstrap`); `DELETE` re-reads under `self.lock` and fails closed (`400`) on unreadable state unless `d5` force-ack applies.
  - Emit server-derived `snapshot["recovery_action"]` (`"retrieve_only"` | `"resume_pipeline"` | `"resume_frames"` | `"jump_blocker"` | `None`) and `snapshot["blocking_target"]` (`{kind, key_or_number}`); reserve `"Retrieve pending results"` (`"retrieve_only"`) strictly for operations that dispatch zero fresh paid work, and label mixed resumes accurately (`"Resume Job (retrieves N + submits M)"`).
- [ENG-H4] Normalized `200`/`202` Stop Envelope, Strict Optional JSON Parser, Safe `open-folder` Reveal, Recursive `prefill` Projection & Route Security:
  - Return the flat `jobs.snapshot(id)` (or `run_state.settlement_snapshot`) on BOTH HTTP `200` and `202` from `POST /api/jobs/<id>/stop`, adding `"poll_url": f"/api/jobs/{identifier}"` as an additive top-level key on `202` (keeping `500 job_stop_cleanup_failed` as RFC 9457 with `job` extension and unwrapping `res.job ?? res` in `results.js`).
  - Add a shared `_optional_json_object()` helper for `/pause`, `/stop`, `/open-folder`, and `DELETE`: return `{}` when `not request.get_data()` (fixing HTTP `415` on bodyless `curl`), otherwise require valid JSON `dict` (returning RFC 9457 `415`/`400` on wrong media type, malformed JSON, or non-object); require literal `val is True` for `acknowledge_unsettled` and finite non-boolean numeric `0 <= wait_seconds <= 15` (rejecting `bool`/`NaN`/`Inf`).
  - Harden `POST /api/jobs/<id>/open-folder`: resolve `output_dir`, verify `resolved.is_dir()` and reject `.app`/`.bundle`/`.framework` (and symlink targets matching them); invoke `["open", "-a", "Finder", "--", str(resolved)]` on macOS and `os.startfile(str(resolved), "explore")` on Windows; truncate and redact stderr to `<= 300` chars.
  - Recursively project only supported keys inside `GET /api/jobs/<id>/prefill` (stripping unknown nested metadata and local paths), add `demo_placeholders` to the whitelist, compute `omitted_reference_count` for both `film` and `one` (`references_omitted = omitted_reference_count > 0`), and emit valid raw shot durations/boundaries so `prefill -> POST /api/jobs` round-trips cleanly including the `3600.4` timeline fixture (`tests/test_dialogue_budget.py:283`).
  - Validate `re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record["id"])` in `Jobs.__init__` and `DELETE`; wrap `workbench/server.py:345` correlation log append in `try/except OSError`; recursively redact all strings inside `api_problem` without mutating inputs; fall back to `request.path` prefix matching in `http_error` when `request.endpoint is None` (covering `405 Method Not Allowed`).
- [ENG-H5] Zero-Stall Byte-Cursor `/logs`, Unified Incremental Console Reader, Bounded Log Buffers & Single Golden Alert Classifier:
  - Harden `GET /api/jobs/<id>/logs`: validate `offset`/`limit` with strict ASCII regex `re.fullmatch(r"[0-9]{1,18}", raw)` (`1 <= limit <= 262144`, default `100000`); compute `next_offset = start_offset + raw_bytes_consumed` strictly from pre-redaction bytes; if incoming `offset > 0` lands mid-line (`byte[offset-1] != b"\n"`), realign to the enclosing line start (bounded `4096`-byte lookback) before decoding/redacting so secrets bisected by a cursor are fully redacted; guarantee forward progress on oversized lines (> `limit`) or settled EOF by advancing to the last valid UTF-8 codepoint boundary (holding back an incomplete tail ONLY when `offset + len(raw) == file_size` and active); emit `generation: null` when missing/empty and trigger `reset: true` ONLY when `offset > 0` and either `offset > file_size` or `generation` (`st_ino:st_ctime_ns:first_line_sha256[:16]:prefix_4k_sha256[:16]`) mismatches.
  - Update `results.js:refreshJob()` to poll `GET /api/jobs/<id>?include_logs=0` (0 log reads when collapsed; eliminates `100KB` sliding-window diff breakage & `0.71 MB/s` 10-view I/O), and reuse ONE incremental `/api/jobs/<id>/logs` poller for Console Tab 1 on BOTH `/` and `/jobs/<id>` (keeping `renderJob(job)` backward-compatible when unit tests supply explicit `typeof job.logs === "string"`).
  - Cap browser retained console DOM lines (`<= 2000` lines / `<= 512 KB`), cap `h3ref/colab_runtime.py` reader internal `lines` list and unterminated-line buffer (`<= 64 KB`), and parse `H3_COLAB_MAX_LOGS` safely (fallback/clamp `20..2000`, default `200`, never crashing at import).
  - Track `log_alert_seq` / `log_alert_counts: {warn, error}` incrementally in `Jobs.run()` and `ColabRuntimeManager._append_log()` (counting traceback entry once as 1 error event while continuation lines inherit `error` display class), verified against a single shared golden fixture `tests/fixtures/log_alerts_golden.json` run in BOTH Python and JS tests.
- [ENG-H6] Zero-Stat Warm `summaries()`, Orphaned `output_dir` Degradation, Accurate Cost Labels & Windows `DELETE` Retry:
  - Persist `title`, `frames_total`, and `clips_total` at `Jobs.create()`, and persist `progress`, `cost_cny`, `log_alert_seq`, `log_alert_counts`, and `has_unsettled_tasks` at settlement (re-persisting on any `edit_frame`/`regenerate_segment`/`assemble`/`rewrite`/`resume` mutation) so warm settled `summaries()` performs `0` filesystem `stat()` calls per poll. For active rows, include `frame-edit.json`, `segment-edit.json`, and staged `segment-<rev>/state.json` in the cache stamp, release `_summary_cache_lock` during disk I/O, and verify record membership/generation before storing back into cache (preventing post-delete resurrection).
  - In `Jobs._snapshot()` (`workbench/jobs.py:274`), catch `(FileNotFoundError, ValueError)` on `inputs/request.json` and return a clean degraded snapshot (`output_dir_missing: True`, preserved `.log`, empty storyboard/segments, Delete enabled) instead of HTTP `500`.
  - Compute `snapshot["cost"]` separately for `kind=="one"` (root `state.json`, `frames_cny=0.0`) vs `kind=="film"`; read `cny`/`run_s`/`wall_s` on segment cards from active `record` (never empty `staged`); align cost tooltip copy with actual recorded accounting (`workbench/storyboard.py:279-281` / `tests/test_workbench.py:223,246`: "Recorded API cost in job state (includes saved frame rerolls; excludes external/Colab compute)") and label ComfyUI `run_s` as "Reported execution time" (preserving `null` when unknown).
  - Retry `<id>.log` / `<id>.json` unlinks on Windows (`WinError 5/32/33`) with brief backoff (matching `h3ref/config.py:atomic_replace`) before returning `500 job_delete_io_error`.
- [ENG-H7] Zero-Build Frontend Module Split, Legacy Node VM Resilience, `.frame-card.is-editing` Grid Fix & Control/Upload Races:
  - Keep `workbench/static/common.js` focused on `state`, `request()`, `t()`, `renderConfig()`, and `renderRuntimeStatus()`, extracting two zero-build `<script defer>` modules loaded in `base.html`: `workbench/static/console.js` (unified dockable log console, cursor poller, traceback classifier, resize/presets/`localStorage`) and `workbench/static/shell.js` (Toast stack, topbar popovers, 3-tab Settings drawer, focus-trapped modals, LIFO `Esc`/`⌘J` dispatcher), wired through safe `typeof ... === "function"` hooks.
  - Protect existing 3,531-line Node VM tests (`tests/test_workbench_ui.js:33,93,98-100`): feature-detect `typeof response.text === "function"` before falling back to `response.json()` in `common.js:request()`; wrap every `localStorage`/`sessionStorage` `JSON.parse()` in `try/catch` + plain-object guard (`localStorage.getItem: () => "en"` safe); guard all browser globals (`window`, `document.addEventListener`, `document.querySelector`, `document.body?.classList`, `el?.closest`, `el?.contains`) and update `makeDom()` to store listener arrays without overwriting prior handlers.
  - Apply `grid-column: 1 / -1` to the owning **`.frame-card.is-editing`** (direct child of `.frame-grid`), NOT nested `.frame-editor`.
  - In `results.js`, give `runControlAction()` its own `state.controlSubmitting` guard, clear `state.pollTimer`, bump `state.jobRevision`, and recover `409 job_operation_mismatch` via `refreshJob()` + Toast; keep existing `failed_only` rule for paused mid-storyboard frames and add a clear disabled tooltip on finished frames while paused/stopped.
  - In `create.js`, enforce `state.images.length + files.length <= 9` BEFORE batching in `one` mode, re-sync `#upload-conflict-tray` inside `renderFilmReferences()` (covering cast-group edits), and clear Undo auto-match state on any subsequent manual binding, group edit, or script edit.
- [ENG-H8] Atomic Demo Writes, Safe Downgrade Helper & Real Rendered-Template Browser Smoke Gate:
  - Update `workbench/demo.py` to write PNGs, FFmpeg clips, and final copies to `.tmp` files and publish via `config.atomic_replace()` (matching `h3ref/media.py:74`) so Hard Stop mid-demo never corrupts assets or destroys previous downloadable `film.mp4`.
  - Add an idempotent offline downgrade script (`scripts/prepare_downgrade.py`, documented in `docs/workbench.md`) that verifies zero active workers and converts quiescent `paused`/`stopped` records in `.workbench/jobs/*.json` to `interrupted` without touching `state.json` (preserving all paid `task_id`s), backed by a prior-reader compatibility fixture test for paused `film` and stopped `one` records.
  - Require a real rendered-template headless browser smoke pass across `1440px`/`1060px`/`760px`/`420px` in both `zh-CN` and `en` (verifying real DOM IDs, `.frame-card.is-editing` span, narrow-Create sticky launch bar, `760px` fixed popovers, hidden-tab validation jump, modal Tab focus trap, and delayed `202` Stop responses) plus Windows CI execution of `tests/test_windows_processes.py` + `tests/test_job_pause_stop.py` prior to packaging.
- [UC2A] Sequential 3-PR Delivery Plan (Approved User Challenge `uc2a`, Superseding Single-PR `CEO-E5`):
  - Deliver the overhaul as **3 sequential PRs merged to `main` in strict order** (queue cost: +2 CI/review merge gates at ~5–10 min each; explicit cross-PR UI, API, and doc coherence guarantees on every PR boundary):
    - **PR 1 (Backend Safe Pause/Stop Engine, Management APIs, `CEO-H1` Fix & Operator API Docs)**: ships `h3ref/control.py`, `h3ref/{frames,film,comfy,rh,rewrite,colab_runtime}.py`, `h3ref.py`, `workbench/{jobs,server,storyboard,segments,demo,processes}.py`, `scripts/prepare_downgrade.py`, backend i18n (`workbench/messages/`, `h3ref/messages/`), `tests/test_job_pause_stop.py`, `tests/test_colab_runtime.py`, `tests/fixtures/log_alerts_golden.json`, `docs/workbench.md` (`#first-offline-job`, `#job-control-errors`, upgrade/rollback), `docs/workbench-cli-parity.md`, and `SKILL.md`. Cross-PR compatibility: zero edits to `workbench/templates/*` or `workbench/static/*`; `GET /api/jobs/<id>` keeps `?include_logs=1` default so legacy `results.js` remains intact; `docs/workbench.md` distinguishes currently-shipped API/CLI controls from UI controls landing in PR 2/3; `README.md` UI layout update stays deferred to PR 3; CI requires green Python suite, `test_locale_parity.py`, `compileall`, existing 3,531-line JS tests, and native Windows `test_windows_processes.py` + `test_job_pause_stop.py`.
    - **PR 2 (Global Workbench Shell, Unified Dockable Console, Popovers & 3-Tab Settings Drawer — stacked on merged PR 1)**: ships `workbench/templates/base.html`, refactored `workbench/static/common.js` + new zero-build `workbench/static/console.js` & `workbench/static/shell.js`, shell/console/drawer/toast CSS in `workbench/static/style.css`, frontend i18n for shell/console, and updated `tests/test_workbench_ui.js` (incl. shared golden alert-classifier test). Cross-PR compatibility: existing `index.html`/`create.js` and `results.html`/`results.js` remain fully functional inside the new shell (`base.html` retains all legacy IDs `#global-error`/`#global-message`/`#config-form`/`#runtime-*`, and legacy `/jobs/<id>` `#job-logs` stays readable without duplicate-ID collision until PR 3 moves Results into Console Tab 1); zero dead links or missing DOM targets on `/` or `/jobs/<id>`.
    - **PR 3 (Create & Results Page Refactor, Interaction Polish, `README.md` Sync & Real Browser Smoke Gate — stacked on merged PR 2)**: ships `workbench/templates/{index,results}.html`, `workbench/static/{create,results,video-segments}.js`, page-layout & responsive CSS (`<=1060px` sticky Create launch bar, `.frame-card.is-editing` full-row span, `760px`/`420px` grids), remaining frontend i18n, updated `README.md` (final UI layout + 2-click Demo + CLI preflight pairing), `docs/design/workbench-ux-prototype.html` supersession banner, `tests/test_results.js`, `tests/test_workbench_ui.js`, and the real rendered-template 4-viewport×2-locale browser smoke gate (`ENG-H8`).
- [ENG2-H1] Process-Tree Settlement Authority, Bounded Log Draining, Verified Ownership & Restart-Safe Orphan Guard:
  - Decouple `child.stdout` draining into a dedicated daemon `log_reader` thread (matching `h3ref/colab_runtime.py:1046-1095`). The single process-tree waiter (`Jobs.run`) waits on `code = child.wait()`, executes `stop_child(child)` FIRST, calls `log_reader.join(timeout=2.0)` (abandoning leaked FD readers without stalling), and ONLY AFTER verified process-tree cleanup commits success bookkeeping (`report.md`/`dirty_segments = []`/`record_assembly()`), persists the final `record`, captures a minimal immutable `RunOutcome` (`{id, status, operation_id, error, cleanup_failed}` — never calling full `snapshot()` before `run_state.event.set()` so artifact/consistency I/O cannot strand waiters), evicts `self.children` strictly by child object identity, and calls `run_state.event.set()`. `POST /stop` builds the response snapshot after waking (falling back to `RunOutcome` if the job was concurrently resumed or deleted).
  - Wrap `WindowsJob.wait_for_exit` (`workbench/processes.py:122-126`) in `try/except Exception` recording completion state matching POSIX `stop_posix_descendants_on_exit`; apply the PID guard to BOTH `os.killpg` sites (`workbench/jobs.py:125` and `138-145`) and treat `PermissionError` during POSIX group cleanup as an unverified cleanup failure (never silently treating it as `ProcessLookupError`).
  - Fix POSIX `worker_locked()` (`workbench/jobs.py:99`) to open existing locks read-only (`"rb"`), explicitly returning `False` on `FileNotFoundError`, `True` on `BlockingIOError`, and distinguishing inaccessible permission errors from active locks. Share ONE verified ownership predicate across `Jobs.available()`, `has_active_work()`, and `has_active_video_work()` that probes ONLY records with `status in ACTIVE_STATUSES`, non-empty `pid` (preserving Colab runtime-stop protection for locked `interrupted` orphans surviving server restart), `cleanup_failed=True`, or active membership in `self.children` — eliminating read-only archive deadlocks and O(N) lock-held history scans.
- [ENG2-H2] Pause/Failure Precedence (`CancelledError` Safe), Inner Retry Admission, Two-Pass Frame Snapshot & Enforced Retrieval-Only Consent:
  - In `stage_generate`, `stage_prompts` (`h3ref/film.py:612`), and `_execute_rewrite_targets` (`workbench/segments.py:1389`), collect ALL admitted future outcomes, explicitly ignore `concurrent.futures.CancelledError` and `comfy.DispatchAbortedError` (never letting deliberate cancellation override `JobPaused`), and give genuine in-flight failures strict precedence over `JobPaused`.
  - In `h3ref/film.py:stage_frames`, snapshot `frames_snapshot = copy.deepcopy(job.state["frames"])` under `job.lock` before entering `ThreadPoolExecutor(4)`; Pass 1 schedules eligible pending tasks (`frames.task_pending(rec)` and not already complete on disk) into `scheduled_keys: set[str]`; Pass 2 walks shots in order against `frames_snapshot` to preserve `known_scenes`/`locked_looks`/`previous` continuity while skipping `scheduled_keys` (zero double-scheduling, zero lockless dict races).
  - Check `should_pause` inside `h3ref/rewrite.py:_llm()`'s 3-attempt HTTP retry loop (`rewrite.py:274`) and `h3ref/http.py:560,597` retry backoff before every fresh outbound POST; at paused stage boundaries (`h3ref/film.py:1430`), route through retrieval-only drain when accepted tasks already exist instead of skipping retrieval; retry transient Windows `PermissionError` 3×20ms inside `h3ref/control.py:should_pause()`.
  - Add `"frames"` to `workbench/jobs.py:911`'s `can_resume_video is False` gate; unify `results.js` `#resume-job` disabled rule as `locked || (job.can_resume_video === false && job.recovery_action !== "retrieve_only")` (resolving the `CEO-H3` vs `DES-H1`/`ENG-H3` conflict); require `"Retrieve pending results"` to send `{"intent": "retrieve_only", "operation_id": ...}` and enforce a backend `drain_only=True` flag inside `stage_frames`/`stage_generate` that strictly forbids fresh provider submissions.
- [ENG2-H3] Append-Stable `/logs` Generation, Monotonic Redacted Cursor, Symlink Containment & Bounded Colab Chunk Reader:
  - Replace `ENG-H5`'s append-breaking `generation` formula (which used POSIX `st_ctime_ns` and `prefix_4k_sha256`) with strictly append-stable `generation = f"{st.st_ino}:{first_complete_line_sha256[:16]}"` (SHA-256 of the first `\n`-terminated line up to 512 bytes; `null` + `next_offset: 0` until the first `\n` is flushed). Open the log via a single FD (`O_NOFOLLOW` on POSIX after asserting `not raw_path.is_symlink()` and `resolved.parent == (storage / "jobs").resolve()`), detecting truncation/replacement on that FD.
  - Enforce `limit` bounds `4096 <= limit <= 262144` (default `100000`, preventing `limit=1` multibyte stalls). Keep `next_offset` strictly monotonic without duplicating prefix text: if an incoming cursor lands mid-line, decode/redact the enclosing line internally and emit only text strictly from `offset` forward; for any single logical line exceeding `limit`, consume the full oversized line through its `\n`/EOF and emit a bounded notice `"[line truncated: N bytes]\n"` while advancing `next_offset` past it (zero stalls, zero bisected-secret leaks).
  - Route all direct server-side job log appends (`Jobs.run()` `except` at `jobs.py:1083` and `server.py:345`) through a unified `append_job_log()` helper that increments `log_alert_seq`/`log_alert_counts`; replace `for line in proc.stdout:` in `h3ref/colab_runtime.py:1050` with `4096`-byte chunk reads enforcing a hard `64 KB` line cap, a `2 MiB` head/tail cap on `_run_command` `collected`, and dynamic `_max_slot_logs()`.
- [ENG2-H4] Private Demo Staging, Dot-File Artifact Filter, State Shape Validation & OS Security Hardening:
  - Stage all `workbench/demo.py` PNGs, FFmpeg clips, tail JPEGs (`h3ref/media.py:33`), and final copies inside a private same-volume directory (`out / "inputs" / ".staging"`) using canonical media extensions (`.png`, `.mp4`, `.jpg`) so Pillow and FFmpeg infer formats natively, then publish via `config.atomic_replace()`; additionally filter `path.name.startswith(".")` in `workbench/jobs.py:214` and `workbench/segments.py:1082` so temporary files never enter listings, progress counts, or `/files/` downloads.
  - Validate dict/list shapes on `state.json`/`request.json` before `read_context()`/`_snapshot()` enrichment (preventing `AttributeError`/`TypeError` on malformed JSON); include unapplied `frame-edit.json` `downloaded=True` in `unsettled_tasks()`, validate `segment-edit.json` `revision` (`[A-Za-z0-9_-]{1,64}`) and non-bool positive int `number`, and fail closed on `DELETE` whenever state is malformed/unreadable (never enabling Delete on corrupt state unless `d5` force-ack applies); add a 10s TTL + explicit invalidation (`GET /api/jobs/<id>` & Refresh button) to settled `summaries()` cache; emit `active_tasks` `rewrite` progress whenever `operation == "pipeline"`, no segment is `task_pending`, and at least one unfinished segment lacks valid `h3`.
  - In `GET /api/jobs/<id>/prefill`, if `any(float(s["end"]) > 3600 for s in shots)`, emit `start=0, end=round(float(s["end"]) - float(s["start"]), 2)` per shot (preserving exact durations/segments/dialogue budget under `[0, 3600]`); in `POST /api/jobs/<id>/open-folder`, use `stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL` + daemon reaper `proc.wait()` on Linux `xdg-open`, expand macOS rejected bundle suffixes (`.app`, `.bundle`, `.framework`, `.pkg`, `.workflow`, `.action`, `.prefPane`, `.command`, `.appex`), and reject Windows UNC paths (`\\...`).
- [ENG2-H5] Canonical Stop Contract Table & Lock-Safe Packaged Downgrade:
  - Lock ONE authoritative `POST /api/jobs/<id>/stop` response table across code and docs: fast exit -> `200` flat snapshot (`stopped`); slow exit -> `202` flat snapshot + additive top-level `poll_url` (`stopping`); observed cleanup failure -> `500` RFC 9457 `job_stop_cleanup_failed` (+ `job` extension); already `stopped` on matching `operation_id` -> `200` idempotent flat snapshot; mismatched `operation_id` -> `409` RFC 9457 `job_operation_mismatch`; any other settled status (or `stopped` without matching `operation_id`) -> `400` RFC 9457 `job_not_active`.
  - Have `scripts/prepare_downgrade.py` acquire the workbench single-instance lock (`workbench/instance.py:7`) throughout conversion, refuse conversion if any job has an errorless `task_pending` frame that the prior release (`h3ref/frames.py:77`) cannot recover (instructing operator to retrieve/settle first), add `scripts/prepare_downgrade.py` to `scripts/package_windows.py`, and test stopped-`film`, paused-`film`, and stopped-`one` downgrade fixtures (including extracted Windows ZIP verification).
- [ENG2-H6] Intermediate-`main` Compatibility Contracts Across PR 1, PR 2 & PR 3 (`UC2A` Hardening):
  - **PR 1 legacy-UI compatibility adapter**: include a minimal ~25-line adapter in `workbench/static/{common,results}.js` and `workbench/static/messages/{zh-CN,en}.json` so existing pages cleanly handle API-driven `pausing`/`stopping`/`paused`/`stopped` statuses (continue polling while `pausing`/`stopping`, keep Create locked while active, enable Resume for `paused`/`stopped` `one`/`film`, show `frameTaskPending` notice when `!running(job)`, localize all 4 new statuses). Keep PR 1 `docs/workbench.md` scoped strictly to shipped API/CLI contracts without referencing unmerged PR 2/3 UI changes.
  - **PR 2 zero-collision shell & complete Results console handoff**: use distinct shell IDs in `base.html` (`#topbar-history-list`, `#topbar-history-empty`, `#console-output`, `#console-panel`) preventing duplicate DOM IDs alongside legacy `index.html:55` and `results.html:75`; share `/api/bootstrap` `state.jobs` hydration and lightweight history refresh across both `/` and settled `/jobs/<id>` in `common.js`/`shell.js`; migrate BOTH `results.js:390` and `results.js:478` to `?include_logs=0` and wire `console.js` Tab 1 to `$("results-page")?.dataset?.jobId` via `/api/jobs/<id>/logs` while retiring legacy `<details class="logs-section">` in PR 2 (one log owner, zero collapsed log reads); hold the History `Clone` button until PR 3 ships `?from=<job_id>` consumption; update `docs/workbench.md` navigation sections in PR 2; gate PR 2 on a rendered-template test verifying zero duplicate IDs, live `/jobs/<id>` topbar history, and Console Tab 1 output.
  - **PR 2/3 storage & drag-drop safety**: invoke `refreshFilmReferences()` inside `restoreCreateDraft()` after restoring non-empty `film_json`; wrap ALL `sessionStorage`/`localStorage` `getItem`/`setItem` calls in safe helpers catching `QuotaExceededError`/`SecurityError`; restrict global `window` `dragover`/`drop` `preventDefault()` to `event.dataTransfer?.types?.includes("Files")`; cap single drop batches at `<= 36` files and document `.workbench/uploads/` cleanup.
<!-- /autoplan-accepted:eng -->

### Step 0 — Scope Challenge & Reality Verification

#### A. Code-Grounded Sub-Problem Mapping, Complexity & Reuse Check
- **Scope Challenge Verdict (`MODE: FULL_REVIEW`)**: `scope accepted as-is` (per Autoplan Phase 3 override P2: never reduce scope; instead eliminate fragility and split monolithic `common.js` cleanly via `ENG-H7`).
- **Existing Code Reuse Mapping**:
  - Control/locking boundary: reuse `h3ref/config.py:atomic_replace`, `workbench/processes.py:WindowsJob` / `windows_process_alive`, and `H3_WORKBENCH_WORKER=1` (`h3ref/film.py:1345`).
  - Paid-task state resolution: reuse `h3ref/frames.py:task_pending`, `workbench/segments.py:task_unresolved`, `workbench/segments.py:pending_superseded`, `workbench/storyboard.py:frame_edit_task_pending`.
  - HTTP errors: reuse `workbench/server.py:api_problem()` (`server.py:283`) via thin domain wrapper `job_problem()`.
  - Atomic media publishing: reuse `h3ref/config.py:atomic_replace` pattern from `h3ref/media.py:74` inside `workbench/demo.py`.
- **Complexity Count (Proposed Work)**: ~23 touched existing files + 4 new focused files (`h3ref/control.py`, `workbench/static/console.js`, `workbench/static/shell.js`, `scripts/prepare_downgrade.py`, plus `tests/test_job_pause_stop.py` & `tests/fixtures/log_alerts_golden.json`). No new frameworks, no build toolchain, 1 new small exception class (`JobPaused`) + 1 small dataclass (`RunState`).
- **Layer Classification (Search Before Building)**:
  - `[Layer 1]` Built-in/stdlib: `fcntl.flock`, `threading.Event`/`RLock`, `re.fullmatch`, `os.startfile(..., "explore")`, `subprocess.run(..., stdin=DEVNULL)`, `config.atomic_replace`.
  - `[Layer 1]` Existing repo helpers: `api_problem()`, `segments.task_unresolved()`, `segments.pending_superseded()`, `frames.task_pending()`, `WindowsJob`.
  - `[Layer 3]` First-principles design: `RunState` immutable settlement snapshot + run-scoped `H3_WORKBENCH_OPERATION_ID` control file + unified single incremental `/logs` cursor reader across `/` and `/jobs/<id>`.
- **TODOS.md Cross-Reference**: Existing `TODOS.md` has 0 open items (`TODOS.md:1-5`); 5 deferred items across Phases 1–3 (`CEO-E6`, `CEO-E10`, `CEO-E11`, `CEO-E12`, `ENG-E1`) written to `TODOS.md` below.
- **Distribution Check**: Zero-build Flask + Jinja2 + Vanilla JS preserves `start-windows.cmd` packaging (`docs/windows.md`) and `default-src 'self'` CSP (`workbench/server.py:280`). Windows CI lane required before packaging (`ENG-H8`).

#### Premise Table (`unverified=0`)

| Premise | Claim | `file:line` Receipt | Verdict |
|---|---|---|---|
| P1 | `h3ref/frames.py:task_pending` misses hard-stopped errorless tasks | `h3ref/frames.py:77-79` requires `bool(record.get("error"))` | VERIFIED |
| P2 | `workbench/jobs.py:worker_locked()` returns `False` unconditionally on Windows | `workbench/jobs.py:91-93` (`if not output_dir or os.name == "nt": return False`) | VERIFIED |
| P3 | `workbench/jobs.py:stop_posix_group` has two unbounded `child.wait()` calls | `workbench/jobs.py:132,134` | VERIFIED |
| P4 | `h3ref/film.py` has 3 unguarded `except Exception` handlers on Pause paths | `h3ref/film.py:413` (`stage_frames.draw`), `h3ref/film.py:748,772` (`_generate_segment`) | VERIFIED |
| P5 | `workbench/segments.py` stores staged retry `task` in `inputs/segment-<rev>/state.json` | `workbench/segments.py:632-637` | VERIFIED |
| P6 | `workbench/server.py` has `api_problem()` RFC 9457 helper unused by new job routes | `workbench/server.py:283-348` | VERIFIED |
| P7 | Existing JS UI test suite totals 3,531 lines and mocks `localStorage.getItem: () => "en"` & no `response.text()` | `tests/test_workbench_ui.js:33,93,98-100` (`2490+393+509+139=3531` lines `[measured: wc -l]`) | VERIFIED |
| P8 | Frame replacement accumulates `previous.cny + pending.cny` | `workbench/storyboard.py:279-281`, `tests/test_workbench.py:223,246` | VERIFIED |

#### Eng Reality Rows (`gstack-plan-reality`)
REALITY: premise-table pass 8/8 premises verified against repo source with unverified=0 workbench/jobs.py:91
REALITY: already-done pass HEAD 1366481 has zero pause/stop/console/open-folder/prefill/logs routes in workbench/server.py:740
REALITY: surface-check pass Verified existing routes, CLI flags, api_problem(), and new endpoints against workbench/server.py:283
REALITY: binding-decisions pass No conflicting branch decisions in gstack-decision-search; all 4 user non-goals preserved verbatim docs/plans/2026-11-26-workbench-ux-overhaul.md:43
REALITY: numbers pass Re-measured JS tests=3531 lines [measured: wc -l] tests/test_workbench_ui.js:1-2490; common.js=925 lines [measured: wc -l] workbench/static/common.js:1-925; jobs.py=1107 lines [measured: wc -l] workbench/jobs.py:1-1107; server.py=938 lines [measured: wc -l] workbench/server.py:1-938
REALITY: deferred-asks pass No linked issue; all out-of-scope roadmap items recorded in TODOS.md TODOS.md:1
REALITY: pr-count pass Approved UC2A splits delivery into 3 sequential PRs (+2 CI merge gates ~5-10m each) with explicit PR 1/PR 2/PR 3 cross-PR compatibility & doc contracts docs/plans/2026-11-26-workbench-ux-overhaul.md:375

---

### Step 0.5 — Dual Voices (Eng)

#### agy (in-host) SUBAGENT (Eng — independent review)
- **Conversation ID**: `2288a2f6-1d29-450f-9b7a-e270d548eaca` (`native_s = 474`, verified header `INPUT: eng b3466fca7fcbe19cac178bf09f2d02f8b92874cd9add90e59c3627232c7a2d98`).
- **Findings (18 total: 6 Critical, 6 High, 6 Medium)**:
  - **6 Critical**: `ENG-1` (3 missed `except Exception` handlers at `film.py:413,748,772` + missing `comfy.submit_with_retry` pause check), `ENG-2` (Windows mutex bypass because `worker_locked()` returns `False` on `nt`), `ENG-6` (`Jobs.run()` `try:` post-exit mutations + `except` clobber + `has_active_work` ignoring `self.children` + `stop_posix_descendants_on_exit` double wait), `ENG-7` (`unsettled_tasks()` missing `segment-<rev>/state.json`, blocking `terminal_failed` `one`, and 500-bricking `/api/bootstrap` on corrupt JSON), `ENG-8` (`/logs` infinite stall on >100KB lines, redaction byte-offset drift, empty->line1 false `reset: true`, loose int validation), `ENG-15` (existing Node VM tests crash on `response.text()` and `JSON.parse(localStorage)`).
  - **6 High**: `ENG-3` (200 flat vs 202 wrapped `{job}` schema mismatch), `ENG-4` (925-line `common.js` god-file bloat -> split `console.js` & `shell.js`), `ENG-9` (stale `summaries()` cache missing edit files, cold-cache lock contention, deleted `output_dir` 500 on `/jobs/<id>`), `ENG-10` (parallel `stage_generate` `JobPaused` masking real `RuntimeError` + `ComfyNodePool.acquire` wake), `ENG-13` (macOS `.app` bundle execution via `open` + bodyless POST `415`), `ENG-16` (100KB sliding-window `job.logs` diff breaks + `results.js` poll-vs-control race).
  - **6 Medium**: `ENG-5` (dual Python/JS log classifier drift), `ENG-11` (`kind=="one"` cost/active clip/`demo_placeholders`), `ENG-12` (`H3_COLAB_MAX_LOGS` import crash + Windows `DELETE` reader lock), `ENG-14` (`record["id"]` containment + `server.py:345` `OSError` + `405` RFC 9457), `ENG-17` (`create.js` `one` 9-file cap, conflict-tray group sync, Undo invalidation), `ENG-18` (paused mid-storyboard `failed_only=True` friction).

#### Codex SAYS (eng — architecture challenge)
- **Execution**: `OUTSIDE_STATUS: completed provider=codex host=agy` (`model: gpt-6.2-astra`, `outside_s = 435`, `VERDICT: findings`, `FINDINGS: P1`).

```tool-output
**Do not approve the amended plan for implementation yet.** Three proposed behaviors can lose the recovery path for paid work: treating an absent worker lock as proof that cleanup finished, omitting staged reroll state from Delete, and offering direct CLI recovery without exclusive ownership of the output directory.

This was a static review of the repository code and tests. The prior consensus describes proposed changes. For example, the checked-out `task_pending()` still requires an error, and the current POSIX cleanup still has two unbounded waits. I did not run workers, contact providers, or measure performance. `h3ref/frames.py:77`, `workbench/jobs.py:132`, `workbench/jobs.py:134`.

### Architecture and paid-work recovery

**1. An absent worker lock cannot establish that a process tree stopped.**  
Severity: High

`worker_locked()` always returns false on Windows and also returns false on POSIX lock-file I/O errors. A POSIX leader can exit while a descendant retains stdout. Windows already has a stronger ownership signal in the Job Object’s `ActiveProcesses` count. DX-H4 could clear a PID and admit another writer prematurely. Conversely, `available()` rejects any entry in `self.children` before checking the lock, so a blocked log reader can keep the entire workbench busy after the API reports `failed`. `workbench/jobs.py:91`, `workbench/jobs.py:105`, `workbench/jobs.py:149`, `workbench/jobs.py:455`, `workbench/processes.py:110`, `tests/test_windows_processes.py:161`.

Recommendation: Keep a run-owned cleanup supervisor until the owned group or Job Object is verified empty, distinguish permission failure from process absence, and release ownership independently of pipe EOF because neither a missing lock nor an exited leader establishes that descendants have stopped writing.

**2. Direct CLI recovery can race browser Resume and submit twice.**  
Severity: High

The film CLI briefly probes and releases the POSIX lock before doing work. Windows skips that probe. `cmd_one()` reads and writes the shared state without a lifetime lease. A CLI recovery started first and a subsequent browser Resume can therefore both read an unsubmitted item. They can also contend on the same `state.tmp`. The server’s single-instance lock only covers workbench servers. `h3ref/film.py:328`, `h3ref/film.py:1345`, `h3ref/film.py:1356`, `h3ref.py:39`, `h3ref.py:98`, `workbench/jobs.py:1030`, `workbench.py:19`.

Recommendation: Use the workbench Resume route as the supported recovery instruction unless all CLI and managed entry points acquire the same cross-platform lifetime lease because a run-scoped `control.json` identifies a run but grants no exclusive write or submission rights.

**3. A Stop waiter can return the result of a different operation.**  
Severity: High

`restart()` mutates the same record and replaces its `operation_id`. `snapshot(identifier)` reads whichever operation is current. After run A sets its event, another tab can resume as B or delete the job before A’s Stop request takes its snapshot. A request-side precondition cannot fix that response race. The existing finalizer also removes `children[identifier]` by job ID alone. `workbench/jobs.py:234`, `workbench/jobs.py:782`, `workbench/jobs.py:1093`.

Recommendation: Attach an immutable settlement result to A’s event and condition every finalizer write and child eviction on run identity because waking a waiter provides ordering for A without reserving the job against B. Test `stop(A) → resume(B) → return stop(A)` with barriers.

**4. The control protocol has two independently durable writes and no recovery rule for the gap.**  
Severity: High

`write_json()` atomically replaces one file. The plan writes both `control.json` and the registry status. Failure between them can leave a worker honoring a Stop that the registry never recorded. In the opposite direction, creation and restart persist `queued` before calling `launch()`. If the proposed control-file unlink, event registration, or thread start fails, the record can block all future launches. The proposed 1.5 seconds also bounds only `Event.wait()`. Registry writes, contended locks, Windows replacement retries, and full snapshot construction sit outside that bound. `workbench/jobs.py:26`, `workbench/jobs.py:456`, `workbench/jobs.py:548`, `workbench/jobs.py:782`, `h3ref/config.py:53`, `workbench/jobs.py:241`.

Recommendation: Define which same-operation journal governs restart, compensate failed launch registration, and distinguish the wait budget from total HTTP latency because atomic replacement of individual files cannot commit the whole transition. Inject failures between each write and before `thread.start()`.

**5. The proposed exception audit misses the frame worker that receives the new pause exception.**  
Severity: High

The post-upload APIMart check runs inside `frames.draw()`. Its caller inside `stage_frames()` catches `Exception`, calls `failure()`, and records a shot error. The later skip condition excludes that shot, and the proposed resume predicate also excludes non-pending errored shots. The `except` at line 474 covers the planning thread, not this executor thread. Inserting the check after the unknown-submission marker would additionally record an ambiguous charge despite zero generation POSTs. `h3ref/film.py:400`, `h3ref/film.py:408`, `h3ref/film.py:413`, `h3ref/film.py:426`, `h3ref/film.py:474`, `h3ref/frames.py:277`, `h3ref/frames.py:365`.

Recommendation: Propagate `JobPaused` from the drawing worker and check admission before setting `submission_unknown` because an uploaded reference with no generation request must remain resumable. Test the exact interval after upload and assert zero POSTs, no shot error, and successful resume.

**6. Draining scheduled futures misses tasks accepted by an earlier worker.**  
Severity: High

`stage_frames()` discovers saved IDs one shot at a time. Breaking both loops on Pause can leave a later paid shot unpolled even though every scheduled future finishes. A staged segment reroll has the same issue if `check_pause(out)` runs unconditionally before `prepare_prompt()` or `gen_segment()`. The staged job can already contain its task ID. `h3ref/film.py:417`, `h3ref/film.py:433`, `h3ref/film.py:473`, `workbench/segments.py:961`, `workbench/segments.py:971`, `workbench/segments.py:982`, `tests/test_frame_batch.py:955`.

Recommendation: Enumerate recoverable accepted tasks before admitting fresh work and let those tasks reach durable download and publication because remote acceptance can predate every future in the current process. Test a paid later shot and a paid staged reroll with Pause already set.

**7. Provider retries and ComfyUI waiters do not share one dispatch boundary.**  
Severity: High

`ComfyNodePool.acquire()` observes a process-local event. A control-file write in Flask cannot set that event or notify the worker’s condition. The ComfyUI submission wrapper sets `submission_unknown` before calling `submit()`, and the pool’s exception path can mark a healthy endpoint unhealthy if `JobPaused` propagates through it. RunningHub likewise sets the marker before each attempt. Composition and rewrite each have internal inference retry loops, so a single check outside the logical item can still permit fresh provider requests after Pause. `h3ref/comfy.py:452`, `h3ref/comfy.py:994`, `h3ref/comfy.py:1010`, `h3ref/film.py:772`, `h3ref/rh.py:121`, `h3ref/frames.py:132`, `h3ref/rewrite.py:322`.

Recommendation: Define a worker-side admission point before each fresh attempt and its unknown marker, observe control changes during bounded waits, and release a paused node lease without penalizing node health because the server cannot wake a condition in another process. Document that a request already past admission can still be accepted remotely.

**8. Delete can orphan a paid reroll, and the same rule can permanently block a settled single clip.**  
Severity: High

The five stated locations omit `inputs/segment-<revision>/state.json`. During a reroll, the staged state holds the new task while the root record can still describe the previous successful clip. The segment view already resolves the staged attempt and checks supersession. Conversely, a terminal `one` failure retains its task ID without a clip. A downloaded `one` clip can also exist before billing state is saved. File presence and task presence alone give the wrong answer in both directions. `workbench/segments.py:632`, `workbench/segments.py:635`, `workbench/segments.py:984`, `workbench/segments.py:1009`, `h3ref.py:47`, `h3ref.py:170`, `h3ref.py:176`, `h3ref.py:184`.

Recommendation: Derive blockers from the current revision, provider terminal flags, and durable completion criteria, reread them under exclusive ownership for Delete, and fail closed on unreadable state because a cached summary or previous clip cannot establish settlement. Test ordinary and forced Delete separately.

**9. “Retrieve pending results” can authorize fresh paid work.**  
Severity: High

The `/resume` exception at line 906 requires `remaining_pipeline`, at least one pending task, and no unknown sibling. A mixed pending/unknown job can therefore show a retrieval action the backend rejects. For an ordinary resumed pipeline without a blocker, `stage_generate()` schedules both saved tasks and unstarted segments. A retrieval label would understate what the click authorizes. `workbench/jobs.py:906`, `workbench/jobs.py:915`, `workbench/jobs.py:935`, `h3ref/film.py:979`, `h3ref/film.py:1064`.

Recommendation: Return one server-derived recovery action with explicit billing semantics, and reserve “Retrieve” for an operation that cannot dispatch fresh work because a creator can reasonably consent to collecting an existing result without consenting to another purchase.

### Security and HTTP contracts

**10. Open Folder can dispatch an application bundle.**  
Severity: High

The creation API accepts a custom output directory outside the registry. It checks emptiness and resolves its path at creation, but that path can subsequently be replaced. On macOS an `.app` bundle satisfies `is_dir()`. Passing it to the planned `open --` asks Launch Services to open the bundle. `--` protects argument parsing, not the choice between revealing a directory and launching an application. Historical jobs can legitimately live outside today’s configured output root. `workbench/jobs.py:525`, `workbench/jobs.py:529`, `workbench/jobs.py:532`, `workbench/directories.py:28`.

Recommendation: Use a reveal operation with explicit file-manager semantics, validate the current target against its registered identity, and test replaced directories, symlinks, bundles, and custom roots with a mocked launcher because directory type and client-side path omission do not establish safe dispatch.

**11. A nested film object defeats the prefill whitelist, and some valid jobs cannot round-trip through Clone.**  
Severity: Medium

`describe_references()` deep-copies the script, strips `cast[].picture`, and converts `board` to a boolean. It returns all other keys, including unknown nested metadata. Separately, creation stores normalized cumulative shot times, while fresh input enforces the raw 3,600-second limit. An existing regression fixture ends at `3600.4` and explicitly demonstrates the difference. `workbench/references.py:23`, `workbench/references.py:31`, `workbench/references.py:41`, `workbench/inputs.py:132`, `workbench/inputs.py:152`, `tests/test_dialogue_budget.py:283`.

Recommendation: Project supported fields recursively and emit a script that passes the normal Create validator because a top-level whitelist cannot sanitize nested metadata or make stored timelines valid raw input. Test `prefill → create`, including the existing long-timeline fixture.

**12. Optional JSON bodies and the 202 envelope need executable contracts.**  
Severity: Medium

Existing routes use both strict `get_json()` and `get_json(silent=True)` followed by an empty-object default. The former can reject a legitimate bodyless control POST with 415. The latter can turn malformed input into an unconditional action, losing an intended operation precondition. The browser helper returns the parsed body unchanged, while current action code treats successful bodies as job snapshots. DX’s `{status, operation_id, poll_url, job}` envelope therefore needs explicit unwrapping. `workbench/server.py:371`, `workbench/server.py:656`, `workbench/server.py:660`, `workbench/static/common.js:55`, `workbench/static/common.js:64`, `workbench/static/results.js:418`.

Recommendation: Accept a genuinely empty optional body, reject malformed or non-object bodies, require a literal JSON `true` for forced deletion, validate finite non-boolean wait values, and normalize the 200/202 response shape because transport success alone does not establish a valid control request or renderable snapshot.

### Logs, performance, and accounting

**13. The byte-cursor contract can stall, skip data, or expose part of a secret.**  
Severity: High

DX-H5 accepts arbitrary decimal offsets but only constrains the *next* boundary. An input offset can start in a UTF-8 sequence or a credential. The existing redactor replaces whole remembered strings, so redacting a suffix cannot remove a secret whose prefix was skipped. A line longer than `limit`, or a final line without a newline, can yield the same cursor indefinitely. Inode plus first-line hash also cannot distinguish an in-place truncate-and-regrow that preserves the first line. The existing snapshot already starts at an arbitrary byte boundary. `workbench/jobs.py:182`, `workbench/jobs.py:195`, `workbench/jobs.py:245`, `workbench/jobs.py:247`, `workbench/jobs.py:1051`.

Recommendation: Specify a raw-byte cursor independent of redacted output length, validate or realign incoming boundaries, and define bounded forward progress for oversized lines and terminal EOF because complete-line redaction, arbitrary offsets, and a hard read cap cannot all be satisfied by naïve slicing. Test multibyte text, a secret bisected by the cursor, same-header rotation, and repeated empty polls.

**14. The collapsed console still has expensive data sources, and a line cap does not bound memory.**  
Severity: Medium

Results polls every 1.4 seconds and reads up to 100,000 log bytes per snapshot. Ten active views can read and redact about **0.71 MB/s of log text alone** at that cap, before artifact enumeration and serialization. Runtime status performs remote probes and includes log bodies. Increasing the slot tail also leaves a separate unbounded `lines` list and an unterminated-line buffer in the Colab reader. `workbench/static/results.js:381`, `workbench/static/results.js:390`, `workbench/jobs.py:213`, `workbench/jobs.py:245`, `h3ref/colab_runtime.py:1186`, `h3ref/colab_runtime.py:1194`, `h3ref/colab_runtime.py:1939`, `h3ref/colab_runtime.py:1960`, `h3ref/colab_runtime.py:2053`.

Recommendation: Use `include_logs=0` for routine Results polls, one cursor reader for the visible source, cached counters for collapsed views, and byte limits for both reader and browser buffers because line counts and a Create-only incremental endpoint leave the dominant costs intact. Verify zero log-body reads for collapsed polls and measure ten concurrent views.

**15. The summary cache introduces steady-state I/O and misses authoritative invalidation inputs.**  
Severity: Medium

Current history summaries select up to 50 in-memory records and return before disk access. Checking seven filesystem entries for each of 50 jobs adds roughly 350 metadata checks per warm history refresh before any cache miss. Meanwhile, `frame-edit.json`, `segment-edit.json`, and staged revision state can change the Delete badge without touching the proposed key. A cold calculation can also finish after eviction and repopulate obsolete data. `workbench/jobs.py:234`, `workbench/jobs.py:238`, `workbench/jobs.py:424`, `workbench/segments.py:28`, `workbench/segments.py:633`, `workbench/storyboard.py:127`.

Recommendation: Serve settled rows from persisted summaries, explicitly invalidate active revisions on publication, and verify record generation before publishing cache results because history polling should not repeatedly scan every settled output or resurrect a deleted entry. Benchmark warm and cold refreshes separately and count filesystem calls.

**16. The cost label contradicts an existing accounting regression test.**  
Severity: Medium

A successful frame replacement stores `previous.cny + pending.cny`. The existing test asserts `0.5` after replacing a `0.2` image with a `0.3` image. Summing current frame records therefore includes at least some discarded rerolls. ComfyUI also derives execution duration from provider timestamps when available, which does not establish billed GPU occupancy. `workbench/storyboard.py:279`, `workbench/storyboard.py:281`, `tests/test_workbench.py:223`, `tests/test_workbench.py:246`, `h3ref/comfy.py:465`, `h3ref/film.py:902`.

Recommendation: Label the historical values according to their recorded semantics and preserve unknown durations because “kept assets only,” “discarded rerolls excluded,” and “GPU seconds” make stronger claims than the stored data supports.

### Test coverage and deployment

**17. The planned browser tests can pass with missing elements and broken focus behavior.**  
Severity: Medium

The UI fixture manufactures a node for every requested ID, gives `focus()` no behavior, and stores only one listener per event type. A Results fixture never resolves language initialization. Those tests cannot prove native hidden-tab validation, modal focus restoration, real template IDs, or command-bar layout. One concrete layout dependency is already visible: `.frame-editor` is nested inside a card, so setting its `grid-column` cannot span the outer storyboard grid. `tests/test_workbench_ui.js:33`, `tests/test_workbench_ui.js:37`, `tests/test_workbench_ui.js:42`, `tests/test_results.js:23`, `tests/test_results.js:32`, `workbench/static/results.js:103`, `workbench/static/results.js:175`.

Recommendation: Make a real rendered-template browser smoke test a release gate and place the spanning editor or its owning card at the correct grid level because VM stubs cannot establish focus, event propagation, element existence, or computed geometry. Include delayed Stop responses, both locales, and the narrow sticky launch bar.

**18. Hard Stop can publish damaged demo artifacts, weakening the proposed end-to-end test.**  
Severity: Medium

The demo saves PNGs directly, treats an existing PNG as complete, invokes FFmpeg with the canonical clip as output, and copies a single clip directly onto the final. Termination during those writes can leave an invalid asset or destroy the previous downloadable version. The live join helper already uses a temporary file and atomic replacement. A film created through the normal endpoint also stops at frame review, so a documented demo expecting a final film must exercise the confirmation step. `workbench/demo.py:41`, `workbench/demo.py:90`, `workbench/demo.py:118`, `workbench/demo.py:137`, `h3ref/media.py:74`, `workbench/jobs.py:514`, `tests/test_review_flow.py:60`.

Recommendation: Publish demo assets through validated temporary files and make the documentation smoke test follow the actual review flow because the offline path is the plan’s main CI surrogate for interruption and previous-version recovery.

**19. Quiescence alone does not make downgrade safe.**  
Severity: High

The old reader recovers only `queued` and `running`. Its normal resume gate recognizes `failed` and `interrupted`, subject to special film exceptions. A fully quiescent `paused` or `stopped` single-clip job can therefore become inaccessible after downgrade. Restoring a pre-upgrade backup after new paid submissions can also discard the latest task IDs. Existing native process tests are relevant here: the POSIX mocks omit `getpgrp()`, and the Windows suite includes real descendant and owner-crash cases that a Linux-only green run cannot establish. `workbench/jobs.py:174`, `workbench/jobs.py:267`, `workbench/jobs.py:867`, `tests/test_windows_processes.py:128`, `tests/test_windows_processes.py:138`, `tests/test_windows_processes.py:393`, `tests/test_windows_processes.py:408`.

Recommendation: Add an offline downgrade conversion or version guard that preserves the latest task journals, and run the previous reader against newly stopped single-clip and paused-film fixtures because three reversible Git commits do not make persisted state backward-compatible. Require native Windows process-tree coverage before packaging.

### Six-dimension engineering verdict

| Dimension | Verdict |
|---|---|
| Architecture | Block on explicit ownership of the output directory, process tree, and operation result. |
| Test coverage | Add deterministic pause/stop interleavings, staged paid-task fixtures, a real browser gate, and native Windows termination coverage. |
| Perf risks/budgets | Set and verify budgets for collapsed log reads, warm-history filesystem calls, remote probes, retained bytes, and ten-view polling. |
| Security threats | Resolve application-bundle dispatch, nested prefill disclosure, malformed optional controls, and cursor-boundary redaction. |
| Error paths | Specify recovery for partial control writes, failed launch registration, failed cleanup, and accepted tasks outside the current executor queue. |
| Deployment risk | Block downgrade until the previous release can recover current paid work without restoring stale task identities. |

VERDICT: FINDINGS
```

#### ENG DUAL VOICES — CONSENSUS TABLE

```
ENG DUAL VOICES — CONSENSUS TABLE:
  Dimension                           agy (in-host)                           Codex                                  Consensus
  1. Architecture sound?              Gaps: Windows mutex, RunState, common.js Gaps: Process-tree/RunState/CLI lease  CONFIRMED (fixed by ENG-H1/H2/H7)
  2. Test coverage sufficient?        Gaps: VM crashes, 6 regression suites   Gaps: interleavings, browser, Win CI   CONFIRMED (fixed by ENG-H7/H8 + Test Plan)
  3. Perf risks + budget/bench named? Gaps: 100KB diff, summaries() stat storm Gaps: 0.71MB/s 10-view, Colab mem cap  CONFIRMED (fixed by ENG-H5/H6)
  4. Security threats covered?        Gaps: .app open, bodyless 415, ID check Gaps: .app open, nested prefill, cursor CONFIRMED (fixed by ENG-H4/H5)
  5. Error paths handled?             Gaps: 3 missed excepts, staged DELETE   Gaps: partial write, drain, one DELETE CONFIRMED (fixed by ENG-H1/H2/H3)
  6. Deployment risk manageable?      Gaps: H3_COLAB_MAX_LOGS, Win DELETE     Gaps: downgrade paused/stopped + demo  CONFIRMED (fixed by ENG-H6/H8)
CONFIRMED = native + outside agree; primary cannot replace outside. DISAGREE -> taste.
Zero new Eng disagreements -> gate; all 6 dimensions 100% CONFIRMED and resolved via ENG-H1..ENG-H8.
```

---

### Section 1: Architecture Review

#### System & Component Dependency Diagram (ASCII)
```
Browser (Zero-Build Vanilla JS, CSP: script-src 'self')
  ├── base.html ──> common.js (state, request(), t(), renderConfig/Runtime)
  │             ├── shell.js  (Topbar Popovers, 3-Tab Settings Drawer, Toast, Modals, LIFO Esc/⌘J)
  │             └── console.js (Single Incremental /logs Poller, Traceback Classifier, Resize/Storage)
  ├── index.html ──> create.js (2-Click Demo, Batch Drop <=9/28MiB, Conflict Tray, Cast Groups, Draft)
  └── results.html ─> results.js + video-segments.js (<=56px Sticky Bar, ?include_logs=0, 3 Tabs)
                           │  HTTP (X-Workbench: 1, RFC 9457 application/problem+json)
                           ▼
Flask Server (workbench/server.py — _optional_json_object, job_problem, api_problem)
  ├── Jobs (workbench/jobs.py — RLock, RunState[op_id], settled 0-stat summaries, unsettled_tasks)
  │     ├── POSIX: fcntl worker.lock + bounded stop_posix_group (PID guard)
  │     └── Windows: WindowsJob (ActiveProcesses) + windows_process_alive (NEVER worker_locked)
  ├── ColabRuntimeManager (h3ref/colab_runtime.py — bounded 64KB line / 200-line ring + alert_seq)
  └── Isolated Worker Subprocess (H3_WORKBENCH_WORKER=1, H3_WORKBENCH_OPERATION_ID=<op_id>)
        ├── h3ref/control.py  (STDLIB-ONLY: should_pause, check_pause, JobPaused, EXIT_CODE_PAUSED=75)
        ├── h3ref/film.py + frames.py + comfy.py + rh.py + rewrite.py (pre-POST admission + drain)
        ├── workbench/storyboard.py + workbench/segments.py (staged segment-<rev>/state.json)
        └── workbench/demo.py (atomic .tmp + config.atomic_replace)
```

#### Two Shapes Before One (Design-Twice)
- **Shape A (Chosen — Run-Scoped File Control + Server `RunState` + Split Zero-Build Modules)**: `h3ref/control.py` is a deep stdlib-only boundary hiding env-var scoping (`H3_WORKBENCH_WORKER` + `H3_WORKBENCH_OPERATION_ID`), corrupt-JSON tolerance, `JobPaused`, and `EXIT_CODE_PAUSED=75`; `workbench/jobs.py` owns `RunState` + `unsettled_tasks()`; `workbench/static/{console,shell}.js` isolate new shell UI from `common.js`.
- **Shape B (Alternative — Local Loopback Control Socket / HTTP Poll Endpoint + Monolithic `common.js`)**: Workers poll a local HTTP/IPC socket owned by Flask for pause/stop commands; all frontend shell logic lives inside `common.js`.
- **Depth Comparison**: Shape B introduces per-worker IPC auth, port allocation, Windows named-pipe divergence, and fails when Flask restarts mid-drain, all to convey one cooperative bit. Shape A hides every OS/scoping/corruption concern behind two functions (`should_pause`, `check_pause`), stays crash-durable on disk, preserves zero reverse `h3ref -> workbench` imports, and keeps `common.js` modular under 1,000 lines.

#### Eight Red Flags Screen
1. **Shallow module**: Clean (`h3ref/control.py` hides env scoping, JSON errors, and exit contract behind 2 calls).
2. **Information leakage**: **Fixed** (`ENG-H4` recursive `prefill` projection; `ENG-H3` centralizing staged `segment-<rev>/state.json` inside `unsettled_tasks()`).
3. **Temporal decomposition**: Clean (modules partitioned by state/domain ownership).
4. **Pass-through method**: Clean (`job_problem` adds domain anchor, recursive redaction, and correlation trace policy).
5. **Split ownership**: **Fixed** (`ENG-H2` single `RunState` owner for `Jobs.run()` vs `T_stop`; `ENG-H5` single golden JSON fixture for Python/JS alert classification).
6. **Two ways to do one task**: **Fixed** (`ENG-H5` unified incremental `/logs` reader on both `/` and `/jobs/<id>`; `ENG-H4` single `_optional_json_object()` parser).
7. **Importable internals**: Clean (`h3ref/` never imports `workbench/`).
8. **Hand-synced list**: **Fixed** (`ACTIVE_STATUSES`/`RESUMABLE_STATUSES`/`SETTLED_STATUSES` constants + shared golden alert fixture).

#### Worktree Parallelization Strategy (If Implemented in Parallel)

| Step | Modules Touched | Depends On |
|---|---|---|
| Lane A (Backend Engine & APIs) | `h3ref/`, `workbench/jobs.py`, `workbench/server.py`, `workbench/segments.py`, `workbench/storyboard.py`, `workbench/demo.py`, `scripts/prepare_downgrade.py` | — |
| Lane B (Docs & Golden Fixture) | `docs/`, `README.md`, `SKILL.md`, `tests/fixtures/log_alerts_golden.json` | Lane A contract freeze |
| Lane C (Global Shell & Console) | `workbench/templates/base.html`, `workbench/static/{common,console,shell}.js`, `workbench/static/style.css` | Lane A API shapes |
| Lane D (Create & Results Pages) | `workbench/templates/{index,results}.html`, `workbench/static/{create,results,video-segments}.js` | Lane C shell hooks |

**Execution order**: Complete Lane A first (shared API/state contract), then run Lane B + Lane C in parallel (disjoint files), merge both, then run Lane D + real browser smoke pass. Conflict flag: `workbench/static/messages/{zh-CN,en}.json` and `workbench/static/style.css` are touched by C and D — sequence C before D.

---

### Section 2: Code Quality Review

#### Shared-Code Evaluation Rubric
1. **`unsettled_tasks(job_dir, kind)` in `workbench/jobs.py`** (Callers: `Jobs.summaries()` + `Jobs.delete()`): Reuses `frames.task_pending()`, `segments.task_unresolved()`, and `segments.pending_superseded()` (~45 lines added, replaces ~70 lines of divergent checks; net savings ~25 lines + eliminates the `kind=="one"` and staged-reroll blind spots; tested via shared contract + caller tests).
2. **`_optional_json_object()` in `workbench/server.py`** (Callers: `pause_job`, `stop_job`, `open_job_folder`, `delete_job`): ~18 lines replacing 4 ad-hoc `get_json()` blocks (~32 lines); net savings ~14 lines + eliminates bodyless `415` and silent malformed-JSON acceptance.
3. **`workbench/static/{console,shell}.js` split from `common.js`**: Prevents `common.js` from growing from `925` lines to `~2,000` lines; zero runtime dependency overhead (`<script defer>` in `base.html`), zero breakage to existing `vm.runInContext(common.js)` harnesses.
4. **Rejected extraction**: Do NOT share a unified Python/JS regex library across languages; keep minimal independent implementations synchronized by a single checked-in golden fixture `tests/fixtures/log_alerts_golden.json`.

---

### Section 3: Test Review (Codepath-to-Test & User-Flow Diagram)

**Test Frameworks Detected**: Python `unittest` (`python -m unittest discover -s tests -v`, 37 test files) + Node 22 built-in `node:test` (`node --test tests/test_results.js tests/test_frame_character_picker.js tests/test_video_segment_prompts.js tests/test_workbench_ui.js`, 3,531 lines) + real headless browser smoke gate (`ENG-H8`).
**LLM / Prompt Eval Scope**: None required (`Non-Goal #3`: zero changes to storyboard prompt rules, H3 six-section rewrite prompts, or ComfyUI/RunningHub workflow graphs).

```
CODE PATHS                                               USER FLOWS
[+] h3ref/control.py & Worker Admission                  [+] Onboarding & Creation (/)
  ├── should_pause / check_pause (op_id + worker=1)        ├── [GAP] [→E2E] 2-click Try Offline Demo -> Storyboard
  │   ├── [GAP]        Corrupt control.json degrade        ├── [GAP]        Batch drop (>9 files, >28MiB, partial retry)
  │   ├── [GAP]        Foreign op_id ignored by CLI        ├── [GAP]        Short <2 char guard + Undo auto-match
  │   └── [GAP]        Exit 75 stderr JOB_PAUSED           ├── [★★ TESTED]  Cast groups & >9 refs (test_workbench_ui.js)
  ├── stage_frames.draw (film.py:413)                      └── [GAP]        Clone ?from= + draft backup/undo + 3600.4
  │   ├── [GAP]        Post-upload JobPaused (no error!) [+] Pause / Stop / Recovery (/jobs/<id>)
  │   └── [GAP]        Pre-existing task drained first     ├── [GAP] [→E2E] Pause mid-run -> drain -> Resume (0 dupes)
  ├── ComfyUI & RunningHub submit_with_retry               ├── [GAP] [→E2E] Stop Local Worker -> 200/202/500 -> Resume
  │   ├── [GAP]        RH 60s backoff Pause interrupt      ├── [GAP]        409 stale op_id -> refreshJob() recovery
  │   ├── [GAP]        Comfy failover JobPaused (748/772)  ├── [GAP]        DELETE blocked (unsettled) vs ?force=1
  │   └── [GAP]        Parallel RuntimeError > JobPaused   └── [★★ TESTED]  Manual seg/frame edit (test_results.js)
[+] workbench/jobs.py (RunState & Persistence)           [+] Unified Console & Shell (All Pages)
  ├── Jobs.run() vs T_stop settlement                      ├── [GAP]        Collapsed unread badge matches golden fixture
  │   ├── [GAP]        stop(A)->resume(B)->return stop(A)  ├── [GAP]        ?include_logs=0 + incremental /logs Tab 1
  │   ├── [GAP]        cleanup_failed + late code==0       ├── [GAP]        Traceback filter + scroll lock + 2K cap
  │   └── [GAP]        Windows nt cleanup_failed mutex     ├── [GAP] [→E2E] Real browser 4-viewport x 2-locale layout
  ├── unsettled_tasks() (5+staged locations)               └── [GAP] [→E2E] Keyboard focus trap, LIFO Esc, hidden-tab jump
  │   ├── [GAP]        Staged segment-<rev>/state.json   [+] Error & Deployment States
  │   ├── [GAP]        kind=="one" terminal_failed=True    ├── [GAP]        Deleted output_dir degraded /jobs/<id>
  │   └── [GAP]        Corrupt JSON -> bootstrap 200       ├── [GAP]        macOS .app rejected / headless Linux 400
[+] workbench/server.py (6 New Routes + RFC 9457)          └── [GAP]        Downgrade paused/stopped + atomic demo stop
  ├── [GAP]        _optional_json_object (bodyless, bad JSON, 405)
  ├── [GAP]        GET /logs (150KB line, bisected secret, UTF-8, reset)
  └── [★★★ TESTED] api_problem() base & film_references (test_dialogue_budget.py)

COVERAGE (New/Modified Scope): 3/31 paths tested (10%)  |  Code paths: 1/17 (6%)  |  User flows: 2/14 (14%)
QUALITY: ★★★:1 ★★:2 ★:0  |  GAPS: 28 (all mapped to ENG-T1..T7 + Test Plan Artifact, 5 marked [→E2E])
```
Legend: `★★★` behavior + edge + error | `★★` happy path | `★` smoke check | `[→E2E]` needs integration/browser test.

- **Test Plan Artifact Written**: `/Users/qiaoyu/.gstack/projects/garycaogan-h3-ref-video/qiaoyu-main-eng-review-test-plan-20261127-004000.md` (with Value cards for every Edge Case and Critical Path).
- **Regression Rule**: All 3,531 lines of existing JS VM tests and 37 Python suites remain mandatory green; `ENG-H7` preserves VM compatibility (`response.text()` / `localStorage`) while `ENG-H8` adds real rendered-template browser coverage.

---

### Section 4: Performance Review (Concrete Budgets & Hot Paths)

| Hot Path | Baseline / Pre-Fix Risk | Scale | Target Budget & Verification (`ENG-H5`, `ENG-H6`) |
|---|---|---|---|
| Results poll (`GET /api/jobs/<id>?include_logs=0`) | `100KB` log read + full-string redaction every `1.4s` (`~0.71 MB/s` across 10 views) | 10 concurrent views, `>100KB` log | **0 bytes of `.log` read** when console collapsed (`?include_logs=0`); verified by asserting zero log file opens in collapsed poll test |
| History poll (`Jobs.summaries(50)`) | ~7 `stat()` syscalls × 50 jobs = **~350 syscalls every `1.8s`** (`~194 syscalls/s`) | 50 settled jobs | **0 filesystem `stat()` calls** on warm settled rows (persisted in `record` + in-memory cache; active jobs cache outside `self.lock`); count syscalls in benchmark test |
| Incremental log read (`GET /api/jobs/<id>/logs`) | Unbounded read or O(N) re-read | 10 MB log file | Bounded `<= 100KB` default (`<= 256KB` max) per call, O(chunk) seek, `<= 4KB` line-start lookback |
| Browser Console DOM & Colab Reader Memory | Unbounded DOM lines + unbounded Colab `lines` / unterminated line buffer | 2-hour Colab session | Browser capped at `<= 2,000` lines (`<= 512 KB`); Colab reader line buffer `<= 64 KB`, ring `H3_COLAB_MAX_LOGS` (`default 200`, max `2000`) |
| `POST /api/jobs/<id>/stop` latency | Unbounded `child.wait()` (infinite hang) + full snapshot I/O outside wait budget | Worst-case stuck child | `Event.wait(wait_seconds)` bounded `0..15s` (`default 1.5s`); POSIX `child.wait(timeout=5)` bounded; total worst-case HTTP latency explicitly distinguished from `Event.wait` |

---

### Phase 3 Required Outputs

#### NOT in scope (Eng)
- Cross-platform exclusive lifetime lock for direct CLI `h3ref.py film`/`one` (deferred to `TODOS.md` as `ENG-E1`; Workbench Resume is the primary supported recovery path).
- SSE / WebSocket push transport (`CEO-E6`, deferred to `TODOS.md`).
- Changes to H3 rewrite prompts, storyboard composition prompts, or ComfyUI/RunningHub workflow JSON (`Non-Goal #3`).

#### What already exists (Eng)
- `workbench/server.py:283-348` (`api_problem()` RFC 9457), `workbench/processes.py` (`WindowsJob`, `windows_process_alive`), `workbench/segments.py:627-642` (`attempt_record`, `task_unresolved`, `pending_superseded`), `h3ref/frames.py:77` (`task_pending`), `h3ref/config.py:53` (`atomic_replace`), `h3ref/film.py:1345` (`H3_WORKBENCH_WORKER`), and `tests/test_windows_processes.py` (native Windows process-tree suite).

#### Failure Modes Registry (Final Post-Eng Audit)

| Codepath / Integration | Realistic Production Failure | Error Handling & Test Coverage | User Experience | Critical Gap? |
|---|---|---|---|---|
| `stage_frames.draw` (`film.py:413`) | Pause fires after APIMart upload, before generation POST | `except JobPaused: return` (`ENG-H1`); regression in `ENG-T1` | Clean exit `75`, shot stays clean & resumable | No (resolved) |
| ComfyUI multi-node failover (`film.py:748,772`) | Pause fires mid-upload or mid-failover while 2nd node healthy | `except JobPaused: raise` + `acquire()` 0.5s check (`ENG-H1`); `ENG-T1` | Healthy nodes stay online; in-flight drains cleanly | No (resolved) |
| Windows `cleanup_failed` (`jobs.py:91,451`) | Child resists termination on Windows (`worker_locked() == False`) | POSIX-only lock clear; Windows checks `WindowsJob` + `windows_process_alive` (`ENG-H2`); `ENG-T2` | Workbench stays safely locked until child dies; clear RFC 9457 `500` | No (resolved) |
| `stop(A) -> resume(B)` race (`jobs.py:782,1093`) | Another tab resumes job as B before Stop A returns | Immutable `RunState.settlement_snapshot` + `child`-identity eviction (`ENG-H2`); `ENG-T2` | Stop A returns A's exact status; never corrupts B | No (resolved) |
| `DELETE` with staged reroll (`segments.py:632`) | User deletes history while single-segment reroll has live `task` | `unsettled_tasks()` reads `segment-<rev>/state.json` under lock (`ENG-H3`); `ENG-T3` | Blocked with RFC 9457 `400` naming exact segment & task | No (resolved) |
| `GET /logs` oversized line / bisected secret | 150KB single error line or offset mid-secret | UTF-8 forward progress + 4KB line-start redaction lookback (`ENG-H5`); `ENG-T5` | Continuous streaming, zero stalls, zero secret leak | No (resolved) |
| `open-folder` macOS `.app` / headless Linux | Output dir replaced by `.app` or SSH session has no `DISPLAY` | Reject `.app`/symlinks + `open -a Finder --` + headless `400` (`ENG-H4`); `ENG-T4` | Safe error with copyable path; zero app execution | No (resolved) |
| Legacy Node VM suite on new `common.js` | Mock `localStorage` returns `"en"` & mock `fetch` lacks `.text()` | Feature-detect `.text()` + guarded `JSON.parse()` (`ENG-H7`); `ENG-T6` | All 3,531 existing JS test lines stay green | No (resolved) |
| Downgrade with `paused`/`stopped` records | Operator rolls back to pre-UX-overhaul commit | `scripts/prepare_downgrade.py` converts statuses preserving `state.json` (`ENG-H8`); `ENG-T7` | Old release resumes all paid tasks safely | No (resolved) |

**Critical Gaps Flagged**: **0 unmitigated** (all 9 critical failure modes mitigated via `ENG-H1..ENG-H8` and mapped to `ENG-T1..ENG-T7`).

#### Implementation Tasks (Phase 3 Eng)
- [ ] **ENG-T1 (P0, human: 6h / CC: 25min)** — `h3ref/control.py, film.py, frames.py, comfy.py, rh.py, rewrite.py, workbench/segments.py` — 3 missed `JobPaused` catch-sites (`film.py:413,748,772`), pre-`submission_unknown` admission + `ComfyNodePool.acquire()` 0.5s check, drain pre-existing tasks first, parallel error precedence.
- [ ] **ENG-T2 (P0, human: 6h / CC: 30min)** — `workbench/jobs.py, processes.py, h3ref.py` — Fix Windows mutex bypass (POSIX-only `worker_locked`), `RunState` immutable settlement + child-identity eviction, `Jobs.run()` vs `T_stop` serialization, registry-before-control write ordering, `cmd_one` POSIX lock check.
- [ ] **ENG-T3 (P0, human: 5h / CC: 25min)** — `workbench/jobs.py, segments.py, server.py` — Fix `unsettled_tasks()` (`segment-<rev>/state.json`, `one` `terminal_failed`, corrupt-JSON safe `summaries()`), server-derived `recovery_action`, 0-stat persisted summaries, graceful deleted-`output_dir` `_snapshot()`.
- [ ] **ENG-T4 (P0, human: 5h / CC: 25min)** — `workbench/server.py, jobs.py, references.py` — Flat `200`/`202` Stop snapshot (`+poll_url`), `_optional_json_object()`, macOS `.app`-safe `open-folder`, recursive `prefill` projection + `3600.4` round-trip, ID validation + `server.py:345` `OSError` + `405` RFC 9457.
- [ ] **ENG-T5 (P0, human: 6h / CC: 25min)** — `workbench/jobs.py, server.py, h3ref/colab_runtime.py, workbench/static/console.js, results.js` — Zero-stall/secret-safe `/logs` cursor, unified `?include_logs=0` + incremental `/logs` reader on both pages, bounded browser/Colab log buffers, shared Python/JS golden alert fixture.
- [ ] **ENG-T6 (P1, human: 6h / CC: 30min)** — `workbench/static/{common,console,shell,results,create}.js, base.html, style.css` — Extract `console.js` & `shell.js`, Node VM `response.text()` / `localStorage` guards, `.frame-card.is-editing` grid span fix, `results.js` `controlSubmitting`/`jobRevision`, `create.js` `one` 9-file cap / conflict-tray sync / Undo invalidation.
- [ ] **ENG-T7 (P1, human: 5h / CC: 25min)** — `workbench/demo.py, scripts/prepare_downgrade.py, tests/` — Atomic demo writes, offline downgrade helper + prior-reader fixture test, real rendered-template 4-viewport×2-locale browser smoke gate + Windows CI termination lane.

#### Eng Review Completion Summary
- Step 0: Scope Challenge — `scope accepted as-is` (`MODE: FULL_REVIEW`)
- Reality rows: `7/7` applicable rows emitted (`PLAN_REALITY` verified `verdict=complete`), `0` unverified premises (`unverified=0`)
- Architecture Review: 5 issues found (all resolved via `ENG-H1`, `ENG-H2`, `ENG-H4`, `ENG-H5`, `ENG-H7`)
- Code Quality Review: 4 issues found (all resolved via `ENG-H3`, `ENG-H4`, `ENG-H5`, `ENG-H7`)
- Test Review: diagram produced (`3/31` new/modified paths currently covered, `28` gaps identified and mapped to `ENG-T1..ENG-T7` + Test Plan Artifact on disk)
- Performance Review: 5 hot paths budgeted (`0` log bytes when collapsed, `0` stat syscalls on warm settled history, bounded buffers)
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 5 items written across all phases (`CEO-E6`, `CEO-E10`, `CEO-E11`, `CEO-E12`, `ENG-E1`)
- Failure modes: 0 unmitigated critical gaps flagged (9 critical modes mitigated)
- Unresolved decisions: 0 new in Eng (all 7 cross-phase Gate items `uc1`, `uc2`, `d1`–`d5` queued for Phase 4 Final Approval Gate)
- Outside voice: Codex (`gpt-6.2-astra`), completed (`outside_s=435`, 19 findings); Native subagent (`2288a2f6-1d29-450f-9b7a-e270d548eaca`), completed (`native_s=474`, 18 findings); Consensus `6/6` confirmed
- Parallelization: 4 lanes (Lane A sequential foundation -> Lanes B + C parallel -> Lane D + Browser QA)
- Lake Score: `8/8` (100% complete remedies chosen)

---

## Phase 3 (Cycle 2): Eng Re-Review After Approved `UC2A` (3-PR Sequential Split)

### Cycle 2 Scope & Reality Verification
- **Trigger**: User replied `rec` at `gate_rev=1`, approving User Challenge `uc2a` (3 sequential PRs: PR 1 Backend Engine/APIs/Docs → PR 2 Global Shell & Console → PR 3 Create/Results & Real Browser Smoke Gate) along with `uc1a` and `d1a–d5a`.
- **Dual Voices Coverage**:
  - **agy (in-host) SUBAGENT (Eng Cycle 2 — independent review)**: `def29b7a-6bf4-4908-b07a-5cee49bccf0a` (`native_s = 659`, verified header `INPUT: eng fdcfb87b4990879aec550bcbb91333b5a3fcabaf1360139a82a503005d82f5b7`), 14 findings (3 Critical: `A1` inline stdout deadlock, `E1` `st_ctime_ns`/`prefix_4k` append reset, `E2` `CancelledError` beating `JobPaused`; 6 High: `A2`, `A3`, `A4` PR 2 breakages, `E3` read-only archive lock deadlock, `E4` demo `.tmp`/glob dotfile bug, `H1` two-pass frame lockless read; 5 Medium: `E5`, `E6`, `E7`, `S1`, `S2`, `H2`, `H3`) + 10 targeted regression tests.
  - **Codex SAYS (eng Cycle 2 — architecture & PR-split challenge)**: `OUTSIDE_STATUS: completed provider=codex host=agy` (`model: gpt-6.2-astra`, `outside_s = 391`, `VERDICT: findings`, `FINDINGS: P1`), 16 findings (15 High, 1 Medium).

```tool-output
**Cycle 2 decision: revise before PR 1 merges.** The three-PR split can work, but the current plan leaves paid-work recovery, worker settlement, and both intermediate versions of `main` with observable failures. This was a source and plan review. The proposed implementation and its tests were not run. `eng-implementation.md` below refers to the supplied amended plan.

### Worker control and paid-work recovery

**1. A clean stop can remain `stopping` indefinitely**  
Severity: High  
`workbench/jobs.py:1049` reads `child.stdout` to EOF before reaching `child.wait()` at `workbench/jobs.py:1053`. If the owned tree exits successfully while another process retains a pipe writer, the cleanup-failure handler never fires and the finalizer never releases `self.children`. ENG-H2 also permits assembly bookkeeping after leader exit, before tree cleanup has succeeded. `eng-implementation.md:333`, `workbench/jobs.py:455`, `workbench/jobs.py:1072`, `workbench/jobs.py:1090`.

Recommendation: Give one process-tree waiter authority to settle the run, bound log draining independently, and commit success bookkeeping only after verified cleanup because leader exit, tree exit, and pipe EOF are three separate events.

**2. POSIX ownership checks can block every job or release ownership too early**  
Severity: High  
`worker_locked()` opens an existing lock with `a+b`, and `available()` probes every historical record. Under ENG-H2, an unlocked, readable `0444` lock in an archived output can block all launches. Conversely, group cleanup treats `PermissionError` like `ProcessLookupError`, so it can report success without verifying that descendants exited. `eng-implementation.md:329`, `workbench/jobs.py:99`, `workbench/jobs.py:459`, `workbench/jobs.py:133`, `workbench/jobs.py:145`.

Recommendation: Probe existing lock files read-only, distinguish missing, contested, and inaccessible ownership, and treat permission-denied group cleanup as unverified because neither archive permissions nor a dead leader establish whether a worker remains.

**3. Restart loses the runtime-stop protection for a live orphan**  
Severity: High  
Recovery marks a locked orphan `interrupted`. The new server starts with an empty `children` map. Adding children to the active-work checks therefore still lets the ordinary Colab Stop path see no local video work while the orphan drains. `eng-implementation.md:211`, `eng-implementation.md:333`, `workbench/jobs.py:160`, `workbench/jobs.py:438`, `workbench/server.py:666`.

Recommendation: Use the same verified ownership predicate for launch admission, configuration changes, and runtime shutdown because a service restart discards the child handle while the worker can continue using compute.

**4. Pause can become failure, and a genuine provider failure can become pause**  
Severity: High  
ENG-H1 gives every non-`JobPaused` exception precedence. `stage_generate` cancels queued futures and then calls `result()` on them, so its own `CancelledError` qualifies as a real failure. Frame drawing instead consumes ordinary exceptions, and both rewrite collectors stop inspecting results after the first raised future. `eng-implementation.md:327`, `h3ref/film.py:413`, `h3ref/film.py:612`, `h3ref/film.py:1083`, `workbench/segments.py:1389`.

Recommendation: Collect all admitted outcomes, ignore futures deliberately cancelled before starting, and give genuine in-flight failures priority over pause because executor iteration order must not decide the persisted job status.

**5. The two-pass frame drain can schedule the same shot twice**  
Severity: High  
Pass 1 schedules accepted tasks. Pass 2 can read the same still-pending record before its download finishes. `Job.put()` replaces records under a lock, while the planning loop reads records and file existence separately. A saved `task` can also belong to a completed shot. `eng-implementation.md:325`, `h3ref/film.py:333`, `h3ref/film.py:421`, `h3ref/film.py:425`, `h3ref/film.py:473`.

Recommendation: Snapshot eligible pending keys under `job.lock`, exclude those keys from second-pass dispatch, and retain the shot-order composition walk because one accepted task needs exactly one downloader while later shots still need their composition context.

**6. Admission checks miss inner retries and can skip accepted tasks at stage boundaries**  
Severity: High  
Checking `rewrite()` at its outer loop misses `_llm()`’s three-attempt HTTP loop. A pause during transport backoff can still be followed by another paid POST. Conversely, a blanket pause check between stages can exit a resumed pipeline before `stage_generate()` retrieves previously accepted video tasks. `eng-implementation.md:324`, `h3ref/rewrite.py:274`, `h3ref/rewrite.py:322`, `h3ref/http.py:560`, `h3ref/http.py:597`, `h3ref/film.py:937`, `h3ref/film.py:1430`.

Recommendation: Check admission at each actual fresh POST and route paused stage transitions through a retrieval-only drain because a retry is fresh work while polling an accepted task recovers existing work.

**7. “Retrieve pending results” has no enforceable no-charge contract**  
Severity: High  
CEO-H3 disables `#resume-job` whenever `can_resume_video === false`. DES-H1 enables retrieval for some of those same records. ENG-H3 adds a response label, but the button posts `{}`, the server recomputes the operation, and the ordinary pipeline can schedule untouched segments alongside accepted ones. `eng-implementation.md:220`, `eng-implementation.md:239`, `eng-implementation.md:338`, `workbench/static/results.js:24`, `workbench/static/results.js:469`, `workbench/jobs.py:906`, `h3ref/film.py:979`.

Recommendation: Make the server action authoritative for button behavior and require an operation-scoped retrieval-only intent that forbids fresh submissions because the user’s billing consent must survive a stale snapshot or concurrent resume.

### Logs, settlement, and artifacts

**8. Normal appends invalidate the `/logs` generation**  
Severity: High  
The proposed token includes POSIX `st_ctime_ns`, which changes on writes, and a first-4-KiB hash, which changes as a short log grows. The existing writer flushes each line. Normal activity can therefore trigger repeated rotation resets and replay the same prefix. `eng-implementation.md:346`, `workbench/jobs.py:1012`, `workbench/jobs.py:1051`.

Recommendation: Use an append-stable log identity and detect truncation or replacement separately on the opened file because an ordinary append must preserve every previously issued cursor. Test short-file growth, growth past 4 KiB, truncation, and equal-size replacement on both operating systems.

**9. The byte cursor cannot guarantee progress and safe redaction as specified**  
Severity: High  
With `limit=1`, a multibyte character may have no complete code point in the read. Rewinding a mid-line cursor can repeatedly return the same limited prefix. A 4-KiB lookback also cannot guarantee a complete logical line, and the current redactor matches whole secret strings. `eng-implementation.md:309`, `eng-implementation.md:346`, `workbench/jobs.py:182`.

Recommendation: Define a monotonic raw-byte cursor and a bounded safe policy for oversized records, such as consuming and replacing the entire record, because rewinding or returning arbitrary fragments can duplicate output, stall polling, or disclose part of a credential.

**10. A snapshot error can strand a successfully persisted settlement**  
Severity: High  
ENG-H2 requires a full immutable snapshot before signalling waiters. Snapshot construction reads artifacts, logs, and state, and can raise after three consistency retries. The proposed missing-request fallback runs after `read_context()`, where valid JSON of the wrong shape can raise `AttributeError` or `TypeError`. The same malformed state must not enable Delete. `eng-implementation.md:331`, `eng-implementation.md:337`, `eng-implementation.md:352`, `workbench/jobs.py:228`, `workbench/jobs.py:241`, `workbench/segments.py:32`.

Recommendation: Freeze a minimal run-specific outcome and signal immediately after durable settlement, validate state shapes before enrichment, and fail closed on unreadable deletion evidence because optional presentation I/O must not control worker release or erase a paid-task recovery handle.

**11. Demo temporary files can fail encoding or become public artifacts**  
Severity: High  
Pillow and FFmpeg infer formats from destination names, so a bare `.tmp` breaks their existing calls. A dot-prefixed `.tmp.mp4` or `.tmp.png` under `clips/` or `frames/` passes `Path.glob("*")`, the suffix filter, and the download-route allowlist. Tail extraction also deletes the prior JPEG before writing its replacement. `eng-implementation.md:362`, `workbench/demo.py:41`, `workbench/demo.py:118`, `h3ref/media.py:33`, `workbench/jobs.py:214`, `workbench/server.py:895`.

Recommendation: Stage correctly suffixed media in a private same-volume directory and atomically publish completed PNGs, clips, tails, and finals because a hard stop must preserve the last downloadable version and keep partial files out of both listings and direct downloads.

### Sequential merge boundaries

**12. PR 1 breaks the deployed UI as soon as an API client pauses or stops a job**  
Severity: High  
Keeping `logs` in the default snapshot preserves one field. The old UI recognizes only `queued` and `running`, stops polling on `pausing` or `stopping`, can re-enable Create, and hides Resume for a paused `one` job. A hard-stopped frame with `task_pending` and no error still renders as waiting. New status strings also fall back to raw translation keys. `eng-implementation.md:204`, `eng-implementation.md:367`, `workbench/static/common.js:20`, `workbench/static/common.js:81`, `workbench/static/create.js:23`, `workbench/static/results.js:120`, `workbench/static/results.js:348`, `workbench/static/results.js:396`.

Recommendation: Move the minimal status, polling, recovery-notice, and locale adapter into PR 1 and test the legacy pages against API-driven transitions because the new statuses become publicly observable at the first merge.

**13. PR 2 has competing DOM owners and no complete Results console handoff**  
Severity: High  
Create already owns `#history-list`, `#history-empty`, and `#refresh-history`. Results already owns `#job-logs` and replaces its entire text. Reusing those IDs in the shell risks duplicate IDs or handlers targeting the wrong node. Giving Tab 1 a distinct node still leaves it needing a source, while both legacy Results fetches continue requesting the full log. The VM fixture fabricates missing IDs. `eng-implementation.md:347`, `eng-implementation.md:368`, `workbench/templates/index.html:55`, `workbench/static/create.js:992`, `workbench/templates/results.html:75`, `workbench/static/results.js:365`, `workbench/static/results.js:390`, `workbench/static/results.js:478`, `tests/test_workbench_ui.js:42`.

Recommendation: Assign distinct shell IDs, bind Tab 1 to the viewed job, migrate both Results fetches and the legacy writer together, and run a rendered-page check in PR 2 because a green VM suite cannot prove unique IDs, correct log ownership, or zero collapsed-console reads.

**14. PR 2 history is empty on Results, and Clone can silently open a default script**  
Severity: High  
Bootstrap returns jobs. Create stores them, while Results copies only `bootstrap.config`. A settled Results page also stops polling, so an active background job cannot reliably drive its topbar. Meanwhile, the old Create initializer ignores `?from=`, although the new history design offers Clone. `eng-implementation.md:255`, `eng-implementation.md:263`, `eng-implementation.md:368`, `workbench/server.py:360`, `workbench/static/create.js:853`, `workbench/static/create.js:1008`, `workbench/static/results.js:484`, `workbench/static/results.js:493`.

Recommendation: Share bootstrap hydration and history refresh across pages, and ship Clone with its prefill consumer because viewing settled job B must still show active job A and cloning must load the selected script.

**15. The first merge would publish conflicting Stop contracts, and the second leaves navigation docs stale**  
Severity: Medium  
The plan specifies slow Stop as `200` with a snapshot, `202` with a nested job, and `202` with a flat snapshot. It also specifies both terminal `400` and matching-operation replay `200`. PR 1 owns the operator API docs. PR 2 moves Colab into a popover while the operator guide still directs users to a top-of-page card. `eng-implementation.md:215`, `eng-implementation.md:298`, `eng-implementation.md:340`, `eng-implementation.md:367`, `eng-implementation.md:368`, `docs/workbench.md:31`.

Recommendation: Publish one Stop response and replay table, execute the documented requests in PR 1, and update navigation instructions in PR 2 because users and API clients consume each merged version independently.

**16. The downgrade procedure can race the service and is absent from the Windows bundle**  
Severity: High  
A zero-worker check leaves an idle server free to launch or rewrite a registry record during conversion. The existing server already has a cross-platform instance lock. Status conversion also cannot teach the prior reader to recognize a saved frame task with no error, and the proposed fixtures omit stopped film. Finally, the Windows packager enumerates individual scripts and does not include the proposed helper. `eng-implementation.md:363`, `eng-implementation.md:367`, `workbench.py:19`, `workbench/instance.py:7`, `h3ref/frames.py:77`, `scripts/package_windows.py:14`, `scripts/package_windows.py:24`.

Recommendation: Hold the instance lock throughout conversion, reject states the prior reader cannot safely recover, and run a stopped-film recovery fixture from the extracted Windows ZIP because safe rollback requires both exclusive registry access and a usable tool in the delivered package.

### Six-dimension Cycle 2 verdict

| Dimension | Verdict |
|---|---|
| Architecture | Revise. Separate process settlement, paid-task retrieval, and snapshot rendering. |
| Test coverage | Insufficient. PR 1 needs deterministic pause/failure races and native ownership tests. PR 2 needs rendered Create and Results tests before merge. |
| Perf risks/budgets | Unmet. Verify monotonic log progress, zero collapsed Results log reads, and zero warm settled-summary stats independently. |
| Security threats | Open. Cursor-boundary redaction, public temporary media, and deletion with unreadable paid-task state need explicit tests. |
| Error paths | Open. Pipe EOF, cleanup permission denial, malformed state, and stop replay still have ambiguous outcomes. |
| Deployment/PR-split risk | High. Keep the three PRs, move compatibility adapters and documentation into the merge that changes behavior, and test the packaged rollback path. |

VERDICT: FINDINGS
```

#### ENG CYCLE 2 DUAL VOICES — CONSENSUS TABLE

```
ENG CYCLE 2 DUAL VOICES — CONSENSUS TABLE:
  Dimension                           agy (in-host)                           Codex                                  Consensus
  1. Architecture sound?              A1 stdout deadlock, A2/A3/A4 PR2 split  #1/#2/#3/#7 settlement/orphan/consent  CONFIRMED (fixed by ENG2-H1/H2/H6)
  2. Test coverage sufficient?        10 new Cycle 2 regressions mapped       PR1 races + PR2 rendered + Win ZIP     CONFIRMED (fixed by ENG2-H5/H6 + Test Plan)
  3. Perf risks + budget/bench named? E1 ctime/4k reset, E3 O(N) lock, H3     #8/#9 monotonic cursor + zero stats    CONFIRMED (fixed by ENG2-H1/H3/H4)
  4. Security threats covered?        S1 Linux FD/macOS/UNC, S2 symlink /logs #9/#11 cursor redact + private stage   CONFIRMED (fixed by ENG2-H3/H4)
  5. Error paths handled?             E2 CancelledError, H1 two-pass, E5/E6   #4/#5/#6/#10/#15 retry/outcome/table   CONFIRMED (fixed by ENG2-H1/H2/H5)
  6. Deployment/PR-split risk?        A4 PR 2 breakages (IDs/jobs/console)    #12/#13/#14/#16 PR1/2 adapters & ZIP   CONFIRMED (fixed by ENG2-H5/H6)
CONFIRMED = native + outside agree; primary cannot replace outside. Zero disagreements -> gate.
```

#### Eng Cycle 2 Completion Summary
- **Mode**: `FULL_REVIEW` (Cycle 2 re-review after approved `UC2A` 3-PR split)
- **Reality rows**: `7/7` applicable rows emitted & re-verified (`PLAN_REALITY: phase=eng verdict=complete`, `pr-count` updated to approved `UC2A` 3 sequential PRs with +2 CI gates and explicit PR 1/PR 2/PR 3 contracts)
- **Cycle 2 Findings**: Native 14 (3 Critical, 6 High, 5 Medium) + Codex 16 (15 High, 1 Medium); **6/6 dimensions CONFIRMED, 0 disagreements**, ALL resolved deterministically in `ENG2-H1..ENG2-H6`.
- **Critical Unmitigated Gaps**: **0**
- **Test Plan Artifact**: updated in place at `/Users/qiaoyu/.gstack/projects/garycaogan-h3-ref-video/qiaoyu-main-eng-review-test-plan-20261127-004000.md` with all Cycle 2 regressions.
