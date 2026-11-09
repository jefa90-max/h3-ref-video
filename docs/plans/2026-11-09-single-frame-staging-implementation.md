# 单首帧场面调度、逐镜角色描述与视频片段编辑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task after review and renewed implementation authorization. The user selected implementation in the main session, followed by one independent Codex review. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留本项目「一镜一张首帧」，将场景固定物、逐镜人物调度与道具状态贯穿图片及视频提示词，并让视频片段具备与分镜图对应的提示词查看、手动编辑及单项重生成能力。

**Architecture:** 扩展现有 `composition` 缓存，新增一个负责调度结构校验与文本输出的 `h3ref/staging.py`；原始 film JSON、图片供应商和 H3 Ref2VA 工作流保持现有接口。复用每镜文字整理调用，顺序缓存状态后并行生成唯一首帧，图片与视频共用逐镜状态；视频编辑复用 `workbench/segments.py` 的单段暂存、恢复与成功后发布事务，不另建生成链路。

**Tech Stack:** Python 3.10+、标准库 `json` / `unittest.mock` / `concurrent.futures`、现有 Pillow、Flask 3.1、APIMart 图片/文字接口、RunningHub H3 Ref2VA；不新增依赖。

**Spec:** 用户明确需求（含追加的逐镜角色描述、视频片段提示词编辑/重生成，以及最新的仅更新计划并本地提交指令），以及 [cwp 参考价值评审](2026-11-09-cwp-ai-video-review.md)、[分镜连续性研究](2026-11-08-storyboard-continuity-research.md)。视频侧基线参见 [现有视频片段方案](video-segment-review.md) 和 [现有交互设计](../design/video-segment-review.md)；其中「本轮不增加视频提示词编辑器」已被此次补充需求覆盖。研究中的首尾帧、换模型、Director 续接建议仅作背景。下文「需求边界与数据契约」是本计划实施时必须同时阅读的规格。

**Current authorization (2026-11-09):** 用户已撤回「改完计划立即实施」。本轮只更新并本地提交计划与必要协作记录，提交后停止；不写业务代码、不运行 autoplan、不调用付费生成。用户将清空会话后另行运行 autoplan；本计划的六项实施任务尚未开始，待评审及重新授权。主会话逐项实施、最后独立审查是保留的执行方式，不代表当前已获实施许可。

## Global Constraints

- 「保留本项目现有『一镜一张首帧』工作流，不要改成正式的首尾帧生产路线。」
- 「单独写清场景固定物、人物位置、身体朝向、视线、机位，以及道具状态（如袋口朝向、袋底方向、结扣位置等要分开写，避免歧义）。」
- 「每镜的角色描述要随该镜动作、朝向、位置变化，不要整片共用同一段固定角色文案。」
- 「视频片段也要能像分镜图那样手动改提示词、手动重新生成」；对照图片侧查看/编辑/重生成交互与数据流落实同等能力，不以已有的换种子按钮冒充完成。
- 「本轮先不实施编码」「更新计划文件后本地 git 提交计划改动」「提交后停下」；不要推送、不要部署。后续实施和真实付费文字/图片/视频调用分别取得授权，不能沿用已撤回的立即实施许可。
- 所有 Python 命令使用项目 `.venv`；不启动或重启用户服务，不运行 `pnpm dev`，不调用或探测 Claude。
- 新增或修改的可见标题、提示和错误使用现有中英文 JSON 及 `message()`；不硬编码新的界面文案。不翻译用户剧本内容。
- 不恢复首镜锚图，不自动附加上一镜图片，不增加尾图、候选图、视觉评审或新的全片 LLM 调用；保留审核拒绝、未知提交、单张失败继续和整批审图确认的保护。
- 保持现有 `shot/first/none` 模式、参考图顺序及 H3 九图上限；主要验收 `shot`。不改变切段算法，不悄悄增加图片数量。
- 实施后先运行构建等价检查再执行任何 git 操作；bug 修复用独立 `fix(...)`，永久回归用独立 `test(fix):`。一次性验证脚本删除，能复现本轮缺陷的回归保留。

## Review Focus

1. `cast.look` 同时含外貌与旧姿势/持物：新镜必须采用本镜姿势与持有者，同时保持同一角色身份；Task 2 测试。
2. 特写裁掉袋底或正反打改变画面左右：物体固有方向、世界位置与裁切分开，不能因此倒袋或移动锅；Tasks 1、2、3 测试。
3. 换场、返回原场、跨视频切段及单镜规划失败：只继承可信的文字布局，不把另一地点或失败镜头当成已发生状态；Task 3 测试。
4. 旧三字段 composition、已生成图片、已接单及提交未知任务：查看/续跑不重新规划、不换提示词、不重新付费；Tasks 3、4 测试。
5. 手动改提示词后旧状态仍进视频、或角色参考增删导致编号错位：失配缓存退出下游，保留实际参考顺序和现有视频失效标记；Task 4 测试。
6. 视频草稿被轮询覆盖、改词后仍沿用旧 H3、失败后旧片被覆盖、未知任务被改词替代：图片侧已有的草稿/事务语义在视频侧也成立；Tasks 5、6 测试。

---

## 需求边界与数据契约

### 1. 原流程与本次接入点

当前 `h3ref/frames.py:59-115` 的 `describe_composition()` 只整理 `scene/frame/orientation`；`prompt()` 随后逐镜直接拼接 `cast[].look`。这会让外观描述中夹带的固定姿态、持物或位置反复进入每镜。`h3ref/film.py:207-255` 在四线程中同时规划和画图，各镜看不到相邻镜已规划的状态。

`film.compile_prompt()`（约 113-166 行）还会直接拼接全片 setting 与 cast.look，且目前不知道图片 composition。只改图片文字会让视频阶段重新引入旧状态；本次必须一起接通。`workbench/storyboard.py` 的查看与重生成复用这些 API，页面轮询不能触发新的模型调用。

借鉴 cwp 固定快照 `eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2` 的 `skills/reference/cinematic-rules.md`：空间锚点、身体/视线分离、机位与轴线、交接接触链。不开新平台、不复制其完整导演规则，不强制改写剧本、镜数、时长、对白或运镜。

### 2. composition v2（内部缓存，不要求用户改输入 JSON）

保留兼容字段 `scene/frame/orientation`，新增以下字段；普通 dict 延续项目风格，无需模型库或通用状态机。

