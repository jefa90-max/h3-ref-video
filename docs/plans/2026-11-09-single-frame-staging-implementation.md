<!-- /autoplan restore point: "/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/feat-video-segment-review-autoplan-restore-20261109-staging.md" -->
## URGENT, outside this plan

None.

**Autoplan plan status:** APPROVED（2026-11-09，用户回复 `rec`，gate_rev=1，d1a/d2a/p1a）。仅认可本版文档；IMPLEMENTATION NOT STARTED，真实付费生成仍须另行询问。

## Implementation plan
# 单首帧场面调度、逐镜角色描述与视频片段编辑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task after review and renewed implementation authorization. The user selected implementation in the main session, followed by one independent Codex review. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留本项目「一镜一张首帧」，将场景固定物、逐镜人物调度与道具状态贯穿图片及视频提示词，并让视频片段具备与分镜图对应的提示词查看、手动编辑及单项重生成能力。

**Architecture:** 扩展 composition 缓存，新增纯校验/渲染 h3ref/staging.py；原 film JSON、供应商和 H3 Ref2VA 接口保持。文字调度按镜头顺序逐项校验、落盘后，立即将该镜首帧投入原四线程池，与后续顺序文字规划重叠；图片线程不反向修改规划上下文。图片与视频共用有效逐镜状态。视频编辑复用 segments 单段暂存、恢复和成功后发布，不另建链路。

**Tech Stack:** Python 3.10+、标准库 `json` / `unittest.mock` / `concurrent.futures`、现有 Pillow、Flask 3.1、APIMart 图片/文字接口、RunningHub H3 Ref2VA；不新增依赖。

**Spec:** 用户明确需求（逐镜调度、角色描述与视频片段提示词查看/编辑/重生成），以及 [cwp 参考价值评审](2026-11-09-cwp-ai-video-review.md)、[分镜连续性研究](2026-11-08-storyboard-continuity-research.md)。视频侧基线参见 [现有视频片段方案](video-segment-review.md) 和 [现有交互设计](../design/video-segment-review.md)；其中“不增加编辑器”已被本需求覆盖。研究中的首尾帧、换模型、Director 续接仅作背景。当前轮授权是运行 autoplan 并写回计划或审查说明。

**Current authorization (2026-11-09):** 本轮已获授权运行 autoplan、审查并完善本计划，仅修改计划、审查说明及会话 todo；不写业务或测试代码，不提交、不推送、不部署，不启动/重启服务，不调用/探测 Claude。真实付费文字、图片和视频生成必须另行询问。计划审查通过不等于获准实施；六项实施任务仍未开始，后续由主会话顺序实施、末尾独立 Codex 审查的方式保留，须重新获得实施授权。

## Global Constraints

- 「保留本项目现有『一镜一张首帧』工作流，不要改成正式的首尾帧生产路线。」
- 「单独写清场景固定物、人物位置、身体朝向、视线、机位，以及道具状态（如袋口朝向、袋底方向、结扣位置等要分开写，避免歧义）。」
- 「每镜的角色描述要随该镜动作、朝向、位置变化，不要整片共用同一段固定角色文案。」
- 「视频片段也要能像分镜图那样手动改提示词、手动重新生成」；对照图片侧查看/编辑/重生成交互与数据流落实同等能力，不以已有的换种子按钮冒充完成。
- 当前用户指令覆盖上一轮“仅更新并本地提交、下轮运行 autoplan”的时序：本轮运行审查并写回文档后停止；无业务编码、提交、推送或部署授权。未来实施和真实付费生成分别确认。
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

1. `stage_frames()` 按全片顺序整理文字，逐镜校验落盘后立即交给原四线程画图池，不设全片文字完成屏障。规划始终单写者，图片并行只能更新各自生成记录，不能回写 known_scenes/previous_composition；所有 Future 均回收并按原顺序汇总单镜失败。
2. 新文字请求收到原始角色资料、当前镜头、既有前文、最近可信 composition 与 `known_scenes`（`scene_id -> {world_frame, fixed_objects}`）。已知 scene_id 的 world_frame/fixed_objects 视为固定输入，要求逐字复用，不允许因反打而换边；新场景新建布局，返回原场复用布局。不能把旧场景人物持物强灌到新场景。
3. 每镜仍最多复用现有的一次文字整理；不再追加一轮整片规划或每角色调用。增加输出字段会增加 token，串行文字规划会延后首张图开始时间；本轮不宣称文字费用/总耗时不变。
4. `expected_end` 始终是计划，不能标为「已在图片发生」。规划失败记到单镜，后续镜仍尝试；下一镜只接最近可信记录及其来源镜号，未覆盖的动作由原剧本文字提供，不伪造缺失状态。
5. 原frames key仍为s{段号}-{段内镜号}；自动v2保存composition_prompt及composition_reference_version。下游必须同时确认prompt非空相等、有序参考来源版本相等；缺任一标记保守回退原词、不请求模型。参考来源按E5复用实际路径/顺序/既有文件版本。
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
| 提示词格式 | 图片编辑的是实际画图文字 | 有 source 默认编辑源词；同一编辑器允许明确切换到实际 H3，复用本地六段/编号校验并直接提交；H3-only旧任务默认H3。切换保存各格式草稿，不将source改写结果冒充手改H3。 |
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
- Produces `saved_compositions(records: dict, *, reference_versions: dict[str, str]) -> dict[str, dict]`；只接受v2、非空匹配的composition_prompt与composition_reference_version。调用者用实际有序参考路径/版本构造本次reference_versions，规划、view、preflight、stage_prompts复用此判定；缺来源则排除，不额外规划。
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
- [ ] **Step 3: 实现顺序规划与缓存筛选。** wanted 按全片镜头顺序推进；从严格早于当前镜的有效 v2 重建场景与前序状态，逐镜保存后立即入原四线程池。规划失败落单镜、不中止后续规划；图片失败不撤销已校验的“计划状态”，不宣称画面已执行。所有Future结束后汇总失败。已有图/task/旧composition/未分配图片位不得额外规划；恢复原task只用已存提示词/参考，不先生成新版计划。
- [ ] **Step 4: 运行 green。** 同 Step 2 并运行现有 test_frame_batch.py、test_frame_moderation.py、test_submission_retry.py；预期全绿，FrameBatch/BatchTaskRecovery 的恢复保护保持通过。
- [ ] **Step 5: 运行构建检查。** 预期全绿。
- [ ] **Step 6: 本地提交实现。** `feat(storyboard): cache staging before parallel frame drawing`，包含必要旧夹具适配。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): preserve staging continuity without extra generation`。

### Task 4: 视频提示词与工作台使用相同有效状态

**Files:** Modify `h3ref/film.py:113-166,259-277`、`h3ref/rewrite.py:24-48`、`workbench/storyboard.py:17-37,67-92,205-217`、`workbench/inputs.py:174-191`、`README.md`、`docs/workbench.md`；Extend `tests/test_staging_flow.py`, `tests/test_frame_staging.py`。

**Interfaces:**
- Extends `compile_prompt(film: dict, seg: dict, pics: list[dict], count: int, *, compositions: dict[str, dict] | None = None) -> str`；key 是 Task 3 frame key；本段完全没有有效 v2 时保留原输出，避免旧缓存无故失效。混合段逐镜消费：v2 镜用结构状态，旧镜保留原 camera/action/line，并只在该旧镜内提供 setting 背景，不再在全段顶部混入全片场景；cast 中已由 v2 描述的角色不再段首拼旧 look。
- stage_prompts与workbench preflight共享一次读取和实际reference_versions，调用saved_compositions后以相同compositions编译；先处理已有task/unknown/完成片段冻结及override，不用自动编译覆盖它们。
- `storyboard.plan/view` 仅渲染缓存；`regenerate` 继续保存用户真实 prompt，保留 composition_prompt 来源记录，不为手改重新规划。

- [ ] **Step 1: 写失败回归。** `test_video_uses_matching_shot_states_without_global_pose` 断言两镜各自 camera/pose/gaze/prop 字段进入对应时间段，递袋动作顺序与原台词保持，expected_end 只进视频；对拥有 v2 的角色，不再把整段旧 cast.look 放入段首身份定义。有 v2 时 scene 布局按镜头输出，不能另拼全片 setting 混入另一地点。`test_manual_prompt_change_excludes_stale_composition` 改已保存 prompt，断言 saved_compositions 排除该镜、视频不含旧调度断言；不改变其它镜。`test_preview_and_preflight_do_not_replan` 在 Flask test client 中读取结果/调用 preflight，令所有文字/图片网络入口遇调用即失败，断言与 stage_prompts 的缓存命中一致。

```python
def test_manual_prompt_change_excludes_stale_composition(self):
    composition = self.fixture['transfer_start']
    records = {'s1-1': {'composition': composition,
                         'composition_prompt': '原自动提示词', 'prompt': '手动新提示词',
                         'composition_reference_version': 'refs-v1'}}
    refs = {'s1-1': 'refs-v1'}
    self.assertEqual(film.saved_compositions(records, reference_versions=refs), {})
    records['s1-1']['prompt'] = '原自动提示词'
    self.assertEqual(film.saved_compositions(records, reference_versions=refs), {'s1-1': composition})
    self.assertEqual(film.saved_compositions(records, reference_versions={'s1-1': 'refs-v2'}), {})
    del records['s1-1']['prompt'], records['s1-1']['composition_prompt']
    self.assertEqual(film.saved_compositions(records, reference_versions=refs), {})
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

**Files:** Modify `workbench/segments.py:50-176`、`workbench/jobs.py:95-182,195-200,341-391`、`workbench/server.py:251-276`、`h3ref/film.py:259-330,386-395`、`h3ref/rewrite.py:107-120`、`workbench/storyboard.py:230-248`、`workbench/inputs.py:174-191`、`workbench/messages/{zh-CN,en}.json`；Create `tests/test_segment_prompts.py`；Extend `tests/test_segment_recovery.py`、`tests/test_review_flow.py`、`tests/test_film_completion.py`、`tests/test_submission_retry.py`。

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
- [ ] **Step 3: 扩展既有事务实现契约。** 校验/预检完成后才写pending/启动worker，先存新H3再提交，恢复复用staged与task。按E3先备份整组并建立版本映射，以applied原子写作为唯一可见提交点；此前所有读取返回previous，此后返回new。逐个replace不视为整组原子。API/snapshot/media/assembly统一读取规则，v不匹配不得返回别版字节。E1保留override、E2覆盖初次pipeline终态门禁、E4严格H3编号、E6配对来源；只影响本段，不自动拼接。
- [ ] **Step 4: 运行 green。** 同 Step 2，并运行 `.venv/bin/python -m unittest discover -s tests -p 'test_segment_recovery.py' -v`；预期全部通过，新/旧尝试费用记录沿用现有口径。
- [ ] **Step 5: 运行构建检查。** 预期全绿；如修复既有任务覆盖/重提缺陷，报告具体行号与 red/green。
- [ ] **Step 6: 本地提交实现。** 新能力用feat(video)；既有缺陷E1/E2/E3/E4按根因单独fix(...)提交，不混入feat。每次git前构建，报告修复文件/行号及red/green；回归另以test(fix):提交。此处全是未来获实施授权后的动作。
- [ ] **Step 7: 再运行构建检查。** 检查通过才继续 git 操作。
- [ ] **Step 8: 本地提交回归。** `test(fix): preserve edited video prompts and paid task recovery`；不推送、不部署。

### Task 6: 与图片侧对齐的视频提示词交互和验收

**Files:** Modify `workbench/static/video-segments.js:54-137`、`workbench/static/results.js:3-35`、`workbench/static/style.css:372-382`、`workbench/static/messages/zh-CN.json`、`workbench/static/messages/en.json`、`docs/workbench.md`；Create `tests/test_video_segment_prompts.js`；Extend `tests/test_results.js` 仅在共享状态夹具需要时；Modify `.github/workflows/test.yml`，将新增 JS 回归加入现有 node --test 命令。

**Interfaces:**
- `state` 新增 `segmentDrafts: Map<number, {selectedFormat: 'source'|'h3', drafts: Partial<Record<'source'|'h3', {text: string, basePromptVersion: string, baseClipVersion: string|null}>>}>`、`expandedSegments: Set<number>`、`segmentSubmissions: Map<number, {prompt: string, prompt_format: string, prompt_version: string, version: string|null, revision: string|null}>`；按job清理，按段/格式保留草稿与基线。
- `renderVideoSegments(job)` 保留入口并消费Task5快照。首次编辑绑定basePromptVersion/baseClipVersion，POST使用该基线，不在点击时取最新版本；冲突保留草稿，只在用户复核并明确采用当前版本后rebase。匹配成功revision、文本和格式且用户未再改时才清该格式草稿。
- 同一提示词编辑器默认 source，允许明确切换到实际 H3；每种格式保留独立草稿与基线。复用原生 details/label/textarea 和轻量格式选择，不加模型选择器或可编辑视频参考图。

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

**检查点 A：当前仅审查文档。** autoplan 按 CEO → Design → DX（scope 命中）→ Eng 顺序审查，写回修订、验收与审查记录；最终确认只确认计划，不开启实施。本轮不提交。后续重新授权后主会话顺序实施六项任务，末尾独立 Codex 审查；无需重问执行方式。

**构建等价检查（无前端构建器，不伪造 pnpm build）：**

```bash
.venv/bin/python -m compileall -q h3ref.py workbench.py h3ref workbench scripts
```

对 `workbench/static/` 自有 JS 及所有 `tests/*.js` 逐文件运行 `node --check`（排除 vendor）；递归校验 `h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json`、`workbench/static/messages/{zh-CN,en}.json`、`scripts/messages/windows-{zh-CN,en}.json` 四组语言 JSON 的 key/format 占位符。只对存在的文件执行；新增测试尚未创建时不伪称运行通过。任何 git 操作前先完成构建等价检查。

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
- 上一轮已完成文档提交；本轮以最新用户指令为准执行 autoplan 并写回审查，不提交、不实施、不运行生成服务。Context7 再次返回 Monthly quota exceeded，回退核对 Flask test client、Python unittest.mock/concurrent.futures 官方文档；未新增依赖。

