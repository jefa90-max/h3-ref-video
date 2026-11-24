# 工作台与 CLI 对照

核对范围：`h3ref.py`、`h3ref/*`、`workbench/*`、`scripts/start_colab.py`、`workflows/comfy-h3-ref2va.json`、`SKILL.md`、`prompts/h3_examples.md`、`examples/film.example.json`。核对日期：2026-11-24。

| 步骤 | 原仓库实现 | 工作台行为与修正 |
| --- | --- | --- |
| one 输入 | 默认 12 秒，5–15 秒，三种画幅，参考图顺序，seed/check | 同参数调用 `h3ref.py one`；字幕检查目前暂停，check 参数保留兼容但不执行检查，页面选项禁用 |
| one 改写 | 已有六段式原样使用；否则 rewrite 模块读取官方指南、RULES、示例并调用改写器 | 完全复用；不拼另一套系统提示词；界面根据缓存和最终提示词判断配置需求 |
| film 解析 | `film.load` 过滤空 action、默认 2 秒、至少 0.5 秒、时间归一化与 `H3_MAX_SHOTS` 上限 | 删除 UI 重复时间算法，先做 0–3600 秒有限数值校验再调用原 load；页面从角色/总表清单绑定上传文件，启动时保存绝对路径 |
| 切段与参考图 | `film.plan / refs / compile_prompt`（含 `H3_MAX_SEGMENTS` 上限），按实际去重后的参考图张数（多角色共用同一 `picture` 合图时合并为 1 个图位）不超过 9 张，shot/first/none | 直接使用相同函数；第一步支持勾选多个角色合并上传一张多角色合图（请求携带 `reference_groups`，服务端展开并将同组成员的 `cast.picture` 指向同一文件路径）；显示实际参与生成的分镜，不承诺超出参考图预算的镜头都有图 |
| 画图 | `film.stage_frames` → `frames.describe_composition`（v3 结构化构图 + `locked_look` 跨镜外貌锁死，连续 3 次重试失败自动回退原分镜词，或 `--no-optimize-storyboard` 直接使用原词）→ `prompt / draw` | 完整出片和“先生成分镜”使用同一阶段；页面提供 `optimize_storyboard` 开关（默认开启，与 CLI 默认一致；关闭等价于 `--no-optimize-storyboard`）；总表→角色合图顺序一致，不引用首张分镜 |
| 视频提示词 | `stage_prompts` → 原 rewrite，官方六段式 | 真实流水线完全沿用；分镜卡片修改图像生成提示词，视频片段卡片可查看或修改单段源词 / H3 提示词并局部重生成，还可单独或整片执行“转化为英文 H3 提示词”（`rewrite_segment` / `rewrite_prompts`，仅写提示词、不触发视频生成） |
| 出片与检查 | 双视频后端路由（`VIDEO_PROVIDER=comfyui` 或 `runninghub`）：ComfyUI 使用 `workflows/comfy-h3-ref2va.json`、可选 `768p`（`768×1344` / `1344×768` / `1024×1024`，默认）或 `544p`（`544×960` / `960×544` / `736×736`）确定性尺寸、可选 RealESRGAN 2× 超分（`COMFYUI_UPSCALE`，默认关闭）、`full20`（默认，含 EasyCache 与 BlockSparseAttention）/ `8step` / `4step` 预设，并支持多节点并发池分发与按段持久化 `comfyui_url` / `comfyui_key_fp`；RunningHub 保持原 544p 工作流；按任务记录的 `provider` 隔离续跑；字幕检查目前暂停 | one 直接调用 CLI；film 先调用 stage_frames，全部确认后才调用原 CLI / film.run；整片及单段重生成均跳过字幕检查。ComfyUI 模式下进入真实视频生成前先探活远端 `/system_stats`，未在线时若开启 `COLAB_AUTO_START=1` 且装有 `colab` CLI 则由 Flask 主进程触发后台拉起并提示等待就绪；结果页展示 ComfyUI 实时阶段、步数、显存与节点进度 |
| 单镜重出 | `frames.draw` 是原图像 API；CLI 的 `--redo` 只重做视频 | 单镜用原 draw；成功后替换，失败保留旧图。已有 task 但结果未知时只查询原任务，不能用新编辑覆盖；明确终止失败后才允许卡片新生成，审核拒绝须先改词 |
| 更新成片 | `--redo`：shot 只重做所属段；first/none 从该段向后重做 | 抽取原失效逻辑为 `film.invalidate_segments`，CLI 与 UI 共用；失效只应用一次，网络失败续跑不会清掉刚提交的新 task |
| 配置与恢复 | 根目录 .env / 环境变量；`VIDEO_PROVIDER`（`.env.example` 默认 `comfyui`，未设置时代码回退 `runninghub`）、`COMFYUI_URL`（支持多节点）、`COMFYUI_API_KEY`、`COMFYUI_CONCURRENCY`、`COMFYUI_PRESET`、`COMFYUI_RESOLUTION`、`COMFYUI_UPSCALE`、`COLAB_GPU`（`G4` / `A100` / `H100`）、`COLAB_AUTO_START`；`LLM_PROVIDER`（`apimart` / `gemini`）与阶段独立模型；state.json 恢复 | 同配置入口与状态；按当前阶段、任务已记录的 `provider` 和缓存预检：`comfyui` 视频阶段校验 `COMFYUI_URL`（公网 HTTPS 隧道另需 `COMFYUI_API_KEY`，所有 `COMFYUI_URL` origin 均变更且未重填 key 时自动清空旧 `COMFYUI_API_KEY`，更换或清空 key 时自动剔除密钥不匹配的已记住第 2 节点地址），不强制要求 `RUNNINGHUB_API_KEY`；先画图需 `APIMART_API_KEY`，且在开启 `optimize_storyboard` 且构图未缓存时需所选文字服务密钥；仅转英文提示词只需文字服务密钥。顶部提供 ComfyUI / Colab 运行状态卡片（只读探活、一键启动主节点与基于 cloudflared 的第 2 节点、Google Drive 授权确认、带在途保护二次确认的终止实例），打开页面不自动开卡。工作台运行或存在未决单图／单段编辑时，CLI `film` 阻止并发写入 |