| 字段 | 精确形状与用途 |
| --- | --- |
| `version` | 固定整数 `2`。无 version 的旧三字段记录只走旧兼容分支。 |
| `scene_id` | 非空字符串；同一地点复用同一 ID，返回旧地点复用已知 ID。无换场依据不得分配新 ID；此语义由规划规则及人工验收约束。 |
| `world_frame` | 非空字符串；定义可辨认的空间基准，例如「以摊主站在摊内面向顾客为准」。不得只写未定义的「左边」。 |
| `fixed_objects` | `list[{name: str, position: str}]`，对象名唯一；位置相对世界基准/其它固定物。锅若剧情中移动，应作为动态道具，不能列成永不移动固定物。 |
| `camera` | `{position: str, view_direction: str, framing: str, axis_side: str}`；都是非空字符串；轴线不适用时明确写「不适用」。保留原镜头给定的景别与运镜。 |
| `characters` | 下述 CharacterState 列表，描述首帧瞬间；已绑定角色用 cast 的正式 name，不用新造别名。允许无人镜为空；原剧本明确出现但未绑定参考图的人物保留原文身份，不自动增加参考图。 |
| `props` | 下述 PropState 列表，描述首帧瞬间；无相关道具时为空。 |
| `action` | 非空字符串，忠于原镜头的动作过程，交接时写清接触者、接触方式、释放与落点；不擅自删减多拍动作。 |
| `expected_end` | `{characters: list[CharacterState], props: list[PropState]}`，只记录原动作结束后的预期文字状态，用于视频描述和下一镜文字上下文；不画尾图，也不当作实际生成结果。 |

**CharacterState**：`{name, appearance, position, body_facing, gaze, pose, expression, screen_position, visibility}`。除 visibility 外均为非空字符串；visibility 是 `visible / cropped / occluded / offscreen`。position 写相对固定物及其他人的位置/距离；body_facing 单写身体，gaze 单写视线目标；转头回望可以与身体方向不同，不能机械判为矛盾。pose 表示此时姿态与动作进度，appearance 仅选本镜可见的稳定外貌/衣着，不把旧姿势和道具位置当成外貌。背影、局部或画外不要求展示脸。

**PropState**：`{name, holder, contact, position, orientation, state, opening_direction, base_direction, knot_position, screen_position, visibility}`。除后三个结构部件字段允许 `null` 外，其余为非空字符串，visibility 同上。无持有者写「无」，contact 写实际接触方式或「无接触」；方向相对世界或物体明确基准。opening_direction/base_direction/knot_position 分别表示开口方向、底部方向、结扣在物体上的位置，不得互相替代。没有对应部件才用 null；某部件被裁切或遮挡不能因此清除其已知方向。

所有数组均必填；不涉及的人物/道具不凭空补进画面。schema 校验负责类型、必填、已绑定角色名称规范化、同名重复和已知固定布局一致，不声称能理解所有自然语言矛盾或验证生成图片。语义检查由离线人工审提示词和经另行授权的真实图验收承担。

**第26镜的验收样例（不绑定业务代码中的镜号/人名）：**

```json
{
  "name": "红袋",
  "holder": "周铁林",
  "contact": "双手托抱袋体两侧",
  "position": "人物胸腹前",
  "orientation": "袋体竖直",
  "state": "袋口扎紧",
  "opening_direction": "朝世界上方",
  "base_direction": "朝世界下方",
  "knot_position": "袋体顶部的袋口处",
  "screen_position": "画面下半部，袋底超出画面下边缘",
  "visibility": "cropped"
}
```

裁切只写进 screen_position/visibility；不能把「袋体在画面下沿」改写成「结扣位于袋体底部」。首帧只描述动作起点，例如递袋前仍由递出者持有；接收者托住、递出者松手的结果放 action/expected_end。

### 3. 数据流、恢复与成本

1. `stage_frames()` 先按全片镜头顺序读取缓存并整理缺失的文字状态，再启动现有四线程画图。缓存每镜一经校验立即保存，保证恢复不重做已完成文字请求。
2. 新文字请求收到原始角色资料、当前镜头、既有前文、最近可信 composition 与 `known_scenes`（`scene_id -> {world_frame, fixed_objects}`）。已知 scene_id 的 world_frame/fixed_objects 视为固定输入，要求逐字复用，不允许因反打而换边；新场景新建布局，返回原场复用布局。不能把旧场景人物持物强灌到新场景。
3. 每镜仍最多复用现有的一次文字整理；不再追加一轮整片规划或每角色调用。增加输出字段会增加 token，串行文字规划会延后首张图开始时间；本轮不宣称文字费用/总耗时不变。
4. `expected_end` 始终是计划，不能标为「已在图片发生」。规划失败记到单镜，后续镜仍尝试；下一镜只接最近可信记录及其来源镜号，未覆盖的动作由原剧本文字提供，不伪造缺失状态。
5. 原 frames key 仍为 `s{段号}-{段内镜号}`；缓存新增 `composition_prompt` 保存生成该 composition 时的最终图片提示词。供下游使用的 v2 记录必须具备非空字符串 composition_prompt，且满足 `record.prompt == record.composition_prompt`；两个字段都缺失不能算命中。
6. 已有图片、旧三字段 composition、已有 task、submission_unknown、终态失败：沿用现有跳过/轮询/人工处理规则，不因升级触发重新规划或重画。只有尚未准备的新镜生成 v2；混合任务允许旧、新记录并存。
7. 手动编辑成功后保留原 composition 供追溯，但 prompt 失配的 composition 不再注入视频或后续文字上下文；不自动调用模型解析手改文字。保留现有 dirty_segments 和整批确认失效规则，人工编辑的物理状态需要用户同时检查下游镜头。
8. 九图上限下，现有 `film.refs()` 会丢总表、甚至舍弃后部镜头参考；本次不顺手重做切段/限额策略，也不偷偷为被舍弃镜头增加收费调用。对这些镜头沿用原视频分镜文字。该已有边界在验收中显式覆盖；常规容量内仍是一镜一张首帧。

### 4. 文件职责