<!-- autoplan-accepted:ceo -->
- 最新授权优先：本轮只审查写文档，不提交、不编码、不启动服务、不调用 Claude 或付费生成；替换旧 Spec、Current authorization、Global Constraints、检查点 A 和旧本轮结论中的过时时序。计划批准与实施/真实生成授权分开。
- 保留六项任务和一镜一张首帧；复用 frames/composition、segments 的 pending/staged/publish、现有播放器与消息 JSON，不新增调度编辑器、视觉自动评审、数据库、依赖、切段/模型或生成链路。
- 调度缓存表示剧本预期状态，expected_end 不是模型执行证据；后续上下文只消费时间上早于当前镜的有效记录，不能从全量缓存读入未来场景或未来持物状态。Task 3 增加未来缓存不污染前镜、失败镜不推进可信状态的离线回归。
- 成本与等待如实表述：新图片位每镜至多一次 describe_composition（tries=1）；新视频 source 改写最多两轮格式纠正，每轮 _llm 沿用 http.request 默认 tries=3，最多六次文字 HTTP 请求（仅 H3_REWRITER=llm 路线；Claude 不调用/不探测）。未来真实授权按物理请求上限预算，响应丢失可能已计费，两轮不等于两次付费请求。查看/草稿/预检/恢复缓存/仅拼接零模型调用；恢复已接单任务不重新规划或改写，不因升级覆写已完成视频使用的词。
- Task 3 记录每镜规划开始、缓存命中、失败和来源镜号，Task 5 记录 revision、格式、改写缓存命中及提交/恢复/发布阶段，沿用 run.log 与 state.json，不新增监控服务。日志不用凭据或完整用户剧本文字；文档写明文字规划总时长为各未命中请求时长之和，600秒只是单次超时，不承诺大型剧本首图立即产出。
- task_pending 只表示当前未发布尝试尚未取得明确终态的任务；成功 active 版本里的历史 task 不锁定重生成。当前未决 task/submission_unknown 仅恢复原尝试，明确终态失败必须由用户新建 revision 才能再次付费；Task 5 加成功 active 带历史 task 仍可再生成的回归。
- Task 5–6 默认编辑 source，同时允许普通及旧任务在同一编辑器明确切换直接 H3 编辑；复用 prompt_format=h3、本地六段/图片编号校验、原事务，H3直提不再改写、不要求文字key。两种格式各保留草稿和对应版本，提交只应用当前选中的一份。Task 5/6 回归覆盖普通有source任务直改H3、切格式不串词及真实RH graph只收到所选文本。
- Task 3 使用顺序规划→逐镜落盘→立即入现有四线程池，取代全片规划屏障；规划单写者，图片线程不能更新调度上下文，所有Future必须回收。用Event控制第2镜规划阻塞时第1镜draw已开始；规划失败/图片失败分别保留、取消/中断保留已落盘成果；不靠sleep或实时时长断言，不增加调用数。
- 未来真实生成授权之前预登记最多六镜对照：历史输出只作非受控基线，冻结新输入/模型/参考；逐镜记身份、姿态/视线、机位/固定物、持有者、袋口/袋底/结扣及裁切，通过标准为全部适用项正确、无身份换人/交接反转/倒袋/固定物无因移动。任一失败都不追加调用；不足全部通过不声称视觉连续性获证。历史与新模型/提示条件有差异时只报告观察，不作单一因素因果结论。离线功能通过与真实画面获证分开记录。
<!-- /autoplan-accepted:ceo -->

<!-- autoplan-accepted:design -->
- Task 6 固定层级为段号/当前视频状态→播放器→本次尝试状态及错误→懒展开提示词；编辑器标识“当前视频使用”“上次失败尚未应用”“未提交草稿”，新尝试运行时注明仍播放旧版。当前生效H3可查看；首次失败无片也显示失败词，不能冒充有成功版本。Task 5/6 加旧片存在、首次失败无片、恢复发布成功的来源显示断言。
- Task 5 segments.view 增只读 reference_images 清单，按将用于本次提交的 film.refs/_picture_paths 实际顺序返回 number、kind、角色/全局镜号、available，包含省略总表及九图裁减提示，不传任意可编辑路径、不新增参考图编辑/上传入口。active历史引用若未保存不能声称可重建，标历史顺序未知并将新清单标“本次重生成参考”。Task 5/6 以无总表、九图裁减和旧H3任务验证清单与捕获的实际上传一一对应。
- Task 5/6 共用尝试动作判断：当前未决task只恢复原任务；submission_unknown解释接单结果未确认并禁止新生成，只保留既有安全恢复路径；明确终态失败显示“用当前提示词再次生成”及新费用说明，resume不得重新付费；成功版本保留普通重生成。卡片内呈现原因和下一步，全局resume与本段按钮不能给出互相矛盾的可用动作。所有可见文字与字段错误使用中英文JSON。
- Task 6 将 segmentDrafts:Map<number,string> 细化为 Map<number,{selectedFormat:'source'|'h3',drafts:Partial<Record<'source'|'h3',{text:string,basePromptVersion:string}>>}>，每段选择与两份草稿跨轮询/Tab/分页/语言保留、换job清空；切格式只切文本不执行转换。有另一格式草稿时明确本次只应用所选格式；成功只清匹配提交且其后未继续修改的那份，不清另一份。测试H3输入→source输入→切页→切语言→提交所见格式及对应payload。
- Task 6 首次改词时绑定当时服务端prompt_version与clip version，轮询不得自动更新草稿基线；POST用该基线，冲突保留文字并展示当前版本供复核。用户明确“以当前版本使用此草稿”后才更新基线并再次允许提交，此动作不生成、不改写；无静默覆盖/自动合并。Task 5/6 测两标签页或受控后台发布造成版本变化后原草稿请求被拒绝、费用调用数为零。
- Task 6 沿用760px断点：宽屏保持原两列片段卡、编辑器在卡内全宽；≤760单列、格式选择和按钮换行，不横向滚动，视频contain保比例；375/760/1060中英长词核验。新增编辑区文本16px、提示/错误对比≥4.5:1、操作触点≥44px、可见label和focus-visible；summary/格式选择/textarea/提交按DOM顺序Tab，箭头保留文本编辑行为，无全局快捷键抢占。轮询不替换已聚焦textarea，不改变selection/IME组合输入；状态变化仅一次aria-live polite，字段错误aria-describedby。Task 6 用DOM测试焦点/选区/组合输入和键盘，最终小屏/读屏由用户UAT验收，不启动服务代替用户检查。
<!-- /autoplan-accepted:design -->

<!-- autoplan-accepted:dx -->
- Task 3 的Files新增 h3ref.py 和其CLI回归所在tests/test_staging_flow.py（h3ref/messages双语错误随Task1/3扩展）。单镜失败继续、Future全回收、成果持久化后，公开film CLI因frame_failures以非零退出，在stderr列失败key、state路径、稳定code及安全修复动作；工作台的批次可修复状态保留，不把任务改成首错即停。真实CLI入口离线回归证明退出非零、其它镜继续、无额外submit。Task3 Step6将此既有信号bug单独fix(cli)提交，回归单独test(fix)，不混入feat；所有提交仅限未来获实施授权后。
- Tasks1/3 校验和文字响应错误给字段路径、脱敏截断实际值、期望类型/约束、镜号、schema版本、模型配置名和记录位置，并说明该规划请求可能已收费。复用现有无网络图片prompt回退，使失败卡片可手工编辑后仅重出图片，不自动再调文字、不新增调度编辑器。无可恢复输入/素材时明确缺失项与人工动作；回归缺gaze、错类型、同scene布局变更、空/拒绝/无效JSON后可以安全手修，不称真实图正确。
- Task5 快照保留error字符串，增加attempt_error可选结构：code、phase、revision、prompt_format、task_present、side_effects(none|partial|complete|unknown)、recovery(action/next_step/retry_bound/attempts_used)。本地无提交失败可修后重试并披露文字重付条件；已有task只恢复；unknown须人工核对；终态失败只由显式新revision再付费。既有重试上限不扩大，不把异步失败伪装成POST已同步失败；运行快照中的失败状态同样有机器可读信息。
- Task5 仅对新增view/版本/读取/prepare边界的意外异常记录脱敏紧凑trace和correlation id，响应给instance及段号/revision的安全上下文；预期校验错误不打trace。沿用已有本地日志，禁止原始凭据、含key的URL、异常链密钥或完整剧本文字。用注入OSError断言可关联、无网络、无敏感值；不重构全站异常体系。
- Task5本次扩展regenerate POST错误返回application/problem+json，字段type（稳定urn:workbench:error:<code>）、title、status、detail、instance（意外失败）、code、field（适用时）、doc_anchor；保留error=detail兼容前端。校验/冲突沿现有HTTP400，意外500，missing job404；details/next_step双语JSON，code稳定，doc_anchor指docs/workbench.md实际锚点。依据RFC9457允许扩展字段，不创建新错误服务；测试状态/code/field/中英detail/脱敏，旧前端仍可读error。
- Task6正文和类型改为首次编辑同时绑定basePromptVersion/baseClipVersion；POST使用草稿基线，只经明确复核才rebase。Task6 Files新增CONTRIBUTING.md：与现有CI同步新增Node回归命令；docs/workbench.md写source/H3各一份GET取版本→带X-Workbench/Accept-Language的完整POST示例、预期快照/费用条件、未决/unknown/终态/发布中断动作表和升级前停任务备份、用户自行重启、旧任务不迁移说明。全部验证离线，不用文档命令实际付费或启动服务；本轮构建文案归一为四组locale和所有自有/测试JS。
<!-- /autoplan-accepted:dx -->

<!-- autoplan-accepted:eng -->
- E1 / Tasks4–5：Files明确包含film.invalidate_segments、storyboard.run及tests/test_review_flow.py。分镜换图后失效视频时保留prompt_override及原source_version/source-H3配对来源；只清旧生成task/费用/检查等运行态，不把override重绑定到新来源。显式redo的其它既有行为不扩展。用真实Flask编辑视频→成功发布→换分镜→重新确认→pipeline继续的离线调用链，捕获实际RH graph仍用手改词、source_changed=true、其它段不变，不能只孤立测stage_prompts。
- E2 / Task5：Files包含film.gen_segment与tests/test_film_completion.py、tests/test_submission_retry.py。初次pipeline和手改单段的远端明确终态失败都持久标记terminal_failed；全局resume只报告人工动作，不清标记再提交。只有用户显式单段新revision才允许新费用。明确拒单且证实未接单仍沿原有重试上限和恢复策略，不能混同终态已收费任务；已有task传输/轮询故障只查原task，unknown仍锁定。离线证明poll终态失败→全局resume submit=0→显式手动新revision submit=1，并保留605/421/415与未知提交旧回归。
- E3 / Task5：复用segment-edit.json的applied作为唯一“新版可见”提交点。先验证候选整组，再完成旧clip/tail/本段zh/h3文件与previous记录的备份及版本映射，最后才设publishing；发布中逐项替换完成并保存active后，原子写applied=true。Jobs.snapshot在一次请求开始读取同一个pending/read context，segments.view、artifacts白名单、提示词下载和媒体路由全部据此解析：提交前返回previous整组，提交后返回new整组。媒体URL的既有v必须匹配所选版本；旧v仍可解析时只返回备份，无法解析则明确拒绝，绝不静默返回新字节；不承诺无限历史选择器、不暴露备份任意路径。工作台assemble和CLI pipeline在未提交publishing时不得消费混版根文件，先恢复发布或给安全指引，不进行模型调用。注入clip/tail/zh/h3/active/applied各步OSError及模拟重启，真实GET与Range请求断言同一版本视频/提示词一致、旧片保留、resume仅发布且rewrite/upload/submit为0。只有applied后才能报成功/清匹配草稿/标待拼接。
- E4 / Task5：Files增加h3ref/rewrite.py及其现有校验测试或tests/test_segment_prompts.py；直接H3的每个Picture引用逐项验证1≤N≤len(actual_reference_images)，在写pending/改写/上传前拒绝Picture0与上界+1，保持六段次序、Shot约束。测试0、上界+1、合法1/上界和无参考图分支，非法请求没有磁盘状态和费用副作用；错误走双语JSON。
- E5 / Tasks3–4：自动v2记录除composition_prompt外保存生成时实际有序参考来源标记（角色/场景表/该镜输入路径及既有版本元数据）；有效composition必须同时匹配当前prompt和该来源。手动换角色参考成功但文字不变也使旧composition失效，历史composition保留但不注入known_scenes/视频。优先复用现有character_reference_paths/参考版本，不新建素材库或哈希整视频；缺来源标记的旧记录保守回退原文字且不规划。缓存恢复、工作台view、preflight和stage_prompts使用同一判定，测试同词异参考/重排/文件版本变化及读取零网络。
- E6 / Tasks4–5：既有可恢复task、unknown、已完成片段的提示词/参考先冻结，不因v2编译升级或查看/preflight覆盖；显式失效后按E1保留override。source→H3复用必须有准确配对来源，直接手改H3发布后不得继续将旧zh标成当前视频源词；旧zh仅留previous追溯，默认进入H3，source为空，用户明确选source则以当前自动编译作为“新草稿”并重新改写，不能复用不配对H3。纯本地统一解析函数返回生效、尝试、自动来源和配对标记，供view/preflight/stage_prompts复用；无数据库和模型反推。测试新旧任务、override、source/H3往返、未变词但参考变、缺key恢复、已完成输出不改字节。
- E7 / Tasks5–6：每次snapshot只读取一份state/pending并共享给所有片段，自动prompt/source_version每段最多编译一次；reference元数据在本次请求复用，新增日志有stage/cache/source/revision且脱敏。100段/1000镜的离线合成夹具做调用次数回归，state/pending读取不随段数重复、compile次数≤段数、模型/上传/提交均0；不对平台抖动写绝对毫秒测试。未来实施前后同机同Python/Node、相同夹具与暖/冷条件交错测10次snapshot，记录中位/p95/输出字节/基线commit；设计目标暖快照p95≤500ms且不超过基线2倍，二者皆为待测预算而非结果。若超标先消除重复读取/编译，再提证据评估，不擅加持久缓存或依赖。
<!-- /autoplan-accepted:eng -->
## Review record

### Intake / Step 0（2026-11-09）

- 原始计划已逐字节备份：`.gstack/tmp/autoplan/feat-video-segment-review-autoplan-restore-20261109-staging.md`，SHA256 `f34d7fa86d95edcf74ba9735df4db74be03e1615c715c149e26b0d301810a0b8`。
- 分支 `feat/video-segment-review`；GitHub，无当前 PR，基线 `origin/main`（本地 remote HEAD），未 fetch，远端新鲜度未验证。工作树既有 lessons 修改和三份未跟踪研究文档保留，不混入本次修改。未找到项目 AGENTS.md、CLAUDE.md、TODOS.md 或当前 worktree `.workflow-state.md`；不启用阶段追踪。
- scope 输入 SHA256 同原计划；UI=true；DX=true，threshold=2、matchCount=15，命中 API×6、CLI×1、library×2、action×5、implement×1；developerTool=false、agentPrimary=false。DX 评审限定本地脚本/API和贡献者恢复体验，不虚构公共 SDK。
- 方法论已读 CEO SKILL 1–1044 与 review-sections 全文（导出 1–2257；外部调用段按用户约束不执行）。本机是 Codex；原 Claude host 外部 driver 不匹配，Codex 外部 CLI 不启动；Claude 按用户长期约束跳过，不探测。独立 Codex 原生子代理是同模型复核，不能记为跨模型共识。GUARD_NOT_INSTALLED：宿主未强制 publication hook，手动执行阶段顺序。
- review depth=implementation-ready；模式 SELECTIVE EXPANSION，来源 /autoplan 的显式 override。预计涉及约30个实现/测试/文档文件；选择复用现有接口并按六任务分段，不擅自删掉视频编辑。PR 数维持默认1，暂不创建。现有设计文档已直接列为输入，不新增 office-hours 流程。

