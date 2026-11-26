# h3-ref-video：H3 参考图出片

像给 Seedance 写提示词、给参考图那样，用 MiniMax H3 出视频（支持 **ComfyUI（Google Colab / 自建 GPU）** 与 **RunningHub** 双视频后端，`.env.example` 默认推荐 `VIDEO_PROVIDER=comfyui`，未设置时代码回退 RunningHub）：你照常写中文分镜和「参考图片1、参考图片2…」，工具把它改写成 MiniMax 官方的六段式格式再提交；一份分镜脚本还能整条出片，每个镜头先用 GPT Image 2.6 画一张分镜图垫给 H3，人物、道具、比例照图来。

为什么折腾这个：除了支持在自有或 Google Colab GPU 上通过 ComfyUI 零按条计费出片（支持 768p/544p 与 2× 超分、多节点并发池），H3 在 RunningHub 上按机器时间收费，标准机型每输出一秒大约 ¥0.009，Seedance 2.0 mini 480p 是 ¥0.102，差了十倍多。一条 27 秒、11 个镜头的竖屏短片，用这个仓库的 `film` 命令跑：11 张 1K 分镜图 ¥1.85，H3 两段并行生成 ¥0.27，一共 ¥2.12，从开始到出片 9 分钟；同长度 Seedance mini 约 ¥2.8。

```bash
# 一条片段：提示词和参考图跟给 Seedance 的一样
python h3ref.py one --prompt 提示词.txt --image 角色.jpg --image 场景.jpg --seconds 12 --aspect 9:16 --out jobs/试一条

# 整条片：分镜脚本 → 切段、每镜分镜图、改写、生成、查烧字幕、拼片
python h3ref.py film jobs/快递 --film examples/film.example.json
```

## 效果

**同一个脚本、同一对演员**：左边 Seedance 2.0 mini（一条 ¥1.53），右边 H3 每个镜头先垫一张分镜图（7 张图 ¥1.06 + 生成 ¥0.15，一共约 ¥1.2）。画面质感基本打平，H3 的舞台灯光更足，Seedance 哭得更夸张。动图没有声音，每张下面可以下载原视频。

![左边 Seedance，右边 H3 加每镜分镜图](docs/media/qiangjinmai-seedance-vs-h3.gif)

有声原视频：[下载 mp4](docs/media/qiangjinmai-seedance-vs-h3.mp4?raw=true)

H3 这条用的 7 张分镜图，第一张画好以后，后面每张都带着它当参考，人和场景才前后一致：

![7 张分镜图](docs/media/qiangjinmai-storyboard-frames.jpg)

**同一个镜头，左边 H3 直出，右边先画一张分镜图再出**：

![左边 H3 直出，右边垫分镜图](docs/media/plain-vs-storyboard.gif)

有声原视频：[下载 mp4](docs/media/plain-vs-storyboard.mp4?raw=true)

**中文真人哭戏，四个版本盲评**（静音）：左上 Seedance，右上 H3 直出（输），左下 H3 垫一张首帧（4:0 赢 Seedance），右下 H3 每个镜头垫分镜图：

![病房哭戏四个版本](docs/media/hospital-four-versions.gif)

原视频：[下载 mp4](docs/media/hospital-four-versions.mp4?raw=true)

**同一段提示词**：左边是给 Seedance 的写法，红字就是会把背景音乐和字幕招进来的「不要……」；右边是改写后的 H3 官方六段式：

![提示词改写前后](docs/media/prompt-seedance-vs-h3.jpg)

## 准备