| 文件 | 本次责任 |
| --- | --- |
| 新增 `h3ref/staging.py` | v2 边界校验、图片起点与视频动作文字输出；不读写任务、不请求网络。 |
| `h3ref/frames.py` | 扩展原文字请求的输入/输出；v2 图片提示词逐镜使用角色状态，旧缓存保留兼容分支。 |
| `h3ref/film.py` | 顺序规划、已有缓存与任务恢复、图片并行；共享有效 composition 筛选；给 H3 中文提示词传逐镜状态，保留显式的视频手改提示词。 |
| `h3ref/rewrite.py` | 补充改写规则，将稳定身份与逐镜姿态/持物分开，保留机位、视线及交接顺序。 |
| `workbench/storyboard.py` | 展示已保存的真实提示词；不触发规划；手动重生成保留缓存来源关联。 |
| `workbench/inputs.py` | preflight 用相同缓存与 compile_prompt 参数计算命中，防止前后台误判需付费改写。 |
| `workbench/segments.py` | 视频源提示词/实际 H3 视图，手改词校验与暂存、改写缓存、失败与恢复、成功后发布；复用既有单段生成和拼接事务。 |
| `workbench/jobs.py`、`workbench/server.py` | 扩展原单段重生成 API 与快照；审核/互斥/版本/未知提交保护；不新建第二套视频接口。 |
| `workbench/static/video-segments.js`、`workbench/static/results.js`、`workbench/static/style.css` | 视频提示词展开/编辑、草稿与提交关联、按钮权限、失败提示；轮询保持播放器与未提交草稿。 |
| `workbench/messages/{zh-CN,en}.json`、`workbench/static/messages/{zh-CN,en}.json` | 视频编辑的所有新增或修改文案/错误/费用提示，键与占位符对齐。 |
| `h3ref/messages/{zh-CN,en}.json` | 新调度段标题与具备字段路径/错误值的校验错误；两种语言同键同占位符。 |
| 新增 `tests/test_staging.py`、`tests/test_frame_staging.py`、`tests/test_staging_flow.py` | 分别覆盖结构/文本、图片角色描述、顺序规划/恢复/视频/工作台的永久回归。共享样例放 `tests/fixtures/staging.json`。 |
| 新增 `tests/test_segment_prompts.py`、`tests/test_video_segment_prompts.js`；扩展 `tests/test_segment_recovery.py` | 视频改词实际生效、离线改写/提交恢复、失败保留旧片、草稿/播放器/分页/双语交互的回归。 |
| `tests/test_frame_batch.py`、`tests/test_frame_moderation.py`、`tests/test_submission_retry.py` | 仅将「模拟新文字响应」的夹具改成 v2；明确测旧缓存的三字段夹具保留。 |
| `README.md`、`docs/workbench.md` | 说明单首帧状态传递与人工验收；只修正相关的首镜参考旧说明，不改历史展示产物。 |

不增调度编辑器、数据库、配置开关或新 CLI 命令；新增页面控件限定为视频片段提示词编辑及必要状态。Tasks 1–4 落地调度，Tasks 5–6 在同一计划中补齐视频侧后端与交互；两组共享实际视频提示词的生成和缓存规则。

### 5. 图片与视频操作能力对照

视频片段指现有 `film.plan()` 的一次生成片段（可能包含多镜），不是新增「每镜一个视频」或从成片重新切割。现有视频已能播放、下载、按版本换种子重生成及手动拼接；缺口是提示词查看/编辑、草稿/提交关联、对应失败项的可恢复编辑。复用现有播放器和事务，不重写这些已存在能力。

| 能力 | 图片侧现有参照 | 视频侧目标与验收 |
| --- | --- | --- |
| 查看 | storyboard.view 返回已保存 prompt；卡片展开 | segments.view 返回本段源提示词及实际提交 H3；本地缓存读取，无模型调用。 |
| 手动编辑 | textarea + frameDrafts + expandedFrames | 单段 textarea + segmentDrafts + expandedSegments；未提交时清楚标注「草稿未应用」，当前视频不变。 |
| 提示词格式 | 图片编辑的是实际画图文字 | 有 zh 的片段默认编辑源提示词，展开只读查看实际 H3；只有 H3 的旧任务允许编辑 H3 并做本地格式校验。编辑器标签明确区分两者，不推测格式。 |
| 应用改词 | 按「重生成」才提交 prompt | 同一个「仅重生成此段」动作提交 prompt/format/版本；改 source 时先用已有 rewrite 改写并缓存 H3，不能继续使用原 H3 冒充新词。 |
| 不改词重生成 | 可以用原 prompt 再生成 | 保留原能力：文本未变则复用已有 H3，不多调文字接口，仅重生成目标片段。 |
| 草稿保留 | 轮询/分页保留；成功匹配后清除 | 轮询、Tab、切页、语言变化不覆盖草稿；仅本次内容和新版本匹配成功后清除，失败保留。与图片现状一致，不新增跨浏览器/跨刷新持久草稿承诺。 |
| 失败与恢复 | 终态失败可手改；已接单/未知提交锁定 | 有失败记录但没有新视频也能显示 prompt/error；终态失败允许用户手动再次生成，已有 task 或 submission_unknown 只恢复原尝试，不接受新词覆盖。 |
| 成功发布 | 临时图成功后替换，旧图备份 | 复用 inputs/segment-<revision>，新视频和尾帧验证成功后替换；期间旧片和旧成片可播放，失败不覆盖它们。 |
| 权限与版本 | 服务端验证状态、素材和审核 | 仍验证已审图、无未应用分镜变更、任务互斥及提示词/文件版本；前端禁用与后端拒绝一致。 |
| 下游 | 改图使对应视频待更新 | 换段成功标记成片待拼接，不自动拼接；first/none 仅提示复核下一段，不级联重生成。 |
| 新增文字 | 中英文 JSON | 编辑器、草稿、费用、格式错误、任务锁定和失败原因均来自现有语言 JSON；不硬编码。 |

**提示词与恢复契约：**