#### Premise table

| Premise | Claim | Receipt | Verdict |
|---|---|---|---|
| P1 | 每镜原 look 会泄漏姿势 | h3ref/frames.py:109 | confirmed |
| P2 | 视频仍拼全局 setting/look | h3ref/film.py:129,149 | confirmed |
| P3 | 视频仅缺编辑，已有单段暂存与发布 | workbench/segments.py:103,130 | confirmed |
| P4 | 结构正确能保证画面对 | 本计划“核心成功标准”明确不作此承诺 | rejected premise |
| P5 | 缓存/恢复天然无需改写 | h3ref/film.py:264,268 当前仅按 zh 判断，需保护 | finding |
| P6 | 所有参考帧都能容纳 | h3ref/film.py:100-111 存在九图裁减 | rejected premise，保留已明示边界 |

#### Alternatives / scope ledger

| ID/owner | Contract/evidence | Current | Proposed | Status | Authority/scope |
|---|---|---|---|---|---|
| C1/CEO | 当前授权冲突 | 旧轮提交且不跑 autoplan | 更新为本轮审查写回 | auto-decided | 用户本轮明示，仅文档 |
| C2/CEO | 双需求范围 | 六任务 | 保留，复用旧链路 | auto-decided | P1/P4/P5，不增加产品能力 |
| C3/CEO | 调度可信度 | 计划状态 | 时间前缀过滤与来源日志 | auto-decided | P1，Task3回归 |
| C4/CEO | 成本/升级 | 隐含缓存假设 | 明确两次改写上限与旧任务冻结 | auto-decided | P1/P5，Task4/5恢复回归 |

方案A（推荐）：结构化调度+现有单段编辑，覆盖所有需求，维护现有 dict/事务；方案B：仅补自然语言模板和视频 textarea，代码更少但无法验证跨镜状态、改词版本及恢复；方案C：全片规划/视觉校验/新任务平台，增加费用与基础设施且越过当前边界。A与B并非同等覆盖，按P1选A；C不采纳。世界坐标字符串逐字复用能阻止漂移，但不能理解语义或反推出实际画面。

12个月理想是可解释且可验证的导演状态与成片一致性。本轮只交付受控提示输入与人工修复闭环，不能把结构化 JSON 当成视觉连续性的证明。

```text
固定 look + 独立构图 -> 逐镜计划状态 + 有版本的单段人工修复 -> 实际画面与状态可验证（未来，未纳入）
```

0G delight scan：①当前/草稿标签、②错误保留旧片、③聚焦后不被轮询打断、④换图复核提示、⑤改写/视频费用提示，均属现有目标所需体验，不加新功能。跨刷新草稿、多版本选择、调度编辑器与自动视觉评分不采纳；不产生 TODOS.md 新增项。

0I：基础阶段明确世界/屏幕/物体坐标；核心阶段验证前缀上下文和旧缓存；集成阶段验证 preflight→staged→RH→publish 与 override；收尾验证交互并等用户 UAT。估计人工团队3–5日、Codex+gstack约4–8小时（含离线测试和修订，非承诺，不含真实生成和用户验收）。关键风险是错误地把“计划的结果”当作“已生成事实”。

<!-- AUTONOMOUS DECISION LOG -->
### Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| C1 | ceo | 更新本轮授权 | Mechanical | P5 | 最新指令明确 | 旧轮时序 |
| C2 | ceo | 六任务复用既有链路 | Mechanical | P1/P4 | 保留完整需求 | 独立平台/只做textarea |
| C3 | ceo | 仅消费前序有效计划并记录来源 | Mechanical | P1 | 禁止未来状态泄漏 | 扫全缓存当上下文 |
| C4 | ceo | 冻结旧任务词，明示两次改写成本 | Mechanical | P1/P5 | 恢复不能产生新消费 | 升级隐式重写 |

<!-- autoplan-baseline-edits:ceo {"sourceSha256":"f34d7fa86d95edcf74ba9735df4db74be03e1615c715c149e26b0d301810a0b8","replacements":[{"oldText":"**Spec:** 用户明确需求（含追加的逐镜角色描述、视频片段提示词编辑/重生成，以及最新的仅更新计划并本地提交指令），以及 [cwp 参考价值评审](2026-11-09-cwp-ai-video-review.md)、[分镜连续性研究](2026-11-08-storyboard-continuity-research.md)。视频侧基线参见 [现有视频片段方案](video-segment-review.md) 和 [现有交互设计](../design/video-segment-review.md)；其中「本轮不增加视频提示词编辑器」已被此次补充需求覆盖。研究中的首尾帧、换模型、Director 续接建议仅作背景。下文「需求边界与数据契约」是本计划实施时必须同时阅读的规格。","newText":"**Spec:** 用户明确需求（逐镜调度、角色描述与视频片段提示词查看/编辑/重生成），以及 [cwp 参考价值评审](2026-11-09-cwp-ai-video-review.md)、[分镜连续性研究](2026-11-08-storyboard-continuity-research.md)。视频侧基线参见 [现有视频片段方案](video-segment-review.md) 和 [现有交互设计](../design/video-segment-review.md)；其中“不增加编辑器”已被本需求覆盖。研究中的首尾帧、换模型、Director 续接仅作背景。当前轮授权是运行 autoplan 并写回计划或审查说明。"},{"oldText":"**Current authorization (2026-11-09):** 用户已撤回「改完计划立即实施」。本轮只更新并本地提交计划与必要协作记录，提交后停止；不写业务代码、不运行 autoplan、不调用付费生成。用户将清空会话后另行运行 autoplan；本计划的六项实施任务尚未开始，待评审及重新授权。主会话逐项实施、最后独立审查是保留的执行方式，不代表当前已获实施许可。","newText":"**Current authorization (2026-11-09):** 本轮已获授权运行 autoplan、审查并完善本计划，仅修改计划、审查说明及会话 todo；不写业务或测试代码，不提交、不推送、不部署，不启动/重启服务，不调用/探测 Claude。真实付费文字、图片和视频生成必须另行询问。计划审查通过不等于获准实施；六项实施任务仍未开始，后续由主会话顺序实施、末尾独立 Codex 审查的方式保留，须重新获得实施授权。"},{"oldText":"- 「本轮先不实施编码」「更新计划文件后本地 git 提交计划改动」「提交后停下」；不要推送、不要部署。后续实施和真实付费文字/图片/视频调用分别取得授权，不能沿用已撤回的立即实施许可。","newText":"- 当前用户指令覆盖上一轮“仅更新并本地提交、下轮运行 autoplan”的时序：本轮运行审查并写回文档后停止；无业务编码、提交、推送或部署授权。未来实施和真实付费生成分别确认。"},{"oldText":"**检查点 A：当前仅文档提交。** 先补齐视频操作能力对照、Tasks 5–6 及验收，自审后运行 git 前构建检查，本地提交计划与必要协作记录，然后停止。用户将清空会话后另行运行 autoplan；本轮不评审、不编码。执行方式已选 Native，待后续评审与重新授权后由主会话顺序实施六项任务，最后独立 Codex 审查；无需再询问执行方式。","newText":"**检查点 A：当前仅审查文档。** autoplan 按 CEO → Design → DX（scope 命中）→ Eng 顺序审查，写回修订、验收与审查记录；最终确认只确认计划，不开启实施。本轮不提交。后续重新授权后主会话顺序实施六项任务，末尾独立 Codex 审查；无需重问执行方式。"},{"oldText":"- 本轮只更新并本地提交计划及必要协作记录；git 前运行构建等价检查，不实施上述接口，不运行业务回归或生成服务，不推送、不部署，不启动 autoplan。清空会话后的评审、重新授权实施、离线验收和真实付费授权仍是后续步骤。","newText":"- 上一轮已完成文档提交；本轮以最新用户指令为准执行 autoplan 并写回审查，不提交、不实施、不运行生成服务。Context7 再次返回 Monthly quota exceeded，回退核对 Flask test client、Python unittest.mock/concurrent.futures 官方文档；未新增依赖。"},{"oldText":"**Architecture:** 扩展现有 `composition` 缓存，新增一个负责调度结构校验与文本输出的 `h3ref/staging.py`；原始 film JSON、图片供应商和 H3 Ref2VA 工作流保持现有接口。复用每镜文字整理调用，顺序缓存状态后并行生成唯一首帧，图片与视频共用逐镜状态；视频编辑复用 `workbench/segments.py` 的单段暂存、恢复与成功后发布事务，不另建生成链路。","newText":"**Architecture:** 扩展 composition 缓存，新增纯校验/渲染 h3ref/staging.py；原 film JSON、供应商和 H3 Ref2VA 接口保持。文字调度按镜头顺序逐项校验、落盘后，立即将该镜首帧投入原四线程池，与后续顺序文字规划重叠；图片线程不反向修改规划上下文。图片与视频共用有效逐镜状态。视频编辑复用 segments 单段暂存、恢复和成功后发布，不另建链路。"},{"oldText":"1. `stage_frames()` 先按全片镜头顺序读取缓存并整理缺失的文字状态，再启动现有四线程画图。缓存每镜一经校验立即保存，保证恢复不重做已完成文字请求。","newText":"1. `stage_frames()` 按全片顺序整理文字，逐镜校验落盘后立即交给原四线程画图池，不设全片文字完成屏障。规划始终单写者，图片并行只能更新各自生成记录，不能回写 known_scenes/previous_composition；所有 Future 均回收并按原顺序汇总单镜失败。"},{"oldText":"| 提示词格式 | 图片编辑的是实际画图文字 | 有 zh 的片段默认编辑源提示词，展开只读查看实际 H3；只有 H3 的旧任务允许编辑 H3 并做本地格式校验。编辑器标签明确区分两者，不推测格式。 |","newText":"| 提示词格式 | 图片编辑的是实际画图文字 | 有 source 默认编辑源词；同一编辑器允许明确切换到实际 H3，复用本地六段/编号校验并直接提交；H3-only旧任务默认H3。切换保存各格式草稿，不将source改写结果冒充手改H3。 |"},{"oldText":"- 源提示词编辑 + 实际 H3 只读展开复用原生 details/label/textarea 方式；旧 H3-only 状态用同一个编辑器，标签显示实际格式；不加模型选择器或可编辑视频参考图。","newText":"- 同一提示词编辑器默认 source，允许明确切换到实际 H3；每种格式保留独立草稿与基线。复用原生 details/label/textarea 和轻量格式选择，不加模型选择器或可编辑视频参考图。"},{"oldText":"- [ ] **Step 3: 实现顺序规划与缓存筛选。** 对既有 wanted 按镜头顺序先整理待生成项，逐项保存；从已存 v2 重建 known_scenes，包含上下文来源镜号。规划失败按现有单镜边界落盘，不进入画图池；其余已准备项进入原四线程 draw。不为已有图/任务、旧 composition 或未分配图片位的镜头额外调用文字模型。已有图片缺失但 task 已存在时，只用已保存提示词/参考恢复，不能先生成新版计划。","newText":"- [ ] **Step 3: 实现顺序规划与缓存筛选。** wanted 按全片镜头顺序推进；从严格早于当前镜的有效 v2 重建场景与前序状态，逐镜保存后立即入原四线程池。规划失败落单镜、不中止后续规划；图片失败不撤销已校验的“计划状态”，不宣称画面已执行。所有Future结束后汇总失败。已有图/task/旧composition/未分配图片位不得额外规划；恢复原task只用已存提示词/参考，不先生成新版计划。"}]} -->

<!-- autoplan-accepted:ceo -->
- 最新授权优先：本轮只审查写文档，不提交、不编码、不启动服务、不调用 Claude 或付费生成；替换旧 Spec、Current authorization、Global Constraints、检查点 A 和旧本轮结论中的过时时序。计划批准与实施/真实生成授权分开。
- 保留六项任务和一镜一张首帧；复用 frames/composition、segments 的 pending/staged/publish、现有播放器与消息 JSON，不新增调度编辑器、视觉自动评审、数据库、依赖、切段/模型或生成链路。
- 调度缓存表示剧本预期状态，expected_end 不是模型执行证据；后续上下文只消费时间上早于当前镜的有效记录，不能从全量缓存读入未来场景或未来持物状态。Task 3 增加未来缓存不污染前镜、失败镜不推进可信状态的离线回归。
- 成本与等待如实表述：新图片位每镜至多一次 describe_composition（tries=1）；新视频 source 改写最多两轮格式纠正，每轮 _llm 沿用 http.request 默认 tries=3，最多六次文字 HTTP 请求（仅 H3_REWRITER=llm 路线；Claude 不调用/不探测）。未来真实授权按物理请求上限预算，响应丢失可能已计费，两轮不等于两次付费请求。查看/草稿/预检/恢复缓存/仅拼接零模型调用；恢复已接单任务不重新规划或改写，不因升级覆写已完成视频使用的词。
- Task 3 记录每镜规划开始、缓存命中、失败和来源镜号，Task 5 记录 revision、格式、改写缓存命中及提交/恢复/发布阶段，沿用 run.log 与 state.json，不新增监控服务。日志不用凭据或完整用户剧本文字；文档写明文字规划总时长为各未命中请求时长之和，600秒只是单次超时，不承诺大型剧本首图立即产出。
- task_pending 只表示当前未发布尝试尚未取得明确终态的任务；成功 active 版本里的历史 task 不锁定重生成。当前未决 task/submission_unknown 仅恢复原尝试，明确终态失败必须由用户新建 revision 才能再次付费；Task 5 加成功 active 带历史 task 仍可再生成的回归。
- Task 5–6 默认编辑 source，同时允许普通及旧任务在同一编辑器明确切换直接 H3 编辑；复用 prompt_format=h3、本地六段/图片编号校验、原事务，H3直提不再改写、不要求文字key。两种格式各保留草稿和对应版本，提交只应用当前选中的一份。Task 5/6 回归覆盖普通有source任务直改H3、切格式不串词及真实RH graph只收到所选文本。
- Task 3 使用顺序规划→逐镜落盘→立即入现有四线程池，取代全片规划屏障；规划单写者，图片线程不能更新调度上下文，所有Future必须回收。用Event控制第2镜规划阻塞时第1镜draw已开始；规划失败/图片失败分别保留、取消/中断保留已落盘成果；不靠sleep或实时时长断言，不增加调用数。
- 未来真实生成授权之前预登记最多六镜对照：历史输出只作非受控基线，冻结新输入/模型/参考；逐镜记身份、姿态/视线、机位/固定物、持有者、袋口/袋底/结扣及裁切，通过标准为全部适用项正确、无身份换人/交接反转/倒袋/固定物无因移动。任一失败都不追加调用；不足全部通过不声称视觉连续性获证。历史与新模型/提示条件有差异时只报告观察，不作单一因素因果结论。离线功能通过与真实画面获证分开记录。
<!-- /autoplan-accepted:ceo -->