## 有意保留的 UI 差异

- 用户要求 Claude 暂不可用，工作台固定 `H3_REWRITER=llm`，不探测 Claude；CLI 自身默认选择规则未改。
- 页面不要求本地路径：导入后提取 cast 与 board 上传清单，绑定与故事 JSON 分开；启动前写绝对路径给原 CLI。旧路径转成待上传位，board:true 是工作台的必需总表标记，不进入 CLI。底层兼容接口仍保留旧 film_base_dir 行为供已有任务使用。
- HTTP 边界增加类型、有限数值、图片内容与输出目录校验；单任务互斥。默认选中整条片、真实生成，JSON 输入框为空；离线演示需主动选择。演示是占位媒体，不能验证模型效果。
- key 可保存到忽略的本机 `.env`（支持 POSIX 权限的平台使用 0600），也可只放当前服务内存；已保存的 key 不回传浏览器、不进入浏览器存储或任务 JSON。
- film 工作台固定先分镜、统一确认当前整批分镜后再出片；none 先确认脚本计划。服务端验证当前图片版本的确认记录，未审核旧任务续跑会返回分镜阶段；原 CLI 不增加 UI 审核限制。
- 新增独立结果页、每页 12 张的审图卡片、旧视频待更新提示、Plyr 播放器；one 的工作台报告是展示补充，不更改 CLI 产物。
- `film.load` 新增默认开启的 `check_images` 参数，仅历史结果预览关闭图片存在性检查；源参考图被移动后，旧视频仍可看，缺图分镜禁止重出。CLI 校验默认不变。
- Windows worker 显式使用 UTF-8 处理标准输入输出和文件。旧版本以系统编码写出的 `state.json` 不自动迁移或覆盖；遇到编码错误，先保留原文件，核对原编码和远端任务记录后再处理。

## 验证

当前完整开发测试需在 Python 虚拟环境中安装 `requirements-web.txt`，并使用 Node.js 22；执行 `python -m unittest discover -s tests -v` 和 `node --test tests/test_results.js tests/test_frame_character_picker.js tests/test_video_segment_prompts.js tests/test_workbench_ui.js`。初次核对的离线与假 API 证据见 [验证记录](workbench-verification.md)，不代表真实鉴权、余额或模型效果已验证。