1. 扩展原 `POST /api/jobs/<id>/segments/<number>/regenerate`，新 UI 提交 `{version, prompt_version, prompt, prompt_format}`；prompt_format 只能是 `source` 或 `h3`。prompt 是非空字符串；prompt_version 是服务端返回的本段文件版本、现行 zh/H3 和尝试 revision 的哈希，防止仅文字改变但旧页面仍覆盖。旧 `{version}` 请求仅允许在现有视频和有效提示词可用且无待处理尝试时保持原换种子行为。
2. source 优先显示 state.segs 的 zh，离线旧任务可从本段已保存 `.zh.txt` 读取；只有 h3 则编辑器自动进入 h3 标签。没有可恢复文字和没有尝试记录的未开始项保持只读等待，不能借此绕过初次审图流程。缺视频但有终态失败记录的项允许新 UI 用 prompt_version 发起人工修复，不依赖非空视频版本。
3. 单段 pending 保存用户原文、format、提示词版本和 previous；改词后的 zh/H3 写入本次 staged Job，再由原 gen_segment 提交。source 未变且存在对应 H3 时不改写；source 变更则先 rewrite、落盘 H3 后提交视频。h3 模式先用既有 sections/problems 本地检查实际图片编号及六段结构，不调用改写模型。demo 模式不调用任何文字/生成服务，不将占位结果伪称实际 H3 输出。
4. preflight 只为尚未改写的 source 新尝试要求文字 key；直接 H3、原词复用和已接单恢复不新增文字 key 要求。改写/上传/提交/轮询/下载失败分别记录到该尝试；source/H3 缓存、task、submission_unknown 及发布进度在中断后复用，不因点击恢复自动开启一笔新的终态失败生成。
5. 显式手改视频提示词的成功版本带 `prompt_override: {format, text, source_version}`，后续 stage_prompts/preflight 不再用自动 composition 覆盖它；重生成失败时 active 记录和原 prompt 不变。source_version 是当次自动源提示词与本段实际参考文件版本的哈希。若这些输入随后变化，保留手改文本并以 source_changed 提示复核，仍走原 dirty_segments/审图门禁，不悄悄删除或改写它；普通自动生成段继续使用 Tasks 1–4 的共享状态。
6. 只替换目标段及其尾帧，其它段/分镜图不变；新实际 H3 保存到该段 state 和 `.h3.txt`，source 同步 `.zh.txt`。备份保留旧词/旧任务便于恢复，费用沿用现有当前版本口径，不额外扩展历史费用分析。
7. 查看、输入草稿、展开/收起、轮询和只拼接均零模型调用。按钮表明 source 修改可能产生文字改写及视频费用；未改词只产生目标段视频费用。开发验收全部用本地 fixture/mock，不因本轮计划或未来代码获批就执行真实生成。

## 实施任务（后续 autoplan 评审及重新授权后才执行）

测试文件使用 unittest；各自 setUp 通过 `json.loads((Path(__file__).parent / 'fixtures/staging.json').read_text(encoding='utf-8'))` 读取 `self.fixture`。下面断言块列出核心可执行断言，其余同一步命名用例也必须按说明先写后修；不引入额外测试框架。

### Task 1: 调度数据边界与起点/动作输出

**Files:** Create `h3ref/staging.py`, `tests/test_staging.py`, `tests/fixtures/staging.json`；Modify `h3ref/messages/zh-CN.json`, `h3ref/messages/en.json`。

**Interfaces:**
- Produces `validate_composition(value: object, film: dict, shot: dict, known_scenes: dict[str, dict] | None = None) -> dict`，只接收 v2 并返回规范化副本；非法数据抛 ValueError，消息含字段路径和实际值，通过 JSON 文案本地化。
- Produces `render_opening(composition: dict) -> str`，输出 scene/frame、世界布局、机位与当前 characters/props，不包含 action 的完成结果或 expected_end；v2 不再额外输出旧 orientation 汇总，避免重复或冲突指令。
- Produces `render_motion(composition: dict) -> str`，输出同一起点、原动作和文字终点；不生成/引用尾图。
- fixture 定义 `film / transfer_start / transfer_end / reverse_closeup / new_scene / return_scene`；transfer_start、transfer_end 是相邻镜 composition，红袋在前镜 expected_end 与后镜 props 中同为接收者持有。

- [ ] **Step 1: 写失败回归。** `test_prop_parts_survive_bottom_crop` 断言 render_opening 同时包含「朝世界上方」「朝世界下方」「袋体顶部的袋口处」「袋底超出画面下边缘」；`test_opening_excludes_transfer_result` 断言起点仍由递出者持有且不包含终点断言「递出者已经松手」；render_motion 包含这项结果。`test_invalid_state_reports_path_and_value` 用 `characters[0].gaze=7` 断言异常含 `characters[0].gaze` 和 `7`。`test_known_scene_layout_cannot_change_on_reverse` 改同 scene_id 锅的位置，断言校验拒绝而非悄悄覆盖。

```python
def test_prop_parts_survive_bottom_crop(self):
    text = staging.render_opening(self.fixture['reverse_closeup'])
    for expected in ('朝世界上方', '朝世界下方', '袋体顶部的袋口处', '袋底超出画面下边缘'):
        self.assertIn(expected, text)

def test_opening_excludes_transfer_result(self):
    composition = self.fixture['transfer_start']
    self.assertNotIn('递出者已经松手', staging.render_opening(composition))
    self.assertIn('递出者已经松手', staging.render_motion(composition))
```

同 Step 1 一并写「无人道具特写」「有理由的转头回望」「两个场景同名固定物」「中英文文案及占位符一致」子用例，分别断言允许空人物、允许身体/视线不同、场景内校验及完整本地化。
- [ ] **Step 2: 运行 red。** `.venv/bin/python -m unittest discover -s tests -p 'test_staging.py' -v`；预期新模块不存在或上述断言失败，不接受依赖/环境错误当 red 证据。
- [ ] **Step 3: 实现三个接口及样例。** 按数据契约校验；角色可见特征不强求正脸，画外角色保留画外状态；非适用部件允许 null。用现有 message() 输出固定标题及错误。不要实现自然语言左右解析、三维投影或通用 patch 状态机。
- [ ] **Step 4: 运行 green。** 同 Step 2 命令；全部通过。
- [ ] **Step 5: 运行下文构建检查。** 预期编译、JS 语法、语言键/占位符全部通过。
- [ ] **Step 6: 本地提交实现。** `feat(storyboard): add structured staging descriptions`，仅暂存本任务模块/语言文件；不推送。
- [ ] **Step 7: 再运行构建检查。** 源文件检查通过后才能执行下一次 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): cover staging and prop orientation boundaries`，仅暂存本任务 fixture/回归。

### Task 2: 逐镜角色文字与首帧请求

**Files:** Modify `h3ref/frames.py:59-115`；Create `tests/test_frame_staging.py`；Consume Task 1 fixture。

**Interfaces:**
- Extends `describe_composition(film: dict, shot: dict, *, previous_composition: dict | None = None, known_scenes: dict[str, dict] | None = None) -> dict`；沿用原 LLM 配置、600 秒超时及 `tries=1`，新请求必须返回校验过的 v2。
- Preserves `prompt(film: dict, shot: dict, who: list[dict], with_board: bool, composition: dict | None = None) -> str`；v2 使用 render_opening，旧/无缓存分支兼容现有行为，不请求网络。

- [ ] **Step 1: 写失败回归。** `test_same_actor_changes_pose_between_shots` 用同一 cast.look（含「正面站立、毛巾挂脖」）构造「背对镜头站在摊内」与「跪地侧身托袋，毛巾在案板」两镜；对最终 prompt 断言分别包含本镜 position/body_facing/gaze/pose，后一镜不含固定姿态「正面站立」「毛巾挂脖」，两镜都保留角色姓名与参考图身份约束。`test_character_reference_is_identity_only` 断言同一参考图不会把背影改写为正脸。`test_llm_receives_cast_and_previous_staging` mock `frames.http.request`，检查实际请求含 cast look/aliases、原 camera/action、previous_composition/known_scenes；假响应缺 gaze 则在 draw 前报错。

```python
def test_same_actor_changes_pose_between_shots(self):
    script = self.fixture['film']
    who = script['cast']
    texts = [frames.prompt(script, script['shots'][i], who, False, self.fixture[key])
             for i, key in enumerate(('transfer_start', 'transfer_end'))]
    for text, key in zip(texts, ('transfer_start', 'transfer_end')):
        actor = self.fixture[key]['characters'][0]
        for field in ('appearance', 'position', 'body_facing', 'gaze', 'pose'):
            self.assertIn(actor[field], text)
        self.assertNotIn('正面站立', text)
        self.assertNotIn('毛巾挂脖', text)
    self.assertNotEqual(texts[0], texts[1])
