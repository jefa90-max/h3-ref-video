# 工作台与 CLI 对照

核对范围：`h3ref.py`、`h3ref/*`、`SKILL.md`、`prompts/h3_examples.md`、`examples/film.example.json`。核对日期：2026-11-02。

| 步骤 | 原仓库实现 | 工作台行为与修正 |
| --- | --- | --- |
| one 输入 | 默认 12 秒，5–15 秒，三种画幅，参考图顺序，seed/check | 同参数调用 `h3ref.py one`；已改回默认 12 秒，补 seed/check |
| one 改写 | 已有六段式原样使用；否则 rewrite 模块读取官方指南、RULES、示例并调用改写器 | 完全复用；不拼另一套系统提示词；界面根据缓存和最终提示词判断配置需求 |
| film 解析 | `film.load` 过滤空 action、默认 2 秒、至少 0.5 秒、时间归一化 | 删除 UI 重复时间算法，直接使用原 load；页面从角色/总表清单绑定上传文件，启动时保存绝对路径 |
| 切段与参考图 | `film.plan / refs / compile_prompt`，总数不超过 9 张，shot/first/none | 直接使用相同函数；显示实际参与生成的分镜，不承诺超出参考图预算的镜头都有图 |
| 画图 | `film.stage_frames` → `frames.prompt / draw` | 完整出片和“先生成分镜”使用同一阶段；图像模型、参数、总表→角色→首图锚点顺序一致 |
| 视频提示词 | `stage_rewrite` → 原 rewrite，官方六段式 | 真实流水线完全沿用；单镜编辑框只修改图像生成提示词，不冒充视频提示词编辑 |
| 出片与检查 | 原 RunningHub 工作流、轮询、拼接、字幕检查、report | 常规任务直接调用 CLI；更新分镜后的继续出片调用原 `film.run`，未另写生成器 |
| 单镜重出 | `frames.draw` 是原图像 API；CLI 的 `--redo` 只重做视频 | 单镜用原 draw，仅新建该图的任务；成功后替换，失败保留旧图，续跑复用新 task |
| 更新成片 | `--redo`：shot 只重做所属段；first/none 从该段向后重做 | 抽取原失效逻辑为 `film.invalidate_segments`，CLI 与 UI 共用；失效只应用一次，网络失败续跑不会清掉刚提交的新 task |
| 配置与恢复 | 根目录 .env / 环境变量；state.json 恢复 | 同配置入口与状态；按当前阶段和缓存预检，先画图只需 APIMart；film 字幕检查缺 key 沿用原 unchecked 记录行为 |

## 有意保留的 UI 差异

- 用户要求 Claude 暂不可用，工作台固定 `H3_REWRITER=llm`，不探测 Claude；CLI 自身默认选择规则未改。
- 页面不要求本地路径：导入后提取 cast 与 board 上传清单，绑定与故事 JSON 分开；启动前写绝对路径给原 CLI。旧路径转成待上传位，board:true 是工作台的必需总表标记，不进入 CLI。底层兼容接口仍保留旧 film_base_dir 行为供已有任务使用。
- HTTP 边界增加类型、有限数值、图片内容与输出目录校验；单任务互斥；默认离线演示。演示是占位媒体，不能验证模型效果。
- key 可保存到忽略的本机 `.env`（权限 0600），也可只放当前服务内存；不进入浏览器存储或任务请求记录。
- 新增审图卡片、旧视频待更新提示、Plyr 播放器；one 的工作台报告是展示补充，不更改 CLI 产物。
- `film.load` 新增默认开启的 `check_images` 参数，仅历史结果预览关闭图片存在性检查；源参考图被移动后，旧视频仍可看，缺图分镜禁止重出。CLI 校验默认不变。

## 验证

原 14 项测试全部通过；新增 10 项回归覆盖 CLI 时间/路径/key/参数一致性、单图失败恢复、改图后的重出范围、重出失败保留新 task、历史缺参考图与 .env 权限。额外通过本地 HTTP 假 API 验证真实阶段进入原供应商客户端并显示 401；未进行付费生成。详细证据见 [验证记录](workbench-verification.md)。