1. **Python 3.10+、ffmpeg**，`pip install -r requirements.txt`。
2. **视频后端与 API key**，复制 `.env.example` 成 `.env` 填上：
   - `VIDEO_PROVIDER`：视频生成后端，可选 `comfyui`（`.env.example` 默认推荐，连接远端 Colab GPU 或自建 ComfyUI）或 `runninghub`（未设置时的代码默认回退）。选 `comfyui` 时填写 `COMFYUI_URL`（支持英文逗号分隔多节点并行）与 `COMFYUI_API_KEY`（公网 HTTPS 隧道必填，本机 HTTP 可留空），并可按需配置 `COMFYUI_PRESET`（默认 `full20`，可选 `8step` / `4step`）、`COMFYUI_RESOLUTION`（默认 `768p` 3D 潜空间二采，可选 `1088p` 3D 潜空间二采真 1080p 或 `544p` 单采快速档）、`COMFYUI_UPSCALE`（默认 `0`）、`COMFYUI_CONCURRENCY`（默认等于节点数）、`COLAB_GPU`（默认 `G4`，可选 `A100` / `H100`）与 `COLAB_AUTO_START`（默认 `0`）。
   - `RUNNINGHUB_API_KEY`：使用 `VIDEO_PROVIDER=runninghub` 时填写，取自 [RunningHub](https://www.runninghub.cn) 控制台「API 调用」。本项目使用工作流 API；消费级会员 key 扣积分，企业共享 key 扣钱包。默认同时跑 3 段，可通过 `RUNNINGHUB_CONCURRENCY` 按实际 API 额度调整。
   - `APIMART_API_KEY`：[APIMart](https://apimart.ai/register?aff=J2lV)（推荐链接），画分镜图（GPT Image 2.6），默认也用于逐镜头文字描述和视频提示词改写。
   - `GEMINI_API_KEY`：选择 Gemini 官方文字服务时填写。设 `LLM_PROVIDER=gemini`，通过 [Google 官方兼容接口](https://ai.google.dev/gemini-api/docs/openai)调用；分镜图片仍需要 APIMart key。
3. **改写提示词的模型**：装了 [Claude Code](https://claude.com/claude-code) 并登录过，默认就用它（我们一直用的是这个）；没装就按 `LLM_PROVIDER` 走 APIMart（`H3_REWRITER=llm`，默认 `deepseek-v4-pro`，或自定义兼容接口）或 Gemini 官方接口。DeepSeek 改写我们试过，格式检查能过；上 H3 出片对比过的只有 Claude 改写的版本。
4. **MiniMax 官方提示词指南**：工具运行时从 [MiniMax-AI/MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) 现取 `ref-en.txt` 并缓存，没有随仓库分发（它属于 MiniMax H3 Community License 下的文档）。网络不通就自己下载，用 `H3_GUIDE_FILE` 指过去。

构图规划与提示词改写可以使用不同模型：APIMart 分别设置 `LLM_COMPOSITION_MODEL` / `LLM_REWRITE_MODEL`，未设置时沿用 `LLM_MODEL`；Gemini 分别设置 `GEMINI_COMPOSITION_MODEL` / `GEMINI_REWRITE_MODEL`，未设置时沿用 `GEMINI_MODEL`（默认 `gemini-flash-latest`）。CLI 要让改写使用所选 API，设置 `H3_REWRITER=llm`；`auto` 仍优先使用本机 Claude Code。两家密钥独立，不自动跨平台重试。

用 Claude Code 或 Codex 的话，把仓库放进 skills 目录（比如 `~/.claude/skills/h3-ref-video`），直接说「用 H3 把这个分镜做成片」，它会照 [SKILL.md](SKILL.md) 写 film.json、跑、看报告。

## 本地工作台（不用记命令）

Windows 10/11 64 位：解压 Windows 发布包后双击 `start-windows.cmd`，首次自动下载并校验 Python、FFmpeg、curl，安装虚拟环境依赖后打开工作台。无需预装 Python；详细步骤见 [Windows 一键部署](docs/windows.md)。

在源码目录首次使用时，先创建 Python 虚拟环境并安装依赖。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-web.txt
```

已安装 Node.js 和 pnpm 时，在仓库根目录运行 `pnpm dev`，打开 **http://127.0.0.1:7860**。带浏览器和自定义端口可运行 `pnpm dev --port 7862 --open`。也可以用 `.venv/bin/python workbench.py` 直接启动。Windows 源码安装及端口说明见 [工作台使用说明](docs/workbench.md#启动)。

- **先试演示**：默认选中「整条片」和「真实生成」，分镜 JSON 输入框为空。试用时先手动切到「离线演示」 → 下载示例 JSON，再导入 `.json` / `.txt` 分镜文件（内容为 JSON），选择 `shot / first / none` 后启动 → 独立结果页审核分镜，再生成并播放/下载 DEMO 视频，查看日志与报告。也可切到「一条片段」填写提示词后启动。无需 key，不联网、不扣费；缺 ffmpeg 时仍可产出计划和报告。
- **真实生成**：环境配置选择视频后端（ComfyUI 或 RunningHub）并填写对应地址/密钥（ComfyUI 填 `COMFYUI_URL`、`COMFYUI_API_KEY` 及预设/分辨率/超分/GPU 参数，顶部卡片支持探活与可选 Colab 一键启停；RunningHub 填 `RUNNINGHUB_API_KEY`）与 `APIMART_API_KEY`，选择 APIMart 或 Gemini 官方文字服务，分别填写构图规划模型和提示词改写模型；Gemini 另填对应 key。勾选保存到本机 `.env`（也可预先手动配置；取消勾选则只用当前服务）→ 上传参考图（film 导入后按清单逐项上传，角色较多时可勾选多个角色合并为一张合图上传以控制在 9 张上限内，不用填路径） → 关闭离线演示 → 启动。工作台固定用 `H3_REWRITER=llm`，不调用 Claude；APIMart 选项也支持自定义 OpenAI 兼容 key 和 URL。
- **先审图再出片**：整条片选 shot/first（默认开启文字模型精简分镜提示词优化，也可取消勾选或用 CLI `--no-optimize-storyboard` 直接按原剧本词出图） → 生成分镜后进入独立结果页 → 每页 12 张（或切大图用 `←` / `→` 逐张浏览），可单镜改词、增删已导入角色参考图重出 → 浏览完成后统一点一次“确认全部并生成视频”；「视频片段」Tab 支持单段/整片手动重新转化为英文 H3 提示词（不自动扣费生视频）、单段查看/编辑提示词重生成与重新拼接完整视频。已有视频会标为待更新；视频使用本地 Plyr 播放器。
- **看结果/续跑**：独立结果页展示状态、真实日志和产物完整路径；失败或中断后，从任务历史选择原任务续跑。点击“选择文件夹”指定成果根目录，自动记住并在下次打开时恢复；新任务创建按时间命名的子目录，默认根目录为 `jobs/`，续跑保持原目录。

key 仅按选择写入被 git 忽略的本机 `.env`（权限 0600），不会进入版本控制或浏览器存储。界面的环境检查只验证配置是否齐全，不代表远端 key 有效。详细流程、无路径 JSON 和续跑说明见 [本地工作台使用说明](docs/workbench.md)，逐步骤核对见 [CLI 一致性说明](docs/workbench-cli-parity.md)。

## 一条片段：`one`

提示词就按 Seedance 的写法：画面描述、分镜时间、台词、「参考图片N 是谁」。工具先改写成官方格式（结果存在 `--out` 目录的 `prompt.h3.txt`），再连同参考图提交，大约 4–6 分钟出一条视频（ComfyUI 默认 768p 档为 768×1344 竖屏 / 1344×768 横屏 / 1024×1024 方形；RunningHub 或 ComfyUI 544p 档为 544×960 竖屏 / 960×544 横屏 / 736×736 方形），带模型自己生成的声音。已经是六段式的提示词原样提交。字幕检查目前暂停，自动检查及 `--check` 都会跳过，不调用视觉模型。

## 出图前检查台词预算

```bash
python h3ref.py preflight --film examples/dialogue-budget.json --json
```

示例返回 `status: "partial"`（跳过一条空动作），第 2 镜常速超预算、第 3 镜慢速超预算，`meta.calibration_status` 为 `"provisional"`。这条命令支持 `--film -` 读标准输入，且无需服务、密钥或参考图。脚本检查、状态和退出码详见[台词语速预检](docs/workbench.md#dialogue-preflight)。

## 整条片：`film`

分镜脚本写成 JSON，格式看 `examples/film.example.json`：画幅、画风一句话、场景与道具、台词语言、角色（名字、参考图、外观描述）、每个镜头的起止秒数、机位、动作和台词。

工具按镜头边界切成每段不超过 15 秒，然后：

1. **画分镜图**（`--frames shot`，默认；加 `--no-optimize-storyboard` 可跳过文字优化直接按原剧本词画图）：先用文字模型（默认 `deepseek-v4-pro`）按全片镜序输出精简的 `composition v3`（机位构图、当前场景陈设、人物左中右站位与首帧姿态）。程序通过 `locked_look` 保持角色固定外貌和服装一致，剧本明确摘下或脱下的物件按当前镜头描述穿戴状态。若文字模型输出为空或格式不符，自动重试最多 3 次后回退原剧本分镜词继续出图。每镜文字落盘后立即交给四线程图片池，和后续文字规划重叠；多角色共用同一张合图时自动按路径去重并生成防串脸／禁画未出场角色说明。再用图片模型（默认 `gpt-image-2.6-sunburst`）画出镜头开始的一瞬间。各图仅使用场景／道具总表和对应角色图作为参考，不引用首张分镜。参考图按像素收费，上传前缩到 1024 像素。
2. **写提示词**：每段按 Seedance 的写法拼出中文提示词（参考图片1 是总表、2 是谁、之后每张是第几个镜头的首帧），再改写成 H3 官方六段式英文（强制每段非空，空输出或格式／图片编号问题最多纠错重试 6 次）。
3. **生成**：每段参考图最多 9 张（工作流就 9 个图片口），放不下时先拿掉总表，再拿掉后面镜头的分镜图。分镜图模式下各段互不依赖，并行生成（ComfyUI 默认并发等于节点数，RunningHub 默认最多同时生成 3 段），其余等待空位。
4. **拼片、出报告**，出 `film.mp4` 和 `report.md`（每段费用、GPU 时间）。字幕检查目前暂停，整片和单段重生成均跳过；检查代码与已有记录保留。

断了就原样再跑 `film jobs/<任务名>`：每张图、每个视频任务（ComfyUI / RunningHub）提交后先记下任务号与后端信息再等结果，续跑只接着查，不重复花钱。RunningHub 并发已满（421）时每 60 秒尝试一次，最多 31 次，累计等待最多 30 分钟；机器资源不足（415）仍最多尝试 5 次。网络结果未知时不自动重提。某段不满意，`--redo 2` 给第 2 段换种子重跑。

三种分镜图模式：

| 模式 | 做法 | 什么时候用 |
|---|---|---|
| `shot` | 每个镜头一张分镜图 | 默认。人多、镜头多，站位和道具要照剧本来的戏 |
| `first` | 每段只给第一个镜头一张，后一段再接前一段的最后一帧 | 想省图钱、对白戏 |
| `none` | 不画图，只给角色图，各段接着上一段的最后一帧 | 动漫、cos 这类 H3 直出就够好的线 |

## 我们测出来的结论

都是同一份脚本、同样的参考图和时长，对着 Seedance 2.0 mini 盲评出来的：

- **动漫和 coser 片，H3 直出已经赢 Seedance**，成本约十分之一。两个镜头各 4 轮盲评，H3 4:0、4:0。
- **中文真人对白戏，H3 直出会输**（0:4）；垫一张首帧以后哭戏能赢（4:0），带货戏还是输。
- **每镜分镜图能让 H3 稳稳追上 Seedance**：同一个脚本做了 15 条真人 cos 短片，8 条用 Seedance、7 条用 H3 每镜分镜图。同一对演员的两条放在一起，画面质感基本打平，H3 舞台灯光更足，Seedance 哭得更夸张；钱上 Seedance 一条 ¥1.53，H3 加 7 张分镜图约 ¥1.2。分镜图最管用的是让 H3 照着剧本演：人站在哪、镜头从哪起、谁先开口。
- **官方格式是关键**：Seedance 写法的提示词直接给 H3，7 段里 7 段被垫了背景音乐，中文对白会烧字幕，表演也收着。工作流没有负面提示词，「不要背景音乐、不要字幕」反而会把音乐和字幕招进来。改成官方六段式、去掉所有否定句、配乐写 N/A 之后，13 段里只剩 1 段有音乐，和 Seedance 逐段盲评 H3 赢 5、Seedance 赢 1、平 5。

还要注意的：

- **中文对白会烧字幕**，一次把「三千八」写成了 3000，所以有台词的段落交付前建议人工抽看（自动字幕检查目前暂停，已有记录保留）。
- 垫了分镜图的镜头，开头一两秒会比较静，像从照片里动起来。
- 大的情绪反应（惊吓、疼）H3 还是演得偏小，提示词里要写具体的脸和身体动作。
- 高配（Ultra 机型、736×1312、30 步）大约慢 8 倍、贵 17 倍，只换来更锐的细节，不值得开。超分到 720p 每 15 秒多 ¥0.32，手机上看提升一般。
- 会员 API 额度与网站任务共享，按当前套餐核对；默认并发为 3，旧 `.env` 若写了 `RUNNINGHUB_CONCURRENCY=5`，需改成实际允许的值。偶尔会爆显存失败，失败不扣费，再跑一次就好。

## 文件

- `h3ref.py` 命令入口；`h3ref/film.py` 分镜脚本、切段、参考图顺序、中文提示词和整条流程；`staging.py` 逐镜调度结构校验与渲染；`rewrite.py` 改写成官方格式和格式检查；`frames.py` 分镜图；`comfy.py` ComfyUI 视频后端与多节点并发池；`colab_runtime.py` 跨平台 Colab GPU 生命周期控制；`rh.py` RunningHub；`config.py`、`http.py`、`i18n.py` 配置、请求脱敏与多语言文案；`check.py` 烧字幕检查；`media.py` ffmpeg；`workbench.py` 与 `workbench/` 本地 Web 工作台；`scripts/start_colab.py` 远端 Colab ComfyUI 守护与鉴权隧道启动脚本；`start-windows.cmd` 与 `scripts/` Windows 一键启动及发布打包。
- `prompts/h3_examples.md`：两条手工改写、在 H3 上测过的样例（中文 Seedance 写法 → 官方六段式），改写时一起给模型看。
- `workflows/h3-ref2va-turbo.json` 与 `workflows/comfy-h3-ref2va.json`：前者为 RunningHub 用户 user_3ww6n2yr 的公开工作流[「加速版-MiniMax H3｜多参图像音频极速生视频」](https://www.runninghub.cn/post/2113525277564424193)，后者整理自 Comfy-Org 官方内置模板 `video_minimax_h3_r2v`，归原作者，说明见 [workflows/README.md](workflows/README.md)。
- 文档索引：[参与贡献与离线测试](CONTRIBUTING.md)、[工作台设计规范](DESIGN.md)、[安全说明](SECURITY.md)、[Plyr 依赖说明](docs/vendor-plyr.md)、[工作台验证记录](docs/workbench-verification.md)、[工作台交互设计](docs/design/local-workbench.md)、[视频片段审阅设计](docs/design/video-segment-review.md) 与 `docs/plans/` 方案记录。

## 许可

代码和 `prompts/` 是 MIT。`workflows/` 里的节点图归原作者；MiniMax 官方提示词指南和 H3 模型遵守 [MiniMax H3 Community License](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)。

想做「照着原片换人」的翻拍（MV、舞蹈），看姊妹仓库 [mv-swap](https://github.com/garycaogan/gary-mv-swap)。