```

同 Step 1 写 `test_board_optional_reference_numbers_match_upload_order`、`test_crop_does_not_turn_knot_into_bottom`、`test_legacy_composition_stays_readable`，分别检查有/无总表编号、红袋结构字段、旧三个字段的无网络渲染。
- [ ] **Step 2: 运行 red。** `.venv/bin/python -m unittest discover -s tests -p 'test_frame_staging.py' -v`；预期固定 look 泄漏/新上下文未传入/新响应被旧解析丢弃导致断言失败。
- [ ] **Step 3: 最小修改 describe_composition 与 prompt。** 把稳定身份与当前状态分别写入请求；当前镜头及明确的连续动作状态优先于全局 look 中的姿势/道具位置。每个参考角色仍按 who 的真实顺序编号；v2 角色说明用本镜 appearance + position + body_facing + gaze + pose + expression，画外不拉入画。禁止把 expected_end 拼入图片文字，禁止引用其它已生成分镜。
- [ ] **Step 4: 运行 green。** 同 Step 2；全部通过。既有审核回归仍通过，最终 prompt 仍经过原 NEUTRAL_PATTERN。
- [ ] **Step 5: 运行构建检查。** 预期全绿；报告具体修复行号和 red/green 证据。
- [ ] **Step 6: 本地提交实现。** `fix(storyboard): describe characters from each shot state`。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): reproduce fixed character copy across shots`；不把模板测试当作真实图效果通过。

### Task 3: 顺序文字规划、缓存恢复与并行首帧

**Files:** Modify `h3ref/film.py:207-255`；Create `tests/test_staging_flow.py`；Modify 三个既有测试文件中新响应夹具，范围见文件职责表。

**Interfaces:**
- Preserves `stage_frames(job: Job, film: dict, segs: list[dict], pics: dict, *, resume_tasks: bool = False) -> list[str]`。
- Produces `saved_compositions(records: dict) -> dict[str, dict]`；只返回 version=2、composition_prompt 为非空字符串且 prompt 与其相等的记录，key 沿用现有 frame key。legacy、未知版本、缺来源、手动失配记录不注入新上下文。
- frame 新自动记录原子写入 `composition / prompt / composition_prompt`；原 task/submission_unknown/cny 字段和保存锁机制保持现有意义。

- [ ] **Step 1: 写失败回归。** `test_planning_is_ordered_across_segment_boundary` mock describe_composition 记录调用顺序；让第2镜处于新视频段，断言收到第1镜 expected_end 和同一 scene 布局。`test_cut_and_return_keep_world_layout` 使用摊位→街道→摊位，断言返回旧 scene_id，锅仍在原固定物位置；正反打只变 camera/screen_position。`test_planning_failure_does_not_become_previous_state` 让中镜返回无效 JSON，断言后镜仍尝试、错误记录指向中镜、前文只用最近有效规划及原剧本。

核心断言放入上述跨段测试，在 mock describe 返回 fixture、mock draw 写临时小图后执行：

```python
self.assertEqual(film.stage_frames(job, script, segments, pictures), [])
self.assertEqual(describing.call_args_list[1].kwargs['previous_composition'],
                 self.fixture['transfer_start'])
self.assertEqual(drawing.call_count,
                 sum(p['kind'] == 'frame' for refs in pictures.values() for p in refs))
self.assertEqual(film.Job(job.dir).state['frames']['s2-1']['composition_prompt'],
                 film.Job(job.dir).state['frames']['s2-1']['prompt'])
```

