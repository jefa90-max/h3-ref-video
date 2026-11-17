# 分镜提示词精简与优化开关、H3 英文改写重试与手动重转、多角色合图参考方案

状态：用户已确认核心交互决策并补充「精简优化后的分镜提示词（机位构图 + 角色外貌锁死词）」，待确认完整方案与实施计划后进入编码。

## 背景与目标

当前工作台与 CLI 在长片/多角色生产中存在三类痛点：

1. **分镜优化提示词过度冗长（`composition v2` 字段膨胀）且无开关、无降级兜底**
   - 现有 `h3ref/frames.py:describe_composition` 与 `h3ref/staging.py`（`version=2`）强制每镜输出 30+ 个工程化键值字段（`世界空间基准`、`轴线侧`、`可见性`、`开口方向`、`底部方向`、`结扣位置`、`expected_end` 全套复写等）。不仅极大增加了语言模型漏字段报错的概率，还用大量低价值元数据稀释了生图模型对**机位、场景陈设、人物左中右站位与外貌一致性**的注意力。
   - 同时目前无法按需关闭语言模型优化，且构图调用一旦返回空或格式不符会直接令该镜生图失败，不会自动重试或回退原始提示词。
2. **H3 英文提示词改写重试偏少且历史任务缺乏手动重转入口**
   - 视频阶段转为 MiniMax H3 六段式英文提示词（`h3ref/rewrite.py:rewrite`）仅尝试 2 次，偶发空输出或局部格式瑕疵会导致中断；同时历史任务缺乏**不重新花钱跑视频、仅重新转化并预览英文提示词**的手动按钮。
3. **多角色受限于模型 9 张参考图上限**
   - 生图与生视频模型最多支持 9 张参考图（`config.MAX_IMAGES = 9`）。角色较多时容易超出上限，目前界面每个角色固定独占一个上传槽位，后端也未对多角色共用同一张合图做保序去重、9 图前置校验及防串脸/防未出场角色乱入的提示词适配。

### 目标（Goals）

- **精简优化后的分镜提示词（新增 `composition v3`）**：摒弃冗长的工程键值对，改为紧凑直观的三要素自然中文描述——**① 机位与构图**、**② 场景与前中背景核心陈设/道具**、**③ 画面左/中/右人物站位 + 跨镜头逐字一致的「角色外貌锁死词」（固定服装、头饰、五官长相）+ 本镜首帧姿态/朝向/持物**；同时向下兼容历史任务已落盘的 `version=2` 缓存。
- **分镜提示词 LLM 优化开关 + 3 次重试回退**：支持选择是否启用语言模型优化（默认开启）；开启时若输出为空或格式校验失败，**自动附带错误摘要重试最多 3 次**（共最多 4 次请求），仍失败则自动回退到原始剧本分镜提示词继续生图；鉴权/欠费/限流错误（`401/402/403/404/429`）立即报错停止。
- **MiniMax H3 英文提示词有界重试（最多重试 6 次、绝不为空）**：强制六段齐全且每段非空；遇到空输出或格式/图片引用越界问题时自动附带具体错误重试最多 6 次（共最多 7 次请求），鉴权/余额/模型不存在错误立即停止。
- **历史任务手动重新转化英文提示词（单段 + 整片批量）**：结果页「视频片段」支持单段独立重转与整片批量重转（补齐缺失段 / 全部重转），落盘保存并展示在编辑器供预览，不自动触发 RunningHub 扣费生视频。
- **多选角色合并对应一张参考图**：创建页支持勾选多个角色合并为一个上传槽位绑定同一张合图（随时可拆开）；全链路（创建校验、分镜生图、结果页单镜参考图编辑、视频分段）按去重图片槽位计数并限制最多 9 张，提示词层自动防止角色串脸与同图未出场角色乱入画面。

### 非目标与反目标（Non-Goals / Anti-Goals）

- **不把单镜临时道具/动作塞进角色跨镜外貌锁死词**：例如「手持金箍棒」「端着茶杯」「背对镜头」属于本镜状态，放在括号外逐镜变化；括号内的外貌锁死词**严格只锁五官长相、发型胡须、固定头饰与固定服装**，避免后续空手镜头穿帮。
- **不把某一种机位写死为全片固定机位**：若剧本 `shot.camera` 已指定景别/视角则忠实展开，未指定时再按当前镜头人物关系补足清晰机位与构图（如多人殿堂补高角度斜俯拍广角或对称全景）。
- **不做无上限死循环重试**、**手动重转英文提示词不自动扣费跑视频**、**不重跑历史已缓存产物**。

