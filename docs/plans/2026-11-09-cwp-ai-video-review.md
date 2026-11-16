# cwp-cwp/ai-video 参考价值评审

日期：2026-11-09。目标：判断该仓库对本项目分镜图连续性、首尾帧及视频续接的实际帮助。

## 结论

有参考价值，优先借鉴场面调度、生成前静态检查，以及 H3 导演台的前段尾部运动上下文。保留现有 Python 工作台，按问题逐项吸收；当前证据不足以证明其完整分镜生成比本项目更连贯，也不支持直接搬站替换。

本次为 Codex 主代理与两个只读研究子代理的源码核查，没有运行安装包、外部脚本或生成工作流，没有安装依赖、调用付费接口或修改正式流程。Context7 月额度耗尽，依据官方仓库源码和发布说明核查；没有调用 Claude。

## 核查范围与公开程度

- 主仓库：[cwp-cwp/ai-video](https://github.com/cwp-cwp/ai-video)，固定快照 `eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2`，提交时间 2026-11-07 UTC。
- 公开树包含 README、展示图、ComfyUI JSON、技能文档和 `drama.py`。没有 README 所述 Spring Boot / Vue 核心应用源码。分镜生成调用后端接口，无法检查其完整生成模板和实现。
- [Release](https://github.com/cwp-cwp/ai-video/releases) 提供 Windows 便携包；v1.1.3 的 `apache-tomcat-10.1.59.zip` 约477MB。本轮未下载或运行这个发布包，不能根据发布包存在推断完整源码已公开。
- README 声明 MIT，徽章指向的独立 LICENSE 文件在该快照中缺失。应准确表述为“README 有 MIT 声明，许可文档不完整”；模型与插件各有独立条款。另核对作者 Director fork 固定快照的 LICENSE，内容为 Apache-2.0。
- 免费下载软件不代表零成本出片：使用本地 ComfyUI 需要显卡与运行资源，调用云模型仍需按服务计费；本轮没有硬件与成本实测。

本地只读快照保存在 `.workbench/research/cwp-ai-video/`。

## 值得借鉴的部分

| 部分 | 源码证据 | 对本项目的作用与边界 |
| --- | --- | --- |
| 场面调度 | `cinematic-rules.md` 的 `staging.segments`：固定空间锚点，分别描述身体朝向、视线、距离及机位 | 针对锅、案板、人物相对位置漂移，比反复补长段自然语言更有针对性；仍需区分世界坐标与画面左右，不能照抄后就认为空间已锁定。 |
| 单镜动作与道具交接 | 每镜起点、单一动作、终点；交接明确接触者、方式和物件落点 | 可补强第25镜交袋的动作链。保留本项目更明确的 `ff_state/lf_state`，另外记录持有者、物体朝向、放置位置。 |
| Qwen 图片编辑工作流 | 第一图经过 VAE 编码进入采样初始 latent，多图进入编辑文本条件 | 支持“本镜首帧作编辑底图，角色/道具图作辅助参考”的接口设计。该示例 denoise=1，不能保证局部像素保持；也未证明优于现用 GPT Image。 |
| H3 多段运动上下文 | 前段尾部 latent / 视频多帧进入后段条件，采样后裁掉重复前缀 | 这是视频阶段真实的续接机制，可作为 RunningHub 自定义工作流候选。它与分镜图本身的连续性是不同阶段，不能用来替代首尾图审核。 |
| LTX 首尾帧工作流 | 两张图片接入带帧索引的 `LTXVImgToVideoInplaceKJ`，再进入视频采样 | 能区分“指定时间位置的首尾条件”与普通多图参考。需要另一套模型和插件，不能只替换本项目 H3 模型名。 |

官方证据：[调度规则](https://github.com/cwp-cwp/ai-video/blob/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/skills/reference/cinematic-rules.md#L155)、[动作与交接](https://github.com/cwp-cwp/ai-video/blob/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/skills/reference/cinematic-rules.md#L38)、[工作流目录](https://github.com/cwp-cwp/ai-video/tree/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/comfyui工作流)。

## H3 续接的具体实现

主仓库 `video_minimax_h3_r2v_导演台多段衔接.json` 中，三个 ReferenceToVideo 组节点进入 GroupsCombine，再接导演台。时间线设置 `continuityEnabled=true`、`continuityOverlapFrames=22`；首段不接前段，后两段启用 `continuityFromPrev`。

作者 [ComfyUI_MiniMaxH3_Director](https://github.com/cwp-cwp/ComfyUI_MiniMaxH3_Director) 固定快照 `44928604e8d2e28927b709005f6f6e4365e9ad3a` 的执行代码会把前段上下文传入生成：

1. 优先取前段 latent 的尾部；否则取视频末 N 帧重新 VAE 编码，写入 H3 时间条件。
2. 新段生成后裁去视频和音频的重复前缀，避免重叠内容播放两次。
3. 显式新首图 i2v 会跳过这类上下文；fl2v 开启运动上下文时，上下文会接管开头。因此不能假定人工指定首帧与上下文必然同时按原意生效。

证据：[上下文编码](https://github.com/cwp-cwp/ComfyUI_MiniMaxH3_Director/blob/44928604e8d2e28927b709005f6f6e4365e9ad3a/director/h3_motion_context.py#L568)、[重复前缀裁剪](https://github.com/cwp-cwp/ComfyUI_MiniMaxH3_Director/blob/44928604e8d2e28927b709005f6f6e4365e9ad3a/director/h3_motion_context.py#L686)、[模式选择](https://github.com/cwp-cwp/ComfyUI_MiniMaxH3_Director/blob/44928604e8d2e28927b709005f6f6e4365e9ad3a/director/executor_core.py#L834)。

局限：场景参考在示例中主要约束风格和氛围，没有真实三维布局投影；独立加载视频支路未进入三个组的有效输入链。连续运动可考虑启用，切机位或换场应明确断开/重建条件，不能所有镜头一律续接。RunningHub 需要部署兼容的 Director、Group、Combine、H3 核心节点及权重；标准黑盒模型 API 不能直接插入这里的 latent 修改逻辑。可行性、权限、性能及计费均需单独验证，本轮未验证线上部署。

## 不能当作现成解决方案的部分

### 静态检查没有自动看图

`drama.py` 的 audit、coverage、preflight、promptcheck 主要检查时长启发式、文字、资产绑定和字段。`preflight_check` 判断资产是否存在及图片 URL 是否非空，不读取图片内容；所以无法自动发现第26镜袋扣倒置、锅换位置或人物视线错误。技能文档要求逐镜和串联审阅，属于操作规程，不等同于已经实现自动视觉验收。

技能/API 脚本也没有实现“先审本镜首帧、再编辑本镜尾帧”的审批流程，未看到可直接移植的“上一尾帧生成下一首帧”算法。真正分镜生成位于未公开的后端。

证据：[生成接口](https://github.com/cwp-cwp/ai-video/blob/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/skills/SKILL.md#L703)、[preflight](https://github.com/cwp-cwp/ai-video/blob/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/skills/scripts/drama.py#L567)。

### 两处检查逻辑不宜照搬

- `usePrevVideoRef` 注释说明缺省为 true，但跨场检查使用 `s.get("usePrevVideoRef")` 判断。字段缺省时取到 None，跨场仍可能默认续接，却没有触发这条告警。全关检查与跨场检查的默认值处理不一致。
- 台词覆盖判断允许 `x in t`：一小段残句可能被算作完整台词已覆盖；全片任意位置匹配也不能保证说话角色、场次和顺序正确。高覆盖率不能证明故事连贯。

这两点来自静态阅读，未运行外部脚本复现；本轮不修外部仓库。

证据：[续接检查](https://github.com/cwp-cwp/ai-video/blob/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/skills/scripts/drama.py#L386)、[台词覆盖](https://github.com/cwp-cwp/ai-video/blob/eebd8b8a39f40a65e91c0a2e530e9a7147dcc5c2/skills/scripts/drama.py#L511)。

### 宫格与全景不等于连续性

宫格拆分工作流主要裁图，不会自动传递动作/道具状态；该拆分 JSON 也没有后续高清重绘。全景方案可以生成一张全景并从中扫视或取景，但不具备三维重建、深度遮挡和人物调度。它们适合做资产和场景参考，无法单独修复跨镜空间关系。

## 对本项目的建议顺序

1. **先补规划与审核**：沿用“首帧审核通过后编辑尾帧”，增加场景锚点、人物身体/视线、机位，以及道具持有者/方向/落点。单镜验收和相邻镜衔接验收分开，不把所有镜头机械串成同一机位。
2. **优先修描述歧义**：第26镜应把物体结构与构图分开，例如“结扣位于袋体顶部、提手朝上；袋体在人物胸腹前，下半部超出画面下边缘”。此前“结扣位于下沿”可能直接诱导倒置，继续堆参考图不能消除目标描述的歧义。
3. **再测视频续接**：复用已合格图片，对连续运动与新机位切镜各选少量样本，比较现用 H3 与 Director 上下文路线；记录动作/声音衔接、首帧服从性、人物/道具变化、实际费用和重复帧。另行确认 RunningHub 部署能力后再执行付费测试。
4. **暂不搬站或换图模型**：没有完整应用源码，迁移成本和效果收益都缺乏证据。Qwen 工作流可作为后续受控对照候选，本次不把换模型当作先决条件。

以上是后续建议，没有实施。正式业务代码、用户任务和已生成图片保持原状。