同 Step 1 写 `test_resume_does_not_replan_or_resubmit`（恢复 describe/submit=0、原 task 不变）、`test_crash_after_planning_reuses_saved_text`（中断后不重付文字费用）、`test_one_opening_per_wanted_shot_and_no_tail`（四镜 draw=4、key 不变、无尾图、仅原始参考），以及部分失败、first/none、九图限额的参数用例。
- [ ] **Step 2: 运行 red。** `.venv/bin/python -m unittest discover -s tests -p 'test_staging_flow.py' -v`；预期无跨镜传递或顺序断言失败。
- [ ] **Step 3: 实现顺序规划与缓存筛选。** 对既有 wanted 按镜头顺序先整理待生成项，逐项保存；从已存 v2 重建 known_scenes，包含上下文来源镜号。规划失败按现有单镜边界落盘，不进入画图池；其余已准备项进入原四线程 draw。不为已有图/任务、旧 composition 或未分配图片位的镜头额外调用文字模型。已有图片缺失但 task 已存在时，只用已保存提示词/参考恢复，不能先生成新版计划。
- [ ] **Step 4: 运行 green。** 同 Step 2 并运行现有 test_frame_batch.py、test_frame_moderation.py、test_submission_retry.py；预期全绿，FrameBatch/BatchTaskRecovery 的恢复保护保持通过。
- [ ] **Step 5: 运行构建检查。** 预期全绿。
- [ ] **Step 6: 本地提交实现。** `feat(storyboard): cache staging before parallel frame drawing`，包含必要旧夹具适配。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): preserve staging continuity without extra generation`。

### Task 4: 视频提示词与工作台使用相同有效状态

**Files:** Modify `h3ref/film.py:113-166,259-277`、`h3ref/rewrite.py:24-48`、`workbench/storyboard.py:17-37,67-92,205-217`、`workbench/inputs.py:174-191`、`README.md`、`docs/workbench.md`；Extend `tests/test_staging_flow.py`, `tests/test_frame_staging.py`。

**Interfaces:**
- Extends `compile_prompt(film: dict, seg: dict, pics: list[dict], count: int, *, compositions: dict[str, dict] | None = None) -> str`；key 是 Task 3 frame key；本段完全没有有效 v2 时保留原输出，避免旧缓存无故失效。混合段逐镜消费：v2 镜用结构状态，旧镜保留原 camera/action/line，并只在该旧镜内提供 setting 背景，不再在全段顶部混入全片场景；cast 中已由 v2 描述的角色不再段首拼旧 look。
- `stage_prompts()` 和 workbench preflight 都调用 `saved_compositions(state['frames'])`，并以同样的 compositions 调用 compile_prompt。
- `storyboard.plan/view` 仅渲染缓存；`regenerate` 继续保存用户真实 prompt，保留 composition_prompt 来源记录，不为手改重新规划。

- [ ] **Step 1: 写失败回归。** `test_video_uses_matching_shot_states_without_global_pose` 断言两镜各自 camera/pose/gaze/prop 字段进入对应时间段，递袋动作顺序与原台词保持，expected_end 只进视频；对拥有 v2 的角色，不再把整段旧 cast.look 放入段首身份定义。有 v2 时 scene 布局按镜头输出，不能另拼全片 setting 混入另一地点。`test_manual_prompt_change_excludes_stale_composition` 改已保存 prompt，断言 saved_compositions 排除该镜、视频不含旧调度断言；不改变其它镜。`test_preview_and_preflight_do_not_replan` 在 Flask test client 中读取结果/调用 preflight，令所有文字/图片网络入口遇调用即失败，断言与 stage_prompts 的缓存命中一致。

```python
def test_manual_prompt_change_excludes_stale_composition(self):
    composition = self.fixture['transfer_start']
    records = {'s1-1': {'composition': composition,
                         'composition_prompt': '原自动提示词', 'prompt': '手动新提示词'}}
    self.assertEqual(film.saved_compositions(records), {})
    records['s1-1']['prompt'] = '原自动提示词'
    self.assertEqual(film.saved_compositions(records), {'s1-1': composition})
    del records['s1-1']['prompt'], records['s1-1']['composition_prompt']
    self.assertEqual(film.saved_compositions(records), {})
```

同 Step 1 写 `test_mixed_legacy_and_v2_do_not_force_migration`、`test_manual_regeneration_keeps_real_prompt_and_invalidates_review`、`test_reference_edit_preserves_uploaded_order`：保存/展示/实际 draw 字符串一致，原 task 只轮询，手改沿用 dirty_segments/确认失效，角色增删不改固定总表位。参考图调整沿用已有手工提示词编辑方式，不新增自动理解/改写手工文字的能力。
- [ ] **Step 2: 运行 red。** 分别执行 `.venv/bin/python -m unittest discover -s tests -p 'test_staging_flow.py' -v` 与 `.venv/bin/python -m unittest discover -s tests -p 'test_frame_staging.py' -v`；预期 compile_prompt 未接状态、全局旧姿态泄漏或 preflight/缓存失配。
- [ ] **Step 3: 接通消费端并更新相关说明。** 视频调用 render_motion，由它内部复用 render_opening，避免重复拼起点；已覆盖镜头的固定布局不再被全局 setting 盖掉，身份由角色参考与逐镜可见外观锚定。rewrite.RULES 明确身体、视线、物体结构方向、动作接触顺序分别保留，不能把普通参考图说成严格时间关键帧。仅更新相关文档，说明仍无自动视觉验收、旧任务不自动迁移。
- [ ] **Step 4: 运行 green。** 两个新测试文件预期全部通过；rewrite.request 用本地 guide 及 mock backend 验证传入状态与规则，不调用 Claude，不用假响应宣称已验证真实改写质量。
- [ ] **Step 5: 运行下文完整离线验证及构建检查。** 预期全部通过；报告修复行号与回归证据。
- [ ] **Step 6: 本地提交实现与相关文档。** `fix(storyboard): carry current shot staging into video prompts`；不推送、不部署。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): guard staging cache and manual prompt recovery`。

### Task 5: 视频提示词编辑与单段重生成数据流

**Files:** Modify `workbench/segments.py:50-176`、`workbench/jobs.py:195-200,341-363,365-391`、`workbench/server.py:251-257`、`h3ref/film.py:259-277`、`workbench/inputs.py:174-191`、`workbench/messages/zh-CN.json`、`workbench/messages/en.json`；Create `tests/test_segment_prompts.py`；Extend `tests/test_segment_recovery.py`。

**Interfaces:**
- Extends `segments.view(request: dict, artifacts: list[dict], editable: bool) -> list[dict]`，每段新增 `prompt: str`、`prompt_format: str`、`source_prompt: str`、`h3_prompt: str`、`prompt_version: str`、`source_changed: bool`、`error: str | None`、`editable: bool`、`task_pending: bool`、`submission_unknown: bool`；保留原 version/can_regenerate/chained_next。prompt 优先显示本段最近失败尝试的文字供修复，否则显示生效文字；source_prompt/h3_prompt 则明确是已生效版本，不能把失败文字冒充旧视频所用的词。首次失败无生效版本时二者为空、编辑器仍显示失败尝试文字。
- Produces `segment_prompt_version(record: dict, version: str | None, pending: dict | None = None) -> str`，用于新 UI 的文字及尝试版本保护；hash 输入按「提示词与恢复契约」确定。
- Produces `segment_source_version(request: dict, number: int) -> str`，hash 本段当前自动 compile_prompt 结果和实际参考文件的路径/版本；用于保存 override.source_version 与计算 source_changed，不请求网络。旧 override 无来源版本时不自动迁移或重生成。
- Extends `prepare_regeneration(request: dict, number: int, version: str | None, *, prompt: str | None = None, prompt_format: str | None = None, prompt_version: str | None = None) -> str`，沿用返回 revision；旧调用可用，新增参数严格校验。
- Extends `Jobs.start_video_operation(identifier, operation, env, version, number=None, *, prompt=None, prompt_format=None, prompt_version=None)`；沿用原 regenerate 路由。对照 can_edit_frames 统一计算片段可编辑条件，并在 snapshot/POST 同时使用；assemble 保持原语义。
- `regenerate(request)`、`preflight(request, env)` 沿用原入口；成功 active 记录新增 prompt_override，失败/未知提交留在 pending/staged，不能污染生效版本。Task 4 的 stage_prompts/preflight 在存在 override 时复用它。