---

## 详细设计

### 1. 精简分镜提示词（`composition v3`）+ 优化开关 + 3 次重试回退

1. **`composition v3` 精简结构与程序级「角色外貌锁死词」（`h3ref/staging.py`、`h3ref/frames.py`、`h3ref/film.py`）**
   - 语言模型只输出精简的 `version=3` JSON 对象（大幅降低格式出错率与 Token 消耗）：
     ```json
     {
       "version": 3,
       "camera": "高角度斜俯拍广角，对称构图",
       "scene": "西梁女国金銮宝殿内部，红柱金砖，中央摆放一张雕花长案，案上放着明黄色的通关文牒与大红婚书，背景为垂落的珠帘与宝座",
       "characters": [
         {
           "name": "女王",
           "locked_look": "头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵",
           "placement_and_pose": "画面左侧站着，侧身面向中央长案，右手轻按婚书"
         },
         {
           "name": "唐僧",
           "locked_look": "头戴五佛宝冠，身披大红织金锦襕袈裟，内着白僧衣，面容白净",
           "placement_and_pose": "画面中央坐着，双手合十，神情克制"
         },
         {
           "name": "孙悟空",
           "locked_look": "金冠金甲，毛脸雷公嘴",
           "placement_and_pose": "画面右侧站着，手持金箍棒立于身侧"
         }
       ]
     }
     ```
     （无人空镜允许 `"characters": []`。）
   - **程序级跨镜外貌锁死（100% 一致保障）**：
     - `film.stage_frames` 在顺序规划各镜时维护 `locked_looks: dict[str, str]`（也可从已有缓存帧恢复）。
     - 某角色首次出场通过校验后，将其 `locked_look`（仅固定服装、头饰、长相特征，剥离一次性持物和姿势）写入 `locked_looks[name]`，并作为上下文传给后续镜头；**且后续镜头校验返回后，由 Python 代码直接用 `locked_looks[name]` 强制覆盖该角色的 `locked_look`**，从根源杜绝大模型跨镜头改词换装。
   - **自然中文渲染格式（`staging.render_opening` 对 `version==3`）**：
     ```text
     机位与构图：高角度斜俯拍广角，对称构图。
     场景为西梁女国金銮宝殿内部，红柱金砖，中央摆放一张雕花长案，案上放着明黄色的通关文牒与大红婚书，背景为垂落的珠帘与宝座。
     画面左侧站着女王（头戴九凤朝阳金冠，身穿大红绣金凤袍，容貌端庄华贵），侧身面向中央长案，右手轻按婚书。画面中央坐着唐僧（头戴五佛宝冠，身披大红织金锦襕袈裟，内着白僧衣，面容白净），双手合十，神情克制。画面右侧站着孙悟空（金冠金甲，毛脸雷公嘴），手持金箍棒立于身侧。
     ```
     （语法归一化：若模型返回的 `placement_and_pose` 写成「画面左侧站着，侧身……」，程序自动将角色名与外貌锁死词自然嵌入为「画面左侧站着女王（…），侧身……」，读起来与手写剧照提示词完全一致。）
   - **视频中文提示词联动精简（`film.compile_prompt`）**：
     - 对 `version==3` 的镜头，不再拼入旧版 30 行元数据，而是精简为：`【机位】 + 首帧场景与人物站位摘要（含外貌锁死词） + 动作过程：{shot.action}`，让下游 H3 英文改写更准、更稳。
     - 历史任务已保存在 `state.json` 的 `version==2` 构图继续由原 v2 分支兼容读取，绝不破坏旧缓存。

2. **优化开关与传递链路**
   - `request.json` 新增布尔字段 `optimize_storyboard`（缺省 `True`，兼容旧任务）。
   - **创建页（`workbench/templates/index.html`、`workbench/static/create.js`）**：`#film-fields` 新增复选框 `#optimize-storyboard`（默认勾选）；`frames === "none"` 时禁用置灰。
   - **CLI（`h3ref.py`）**：`film` 子命令新增 `--no-optimize-storyboard`（`dest="optimize_storyboard", action="store_false", default=True`）。
   - **预检（`workbench/inputs.py:preflight`）**：`optimize_storyboard=False` 时 `needs_composition=False`，仅跑分镜图时无需配置文字模型 Key。