#### CEO Spec Review Loop — attempt 1

独立 Codex 已读两输入；8/10（Completeness9 PASS、Consistency9 PASS、Clarity8有问题、Scope9 PASS、Feasibility8有问题）。两项发现：S1“已有task锁定”误伤成功历史task（中）；S2两轮改写未计HTTP最多三次重试（高）。均按P1/P5采纳并补充精确契约；改动已应用，等待再次规格复核，不能把父审者修改计为子代理确认。

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| C5 | ceo | 未决与历史task分开 | Mechanical | P1/P5 | 成功记录保留旧task但必须可重生成 | task非空即锁定 |
| C6 | ceo | 按最多六次HTTP预算 | Mechanical | P1 | rewrite两轮×http三次，不能漏算 | 两次请求承诺 |

CEO Spec Review Loop attempt2：9/10，五维全PASS；两项均由同一独立Codex规格审者复核关闭。实际发起2轮，found=2、fixed=2、remaining=0；指标已写入 ~/.gstack/analytics/spec-review.jsonl。0H文档批准按autoplan机制暂作自动继续，最终用户确认仍待Phase4，未获实施授权。

### CEO 独立挑战与主要审查

Native INPUT `b3657b5f249bcb722f09dce1e1ca0ab36e4c345b3ce011c99566116bc94f27d5`，完整405行；high×1、medium×2、critical×0。三项均采纳：普通任务直接H3入口（C7，taste）、去掉整批文字屏障（C8，taste）、预登记视觉对照阈值（C9，mechanical）；无模型间一致性声明。外部Codex driver under_codex不可用，Claude按用户约定跳过；均不计通过。

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| C7 | ceo | 默认source，可明确切H3直改 | Taste | P1/P4 | 最终输入可控且后端已支持格式 | 普通任务H3只读 |
| C8 | ceo | 顺序规划逐镜落盘即并行draw | Taste | P3/P5 | 用现有池去除全批屏障 | 等最后一镜才开始第一图 |
| C9 | ceo | 真实图先登记基线和适用项全通过 | Mechanical | P1 | 防止离线约束冒充视觉收益 | 事后挑成功图 |

#### Sections 1–11

1. **Architecture**：新增staging纯函数边界合适，现有film协调计划与调用；segments继续唯一编辑事务所有者。新增耦合集中在有效状态筛选和提示词决策，禁止UI另算生成策略；逐镜送池避免文字末镜阻塞首图（C8）。
2. **Error & Rescue**：校验/空响应/拒绝/传输错误分别落在单镜或单尝试，失败不能冒充未开始。现有单项catch边界只有持久化具体异常并继续无关项才合理，新增辅助函数不得吞异常；详见下表。
3. **Security**：POST新增prompt/format/version必须服务端校验，用户文字通过textarea.value/textContent渲染。沿用本地工作台信任边界、现有主机/来源保护与文件白名单；无新增凭据/依赖/任意路径参数，外部分享部署不属于本计划。
4. **Data/UX**：双击、旧页、两标签页和后台发布必须以服务端锁与版本拒绝重复写；草稿是用户输入，不随轮询替换。历史task不等于未决task，C5补齐了本来会锁死正常重生成的语义。
5. **Quality**：普通dict、一个staging模块及现有JSON足够，不要工厂/数据库/状态机框架。图片和视频相似但媒体生命周期不同，只复用已有可靠函数，不强行抽通用卡片组件。
6. **Tests**：Task1结构输出、Task2最终图片词、Task3真实调度顺序、Task4视频与preflight、Task5RH提交边界、Task6真实DOM事件形成测试链。新增C5/C8以旧行为失败为证，真实画面另验；mock返回合格H3不证明真实改写质量。
7. **Performance**：主要慢路径是串行LLM、图片生成和RH视频；网络p99未知，不编造数值。C8移除无必要屏障；已有全文前文使大剧本输入总量近似平方增长，本轮不删除原文证据或做新摘要模型，实施以大fixture观察调用计数/无重复，超供应商上下文限制须在真实授权前暴露。
8. **Observability**：缺少规划和改写阶段证据会让用户误认为卡住，C3/C4已要求沿用run.log记录阶段、来源及命中。费用记录继续当前版本口径，不能称为历史累计；不新增监控栈或泄露prompt/密钥。
9. **Rollout**：本轮不部署，未来只在用户停任务后更新Python工作台，由用户重启；不能让新旧进程同时写同一输出。回退时保留任务/backup，先完成未决发布，不删付费任务或用旧代码盲读未完成v2事务。
10. **Trajectory**：当前提升的是可解释输入及手动修复能力，视觉一致性仍是待证目标，C9要求可接受结果先登记。可逆性4/5，文件格式为加法；付费外部调用和已发布媒体不是可自动撤销操作。
11. **Design**：流程是看片→展词→改草稿→明确费用→只重生成目标段→复核→手动拼接。loading/empty/error/success/partial均已纳入目标，具体聚焦、换格式和小屏规则交给下阶段审查；不另建导演面板。

#### What already exists

| 子问题 | 复用及证据 |
|---|---|
| 图片上下文/缓存 | frames.describe_composition与film.stage_frames（h3ref/frames.py:59；h3ref/film.py:207） |
| 角色参考和审图 | storyboard.plan/review_status（workbench/storyboard.py:17,110） |
| 单段编辑事务 | segments.prepare_regeneration/regenerate（workbench/segments.py:103,130） |
| 任务互斥/恢复 | Jobs.start_video_operation/resume（workbench/jobs.py:341,365） |
| 实际H3结构校验 | rewrite.sections/problems（h3ref/rewrite.py:99,112） |
| UI草稿与播放器 | results.js/frameDrafts；video-segments.js/renderVideoSegments |

#### NOT in scope / Dream state delta

不纳入：尾图生产、首镜锚图、视觉自动评分、重分段、换模型、新存储、多版本选择、跨刷新草稿、模型选择器和公共SDK；均不是被延后的已接受需求，因此不新增TODOS.md。保留现有九图容量边界、first/none接缝人工复核与手动拼接。理想中的实际画面状态验证尚未交付；本计划只约束输入并提供人工修复闭环。

#### Error & Rescue Registry / Failure Modes Registry

| Codepath | Trigger / exception | Rescue及用户结果 | Test owner | Logged |
|---|---|---|---|---|
| validate_composition | 缺字段/错类型/重复/布局变更，ValueError | 带字段和值，单镜失败，不出图 | Task1/3 | 单镜原因 |
| describe_composition | 空/拒绝/无效JSON，RuntimeError/ValueError | 不额外重试，后镜继续 | Task2/3 | 来源镜号 |
| staged rewrite | RewriteError/传输RuntimeError | 保留旧片与本次文字；无H3不交RH | Task5 | revision/phase |
| regenerate POST | InputError/过时版本/未决任务 | 本地拒绝，不写新pending/不发网络 | Task5 | 原门禁 |
| RH接受后失联 | task仍在/结果未知 | 恢复同task，未知提交锁定，不换词 | Task5/原恢复回归 | 原task与revision |
| download/tail/publish | OSError/媒体检查失败 | 保留旧片/原成片，复用候选后只完成发布 | Task5 | 发布阶段 |
| UI poll/submit | 旧快照/双击/切页 | 保留草稿和播放器，服务端拒绝冲突 | Task6 | 显式错误 |

所有rescue与测试均为实施义务，尚未实现；现有回归仅提供复用基线。没有采用“静默且无测试”的失败路径；实施前仍需工程阶段验证详细事务协议。

#### Diagrams

```text
film+cast+previous prefix -> describe -> validate -> atomic save -> draw pool -> frames
                    nil/empty/invalid -> item error -----> next planned shot
saved valid composition -> compile_prompt -> rewrite/cache -> RH -> candidate
UI draft -> POST(validate,lock,versions) -> pending/revision -> staged ---^
  invalid/conflict -> localized error, no write/no model
candidate -> validate media -> backup -> publish files+state -> applied
  failure -> old active intact / pending recoverable
```

```text
未开始 -> 有效计划 -> 图片待/运行/失败/成功 -> 批次审图
成功/明确终态失败 -> 手动新revision -> 改写 -> 提交 -> 轮询 -> 下载 -> 发布 -> 生效
未决task -> 仅同task恢复; unknown -> 锁定; terminal failed -> 不经resume自动重新付费
生效新片 -> 成片待拼接 -> 显式assemble -> 新成片
```

```text
未来更新: 停止旧进程 -> 备份任务/源码 -> 更新全套 -> 用户重启 -> 离线smoke -> UAT
回滚: 停写 -> 保留未决task及candidate -> 完成/恢复发布 -> 兼容版本回退（不删任务）
```

取消边界：已落盘文字/已接单task必须保留，线程池中尚未启动项不得新增提交；正在执行调用的取消不声称可撤销供应商计费。实际实现沿用既有进程停止能力，不新增取消按钮。并发回归以Event控制“第1图执行/第2镜规划”两种交错，shared state由同Job.lock保护，规划上下文只由主线程推进；工程阶段再核对锁与持久化。

#### CEO consensus / Completion Summary

| Dimension | Codex native | Claude | Outside Codex CLI | Consensus |
|---|---|---|---|---|
| Premises | 校验成立，视觉收益待证 | skipped | unavailable | N/A |
| Problem | 解决旧姿势回流/人工修复 | skipped | unavailable | N/A |
| Scope | 六任务复用 | skipped | unavailable | N/A |
| Alternatives | 源/H3与流水化取舍 | skipped | unavailable | N/A |
| Competitive | 缺市场证据，不作推断为事实 | skipped | unavailable | N/A |
| Trajectory | 输入可控，真实图待验 | skipped | unavailable | N/A |

| Completion field | Result |
|---|---|
| Mode/System audit/Step0 | SELECTIVE EXPANSION；旧时序、历史task和底层重试已修订；规格2轮PASS9 |
| Sections1–11 | 全部已审；Arch1、Errors7路径、Security无独立高危、Data/UX1、Quality0、Tests新增2、Perf1、Observ1、Deploy1、Future4/5、Design1；与9条审计决定交叉映射，不重复累计 |
| Registries | Error/rescue与Failure共用7行；设计义务已指定rescue/test/user/log，无静默critical gap |
| Scope | 9项决定（7 mechanical、2 taste）；0 user challenge；不删用户需求；0新TODO |
| Artifacts | 原计划修订、CEO归档及指标已保存；任务映射到既有Task1–6 |
| Voices | native3项已采纳；外部unavailable、Claude skipped、所有共识N/A |
| Lake score | N/A（无单独带覆盖分数问题）；3类图；未发现现有ASCII图需要本轮改动 |
| Unresolved | 2 taste留最终gate；真实视觉及实施不在当前授权 |

REALITY: premise-table pass unverified=0；每项输入事实均有本地证据 h3ref/frames.py:59
REALITY: already-done pass 当前分支已有单段生成而无视频改词；未fetch，远端新鲜度未证 workbench/segments.py:50
REALITY: surface-check pass 原POST及JSON字段路径已核对；新字段是计划不冒充已实现 workbench/server.py:251
REALITY: binding-decisions pass 保留单首帧和无Claude、仅文档授权，用户本轮明示 tasks/lessons.md:3
REALITY: numbers pass 四线程/三次HTTP/两轮改写均有代码证据；六任务为计划内计数 h3ref/film.py:245
REALITY: deferred-asks pass 无关联issue和新延后需求 docs/plans/2026-11-09-single-frame-staging-implementation.md:16
REALITY: pr-count pass 默认单PR，当前不创建 .github/workflows/test.yml:1

PLAN_REALITY: phase=ceo rows=7/7 missing=none unverified=0 verdict=complete（实际checker输出，详见ceo-review.md）。远端fetch未运行，already-done只覆盖本地基线；不声称远端最新。Approval readiness: PASS（C1–C9由autoplan六原则决定，C7/C8为待用户最终确认的taste，无实施授权）。

### Design Step0

初评7/10：已有草稿和单段范围，但缺格式切换的版本归属、小屏/键盘/焦点与失败文案层次。未找到DESIGN.md，不新建全站设计系统，复用style.css现有--ink/--muted/--accent/--surface/--border、segment-card、Plyr、details与分页。方法论导出1–1603已读；范围为现有结果页视频Tab。用户只授权文档且付费生成需另问，跳过设计图生成与serve，不启动设计二进制或服务，以计划内ASCII说明现有控件关系；未生成/批准mockup。原生Codex复核继续，外部driver不匹配、Claude按约定跳过。

### Design 7-pass review（原生Codex完成）

Native INPUT `eacc12edb0c8177418878264e7f79dcd1932033132f992f27cb9572fcf23ed39`，完整408行；High3（词来源/编号/恢复动作）、Medium1（双格式状态）。父审者另发现草稿基线被轮询更新和未写小屏/键盘规格两项。全部为本轮既定编辑行为的结构补全，逐项按P1/P5自动采纳，不新增付费调用或页面；新增D1–D6共6条Mechanical决定，Taste沿用CEO两项。

| Pass | Before → after | Analysis / decision |
|---|---|---|
| 1 信息层级 | 7→9 | 当前视频、失败尝试和草稿必须显式区分（D1）。H3编辑补只读实际编号清单（D2），不让用户猜图片位。 |
| 2 状态覆盖 | 7→9 | 现有全局resume不能与本段新付费按钮语义打架（D3）。完成下表，卡片内呈现错误和下一步。 |
| 3 用户旅程 | 8→9 | 延续看片→修一段→复核→拼接；失败保留旧片和输入让用户敢修。D4双格式草稿与D5冲突处理消除输入丢失和旧页覆盖。 |
| 4 AI slop | 9→9 | OPERATE工具界面，卡片就是“看片与修本段”的交互单位，无营销hero/装饰网格/动画。主视觉是视频本身；不新增装饰性设计。 |
| 5 系统一致性 | 7→9 | 无DESIGN.md，但style.css有--ink/--accent/--surface/--border与现有details、label、textarea。只在segment-editor局部复用颜色/间距并改善新控件可读性，不改全站字体/主题。 |
| 6 响应式与可访问性 | 4→9 | D6补375/760/1060及键盘/焦点要求；实际像素和读屏仍需实施后UAT，不能凭文档评分当渲染通过。 |
| 7 决策登记 | 不计分 | D1–D6均已写入实施义务；0新增未决设计取舍，2项CEO taste留最终gate。 |

整体初评7/10；六项rated pass最小值4→9，整体规格分数4→9。七个维度均已审，其中第7维是未决登记，按技能不伪造数值分数。10/10还需要实现后实际界面、读屏和用户使用反馈，本轮不作此声明。