- [ ] **Step 1: 写失败回归。** `test_segment_snapshot_exposes_saved_prompts_without_network` 通过 Flask test_client GET 断言本段 source/H3 与磁盘一致；`test_edited_source_is_rewritten_before_video_submission` 令 mock 文字后端返回合格六段 H3，在 RH 边界捕获实际 graph prompt，断言收到新词而非原词；`test_unchanged_source_reuses_h3` 断言原词重生成不请求文字；`test_h3_only_legacy_edit_validates_before_submission` 检查旧 H3 可编辑、缺段落/越界图片引用本地拒绝。所有网络 mock 在现有 HTTP/RH 边界，保留真实 pending/state/版本/发布逻辑。

```python
# 在真实 prepare/regenerate 流程中检查生成器收到的 graph，而非断言返回假数据本身。
self.assertIn('The recipient kneels beside the counter.', submitted_graph[rh.PROMPT]['inputs']['prompt'])
self.assertNotIn('The recipient stands in front of the counter.', submitted_graph[rh.PROMPT]['inputs']['prompt'])
self.assertEqual(other_clip.read_bytes(), original_other_clip)
self.assertEqual(final_film.read_bytes(), original_film)
self.assertTrue(segments.assembly_status(request)['pending'])
```

同 Step 1 一并写：空/非字符串/未知 format/越界段号/过时 prompt_version 的 HTTP 400；无视频但有终态失败记录可手动重生成；无 task 的未知提交仍拒绝新词覆盖；已接单断线只恢复原 task；source 改写成功后视频失败再恢复不二次改写；source 未变/直接 H3/仅拼接不要求文字 key；旧 `{version}` 请求仍有效。还覆盖 publish 中断后只完成发布、手改 override 不被后续 stage_prompts 覆盖、仅本段源/参考变化时 source_changed 为 true、换图后保留 override 并显示复核状态。用户侧错误通过双语 JSON 给出字段及实际值。
- [ ] **Step 2: 运行 red。** `.venv/bin/python -m unittest discover -s tests -p 'test_segment_prompts.py' -v`；预期 GET 缺 prompt、POST 忽略新文本或旧 H3 被错误提交等行为失败。分清特性缺失与测试环境错误。
- [ ] **Step 3: 扩展既有事务实现契约。** 校验/预检完成后再写 pending 和启动子进程；先缓存新 H3 再提交视频，沿用 staged Job 与原任务号恢复。只有新片及尾帧验证通过才更新 active、prompt 文件和 override；失败保留旧片、旧词及草稿来源。补齐失败态的快照/POST 门禁，已有 task/未知提交锁定，终态失败不通过恢复按钮自动重付。保持其它段与分镜图不变，不自动拼接。普通自动段继续遵守 Task 4，新手改段以明确 override 为准。
- [ ] **Step 4: 运行 green。** 同 Step 2，并运行 `.venv/bin/python -m unittest discover -s tests -p 'test_segment_recovery.py' -v`；预期全部通过，新/旧尝试费用记录沿用现有口径。
- [ ] **Step 5: 运行构建检查。** 预期全绿；如修复既有任务覆盖/重提缺陷，报告具体行号与 red/green。
- [ ] **Step 6: 本地提交实现。** `feat(video): support prompt edits in segment regeneration`，只提交本任务实现及双语错误。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): preserve edited video prompts and paid task recovery`；不推送、不部署。

### Task 6: 与图片侧对齐的视频提示词交互和验收

**Files:** Modify `workbench/static/video-segments.js:54-137`、`workbench/static/results.js:3-35`、`workbench/static/style.css:372-382`、`workbench/static/messages/zh-CN.json`、`workbench/static/messages/en.json`、`docs/workbench.md`；Create `tests/test_video_segment_prompts.js`；Extend `tests/test_results.js` 仅在共享状态夹具需要时；Modify `.github/workflows/test.yml`，将新增 JS 回归加入现有 node --test 命令。

**Interfaces:**
- `state` 新增 `segmentDrafts: Map<number, string>`、`expandedSegments: Set<number>`、`segmentSubmissions: Map<number, {prompt: string, prompt_format: string, prompt_version: string, version: string | null}>`；与 frame* 状态按相同生命周期维护，不混用编号/草稿。
- `renderVideoSegments(job)` 保留原入口，消费 Task 5 的新快照；新 POST payload 使用当前段实时 prompt_version。只在本次提交文本/格式与成功版本匹配时清理对应草稿，不清除其它段或失败项。
- 源提示词编辑 + 实际 H3 只读展开复用原生 details/label/textarea 方式；旧 H3-only 状态用同一个编辑器，标签显示实际格式；不加模型选择器或可编辑视频参考图。

- [ ] **Step 1: 写失败交互回归。** `test_video_prompt_editor_posts_current_draft` 在现有轻量 DOM/vm 测试方式中渲染真实 video-segments.js，展开/输入/点击后捕获实际 API 路径及 payload；`test_polling_keeps_segment_draft_and_player` 连续轮询同一视频，断言编辑文字、展开态和播放器对象不变；`test_segment_drafts_survive_pagination_and_tabs` 切页/Tab 再返回仍显示本段草稿；`test_success_clears_only_matching_submission` 检查失败保留、新成功版本才清本段、其它段不受影响。对 zh-CN/en 两套语言运行。

```javascript
assert.equal(editor.value, "按当前机位，接收者跪地侧身托袋");
assert.equal(posted.url, "/api/jobs/job-1/segments/2/regenerate");
assert.equal(posted.body.prompt, "按当前机位，接收者跪地侧身托袋");
assert.equal(posted.body.prompt_format, "source");
assert.equal(posted.body.prompt_version, "prompt-v2");
assert.strictEqual(card.video, originalVideo);
```

同 Step 1 写未知任务/运行中禁止提交但旧片可播放、空词禁止提交、终态失败无视频仍可编辑、费用及草稿提示/格式标签双语、切换语言不丢输入、仅提示词改变不重建播放器等用例。用中英 JSON 的渲染结果检查可见文案，不用 grep 源码代替行为测试。
- [ ] **Step 2: 运行 red。** `node --test tests/test_video_segment_prompts.js`；预期缺编辑器、payload 只有 version 或轮询丢草稿失败。
- [ ] **Step 3: 补齐视频编辑交互。** 对照 createStoryboardCard 的懒展开、草稿提示与提交关联，实现视频侧同等行为；保留每页六段、Tab 选择、Plyr、下载与手动拼接。按钮同时校验权限、非空文本及锁定状态；新增 source 改词可能产生文字+视频费用的说明和手改后换图需复核提示，全部使用中英 JSON。
- [ ] **Step 4: 运行 green。** `node --test tests/test_results.js tests/test_frame_character_picker.js tests/test_video_segment_prompts.js`；全部通过。用 Flask test_client 与临时文件联通「查看→改词→仅重生成第2段→失败保留→恢复成功→待拼接→手动拼接」，mock 文字/图片/视频网络，真实验证本地状态与其它文件字节不变；不启动服务器、不调用真实生成。
- [ ] **Step 5: 运行完整离线验证及构建检查。** 使用下文命令；所有既有回归保持通过，新增 JS 回归同时进入 CI。更新工作台说明与对照验收证据，不把离线占位视频当真实生成效果。
- [ ] **Step 6: 本地提交实现、CI 与相关说明。** `feat(video): add segment prompt editing controls`。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): retain video prompt drafts and editor state`；UI 实施交付后等待用户体验反馈，再考虑下一轮优化。

