# 连续分镜与首尾帧开源项目调研

调研日期：2026-11-08。目的：为“视频自动生成工作台”的相邻分镜不连贯问题寻找可借鉴实现。本文件是技术选型调研，不是已批准的实现计划。

## 范围和证据

阅读本地生成链路，并核查候选项目的官方 GitHub、模型卡和关键代码。没有部署候选、运行生成对比、调用付费接口或修改业务代码，因此下文的推荐是工程适配判断，不是效果排名。Context7 查询返回月度额度耗尽，改用官方文档与源码；远端 main 分支后续可能变化。

## 当前项目的直接缺口

- `h3ref/frames.py:59` 的构图整理只接收当前镜头、前文文字与全局场景，输出 scene/frame/orientation；描述目标是镜头开始的瞬间，没有单独规划结束状态，也不读取实际生成画面。
- `h3ref/film.py:224` 的图片参考是场景总表与当前角色图；各分镜没有传递实际画面状态。首镜参考此前已按用户要求移除，不应在此次调研中擅自恢复。
- `h3ref/film.py:93` 中默认 shot 模式不传上一段尾帧；first/none 模式会传前段尾帧，但这是段间参考，不等同于每个镜头都成对规划首尾帧。
- `workflows/h3-ref2va-turbo.json` 使用 `MiniMaxH3ReferenceToVideo`，分镜图作为 reference images 输入，没有设置各分镜的关键帧时间位置。在提示词中称图片为首帧，不等同于使用模型的关键帧条件接口。

以上能解释连续性容易丢失的机制，但尚未对照用户此次失败图逐项确认原因。角色像同一个人、场景像同一处、动作能接上、机位/视线不跳，是不同的验收维度。

## 优先候选

### 1. ViMax：首尾状态规划与机位参考组织