```text
视频Tab（独立分页6段；沿用现有两列/窄屏一列）
  段号/时间/当前状态
  当前播放器与下载（失败仍可播旧版）
  本次尝试状态 + 错误 + 对应恢复动作
  <提示词 details>
    来源标签：当前视频 / 上次失败未应用 / 未提交草稿
    格式选择：源提示词 | 实际H3
    带label的textarea + 当前格式草稿提示
    <只读实际参考顺序>；本次仅应用所选格式
    本段生成按钮 + 对应费用说明
  更新成功 -> 成片待拼接 -> 页面原“重新拼接”入口
```

| Feature | Loading | Empty | Error | Success | Partial |
|---|---|---|---|---|---|
| prompt editor | 文字已载入，操作运行中只读，旧片继续 | 未开始无可恢复词：等待初次流程 | 本段失败词标未应用；旧片及有效H3可查看 | 当前版本标签，匹配草稿才清 | 一格式已提交另一格式仍有草稿 |
| reference map | 只读本次顺序，不请求模型 | 无参考则空清单，不虚造图片1 | 缺文件标不可用；不绕过后端 | 与将上传顺序一致 | 九图裁减镜头列为未纳入 |
| regenerate | 明确改写/视频/发布阶段 | 首次终态失败有词可修，无词不可新提 | 未决仅恢复，unknown不新提，终态失败新收费 | 本段换新、成片待拼接 | 其它段和原成片仍旧版 |
| assembly | 原入口展示拼接中 | 缺段禁用及原因 | 旧成片可播 | 新成片与版本 | 本段成功但未拼接 |

| Step | User does | User feels | Supporting treatment |
|---|---|---|---|
| 1 | 找到第2段看片 | 知道改哪里 | 段号、范围和视频先于文字 |
| 2 | 展开词并切格式 | 可控但不担心丢词 | 明确来源、参考序号、独立草稿 |
| 3 | 点击本段生成 | 知道费用与范围 | 当前格式/本段和费用邻近按钮 |
| 4 | 遇到失败/冲突 | 仍能继续 | 旧片可播、文字保留、准确下一步 |
| 5 | 复核新片后手动拼接 | 确认控制权 | 新片/旧成片状态分开 |

5秒能找到目标片段与动作；5分钟能完成一次不丢词的修复；长期能从已保存版本追溯实际用词。后一项是可追溯性，不新增历史版本选择器。

#### Litmus scorecard

| Check | Primary/native evidence | Outside | Consensus |
|---|---|---|---|
| 产品明确 | YES，视频工作台结果Tab | unavailable | N/A |
| 视觉锚点 | YES，实际播放器 | unavailable | N/A |
| 标题可扫描 | YES，段号/状态/词来源 | unavailable | N/A |
| 一区一事 | YES，看片/本段修复/全片拼接 | unavailable | N/A |
| 卡片必要 | YES，每张卡即媒体和局部操作 | unavailable | N/A |
| 动效有用 | NO新增动效，原播放/状态足够 | unavailable | N/A |
| 去阴影仍成立 | YES，媒体/文字层级承担结构 | unavailable | N/A |

Hard rejection=0；不是重新设计全站。未进行图像/像素评审，litmus为计划和既有组件证据，原生未报告的细项是父审者判断，非双声共识。

#### Design accepted decisions / audit

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| D1 | design | 明确当前/失败/草稿来源 | Mechanical | P1/P5 | 旧片用词不能被失败词冒充 | 混用一个prompt标签 |
| D2 | design | H3只读实际参考编号清单 | Mechanical | P1/P4 | 编号校验不能证明指对素材 | 猜固定编号/编辑参考图 |
| D3 | design | 按尝试状态区分恢复与新付费 | Mechanical | P1 | 不让全局resume暗中新提 | 失败统一重试按钮 |
| D4 | design | 每段每格式独立草稿和基线 | Mechanical | P1/P5 | Map<number,string>不能满足双格式 | 切换即改写/覆盖 |
| D5 | design | 草稿绑定开始编辑时版本 | Mechanical | P1 | 点击读取最新version会掩盖冲突 | 静默rebase |
| D6 | design | 局部小屏/键盘/读屏/焦点规格 | Mechanical | P1/P5 | 编辑器必须可用且不打断输入 | 只写“响应式” |

<!-- autoplan-accepted:design -->
- Task 6 固定层级为段号/当前视频状态→播放器→本次尝试状态及错误→懒展开提示词；编辑器标识“当前视频使用”“上次失败尚未应用”“未提交草稿”，新尝试运行时注明仍播放旧版。当前生效H3可查看；首次失败无片也显示失败词，不能冒充有成功版本。Task 5/6 加旧片存在、首次失败无片、恢复发布成功的来源显示断言。
- Task 5 segments.view 增只读 reference_images 清单，按将用于本次提交的 film.refs/_picture_paths 实际顺序返回 number、kind、角色/全局镜号、available，包含省略总表及九图裁减提示，不传任意可编辑路径、不新增参考图编辑/上传入口。active历史引用若未保存不能声称可重建，标历史顺序未知并将新清单标“本次重生成参考”。Task 5/6 以无总表、九图裁减和旧H3任务验证清单与捕获的实际上传一一对应。
- Task 5/6 共用尝试动作判断：当前未决task只恢复原任务；submission_unknown解释接单结果未确认并禁止新生成，只保留既有安全恢复路径；明确终态失败显示“用当前提示词再次生成”及新费用说明，resume不得重新付费；成功版本保留普通重生成。卡片内呈现原因和下一步，全局resume与本段按钮不能给出互相矛盾的可用动作。所有可见文字与字段错误使用中英文JSON。
- Task 6 将 segmentDrafts:Map<number,string> 细化为 Map<number,{selectedFormat:'source'|'h3',drafts:Partial<Record<'source'|'h3',{text:string,basePromptVersion:string}>>}>，每段选择与两份草稿跨轮询/Tab/分页/语言保留、换job清空；切格式只切文本不执行转换。有另一格式草稿时明确本次只应用所选格式；成功只清匹配提交且其后未继续修改的那份，不清另一份。测试H3输入→source输入→切页→切语言→提交所见格式及对应payload。
- Task 6 首次改词时绑定当时服务端prompt_version与clip version，轮询不得自动更新草稿基线；POST用该基线，冲突保留文字并展示当前版本供复核。用户明确“以当前版本使用此草稿”后才更新基线并再次允许提交，此动作不生成、不改写；无静默覆盖/自动合并。Task 5/6 测两标签页或受控后台发布造成版本变化后原草稿请求被拒绝、费用调用数为零。
- Task 6 沿用760px断点：宽屏保持原两列片段卡、编辑器在卡内全宽；≤760单列、格式选择和按钮换行，不横向滚动，视频contain保比例；375/760/1060中英长词核验。新增编辑区文本16px、提示/错误对比≥4.5:1、操作触点≥44px、可见label和focus-visible；summary/格式选择/textarea/提交按DOM顺序Tab，箭头保留文本编辑行为，无全局快捷键抢占。轮询不替换已聚焦textarea，不改变selection/IME组合输入；状态变化仅一次aria-live polite，字段错误aria-describedby。Task 6 用DOM测试焦点/选区/组合输入和键盘，最终小屏/读屏由用户UAT验收，不启动服务代替用户检查。
<!-- /autoplan-accepted:design -->

Design Completion：所有六条决定已按autoplan采纳写入（未实现），整体规格4→9/10；0 deferred、0新TODO、0生成/批准mockup，保留CEO两项taste。NOT in scope沿用CEO，另不做字体重选/全站主题/新编辑页。What already exists：video-segments.js现有卡片与分页、results.js图片草稿、common.js本地播放器、style.css现有tokens。实际像素与UAT待实施，不把9分当渲染通过。

### DX Step0 — 调查先于评分

类型：本地Python CLI + Flask内部API +维护文档；不是公共SDK。Persona：已运行工作台、会Python/Node的本地维护者/贡献者，期望在已有虚拟环境里不配key即可复现一段编辑/恢复。DX POLISH来源autoplan override；目标与persona由同一自动规则采用，不将其描述为用户访谈结果。

Developer perspective（基于README/CONTRIBUTING的预测，不是真人实测）：我打开项目，想先证明改词真的送进目标段，而不是先申请key、生成整条片。我看到贡献说明有虚拟环境和离线测试，知道可以先运行既有unittest与Node用例。接着我想找到新视频编辑的POST例子；只有接口签名时，我还不知道两种格式怎么选、版本值从哪拿。我最怕把某次失败当成没接单，点恢复又付一笔钱，也怕更新代码后已有任务的词变了。我需要看到本段revision、实际H3和明确下一步，确认是在继续原任务。输入错了应指出哪个字段和值，告诉我改后重试；结果未知则明确停下核对。开发端fixture应展示真实状态与文件变化，而不是合成回包自己证明自己。若能在同一个测试中看见新词进入graph、其它段和原成片字节不变，我就理解了这项功能。我会保留任务备份，再请用户自行重启；不会为演示自动调用真实供应商。