## 验证方法与检查点

**检查点 A：当前仅文档提交。** 先补齐视频操作能力对照、Tasks 5–6 及验收，自审后运行 git 前构建检查，本地提交计划与必要协作记录，然后停止。用户将清空会话后另行运行 autoplan；本轮不评审、不编码。执行方式已选 Native，待后续评审与重新授权后由主会话顺序实施六项任务，最后独立 Codex 审查；无需再询问执行方式。

**构建等价检查（无前端构建器，不伪造 pnpm build）：**

```bash
.venv/bin/python -m compileall -q h3ref.py workbench.py h3ref workbench scripts
```

对 `workbench/static/` 自有 JS 及两个 JS 测试逐文件运行 `node --check`（排除 vendor）；用 `.venv/bin/python` 校验 h3ref/workbench/scripts 三组语言 JSON 的 key 与 format 占位符一致。任何 git 操作前先做以上检查；实施完成再跑完整测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_results.js tests/test_frame_character_picker.js tests/test_video_segment_prompts.js
```

预期：无失败；仅保留已知的平台条件跳过，解释任何新 skip。相关新用例必须先在旧行为上失败，再在实现后通过；缺模块的 Task 1 red 与实际行为缺陷的 Tasks 2–4 red 分开记录。每个修复提交前报告具体文件/行号及回归 red/green；若确有未写回归，按规则15提醒「这个 bug 修了 [文件:行号]，但还没写回归测试，要不要顺手补一条？」并补齐，不省略已要求的回归。已经先写回归时如实报告已完成，不能虚报“还没写”。

**检查点 B：离线实现验收。** 以合成的两角色摊位故事审查真实编译出的提示词，覆盖拿起、递交、背面跟拍、正反打、换场返回、袋底裁切；禁止硬编码样例人名/镜号。使用 unittest.mock 拦截文字、图片、视频网络，Flask test client 验证读取与恢复；不启动服务器。核对无尾图路径、无其它生成图引用、无额外任务提交，并保留现有审核和未知提交回归。

视频侧另逐条验收上述能力对照表：源词/实际 H3 可查看、手改实际进入本段生成、原词重生成不多改写、草稿与播放器不被轮询/分页/语言变化打断、失败保留旧片和新草稿、task/未知提交不被覆盖、有效终态失败可人工重生成、只影响目标段、旧成片待拼接。仅拼接零模型调用；直接 H3 与缓存恢复无额外文字调用。用例覆盖 shot/first/none、旧任务缺 zh、没有视频的失败项、过期版本以及分镜更新后保留手改 override 的复核状态。

**检查点 C：用户检查与真实生成前。** 离线验证完成后交付变化/测试证据，停下等用户查看。若要评估真实画面，再单独给出镜头清单、模型、图片/文字调用上限和费用估计，取得授权；不能借旧实验授权继续消费。建议最多六镜：复用原任务 21–26 镜素材，逐镜只画一张首帧，不加候选、不自动重画、不先生成视频。真实图验收分别检查角色身份、逐镜姿态/视线、锅/案板位置、交接持有者、袋口/袋底/结扣及画面裁切。失败计入结果，不通过就不追加付费调用。

核心成功标准：容量允许的 N 镜仅 N 张首帧；同一角色在不同镜头的状态文字随镜头变化；世界布局不随镜头画面左右自动变化；图片提示词不提前演完动作；自动视频文字引用同一有效状态，明确的手改视频文字优先；视频具备图片侧对应的查看/改词/重生成与草稿/事务保护；查看、恢复和升级不会暗中重规划/重提交。离线通过只证明这些输入和流程约束，不证明模型一定画对。

## 资料与本次计划验证

- 已阅读两份指定研究与本地 cwp 固定快照的调度/提示词规则；不运行外部脚本或安装包。
- 2026-11-09 尝试 Context7 的 Python 文档检索，返回 `Monthly quota exceeded`，未获得检索结果；回退查阅 [Python unittest.mock 官方文档](https://docs.python.org/3.10/library/unittest.mock.html) 和 [Python JSON 官方文档](https://docs.python.org/3.10/library/json.html)。本计划沿用标准库 mock 与 dict/JSON，不依赖新 API。
- 视频编辑补充调研尝试 Context7 的 Flask 文档检索，同样返回月额度耗尽；改查 [Flask 3.1 test client 官方文档](https://flask.palletsprojects.com/en/stable/testing/)，确认沿用 test_client 在不启动服务时验证 JSON GET/POST。没有因此新增 pytest 或其它依赖。
- 补充核对了图片侧 storyboard.plan/view/regenerate、frameDrafts/submissions 与视频侧 segments.view/prepare_regeneration/regenerate、Jobs.start_video_operation、原 POST 路由和 video-segments.js；确认复用已有单段重生成，而非另建生成链路。
- 自审覆盖：原四条要求、逐镜角色与视频编辑要求都有任务/断言；字段/签名/缓存 key 统一；六个 Review Focus 均有所属测试，六任务共48个步骤；真实视觉效果与离线约束验证分开。
- 本轮只更新并本地提交计划及必要协作记录；git 前运行构建等价检查，不实施上述接口，不运行业务回归或生成服务，不推送、不部署，不启动 autoplan。清空会话后的评审、重新授权实施、离线验收和真实付费授权仍是后续步骤。