- 仓库：[HKUDS/ViMax](https://github.com/HKUDS/ViMax)，代码 MIT。
- [流水线](https://github.com/HKUDS/ViMax/blob/main/pipelines/script2video_pipeline.py)为镜头规划首帧、尾帧与运动描述；中/大变化镜头生成尾帧，并将两张图交给视频生成器。
- 同机位的后续图基于该机位的首个镜头首帧生成，首尾图可以并发；这不是一次联合生成两图，也不是逐镜传递上一镜尾帧。
- [机位生成器](https://github.com/HKUDS/ViMax/blob/main/agents/camera_image_generator.py)可通过父镜头首帧生成包含切镜的视频，再抽第二镜画面建立新机位参考；该步骤增加生成成本，且依赖切镜检测。
- 需要外部图像/视频 API。对本项目最值得借鉴的是分开记录起止状态、按机位组织参考与挑选候选图；不能把整套流水线当免费本地模型。

### 2. Qwen-Image-Edit 与多角度 LoRA：编辑画面而非逐图重新搭场景

- 基座仓库：[QwenLM/Qwen-Image](https://github.com/QwenLM/Qwen-Image)。[2509 官方说明](https://github.com/QwenLM/Qwen-Image/blob/main/Qwen-Image-Edit-2509.md)支持多图编辑、人物与场景组合、姿态变化及 ControlNet 条件。
- [dx8152 多角度 LoRA](https://huggingface.co/dx8152/Qwen-Edit-2509-Multiple-angles)针对改变机位、俯仰和景别；基座及 LoRA 模型卡标注 Apache-2.0。LoRA 基于 2509，不能未经核实替换成任意新版基座。
- 作者提供 [RunningHub 工作流链接](https://www.runninghub.ai/post/1985311204883243009)，本次没有验证其当前运行状态、节点或套餐可用性。
- 适合试验同一画面派生动作结束图、或同一场景派生另一个视角；机位 LoRA 本身不负责动作衔接，也没有真实三维空间保证。作者记录过一致性不稳定，不能承诺不漂移。

### 3. LocalMiniDrama：可见、可操作的接镜流程

- 仓库：[xuanyustudio/LocalMiniDrama](https://github.com/xuanyustudio/LocalMiniDrama)，代码 MIT；Node/Vue/Electron，依赖 FFmpeg 与外部生成 API。
- [尾帧接镜服务](https://github.com/xuanyustudio/LocalMiniDrama/blob/main/backend-node/src/services/tailFrameLinkService.js)读取上一镜视频、抽图并写入下一镜的 `first_frame_image_id` 和主图字段，存在实际实现。
- 但其 `-sseof -1` 配合 `-frames:v 1` 取的是末尾约一秒处的画面，不是严格最后一帧；直接照搬可能导致动作回跳。此处为源码推断，未运行视频验证。
- 首尾帧是否进入生成还取决于视频后端；全能多参分支可能不使用对应首尾帧字段。适合借鉴人工接镜入口，不能认为所有模型或换机位镜头都适用。

### 4. H3 Motion Context MultiRef：延续现有 H3 视频路线

- 仓库：[seitanism/ComfyUI-H3-Motion-Context-MultiRef](https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef)，GPL-3.0；模型遵守其自身许可。
- [关键帧文档](https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef/blob/main/KEYFRAMES_AND_INSERTS.md)区分指定时间位置的图片引导、受保护的静帧 latent，以及已有视频/生成视频的尾部上下文续接。它解决视频衔接，不负责画分镜图。
- 官方明确：软引导不保证逐像素复现；latent 保护也不保证解码像素完全一致。工作流依赖外部节点、匹配版本与时间网格，不能直接塞入现有 RunningHub 工作流就认定可运行。
- [ComfyUI 核心 H3 节点](https://github.com/comfyanonymous/ComfyUI/blob/master/comfy_extras/nodes_minimax_h3.py)已有接收 `first_frame`/`last_frame` 的 `MiniMaxH3ImageToVideo` 和指定帧位置的 `MiniMaxH3AddGuide`。因此应先检查现有 H3 后端能否使用这些接口，再决定是否换视频模型。

### 5. Story-Iter：连续故事图与姿态约束研究备选

- 仓库：[UCSC-VLAA/story-iter](https://github.com/UCSC-VLAA/story-iter)，旧 Story-Adapter 地址重定向到此；代码 MIT。
- [README](https://github.com/UCSC-VLAA/story-iter/blob/main/README.md)与 `run_controlnet.py` 显示：利用上一轮全部故事图的参考信息迭代，提供 OpenPose ControlNet 版本。
- 更贴近连续分镜图片的一致性研究，但依赖 SDXL/RealVisXL、IP-Adapter 等，与现有 API 链路差异较大。README 仍声明代码未完整；不保证动作可达或空间朝向正确，暂不作为立即替换项。

## 其他项目为何没有优先推荐

- [StoryMem](https://github.com/Kevin-thu/StoryMem)：已发布跨镜头记忆代码及 LoRA，适合记忆驱动视频路线；不是任意首尾双帧控制器。代码 S-Lab 1.0、[权重](https://huggingface.co/Kevin-thu/StoryMem) CC-BY-NC-4.0，均有非商用限制。
- [StoryDiffusion](https://github.com/HVision-NKU/StoryDiffusion)：图片角色一致性可参考，但 README 的视频 Motion Predictor 代码/权重仍未标记发布；不能将视频演示当成现成首尾帧模块。
- [Wan2.1 FLF2V](https://github.com/Wan-Video/Wan2.1#run-first-last-frame-to-video-generation)：有官方首尾帧视频推理代码，但输入两张图的兼容性仍需上游解决。H3 已有相关接口，暂不优先新增另一套视频部署。
- [CANVAS 论文](https://arxiv.org/html/2604.13452v1)：状态记忆、参考检索和候选筛选思路相关；本次未找到作者官方 GitHub 发布，不能当现成开源本地方案。

## 对本项目的建议（待确定后再实现）

1. 先拿同一小组分镜比较现有独立生成与编辑式生成，优先借鉴 ViMax 的起止状态规划、用 Qwen 编辑验证画面保留能力；不先重写工作台。
2. 首尾状态应包含人物位置、视线/身体朝向、道具持有者与开合状态、动作进度和相机方位。以“仍拿同一件道具、动作不倒退、换机位后空间关系正确”为通过条件。
3. 连续动作且机位不变时，可试结束画面接下一镜起始画面；换机位时保留世界状态并重新构图；换场时建立新场景参考。所有镜头机械复制上一尾帧，会限制正常剪辑。
4. 首尾图确认后，再验证 H3 的首尾帧/时间关键帧条件。不要仅将新画的尾图追加到普通参考图列表，就声称实现首尾约束。
5. 测试组至少覆盖：拿起道具、递交道具、背面跟拍、正反打，以及一次换场；同时记录生成次数、人工重画次数和耗时。不以“脸像同一个人”作为唯一合格条件。

未执行：真实生成 A/B、RunningHub 节点可用性检查、供应商费用核对、本地 GPU 部署。没有启动/重启服务、调用 Claude、执行 git 操作或提交。