| Tool | Start→result | Time / evidence | DX choice | Source |
|---|---|---|---|---|
| ComfyUI | 可用服务→提交prompt_id→history查结果 | 未实测；与本地离线fixture起点不同，不比较耗时 | 提交身份与结果查询分离 | [官方路由](https://github.com/Comfy-Org/docs/blob/main/development/comfyui-server/comms_routes.mdx) |
| InvokeAI | 已安装配置→改prompt→显式Invoke | 未实测；模型安装不在相同边界 | 可编辑扩写后文本再生成 | [官方Generate Panel](https://invoke-ai.github.io/InvokeAI-7/users-guide/image-generation/generate-panel/) |
| 本项目 | 已有Python/Node/FFmpeg与.venv→阅读说明→运行目标离线用例并理解graph/文件变化 | 当前未测；估计5–10分钟，目标≤5分钟；冷安装网络时间未知 | 复用unittest/DOM tests，无key无服务 | CONTRIBUTING.md:3；本计划Task5/6 |

TTHW定义为上述“已有环境”到理解首个有效离线改词结果，目标≤5分钟（estimated目标，尚未达到/实测）；全新克隆/装依赖另计，不宣称冷启动<5分钟。Magical moment采用现有Task5 fixture与Flask test_client：只改第2段、捕获RH graph新文字、保留其它字节；demo仅说明交互不证明模型效果。最小载体为现有测试与docs/workbench.md说明，不新增playground、托管服务、遥测或生产默认演示。

| Stage | Developer does / observed source | Friction | Disposition |
|---|---|---|---|
| Discover | README:59进入工作台说明 | 新编辑功能说明待Task6 | 原目标文档更新 |
| Install | CONTRIBUTING.md:5虚拟环境+requirements-web | 冷安装耗时未知 | 不改安装方式 |
| Hello world | unittest + Node离线目标用例 | 新用例命令未加入贡献指南 | 待本阶段决定 |
| Real usage | GET版本→POST单段→等快照 | API四字段与双格式示例欠缺 | 待本阶段决定 |
| Debug | InputError仅本地化字符串，server.py:69 | 缺稳定code/安全恢复指示 | 待本阶段决定 |
| Upgrade | 保留混合旧记录/未决task | 具体升级检查表需落文档 | 既有兼容承诺补齐 |

首次角色模拟（不是计时测量）：T+0打开README本地工作台；T+0:30按贡献说明找到离线命令；T+1核对新用例文件与CI；T+2理解source/h3版本及恢复；T+3看到graph/其它文件断言。后两步目前依赖计划中的文档与测试，不能冒充已经跑通的观察。

### DX 独立结果与八项评分

Native INPUT `ab03c4d56c2e2eef6f29fa291f28436da3afc9d9d4cbe300b1c7ef4c0a83ee16`，完整415行；Critical1、High3、Medium2。CAP4已证实：film.run分镜失败返回目录，h3ref.py film入口忽略而exit0；只在计划补CLI边界非零和回归，本轮无代码修复。CAP6校验修复指引、运行错误上下文、意外异常追踪，以及API稳定错误体/Task6基线冲突分别采纳为X1–X6；不扩成全仓库错误框架。

| Pass | Before→after | Evaluation / disposition |
|---|---|---|
| 1 Getting started | 7→8 | 复用虚拟环境/离线测试即可，无新依赖。补贡献指南目标命令与无付费边界，不改用户正在运行的服务；冷安装时间仍未知。 |
| 2 API/CLI/SDK | 7→8 | 原POST和旧{version}兼容，直接H3是已有format分支。X6统一正文与类型；X1修复新失败沿既有CLI假成功的信号，不新增命令/SDK。 |
| 3 Errors/debug | 4→8 | 已逐行检查InputError、server兜底及film入口；CAP4/CAP6均补明确义务与测试（X1–X5）。下表四类代表路径各项均为计划契约，实施后必须证明，不能记运行通过。 |
| 4 Docs | 7→9 | X6要求source/H3可复制GET→POST例子、预期响应及费用；docs/workbench.md纠正笼统失败续跑。文档与语言JSON随功能交付，不另开大文档工程。 |
| 5 Upgrade | 8→9 | 旧三字段、混合记录、未决task/unknown/发布中断与override均有保留策略。补人工停写备份/用户重启/旧任务只读检查表，不加自动迁移。 |
| 6 Environment | 8→9 | Python标准库+已有Flask/Pillow，CI Python版本与Windows现矩阵沿用，新增Node文件需接入。修正文档把4组语言和所有JS语法检查说清，未创建测试不运行。 |
| 7 Community | 7→7 | 复用LICENSE、CONTRIBUTING与GitHub issue。社区活跃程度/响应速度未测；本地功能无需新社区、SDK生态或“免费真实生成”承诺来冲分。 |
| 8 Measurement | 4→8 | TTHW仅目标，不伪造实测；后续按相同已有环境起点记录理解fixture所用时间，用户UAT反馈即可。无遥测/自动发布门禁/定期审计扩展。 |

Overall DX（算术平均、含Errors封顶后的分值）6.5→8.4/10；native较窄的五维评分保留在其原报告，表中八维是父审者按加载方法论补全。TTHW当前未实测（估计5–10分钟）→目标≤5分钟；不是冷安装承诺，竞争时间不具同边界、排名未证。Hall-of-Fame每个Pass1–8按对应小节已读，其行业倍率/统计未作为本项目收益事实。

#### Error path matrix (agent-ready-v1)

| Path | Class | what/fix | recovery | code/context | trace | redaction/channel |
|---|---|---|---|---|---|---|
| staging.gaze=7 | model-result | 镜号+字段+7+期望str；现有图片词手修 | 已有文字请求可能收费；不自动重规划 | STAGING_INVALID_STATE/schema2/model名 | 预期失败N/A | 局部错误+CLI非零；不泄露key |
| new POST stale prompt_version | usage | 基线过时，先复核当前版本 | 无副作用；不自动重提 | SEGMENT_PROMPT_CHANGED/field | N/A | HTTP400+problem+error兼容别名 |
| submitted task polling/download | dependency | 段号/revision/phase及安全下一步 | 原task恢复；side_effects=partial或unknown，禁止新提 | SEGMENT_ATTEMPT_FAILED/task_present | 预期远端失败N/A | 快照attempt_error；原HTTP操作未冒充同步失败 |
| prompt read/version OSError | unexpected | 哪个操作失败，核对文件/权限并保留记录 | 无新模型调用；已写程度明确 | SEGMENT_IO_ERROR/context+correlation | 脱敏紧凑trace到本地日志 | HTTP500+instance；不泄露凭据/全文 |

修订前例：CLI退出0但有frame_failures；修订后例（产品文字来自JSON）：`[STAGING_INVALID_STATE] s2-1 characters[0].gaze=7，期望非空字符串；本镜未提交图片，已有规划请求可能收费。请在分镜卡片修改生成提示词后仅重出此图；不会自动再次规划。` stderr与非零退出，不把错误贴进stdout后仍报成功。

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| X1 | dx | CLI部分失败最终非零 | Mechanical | P1 | 单镜继续不等于最终成功 | exit0假成功 |
| X2 | dx | 调度错误给可操作手工修复 | Mechanical | P1/P4 | 复用原图片编辑，不需调度编辑器 | 只报gaze=7 |
| X3 | dx | 尝试错误有phase/revision/副作用/下一步 | Mechanical | P1 | 防止恢复导致重复费用 | 只给一句error |
| X4 | dx | 新边界异常关联脱敏trace | Mechanical | P1/P5 | 能定位意外文件错误 | 全文日志/新日志服务 |
| X5 | dx | 本次POST加problem字段兼容error | Mechanical | P1/P4 | 机器不解析双语文案 | 全仓迁移错误框架 |
| X6 | dx | 基线类型归一与完整示例/贡献命令 | Mechanical | P1/P5 | 消除正文冲突和复制缺口 | 点击读最新version |

<!-- autoplan-baseline-edits:dx {"sourceSha256":"3f3ae67f729d50b67c699412b79ebf8595992a6a3f0661f512e61ca4d593e066","replacements":[{"oldText":"- `state` 新增 `segmentDrafts: Map<number, string>`、`expandedSegments: Set<number>`、`segmentSubmissions: Map<number, {prompt: string, prompt_format: string, prompt_version: string, version: string | null}>`；与 frame* 状态按相同生命周期维护，不混用编号/草稿。","newText":"- `state` 新增 `segmentDrafts: Map<number, {selectedFormat: 'source'|'h3', drafts: Partial<Record<'source'|'h3', {text: string, basePromptVersion: string, baseClipVersion: string|null}>>}>`、`expandedSegments: Set<number>`、`segmentSubmissions: Map<number, {prompt: string, prompt_format: string, prompt_version: string, version: string|null, revision: string|null}>`；按job清理，按段/格式保留草稿与基线。"},{"oldText":"- `renderVideoSegments(job)` 保留原入口，消费 Task 5 的新快照；新 POST payload 使用当前段实时 prompt_version。只在本次提交文本/格式与成功版本匹配时清理对应草稿，不清除其它段或失败项。","newText":"- `renderVideoSegments(job)` 保留入口并消费Task5快照。首次编辑绑定basePromptVersion/baseClipVersion，POST使用该基线，不在点击时取最新版本；冲突保留草稿，只在用户复核并明确采用当前版本后rebase。匹配成功revision、文本和格式且用户未再改时才清该格式草稿。"},{"oldText":"对 `workbench/static/` 自有 JS 及两个 JS 测试逐文件运行 `node --check`（排除 vendor）；用 `.venv/bin/python` 校验 h3ref/workbench/scripts 三组语言 JSON 的 key 与 format 占位符一致。任何 git 操作前先做以上检查；实施完成再跑完整测试：","newText":"对 `workbench/static/` 自有 JS 及所有 `tests/*.js` 逐文件运行 `node --check`（排除 vendor）；递归校验 `h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json`、`workbench/static/messages/{zh-CN,en}.json`、`scripts/messages/windows-{zh-CN,en}.json` 四组语言 JSON 的 key/format 占位符。只对存在的文件执行；新增测试尚未创建时不伪称运行通过。任何 git 操作前先完成构建等价检查。"}]} -->

<!-- autoplan-accepted:dx -->
- Task 3 的Files新增 h3ref.py 和其CLI回归所在tests/test_staging_flow.py（h3ref/messages双语错误随Task1/3扩展）。单镜失败继续、Future全回收、成果持久化后，公开film CLI因frame_failures以非零退出，在stderr列失败key、state路径、稳定code及安全修复动作；工作台的批次可修复状态保留，不把任务改成首错即停。真实CLI入口离线回归证明退出非零、其它镜继续、无额外submit。Task3 Step6将此既有信号bug单独fix(cli)提交，回归单独test(fix)，不混入feat；所有提交仅限未来获实施授权后。
- Tasks1/3 校验和文字响应错误给字段路径、脱敏截断实际值、期望类型/约束、镜号、schema版本、模型配置名和记录位置，并说明该规划请求可能已收费。复用现有无网络图片prompt回退，使失败卡片可手工编辑后仅重出图片，不自动再调文字、不新增调度编辑器。无可恢复输入/素材时明确缺失项与人工动作；回归缺gaze、错类型、同scene布局变更、空/拒绝/无效JSON后可以安全手修，不称真实图正确。
- Task5 快照保留error字符串，增加attempt_error可选结构：code、phase、revision、prompt_format、task_present、side_effects(none|partial|complete|unknown)、recovery(action/next_step/retry_bound/attempts_used)。本地无提交失败可修后重试并披露文字重付条件；已有task只恢复；unknown须人工核对；终态失败只由显式新revision再付费。既有重试上限不扩大，不把异步失败伪装成POST已同步失败；运行快照中的失败状态同样有机器可读信息。
- Task5 仅对新增view/版本/读取/prepare边界的意外异常记录脱敏紧凑trace和correlation id，响应给instance及段号/revision的安全上下文；预期校验错误不打trace。沿用已有本地日志，禁止原始凭据、含key的URL、异常链密钥或完整剧本文字。用注入OSError断言可关联、无网络、无敏感值；不重构全站异常体系。
- Task5本次扩展regenerate POST错误返回application/problem+json，字段type（稳定urn:workbench:error:<code>）、title、status、detail、instance（意外失败）、code、field（适用时）、doc_anchor；保留error=detail兼容前端。校验/冲突沿现有HTTP400，意外500，missing job404；details/next_step双语JSON，code稳定，doc_anchor指docs/workbench.md实际锚点。依据RFC9457允许扩展字段，不创建新错误服务；测试状态/code/field/中英detail/脱敏，旧前端仍可读error。
- Task6正文和类型改为首次编辑同时绑定basePromptVersion/baseClipVersion；POST使用草稿基线，只经明确复核才rebase。Task6 Files新增CONTRIBUTING.md：与现有CI同步新增Node回归命令；docs/workbench.md写source/H3各一份GET取版本→带X-Workbench/Accept-Language的完整POST示例、预期快照/费用条件、未决/unknown/终态/发布中断动作表和升级前停任务备份、用户自行重启、旧任务不迁移说明。全部验证离线，不用文档命令实际付费或启动服务；本轮构建文案归一为四组locale和所有自有/测试JS。
<!-- /autoplan-accepted:dx -->

#### DX implementation checklist

- [ ] 在已有环境从阅读目标说明到理解首个fixture有效结果≤5分钟；冷安装/真人时间另测，不作已通过声明。
- [ ] source/H3完整示例、错误动作表和新回归命令随Task6进docs/workbench.md/CONTRIBUTING。
- [ ] 本次新错误code/fix/recovery/context/trace/redaction/channel按X1–X5离线验证；旧error兼容。
- [ ] 无新依赖、新CLI命令、公共SDK、遥测、社区渠道或费用默认变化；TypeScript类型、免费真实模型、codemod不适用此Python/原生JS局部增量。
- [ ] 混合旧记录/task/unknown/publish升级路径有fixture；用户自行重启，真实生成另授权。

DX consensus六项：getting started、API/CLI、errors、docs、upgrade、environment均只有Codex原生+父审，Claude skipped、外部Codex unavailable，所有Consensus=N/A。无模型间confirmed。NOT in scope：公共SDK/CLI改名/新启动器/社区/监控；What already exists：CONTRIBUTING离线命令、InputError、现有JSON、Jobs.redact、run.log与原生成事务。无新TODOS.md项目；剩余未实测TTHW和真实环境标为验收依赖，不阻止当前文档交付。

### Eng Step0 Scope Challenge

已核对实际frames/film/staging接入点、segments完整暂存发布、Jobs锁/启动/恢复、server原POST、inputs preflight、rewrite与HTTP重试、video-segments/results/style及现有恢复/JS测试。staging.py和新测试尚不存在，均为提议不记成运行证据。方法论导出1–2143已读，跳过父技能已处理的外部/发布/前言；模式FULL_REVIEW，沿用CEO/Design/DX全部修订，约30个文件（估计，含测试/语言/文档），仅1新业务模块、0新基础设施。

复杂度选择：原安排=staging纯函数+film编排+segments事务+现有UI；更小安排=把校验/两种渲染塞进frames.py，减少1个模块却使film消费依赖带网络代码并分散数据契约。按P3/P5自动保留原安排，不删功能；待决修复由本阶段独立证据逐项决定。六任务依赖链重叠film/segments，用户此前选择主会话顺序实施，维持顺序，不另建并行worktree。

[Layer1] 使用标准ThreadPoolExecutor、Job.RLock、原子替换和unittest/mock；官方Python/Flask文档已核验，不使用新版本特有buffersize。新引入调度模块不是通用状态平台；另一结构将validate/render直接放frames，接口深度较浅且跨模块知识外泄，拒绝。远端freshness未fetch；本地当前分支已提供单段重生成、原任务保护和两个JS测试，未提供此次编辑器及v2。

当前需要重点核对：saved_compositions只比prompt能否覆盖来源/图片替换，stage_prompts如何冻结旧task与override，preflight必须收到新词后再决定key，clip/tail/zh/h3/state多文件发布的中断顺序，以及prompt_version与参考图版本的边界。此处是问题列表，不预判独立审者结论或擅自修改业务。

### Eng review — architecture / quality / tests / performance

Native Codex已读完整422行盲输入，INPUT=`0190ccac39838a798d13e3115e7d27e79dcbd86a4598d549ae81a325c89718a9`，返回4项（High×3、Medium×1）；父审另补3项。Claude按用户要求跳过，外部Codex因host不匹配不可用，未启动外部进程。以下是计划修订，既有缺陷尚未改代码或验证修复。

#### Section 1 — Architecture

```text
film input + immutable uploaded refs
          │
          ▼
frames.describe ──same paid request──► staging.validate/render (NEW, pure)
          │ sequential validated save            │
          ├──► existing 4-worker draw pool        ├──► image start prompt
          └──► earlier-only planned context      └──► video action compile
                    │
          state + valid composition sources
                    │
      ┌─────────────┴────────────────────┐
      ▼                                  ▼
film.stage_prompts / inputs.preflight   segments.view + reference mapping
      │ same resolver/cache predicate       │ zero network, one read context
      ▼                                     ▼
rewrite (source only) ◄── per-format draft / baseline versions
      │ save before submit
      ▼
staged Job ──known task poll / unknown lock──► RH graph
      │ candidate clip + tail + prompts + override
      ▼
backup complete → publishing → root replacements → applied=true (commit)
      │                                         │
      ├── before commit: serve previous bundle   └── after commit: serve new bundle
      └── crash: resume publication only; versioned URL never aliases other bytes
```

边界：新增业务模块只有staging；不引入DB、队列、状态服务或新付费调用。已有Job RLock只保护当前进程的状态写，Jobs.lock不能使独立worker的多文件替换变原子；故E3要求明确读屏障而非再加一把线程锁。单段生成与初次pipeline共同在film.gen_segment守住终态失败门禁；所有编辑仍走原POST，避免只修UI绕不过CLI恢复。

安全：继续X-Workbench、现有输入/路径白名单与输出目录约束；客户端不能传reference路径、磁盘路径或任意revision目录。服务端reference mapping只用实际film.refs/_picture_paths顺序，H3编号必须在该数组内。prompt_version/source_version纳入参考路径及既有文件版本元数据；不哈希整个视频、不记录密钥。并发旧草稿提交先拒绝再做任何收费操作。新边界异常脱敏，未决提交不给“重试生成”动作。无新依赖；升级只增可选字段，不扫描旧任务补模型输出。

#### Section 2 — Code Quality / decisions

| ID | Evidence / problem | Type | Decision / principle | Alternative rejected |
|---|---|---|---|---|
| E1 | storyboard.run→film.invalidate_segments在film.py:392重建记录，丢override | Mechanical High | 保留提示词所有权与来源元数据，清生成字段，P1/P4 | 仅测stage_prompts而漏真实调用链 |
| E2 | film.py:318–320 poll失败清task；pipeline恢复会重新submit | Mechanical High | 初次与手改共用终态失败门禁，显式新revision才再付，P1 | 只挡segments的手改恢复 |
| E3 | segments.py:169–175顺序替换；server.py:267忽略v，读取混版 | Mechanical High | 既有applied作唯一可见提交点，读旧备份/新整组；版本URL校验，P1/P5 | 声称多个replace天然原子；新增DB |
| E4 | rewrite.py:111–113只拒绝大于n，Picture0漏过 | Mechanical Medium | 本地验证1≤编号≤实际参考数，P1 | 付费模型替用户修编号 |
| E5 | 同prompt换参考图仍满足composition_prompt比较 | Mechanical High | 同时核对生成时参考来源，P1 | 仅按文字相等信任旧物理状态 |
| E6 | 自动/override/已有task的解析与source↔H3配对有多入口 | Mechanical High | 单一无网络解析规则+配对来源标记，冻结既有任务，P1/P5 | preflight、view、stage各猜一次 |
| E7 | 每次轮询按段重读全state/重复编译可能变平方复杂度 | Mechanical Medium | 单次快照共享读取和编译结果，计数回归+离线基准，P1 | 先加持久缓存/新服务 |

命名沿用现有API；新增函数名说明实际解析/版本语义，不造manager/factory。DRY只抽取同一来源解析与一次快照上下文，不能为几十种未来任务设计通用事务框架。原有无关重复/风格不清理；现有两个旧JS套件继续作为回归基线。E1/E2/E3/E4属于既有bug，未来按fix(...)和test(fix):分开提交；本轮只写计划。其它新边界测试与所属feature分开回归提交。无未决的技术选项；CEO两项taste仍待最终计划确认。

<!-- autoplan-accepted:eng -->
- E1 / Tasks4–5：Files明确包含film.invalidate_segments、storyboard.run及tests/test_review_flow.py。分镜换图后失效视频时保留prompt_override及原source_version/source-H3配对来源；只清旧生成task/费用/检查等运行态，不把override重绑定到新来源。显式redo的其它既有行为不扩展。用真实Flask编辑视频→成功发布→换分镜→重新确认→pipeline继续的离线调用链，捕获实际RH graph仍用手改词、source_changed=true、其它段不变，不能只孤立测stage_prompts。
- E2 / Task5：Files包含film.gen_segment与tests/test_film_completion.py、tests/test_submission_retry.py。初次pipeline和手改单段的远端明确终态失败都持久标记terminal_failed；全局resume只报告人工动作，不清标记再提交。只有用户显式单段新revision才允许新费用。明确拒单且证实未接单仍沿原有重试上限和恢复策略，不能混同终态已收费任务；已有task传输/轮询故障只查原task，unknown仍锁定。离线证明poll终态失败→全局resume submit=0→显式手动新revision submit=1，并保留605/421/415与未知提交旧回归。
- E3 / Task5：复用segment-edit.json的applied作为唯一“新版可见”提交点。先验证候选整组，再完成旧clip/tail/本段zh/h3文件与previous记录的备份及版本映射，最后才设publishing；发布中逐项替换完成并保存active后，原子写applied=true。Jobs.snapshot在一次请求开始读取同一个pending/read context，segments.view、artifacts白名单、提示词下载和媒体路由全部据此解析：提交前返回previous整组，提交后返回new整组。媒体URL的既有v必须匹配所选版本；旧v仍可解析时只返回备份，无法解析则明确拒绝，绝不静默返回新字节；不承诺无限历史选择器、不暴露备份任意路径。工作台assemble和CLI pipeline在未提交publishing时不得消费混版根文件，先恢复发布或给安全指引，不进行模型调用。注入clip/tail/zh/h3/active/applied各步OSError及模拟重启，真实GET与Range请求断言同一版本视频/提示词一致、旧片保留、resume仅发布且rewrite/upload/submit为0。只有applied后才能报成功/清匹配草稿/标待拼接。
- E4 / Task5：Files增加h3ref/rewrite.py及其现有校验测试或tests/test_segment_prompts.py；直接H3的每个Picture引用逐项验证1≤N≤len(actual_reference_images)，在写pending/改写/上传前拒绝Picture0与上界+1，保持六段次序、Shot约束。测试0、上界+1、合法1/上界和无参考图分支，非法请求没有磁盘状态和费用副作用；错误走双语JSON。
- E5 / Tasks3–4：自动v2记录除composition_prompt外保存生成时实际有序参考来源标记（角色/场景表/该镜输入路径及既有版本元数据）；有效composition必须同时匹配当前prompt和该来源。手动换角色参考成功但文字不变也使旧composition失效，历史composition保留但不注入known_scenes/视频。优先复用现有character_reference_paths/参考版本，不新建素材库或哈希整视频；缺来源标记的旧记录保守回退原文字且不规划。缓存恢复、工作台view、preflight和stage_prompts使用同一判定，测试同词异参考/重排/文件版本变化及读取零网络。
- E6 / Tasks4–5：既有可恢复task、unknown、已完成片段的提示词/参考先冻结，不因v2编译升级或查看/preflight覆盖；显式失效后按E1保留override。source→H3复用必须有准确配对来源，直接手改H3发布后不得继续将旧zh标成当前视频源词；旧zh仅留previous追溯，默认进入H3，source为空，用户明确选source则以当前自动编译作为“新草稿”并重新改写，不能复用不配对H3。纯本地统一解析函数返回生效、尝试、自动来源和配对标记，供view/preflight/stage_prompts复用；无数据库和模型反推。测试新旧任务、override、source/H3往返、未变词但参考变、缺key恢复、已完成输出不改字节。
- E7 / Tasks5–6：每次snapshot只读取一份state/pending并共享给所有片段，自动prompt/source_version每段最多编译一次；reference元数据在本次请求复用，新增日志有stage/cache/source/revision且脱敏。100段/1000镜的离线合成夹具做调用次数回归，state/pending读取不随段数重复、compile次数≤段数、模型/上传/提交均0；不对平台抖动写绝对毫秒测试。未来实施前后同机同Python/Node、相同夹具与暖/冷条件交错测10次snapshot，记录中位/p95/输出字节/基线commit；设计目标暖快照p95≤500ms且不超过基线2倍，二者皆为待测预算而非结果。若超标先消除重复读取/编译，再提证据评估，不擅加持久缓存或依赖。
<!-- /autoplan-accepted:eng -->

#### Section 3 — Test Review（未实施、未运行下列新回归）

已读tests/test_frame_batch.py、test_segment_recovery.py、test_film_completion.py、test_review_flow.py、test_results.js及相关实际调用点。现有恢复测试覆盖备份中断、丢响应、拒单等，但不等于覆盖此次多文件读屏障；当前JS是轻量DOM/vm测试，无真实读屏/小屏结论。用现有真实边界mock网络，不mock待测resolver/状态机自身。不删除永久bug回归；临时探针若产生则测试完清理。

```text
schema + rendering ─► ordered planner ─► reference-valid cache ─► prompt resolver
     │                    │                     │                      │
 invalid path/value   A saved before B       manual refs/prompt     preflight keys
 scene invariants     future forbidden      old/task/unknown       source/H3 pairing
     └────────────────────┴─────────────────────┴──────────────────────┘
                              ▼
GET zero network → draft per-format/base versions → POST reject or prepare
                              │
                    source rewrite save / direct H3 validate
                              ▼
               accepted / rejected / unknown / terminal-failed
                              │
                   poll/download/check → publish commit
                              ▼
                UI matched success → stale film → manual assemble
```

| New path / branch | Existing coverage / gap | Required test and assertion |
|---|---|---|
| v2完整结构、未知/缺字段、错类型/空值 | 无staging模块 | test_staging结构单测；字段路径/实际值/期望/镜号可操作；bool不冒充编号 |
| world_frame/fixed_objects同场固定、反打、换场返回 | 旧三字段无此契约 | test_staging同场篡改拒绝，新场独立，返回复用且不偷换人物持物 |
| identity/pose/gaze/hand/holder/bag/world-vs-screen | 原look存在姿态污染 | test_frame_staging捕获真实draw prompt；稳定身份与当前姿态分离，起点无动作结果 |
| start/action/expected_end分离，裁切不倒置袋底 | 旧prompt文本覆盖不足 | test_staging逐项语义断言，不只snapshot全文；无末帧路径/其它镜图引用 |
| 单次describe扩字段、坏JSON/拒绝/空响应 | 原三字段夹具 | test_frame_staging mock原HTTP边界，数调用与错误；不补第二模型请求 |
| 顺序A→B→C、仅前文可信、失败跳过 | 原并行draw不测文字时序 | test_staging_flow用Event证明A持久化后B可用、未来记录不入context；无sleep |
| A图可在B文字阻塞时开始、四线程上限 | 尚无该流水线 | test_staging_flow Event检查重叠与最大并发，全部Future回收 |
| 首错/中错/多错、CLI最终退出 | workbench部分失败可修，CLI假成功 | test_staging_flow跑真实CLI入口，exit非零+stderr，后续镜落盘 |
| 旧记录/旧图/task/unknown/终态不额外规划 | test_frame_batch部分覆盖 | 扩展test_staging_flow和旧恢复套件，逐类assert描述/画图/submit=0或仅原task |
| 手改prompt失配、同词换参考/重排/版本变 | 原仅提示词来源 | test_staging_flow+test_frame_batch排除旧composition，保留追溯、无自动重规划 |
| mixed旧新/参考图九张上限 | 旧film refs已有选择 | test_staging_flow只对分配图片位规划；未分配镜沿原词；编号真实映射 |
| stage_prompts与preflight同词/override/冻结 | 旧test_film_completion不含v2 | test_staging_flow+test_film_completion同一resolver无误判key/改写 |
| GET当前/失败attempt/H3-only无视频 | 旧view仅视频状态 | Flask test_client test_segment_prompts，不混当前与失败词，零模型 |
| POST合法source改变→rewrite→RH graph | 旧POST只收version | test_segment_prompts捕获真实RH graph，先保存H3再submit，只目标段改变 |
| source未变/直接H3/仅拼接/原task恢复 | 旧恢复部分覆盖 | test_segment_prompts mock文字调用=0、无新key要求，source-H3配对准确 |
| 空/类型/format/版本/权限/缺素材/越界 | 旧版本门禁部分 | Flask请求拒绝在pending/子进程之前；无费用、原文件字节不变 |
| Picture0/N+1/六段/Shot约束 | rewrite只挡上界 | test_segment_prompts及rewrite现有测试，合法边界通过，非法本地拒绝 |
| rewrite成功后上传/提交/轮询/下载失败 | 旧单段恢复未覆盖source | test_segment_prompts注入每阶段，错误phase/revision/recovery正确、恢复不重改写 |
| 605/415/421明确未接单、accepted断线、unknown | test_submission_retry/test_segment_recovery已有 | 保留既有测试，扩展新编辑路径，区分安全重试/只poll/人工核对 |
| 初次pipeline远端终态失败→全局resume | 旧gen_segment会重提 | test_film_completion+真实Flask恢复链submit=0；显式new revision才submit=1 |
| 多文件发布每步中断、重启、旧URL/Range | 现有仅备份/成片中断不够 | test_segment_recovery对E3每步注入，GET词/视频成套，恢复无网络，v不别名 |
| 手改视频→换图→确认→继续 | 原isolated override测试不足 | test_review_flow真实链捕获RH prompt保留override、source_changed真 |
| per-format drafts、两标签页冲突、显式rebase | 旧JS未有视频编辑 | test_video_segment_prompts真实事件，payload绑首次基线，拒绝零费用，另一草稿不清 |
| polling/Tab/分页/语言/失败/再输入/换job | test_results已有图片保留 | JS断言播放器对象/焦点/选区/IME/草稿保留，匹配revision成功才清本格式 |
| error/problem稳定code、双语、trace脱敏 | 旧InputError不含扩展 | test_segment_prompts注入OSError/含合成密钥错误，响应/log可关联且不泄密 |
| 只换本段→assembly pending→手动拼接 | 原单段/拼接测试可复用 | Flask临时媒体fixture，其他段/分镜/旧film字节不变；join零模型 |
| 大快照读取/编译次数和无网络 | 无预算/bench | test_segment_prompts合成规模计数；另离线测量记录，不写时长断言 |
| 375/760/1060、中英长词/键盘/读屏 | DOM不证明布局/读屏 | Task6离线行为检查后用户UAT；不得把计划目标打成测试通过 |

LLM/eval边界：新增prompt编译先跑合成离线语义用例和全部既有回归；先记录模型/参考/样例/逐项阈值，再另询真实付费授权。未来最多6镜真实样例为质量验收，不能用占位视频、一次漂亮结果或不同配置的历史图宣称因果提升。测试计划另存gstack项目目录；本计划引用其路径，未开始实现不产生red/green伪证据。Tests to Retire=None，原三字段兼容、未知提交、明确拒单测试必须保留。

#### Section 4 — Performance / budgets

| Hot path | Work / risk | Budget / proof (planned, unmeasured) |
|---|---|---|
| 逐镜文字规划 | 原每新镜一次describe；上下文增长，token和关键路径可能变长 | 每新规划镜一次逻辑请求；HTTP底层重试按原预算记实际次数；首张图不等全片文字；记录token/耗时、无“免费提速”结论 |
| H3 source改写 | 2次逻辑改写×每次HTTP至多3次=至多6次物理调用 | 成功即缓存；不变source且配对、直接H3、resume、assemble皆零新增改写；不扩大原重试 |
| GET snapshot/source_version | 逐段重读全state或重复compile趋近O(N²)；大词增加payload | E7单次读取上下文、每段最多编译一次；设计目标暖p95≤500ms且≤同机基线2倍，10次交错样本，未实测 |
| 发布/恢复 | clip复制和备份磁盘开销，读取版本绑定 | 沿现有副本策略、不hash视频、不增生成；注入磁盘错误；空间不足保留previous并给恢复动作 |
| 前端轮询 | 频繁重建video/textarea造成输入/播放中断 | 沿现有轮询节奏、每页6段；更新最小节点，不新请求模型；DOM对象identity/IME回归 |

基准须写环境、Python/Node/commit、夹具100段/1000镜、冷热条件、重复次数、响应字节及中位/p95；这些是未来输入规格和预算，不是本轮观测。CLI并行画图仍4、RH消费级并发/容量重试沿既有配置，不在此调参。没有DB查询，N+1指本地文件读取而非凭空增加数据库。

#### Failure Modes Registry / critical gap disposition

| Failure | Before-plan gap | Planned detection/recovery | Critical gap after plan? |
|---|---|---|---|
| 无效调度/固定布局漂移 | 可能画出矛盾图 | 单镜明确字段错误+无网络手修原prompt | No；语义画质仍需未来真人验收 |
| 同词换参考仍信旧状态 | 错物理状态静默传播 | E5来源失配回退原词、无模型 | No；需新增回归 |
| 换图清掉手改视频词 | 用户内容静默丢失 | E1真实invalidate链保留并复核 | No；需修代码 |
| 初次生成失败恢复自动重付 | 非显式收费 | E2终态持久门禁+显式revision | No；需修代码 |
| 已接单丢响应/unknown | 重复收费风险 | 原task只poll/unknown人工核对 | No；原回归保留并扩展 |
| 发布中断混视频/词 | 旧播放器读新字节/词 | E3读屏障、v校验、仅发布恢复 | No；需逐步故障注入 |
| 过期草稿覆盖新词 | 双标签冲突 | 首改版本锁+明确rebase | No；需前后端测试 |
| 意外IO错误/密钥泄露 | 诊断困难 | X4局部trace脱敏+instance关联 | No；不审计全站日志 |
| 大任务快照超预算 | 无基准/潜在重复读取 | E7次数回归+同机对照bench | No；尚未测量，不承诺速度 |

NOT in scope：双帧/尾图、切换模型/供应商、素材库、调度编辑器、自动评审模型、无限历史版本UI、数据库/队列/新框架、全站异常迁移、并行实现、新付费调用。What already exists：film.refs/Job/compile_prompt、原ThreadPoolExecutor、staged Job及previous备份、write_json原子替换、Flask test client、unittest.mock/DOM vm、现有语言JSON与CI。没有accepted-but-deferred扩展，TODOS.md不新建；未来实施验收任务留六Tasks而非偷偷转成额外项目。

ENG DUAL VOICES — CONSENSUS TABLE：Architecture、Tests、Perf、Security、Errors、Deployment六维均为父审+native Codex；Claude skipped、外部Codex unavailable；Consensus全部N/A。Native提出4项均纳入E1–E4；父审E5–E7独立补充，不能计成外部confirmed。Completion：4 sections完成；测试路径逐项映射；性能预算/bench已命名但未运行；约30个文件估计、1新业务模块、0新基础设施；代码质量/功能/安全只能评估计划覆盖，未给运行PASS。当前实施状态仍NOT STARTED。

<!-- autoplan-baseline-edits:eng {"sourceSha256":"ab15665dc1ff83bd84520adc931228224a57f3f1ad338eb5f547b9fa28c22a0e","replacements":[{"oldText":"5. 原 frames key 仍为 `s{段号}-{段内镜号}`；缓存新增 `composition_prompt` 保存生成该 composition 时的最终图片提示词。供下游使用的 v2 记录必须具备非空字符串 composition_prompt，且满足 `record.prompt == record.composition_prompt`；两个字段都缺失不能算命中。","newText":"5. 原frames key仍为s{段号}-{段内镜号}；自动v2保存composition_prompt及composition_reference_version。下游必须同时确认prompt非空相等、有序参考来源版本相等；缺任一标记保守回退原词、不请求模型。参考来源按E5复用实际路径/顺序/既有文件版本。"},{"oldText":"- Produces `saved_compositions(records: dict) -> dict[str, dict]`；只返回 version=2、composition_prompt 为非空字符串且 prompt 与其相等的记录，key 沿用现有 frame key。legacy、未知版本、缺来源、手动失配记录不注入新上下文。","newText":"- Produces `saved_compositions(records: dict, *, reference_versions: dict[str, str]) -> dict[str, dict]`；只接受v2、非空匹配的composition_prompt与composition_reference_version。调用者用实际有序参考路径/版本构造本次reference_versions，规划、view、preflight、stage_prompts复用此判定；缺来源则排除，不额外规划。"},{"oldText":"- `stage_prompts()` 和 workbench preflight 都调用 `saved_compositions(state['frames'])`，并以同样的 compositions 调用 compile_prompt。","newText":"- stage_prompts与workbench preflight共享一次读取和实际reference_versions，调用saved_compositions后以相同compositions编译；先处理已有task/unknown/完成片段冻结及override，不用自动编译覆盖它们。"},{"oldText":"    records = {'s1-1': {'composition': composition,\n                         'composition_prompt': '原自动提示词', 'prompt': '手动新提示词'}}\n    self.assertEqual(film.saved_compositions(records), {})\n    records['s1-1']['prompt'] = '原自动提示词'\n    self.assertEqual(film.saved_compositions(records), {'s1-1': composition})\n    del records['s1-1']['prompt'], records['s1-1']['composition_prompt']\n    self.assertEqual(film.saved_compositions(records), {})","newText":"    records = {'s1-1': {'composition': composition,\n                         'composition_prompt': '原自动提示词', 'prompt': '手动新提示词',\n                         'composition_reference_version': 'refs-v1'}}\n    refs = {'s1-1': 'refs-v1'}\n    self.assertEqual(film.saved_compositions(records, reference_versions=refs), {})\n    records['s1-1']['prompt'] = '原自动提示词'\n    self.assertEqual(film.saved_compositions(records, reference_versions=refs), {'s1-1': composition})\n    self.assertEqual(film.saved_compositions(records, reference_versions={'s1-1': 'refs-v2'}), {})\n    del records['s1-1']['prompt'], records['s1-1']['composition_prompt']\n    self.assertEqual(film.saved_compositions(records, reference_versions=refs), {})"},{"oldText":"**Files:** Modify `workbench/segments.py:50-176`、`workbench/jobs.py:195-200,341-363,365-391`、`workbench/server.py:251-257`、`h3ref/film.py:259-277`、`workbench/inputs.py:174-191`、`workbench/messages/zh-CN.json`、`workbench/messages/en.json`；Create `tests/test_segment_prompts.py`；Extend `tests/test_segment_recovery.py`。","newText":"**Files:** Modify `workbench/segments.py:50-176`、`workbench/jobs.py:95-182,195-200,341-391`、`workbench/server.py:251-276`、`h3ref/film.py:259-330,386-395`、`h3ref/rewrite.py:107-120`、`workbench/storyboard.py:230-248`、`workbench/inputs.py:174-191`、`workbench/messages/{zh-CN,en}.json`；Create `tests/test_segment_prompts.py`；Extend `tests/test_segment_recovery.py`、`tests/test_review_flow.py`、`tests/test_film_completion.py`、`tests/test_submission_retry.py`。"},{"oldText":"- [ ] **Step 3: 扩展既有事务实现契约。** 校验/预检完成后再写 pending 和启动子进程；先缓存新 H3 再提交视频，沿用 staged Job 与原任务号恢复。只有新片及尾帧验证通过才更新 active、prompt 文件和 override；失败保留旧片、旧词及草稿来源。补齐失败态的快照/POST 门禁，已有 task/未知提交锁定，终态失败不通过恢复按钮自动重付。保持其它段与分镜图不变，不自动拼接。普通自动段继续遵守 Task 4，新手改段以明确 override 为准。","newText":"- [ ] **Step 3: 扩展既有事务实现契约。** 校验/预检完成后才写pending/启动worker，先存新H3再提交，恢复复用staged与task。按E3先备份整组并建立版本映射，以applied原子写作为唯一可见提交点；此前所有读取返回previous，此后返回new。逐个replace不视为整组原子。API/snapshot/media/assembly统一读取规则，v不匹配不得返回别版字节。E1保留override、E2覆盖初次pipeline终态门禁、E4严格H3编号、E6配对来源；只影响本段，不自动拼接。"},{"oldText":"- [ ] **Step 6: 本地提交实现。** `feat(video): support prompt edits in segment regeneration`，只提交本任务实现及双语错误。","newText":"- [ ] **Step 6: 本地提交实现。** 新能力用feat(video)；既有缺陷E1/E2/E3/E4按根因单独fix(...)提交，不混入feat。每次git前构建，报告修复文件/行号及red/green；回归另以test(fix):提交。此处全是未来获实施授权后的动作。"}]} -->


工程测试计划：`/Users/qiaoyu/.gstack/projects/garycaogan-h3-ref-video/qiaoyu-feat-video-segment-review-eng-review-test-plan-20261109-123346.md`（完整路径/分支/路由/用例价值已写；未实施）。

REALITY: premise-table pass 本地输入前提逐项核对，unverified=0 h3ref/frames.py:109
REALITY: already-done pass 本地分支已有单段事务而未有本次编辑器；未fetch故远端新鲜度未知 workbench/segments.py:103
REALITY: surface-check finding 原POST只收version；新参数须扩展且旧调用兼容 workbench/server.py:251
REALITY: binding-decisions pass 当前仅审查，遵守现有执行方式与Claude禁用 docs/plans/2026-11-09-single-frame-staging-implementation.md:15
REALITY: numbers pass 任务与模块数由计划任务标题和Files去重计数；性能均预算未实测 docs/plans/2026-11-09-single-frame-staging-implementation.md:154
REALITY: deferred-asks pass 本轮双需求均保留；无linked issue遗留未接受要求 docs/plans/2026-11-09-single-frame-staging-implementation.md:138
REALITY: pr-count pass 无ship-policy则默认单PR，仅未来建议不创建 docs/plans/2026-11-09-single-frame-staging-implementation.md:423
REALITY: units pass 时间/尺寸/价格单位沿现有接口；无单位变更或阈值重调 h3ref/film.py:116
REALITY: perf-table finding 新路径无实测基线；已命名同机对照fixture与预算，未声称加速 docs/plans/2026-11-09-single-frame-staging-implementation.md:350

### /autoplan 最终审查（已获用户认可）

## /autoplan Review Complete

## URGENT, outside this plan

None.

### Plan Summary

已完成[实现计划](/Users/qiaoyu/projects/h3-ref-video/docs/plans/2026-11-09-single-frame-staging-implementation.md)的CEO、Design、DX、Eng审查，保留六项任务，补齐逐镜调度、source/H3手改与单段重生成，以及换图保留手改词、终态失败不自动再付费、发布一致性和草稿版本保护。

Python编译、8个JS语法与4组语言JSON校验通过；新回归只完成计划，尚未实施，真实画质仍须未来另授权验证。

本轮仅文档修订，未提交、推送、部署或付费；Claude按约束跳过，外部Codex不可用，跨模型共识N/A。

### Decisions Made: 28 total (26 mechanical, 2 taste choices, 0 user challenges)

机械决策逐项见原计划Review record中的C/D/X/E audit；用户于2026-11-09回复rec，认可同编辑器切source/H3、顺序规划后立即进原画图池两项推荐，以及当前六项计划（d1a/d2a/p1a）。最终行政审批p1不计技术决策。24条问题已接受到计划，未称代码已修；原生独立阶段审查共17条，另2条spec检查和5条父审补充。

### Review Scores / coverage

| Phase | Plan assessment | Native Codex | Outside / Claude | Consensus |
|---|---|---|---|---|
| CEO | spec第二轮9/10，五维PASS；范围保留 | completed，3项；spec另2项已补 | outside unavailable；Claude skipped | N/A |
| Design | 六项维度补齐，最小目标9/10；不是像素验收 | completed，4项；父审另2项 | outside unavailable；Claude skipped | N/A |
| DX | 6.5→8.4/10，TTHW真人未测、目标≤5min | completed，6项 | outside unavailable；Claude skipped | N/A |
| Eng | 四section、28路径测试矩阵及预算已覆盖；reality9/9 | completed，4项；父审另3项 | outside unavailable；Claude skipped | N/A |

实际host=Codex，source=in-host，unknown具体模型不推断；仅同模型独立复核，不算跨模型。外部命令未启动，Claude未调用/探测。远端未fetch，本地代码检查不代表远端状态新鲜。

CYCLE: actual 42m (estimate 46m) | ceo 14m | design 4m | dx 8m | eng 15m

### Cross-Phase Themes

- 缓存与实际来源一致：CEO计划状态边界、Design当前/失败/草稿标签、DX版本契约、Eng参考来源及source/H3配对共同要求。
- 恢复避免重复费用：CEO task语义、Design动作一致性、DX可操作错误、Eng初次pipeline终态门禁与发布恢复。
- 用户输入与旧成果保留：Design草稿基线、DX示例一致、Eng真实换图链与多文件读取屏障。
- 离线流程证据与真实画质区分：四阶段均要求另行授权、先登记验收标准。

### Deferred to TODOS.md

None. 未接受的尾图/视觉模型/新平台等在NOT in scope；后续六任务和真实验收均有归属，不制造额外TODOS.md。

### Implementation Tasks (aggregated across phases)

- [ ] **T3 (P1, human: 0.5–2天（估计） / CC: 1–4小时（编码agent估计，非Claude调用）) — Task 3** — 顺序文字规划、缓存恢复与并行首帧
  - Surfaced by: ceo-review — C1–C9 / specs review; full six-task scope
  - Files: h3ref.py, h3ref/film.py, tests/test_frame_batch.py, tests/test_frame_moderation.py, tests/test_staging_flow.py, tests/test_submission_retry.py
- [ ] **T4 (P1, human: 0.5–2天（估计） / CC: 1–4小时（编码agent估计，非Claude调用）) — Task 4** — 视频提示词与工作台使用相同有效状态
  - Surfaced by: ceo-review — C1–C9 / specs review; full six-task scope
  - Files: README.md, docs/workbench.md, h3ref/film.py, h3ref/rewrite.py, tests/test_frame_staging.py, tests/test_staging_flow.py, workbench/inputs.py, workbench/storyboard.py
- [ ] **T5 (P1, human: 0.5–2天（估计） / CC: 1–4小时（编码agent估计，非Claude调用）) — Task 5** — 视频提示词编辑与单段重生成数据流
  - Surfaced by: ceo-review — C1–C9 / specs review; full six-task scope
  - Files: h3ref/film.py, h3ref/rewrite.py, tests/test_film_completion.py, tests/test_review_flow.py, tests/test_segment_prompts.py, tests/test_segment_recovery.py, tests/test_submission_retry.py, workbench/inputs.py, workbench/jobs.py, workbench/messages/en.json, workbench/messages/zh-CN.json, workbench/segments.py, workbench/server.py, workbench/storyboard.py
- [ ] **T1 (P2, human: 0.5–2天（估计） / CC: 1–4小时（编码agent估计，非Claude调用）) — Task 1** — 调度数据边界与起点/动作输出
  - Surfaced by: ceo-review — C1–C9 / specs review; full six-task scope
  - Files: h3ref/messages/en.json, h3ref/messages/zh-CN.json, h3ref/staging.py, tests/fixtures/staging.json, tests/test_staging.py
- [ ] **T2 (P2, human: 0.5–2天（估计） / CC: 1–4小时（编码agent估计，非Claude调用）) — Task 2** — 逐镜角色文字与首帧请求
  - Surfaced by: ceo-review — C1–C9 / specs review; full six-task scope
  - Files: h3ref/frames.py, tests/test_frame_staging.py
- [ ] **T6 (P2, human: 0.5–2天（估计） / CC: 1–4小时（编码agent估计，非Claude调用）) — Task 6** — 与图片侧对齐的视频提示词交互和验收
  - Surfaced by: ceo-review — C1–C9 / specs review; full six-task scope
  - Files: .github/workflows/test.yml, CONTRIBUTING.md, docs/workbench.md, tests/test_results.js, tests/test_video_segment_prompts.js, workbench/static/messages/en.json, workbench/static/messages/zh-CN.json, workbench/static/results.js, workbench/static/style.css, workbench/static/video-segments.js


工时均为估计，不是已用时间；CC字段指未来编码agent估计，不表示调用Claude。六Tasks均NOT STARTED，聚合按branch/commit/latest run精确去重。

### Verification / remaining limits

- 已运行 .venv Python compileall、自有与现有测试JS共8文件node --check、4组locale递归key/类型/format占位符校验。
- Eng close packet完整读取；初次输出中间截断后，151–250和251–350补读齐，逐项语义核对。accepted/baseline helper保留原始恢复点及每阶段输入，最新implementation SHA256=17e86a05ea90e09e22d9ac9695770830fcafba6a13aab58756578437af112194。
- 无业务/测试实现、服务启动、真实生成或git提交；未伪称新测试red/green。实际UI、Windows真机、模型输出与未来性能bench未测。
- gate已由用户rec确认（gate_rev=1，2026-11-09T12:44:25+00:00）；仅认可计划，无编码、收费或发布授权。安装版行政plan项为p1。

最终范围核验：六Tasks/48未来步骤、四阶段accepted block及原始恢复点SHA保留；Python编译/8个JS/4组locale与文档空白检查通过；业务代码/测试/CI/README/CONTRIBUTING及index未改。既有lessons与3份研究文档保留。本轮交付状态=REVIEW COMPLETE / PLAN APPROVED / IMPLEMENTATION NOT STARTED。

### 用户认可记录（2026-11-09）

- 用户原始回复：`rec`；解析时间：2026-11-09T12:44:25+00:00；gate_rev=1，pending=[]、unparsed=[]。
- d1a：同一编辑器默认source，可明确切换实际H3；两种格式分别保留草稿。
- d2a：逐镜顺序规划、校验落盘后立即进入现有四线程画图池。
- p1a：认可当前六项实现计划。PLAN APPROVED；IMPLEMENTATION NOT STARTED。
- 此记录不增加业务编码、提交、推送、部署、服务启动或真实付费授权；不调用/探测Claude。
- owner-brief close已运行，unresolved=0；原各阶段记录中“待确认”是当时状态，以本认可记录为准。原实施正文及accepted要求保持逐字节不变，不重跑审查。