3. **构图优化 3 次重试与回退机制（`h3ref/frames.py`、`h3ref/film.py`）**
   - 新增 `frames.CompositionFormatError(RuntimeError)`。
   - `frames.describe_composition(..., max_retries: int = 3)`：
     - 最多执行 4 次请求（首次 + 3 次重试）。
     - 鉴权/余额/限流/模型错误（`HTTPResponseError` 状态码 `401, 402, 403, 404, 429`）**立即抛出，不重试、不静默降级**。
     - 返回空内容、JSON 解析失败或 `staging.validate_composition` 抛出 `ValueError` 时，将本轮错误摘要追加进下一轮消息自动重试；4 次均失败抛出 `CompositionFormatError`。
   - `film.stage_frames` 捕获 `CompositionFormatError` 后：记录回退日志，以 `composition=None` 生成原始提示词继续生图，不标记该镜失败，也不污染后续镜头的 `previous_composition`。

### 2. MiniMax H3 英文提示词有界纠错重试（最多重试 6 次、绝不为空）

- **增强校验（`h3ref/rewrite.py`）**：`sections()` / `problems()` 除检查六段标题顺序外，强制检查每一段正文 `strip()` 非空，并保留负向词检查、`[Shot 1]` 检查和 `<Picture N>` 范围检查（`1 <= N <= n_images`）。
- **错误分级与重试**：
  - 新增 `RewriteFatalError(RewriteError)`：本地缺少 `claude` 命令，或 `_llm()` 遇到 `401, 402, 403, 404` 时立即终止，不重试。
  - 空返回或 `problems()` 校验不通过时，`rewrite(..., max_retries: int = 6)` 最多尝试 **7 次（首次 + 6 次重试）**，每轮附带具体问题反馈给模型修正；仍未通过则抛出 `RewriteError`，杜绝空提示词或坏格式进入视频生成。

### 3. 历史任务手动重新转化英文提示词（单段 + 整片批量）

- **后端操作（`workbench/server.py`、`workbench/jobs.py`、`workbench/segments.py`、`workbench/inputs.py`）**：
  - 新增 `Jobs` 子进程操作 `rewrite_segment`（`POST /api/jobs/<id>/segments/<number>/rewrite`）与 `rewrite_prompts`（`POST /api/jobs/<id>/segments/rewrite`，支持 `scope: "missing" | "all"`）。
  - 预检仅校验文字改写模型 API Key（以及除 `kind == "prev"` 上段尾帧之外的静态/分镜参考图文件存在——保证 `first`/`none` 串联模式在首段视频尚未生成时也能正常预改写后续段落提示词），不要求 `RUNNINGHUB_API_KEY` 或 `ffmpeg`；演示模式（`demo=True`）下生成合法六段式演示英文提示词以便预览测试。
  - 单段/批量转化只更新 `state.json`（`zh`、`h3`、`h3_source`、`h3_reference_version`、必要的 `prompt_override`）与 `prompts/segN.{zh,h3}.txt`，保留已有 `segN.mp4` 不变、不调用 RunningHub；之后用户点「重新生成片段」会自动命中 `reuse_h3` 直接生成视频，无需二次改写付费。
  - 修复 `segments.py:view` 在尚无 `mp4`（`version is None`）时隐藏 `source_prompt` / `h3_prompt` 的限制；并在 `jobs.py` 中使 `can_resume_pipeline` 兼容 `rewrite_segment` / `rewrite_prompts` 操作后的未完结历史任务续跑。
- **前端交互（`workbench/templates/results.html`、`workbench/static/video-segments.js`）**：
  - 单段提示词编辑区新增「重新转化为英文提示词」按钮，转化完成后自动切至 `h3` 视图展示新生成的英文提示词供检查。
  - 视频片段面板顶部新增「补齐未转化英文提示词」与「全部重新转化为英文提示词」按钮。

### 4. 多选角色合并为一张参考图（创建页建组 + 全链路去重 + 防串脸/防乱入 + 9 图校验）

- **创建页建组与绑定（`workbench/static/create.js`、`workbench/references.py`）**：
  - 当角色数 $\ge 2$ 时显示合并工具条与当前角色参考图张数统计；勾选多个单角色卡片后可合并为一张「多角色合图」卡片，共用一次上传（及支持随时「拆开单独上传」），支持建立多个不同合图组。
  - 同一合图组的所有角色在 `film.json` 中指向同一个上传图片路径（演示占位模式通过 `reference_groups` 共享同一占位图路径），天然与 CLI 共用同一套底层数据模型。
- **全链路保序去重与 9 张上限（`h3ref/film.py`、`h3ref/frames.py`、`workbench/inputs.py`、`workbench/storyboard.py`、`workbench/jobs.py`）**：
  - 新增 `film.group_cast_by_picture(active_cast, all_cast)` 按图片路径保序聚合当前镜头/分段出场角色，计算 `active`（本镜/本段出场）与 `absent`（同在合图上但本镜/本段未出场）。
  - 在 `inputs.validate`（创建校验，同时覆盖分镜生图与视频分段）、`jobs.edit_frame`（结果页单镜增删角色参考图）、`frames.submit` 与 `film.refs` 四处统一限制参考图总数 $\le 9$。
  - 结果页分镜参考图、角色参考图选择器与视频片段参考图清单统一按路径去重并将合图名称展示为 `角色A / 角色B`。
- **防串脸与防乱入提示词**：
  - 单人单图保持原提示词不变；多人合图在同一编号 `参考图片{k}` 下逐一列出本镜/本段出场角色名及外观特征要求对号入座，若 `absent` 非空则自动追加「该参考图中还包含未出场的 X，严禁将未出场角色画进画面」；`rewrite.RULES` 同步补充单张 `<Picture N>` 映射多个 `<Subject>` 及排除未出场角色的规则。

---

## 实施步骤与验证计划（步骤 → 验证方式）

1. **阶段 1：精简分镜构图 `v3`（含程序级角色外貌锁死词）、优化开关、3 次重试回退与 H3 改写 6 次重试**
   - 修改文件：`h3ref/staging.py`、`h3ref/frames.py`、`h3ref/rewrite.py`、`h3ref/film.py`、`h3ref.py`、`h3ref/messages/{zh-CN,en}.json`。
   - 验证方式：离线单测验证 `v3` 输出格式紧凑自然、跨镜头 `locked_look` 逐字锁定一致、旧 `v2` 缓存兼容读取、`optimize_storyboard=False` 跳过构图模型、构图 3 次重试与回退原始提示词、`401/403` 立即抛错、`rewrite` 空块拦截与 6 次纠错重试。

2. **阶段 2：多角色合图去重、防串脸/防乱入提示词与全链路 9 图校验**
   - 修改文件：`h3ref/film.py`、`h3ref/frames.py`、`h3ref/rewrite.py`、`workbench/references.py`、`workbench/inputs.py`、`workbench/storyboard.py`、`workbench/jobs.py`。
   - 验证方式：离线单测验证 10+ 角色合并为 $\le 9$ 张合图全流程跑通、合图部分角色出场时自动生成防串脸与禁画未出场角色提示词、超 9 张在创建/单镜编辑/提交时准确拦截、结果页合图名称正确展示。

3. **阶段 3：历史任务手动重转英文提示词后端接口与续跑状态衔接**
   - 修改文件：`workbench/server.py`、`workbench/jobs.py`、`workbench/segments.py`、`workbench/inputs.py`、`workbench/demo.py`、`workbench/messages/{zh-CN,en}.json`。
   - 验证方式：Flask `test_client` 离线测试单段重转与整片批量重转（`missing` / `all`）落盘更新、不触发 RunningHub、后续生视频直接命中 `reuse_h3`、中断历史任务重转后 `can_resume_pipeline` 正常续跑。

4. **阶段 4：前端交互（优化开关、角色多选合图、单段/批量重转英文按钮）、双语文案与全量回归**
   - 修改文件：`workbench/templates/{index,results}.html`、`workbench/static/{create,video-segments,frame-references,results}.js`、`workbench/static/style.css`、`workbench/static/messages/{zh-CN,en}.json`、`docs/workbench.md`、`README.md`。
   - 验证方式：运行全量 Python 单元测试（`.venv/bin/python -m unittest discover -s tests -q`）、Node 前端测试（`node --test`）、语法检查与四套中英文语言包键/占位符一致性校验（不触碰正在运行的开发服务器）。
