<!-- /autoplan restore point: "/Users/qiaoyu/projects/h3-ref-video/.gstack/tmp/autoplan/main-autoplan-restore-20261124-172322.md" -->
## URGENT, outside this plan

None.

## Implementation plan
# 台词语速预检与分镜/H3 提示词质量增强计划

状态：**规划完成，等待用户合并当前分支后启动实施**（本阶段仅沉淀计划文档，未修改业务代码）。

## 背景与来源

对开源项目 `manju-laoli-skill`（V7.5.0-SLIM）做全面拆解后确认：该仓库已剥离 MiniMax H3 支线、专注上游 Seedance 2.5/2.0 纯文本导演规则库，其 155 文件规则体系与 1.48 万行正则机检脚本不适合直接引入。但其中 4 项经过大量短剧实片踩坑沉淀的方法论，精准命中本项目当前 CLI 与本地工作台的真实短板：

1. **台词语速与镜长预算缺少前置检查**：[h3ref/film.py](../../h3ref/film.py) 目前仅按起止秒数切段，不检查单镜台词是否塞得进镜长，易导致 H3 口型加速崩坏、吞字或乱切。
2. **跨镜连续性与群像穿帮缺少高频护栏**：历史任务反复出现参考图穿戴「叠加而非替换」（如摘眼镜/脱外套）、前镜强姿态「甩锅」给他人、背景多人齐动、方位与过肩左右错乱。
3. **禁令词根与审核安全转译仍有优化空间**：[h3ref/frames.py](../../h3ref/frames.py) 的 `NEUTRAL_WORDS` 对部分冲突动作替换过于消力；[h3ref/rewrite.py](../../h3ref/rewrite.py) 目前只拦音乐/字幕否定句，尚未覆盖高关联视觉词根的否定（如「面无笑意」「不发光」）。
4. **H3 大情绪表演偏收**：[README.md](../../README.md) 实测记录显示 H3 在惊吓、疼痛、崩溃等大情绪上偏克制，需要把抽象情绪稳定拆成面部四区肌肉与肢体可见动作。

### 核心目标（Goals）

- **项 1 · 台词语速与镜长预算预检（确定性 WARN）**：CLI 与工作台共享一套零 API 成本的纯 Python 预检，在花钱出图前标出「真念不完」和「超长句」镜头。
- **项 2 · 分镜与视频防穿帮微规则（保持 `composition v3` 精简）**：把「资产状态偏离覆盖、强姿态终结交接、背景群像静动分离、画面左右与过肩映射」4 条高密度规则融入构图与改写引导，不膨胀回繁复的 v2 字段。
- **项 3 · 物理合规安全转译 + 禁令词根正向化**：在严守「零『死』字」红线前提下优化冲突/动作类转译词保留画面张力；扩展英文改写对高关联视觉否定词的正向化引导，并对生图端否定句做小样本实测验证。
- **项 4 · FACS 四区面部肌肉动作速查**：在不引入任何 `AUxx` 编码进入正文的前提下，强化 `rewrite.RULES` 第 7 条，提升 H3 惊吓/疼痛/哭笑/隐忍等面部表演幅度与区分度。

### 非目标与铁律（Non-Goals & Constraints）

结合 [tasks/lessons.md](../../tasks/lessons.md) 已有教训，实施过程严格遵守以下约束：

- **绝不把 `composition v3` 重新做重**：不引入角色状态表、空间坐标图、30 字段 JSON 或 40+ 条正则门禁；新增规则严格控制在数行高密度描述内。
- **先小样本 A/B 实测，再合入默认提示词**（2026-11-15 教训）：项 2 / 3 / 4 涉及生图（GPT Image 2.6）与视频（MiniMax H3）提示词行为，离线单测通过不代表真实出片变好；必须先用 3–5 个历史痛点镜头做单变量 A/B 实测，确认 APIMart 审核通过率不低于基线、3–5 镜人工盲评净正向、单镜/单段提示词字符与单镜估算费用增幅 ≤ 15% 后再升级为默认规则。
- **「死」字字符级兜底红线不动摇**（2026-11-08 教训）：任何替换表升级都必须保证图片提示词任意位置零「死」字，且不把「死命/死盯」等常见强调语误写成静止状态。
- **`locked_look` 与单镜动态状态严格分离**（2026-11-09 教训）：资产偏离声明只进入本镜 `placement_and_pose`（或视频镜行），绝不污染跨镜复用的 `locked_look`。
- **CLI 与工作台单一事实源 + 全量 i18n**（2026-11-02 教训）：工作台不另写一套语速校验逻辑，直接调用 `h3ref/film.py` 纯函数；所有新增提示文案必须写入全部三套语言包 `h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json` 与前端 `workbench/static/messages/{zh-CN,en}.json`，严禁硬编码。

---

## 详细设计

### 项 1：台词语速与镜长预算 WARN 预检

#### 1.1 纯函数设计（纯标准库 `h3ref/preflight.py`，由 `h3ref/film.py` 原样再导出）

将纯标准库预检引擎独立为 `h3ref/preflight.py`（零 `h3ref.config` / `.env` / 网络副作用），由 `h3ref/film.py` 原样再导出 `normalize_shots`、`normalize_speech`、`inspect_dialogue_budget`、`check_dialogue_budget`、`FilmValidationError`、`DIALOGUE_RATE_CONFIG`、`sanitize_line_preview`、`escape_markdown_cell`，同时移除 `h3ref/i18n.py:7` 对 `config` 的顶层导入（改读 `os.environ.get("H3_LANG", "").strip()`），供 CLI、独立 `preflight` 子命令与工作台共同调用：

- **共享镜头归一化（非就地修改 + 草稿容错 + 时间戳有界）**：新增纯函数 `normalize_shots(shots, *, strict=False) -> list[dict]`，返回全新浅拷贝列表、**绝不就地修改入参**。沿用 `film.load()` 规则：过滤空 `action` 镜头、缺失或仅单边填写 `start`/`end`（或超出 `[0.0, 3600.0]` 上界，防 `1e308` 触发 `t + length` 溢出）默按 `2.0s` 并计入 `default_duration_shots`、时长下限 `max(0.5, length)`、`round(..., 2)`。`strict=True`（供 `load()` 调用）对空 `shots`、超上限或非法时间抛 `FilmValidationError`；`strict=False`（供 `inspect_dialogue_budget()` 调用）遇缺失/非 list `shots`、非 dict 项、非 `str` 的 `action`（视为无效并跳过）、或 `bool`/非有限数（`NaN`/`Inf`）/越界/不可解析的 `start`/`end`（回退 `2.0s`）时安全容错，不抛 500。非 `str` 的 `line` 或 `speech` 统一安全视为 `""`，`normalize_speech()` 仅用于预检与 `stage_check()`，**绝不改动 `film.compile_prompt()`（`film.py:264,293,299`）对旧任务非规范 `speech` 的字节级输出**。
- **`speech` 缺失保护**：若归一化后 `speech` 为空/未设置且至少 1 个归一化镜头含非空 `line.strip()`（与 `film.py:293` `compile_prompt()` 行为一致），跳过逐镜语速计算，发出单条整片级告警 `code="dialogue_speech_unset"`；若全片无非空 `line`，返回 `[]`。
- **慢速语气检测顺序（先检测、后清洗前缀）**：慢速关键词检测必须在**原始 `action` 与原始 `line`** 上执行（早于剥离括号/冒号说话人前缀，防止 `（哽咽）...` 或 `（小林，低声）...` 中的表演提示被洗掉）。关键词覆盖中文（`耳语|低语|低声|轻声|气声|哽咽|一字一顿|一字一句|断续|拖长|放缓`）与英文（`\b(?:whisper(?:s|ing|ed)?|murmur(?:s|ing|ed)?|softly|slowly|chok(?:e|es|ing|ed)|sob(?:s|bing|bed)?)\b`，忽略大小写）。
- **说话人前缀清洗（确定性循环，最多 5 轮）**：先做全角 ASCII 标准化，再在行首循环剥离以下前缀直至不再匹配或达到 5 轮上限：① 严格成对全/半角括号及方括号标签（`^(?:（[^）]{1,20}）|\([^)]{1,20}\)|\[[^\]]{1,20}\]|【[^】]{1,20}】)\s*`，拒绝 `(你好】` 跨类型错配）；② 显式说话人冒号前缀（仅匹配经清洗的 `film.cast` 角色名/别名——校验 `cast` 为 `list`、项为 `dict`、`aliases` 为 `list[str]`、过滤空串、限制长度 `<= 64`、按长度降序排列使 `林医生` 优先于 `林`——或常见显式说话人标签 `旁白|画外音|路人[甲乙丙0-9]?|众人|Narrator|VO|OS`（ASCII 忽略大小写），防止误剥 `12:30`、`记住：千万别回头` 或 `Wait: don't go`）。
- **脚本族分流、确定性计数与 `count == 0` 跳过**：
  - 非空格脚本（`speech` 为 `zh` / `ja` / `ko`，`unit="chars"`）：汉字（`\u4e00-\u9fff`）、日文假名（`\u3040-\u30ff`）、韩文音节（`\uac00-\ud7af`）与单个数字（`[0-9]`）各计 `1`，每个连续拉丁词（`[A-Za-z]+`）计 `1`，不含标点与空白；常规档 `6.0` 字/秒、慢速档 `3.5` 字/秒、超长句阈值 `> 24` 字。
  - 空格分词脚本（`speech` 为 `en` / `es` / `pt` 及其他非空代码，`unit="words"`）：按 `re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:'[0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*|[\u4e00-\u9fff]", text)` 计词数（孤立单引号不计为词）；常规档 `3.2` 词/秒、慢速档 `2.0` 词/秒、超长句阈值 `> 18` 词。
  - **独立未覆盖脚本检测与 `count == 0` 跳过**：计数前将全角 ASCII 标准化、英文词正则支持弯引号 `['’]`、CJK 补齐扩展 A 区 `\u3400-\u4dbf` 与 `々`（`\u3005`）、`normalize_speech()` 按 `re.split(r"[-_]", cleaned, maxsplit=1)[0]` 切分。对清洗后文本**独立扫描**是否存在未覆盖脚本字母（如西里尔/阿拉伯/泰文/天城文等 `ch.isalpha()`）——不以 `count == 0` 为前提，防止 `"Привет 2026"` 等混合脚本绕过——命中即计入 `meta.unsupported_script_shots` 并发出 `dialogue_unsupported_script`（`status` 为 `"unsupported"`/`"partial"`）；仅当无未覆盖脚本且 `count == 0`（如纯标点 `"……"` 或纯标签）时安全跳过该镜。
- **标点停顿预算（全语种适用、正则 token 去重计次）**：按优先级一次扫描清洗后的文本，避免 `……` 或 `？！` 被重复累加：
  - `……|\.{3,}|…`：每次 +0.8s
  - `——|--`：每次 +0.6s
  - `[。？！.?!]+`：每次 +0.4s
  - `[，、,]+`：每次 +0.2s
- **单镜互斥优先级与数值口径**：同一镜头最多输出 1 条告警，优先级为 `dialogue_slow_over_budget > dialogue_over_budget > dialogue_long_line`。统一按 `rate = slow_rate if is_slow else regular_rate` 计算 `raw_min = count / rate + pause`、`min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10`（向上进位到 0.1 秒）；`duration = round(end - start, 2)`，`effective_duration = min(duration, float(MAX_SECONDS))`（`MAX_SECONDS = 15.0`）；超镜长判定按 `raw_min > effective_duration + 1e-9`（整数毫秒停顿 + `1e-9` 容差），所有超预算告警严格满足不变量 **`min_seconds > effective_duration`**（当 `effective_duration < duration` 即单镜超过 15s 上限时，告警携带 `effective_duration` 并由全部三套语言包渲染专用文案「估算约 `{min_seconds}s` / 有效生成预算 `{effective_duration}s`（原镜长 `{duration}s`）」，避免 `16s > 20s` 矛盾展示）；`line_preview` 取清洗前缀并脱敏控制字符后的文本，长度 `> 40` 时截为 `text[:39] + "…"`（总长 ≤ 40）。
- **单一事实源返回与可复现元数据 Schema**：新增 `inspect_dialogue_budget(film: dict, *, rate_config: dict | None = None) -> dict` 返回 `{"status": "complete"|"partial"|"unsupported"|"invalid", "warnings": [...], "meta": {"estimator_version": str, "effective_config_sha256": str, "calibration_status": "provisional"|"calibrated"|"custom", "shots_invalid": bool, "shots_missing": bool, "total_raw_shots": int, "checked_shots": int, "skipped_shots": int, "default_duration_shots": int, "unsupported_script_shots": int}}`，`check_dialogue_budget(film, *, rate_config=None) -> list[dict]` 作为薄包装返回 `warnings`。`shot`/`affected_shots` 为归一化 1-based 序号，`raw_shot`/`affected_raw_shots` 为原始数组 1-based 位次，并附 0-based `raw_shot_index` 供自动化 Agent 回写 JSON；`rate_band` 统一为实际估算语速档 `"regular"|"slow"`：
  - `dialogue_over_budget` / `dialogue_slow_over_budget`：`{"code", "severity": "warn", "shot": int, "raw_shot": int, "raw_shot_index": int, "duration": float, "count": int, "unit": "chars"|"words", "min_seconds": float, "rate_band": "regular"|"slow", "line_preview": str}`。
  - `dialogue_long_line`：同上字段（`rate_band` 反映该镜实际使用的 `"regular"|"slow"` 档位，告警类别由 `code` 区分）。
  - `dialogue_speech_unset`：`{"code": "dialogue_speech_unset", "severity": "warn", "shot": null, "affected_shots": [int, ...], "affected_raw_shots": [int, ...]}`。
  - 结构/覆盖诊断：`dialogue_shots_invalid`（`strict=False` 收到非 list `shots` 时由 `inspect_dialogue_budget` 发出，防止静默返回 `[]`）与 `dialogue_unsupported_script`（未覆盖文字脚本时发出）。

#### 1.2 CLI 与工作台接入

- **CLI 与工作台任务链（`h3ref/film.py`、`workbench/jobs.py`、`workbench/storyboard.py`、`workbench/demo.py`）**：
  - `workbench/jobs.py:502` `create()` 必须在调用 `validate(payload, self.root)` **之前**（`validate()` 内部 `workbench/inputs.py:148-152` 已运行 `film.load()` 覆盖 `request["film"]`）对原始 `payload.get("film")` 计算完整 `PreflightResult` 快照（含 `estimator_version`/`effective_config_sha256`/`calibration_status`），并在 `_commit()` 中原子写入不可变 `inputs/preflight.json`（绝不污染 `film.json` 或 `state.json`）；新建工作台任务默认走 `workbench/storyboard.py:298`（`operation="frames"`），同样读取 `inputs/preflight.json`（在 `H3_SKIP_PREFLIGHT != "1"` 时）并写入 `job.log("preflight", ...)`。
  - `film.run()` 在 `load()` 之前优先读取 `inputs/preflight.json` 快照（无快照时读取原始 `film_path` 调用一次 `inspect_dialogue_budget()` 并落盘），将告警与部分/未覆盖摘要写入 `job.log("preflight", ...)`（`line_preview` 经 `sanitize_line_preview()` 脱敏）。**一律为 WARN，不阻断执行**。
  - `film.report()` 保持原函数签名不变，优先按创建时落盘的 `inputs/preflight.json` 快照原样展示（保证配置升级后历史报告结果可复现），缺失时回退调用 `inspect_dialogue_budget(film)` 并显式标注 `provenance_unknown` 与 `recomputed_with_config_sha256`；若有告警或部分检查摘要，在 `report.md` 顶部基础摘要之后、逐段提示词详情之前输出「脚本预检提示」Markdown 表格（单元格经 `escape_markdown_cell()` 转义 `|` 与换行，无告警时省略该小节）。
  - 全量 `film.load()` 调用点（`workbench/demo.py:72`、`workbench/storyboard.py:25,73,317`、`workbench/jobs.py:584`、`workbench/server.py:715`、`h3ref.py`）统一扩宽捕获 `except (SystemExit, FilmValidationError)`。
- **工作台参考图与校验接口（`workbench/references.py`、`workbench/server.py`）**：
  - `describe_references(script, *, lang="zh-CN", include_preflight=False)` 默认不运行预检与 `H3_DIALOGUE_RATE_CONFIG` 加载，保证 `GET /api/film/example` 与 `bind_references()` → `POST /api/jobs` 不被预检配置耦合或崩溃；仅 `POST /api/film/references` 传入 `lang=language(), include_preflight=True`，调用一次 `inspect_dialogue_budget(script)` 并固定返回 `{"film", "slots", "warnings", "preflight_status", "preflight_meta"}`（绝不修改 `result["film"]["shots"]`）。
  - 锁定 `shots` 四态与 10x 上限：`shots` 缺失/`null` → 200 `preflight_status="invalid"`（`shots_missing=True`）；存在且非 list 或长度超过 `max(2000, config.max_shots() * 2)` → HTTP 400 RFC 9457 `api_problem()`；`[]` 或 `checked_shots == 0` → `preflight_status="partial"`；正常返回 `complete`/`partial`/`unsupported`。将 `workbench/server.py:274` `segment_problem()` 泛化为 `api_problem(..., title_key, doc_anchor, ...)`，覆盖 `POST /api/film/references` 的 400 / 413（`RequestEntityTooLarge`）/ 415（`UnsupportedMediaType`）`HTTPException` 与 500 意外异常（脱敏日志写入 `storage/server.log` + `instance` 关联号，保留 `error` 兼容键）。
- **工作台前端（`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`）**：
  - 复用 `.notice.warning`（补齐 `border: 1px solid #eddfb9` 与 `.notice.warning .hint{color:#81612a}`，符合 `DESIGN.md` `#81612a` on `#fff8e8`，并回归核对 `base.html:36,43`），作为 `<section id="film-warnings" class="notice warning film-warnings" aria-labelledby="film-warnings-title" hidden>` 挂载于 `#film-parse-status` 之后、`.film-reference-section` 之前。
  - 采用风险分层 + 渐进披露（超预算类置前、`dialogue_long_line` 置后，组内按镜序；1 行紧凑摘要头含「仅提示，不阻止继续」标签；默认内联展示前 3 条，超 3 条用原生 `<details>` 折叠其余项，待 Phase 4 `d3` 终审确认），不设「永久忽略/关闭」按钮，且对每条逐镜告警提供轻量非修改「定位到脚本」按钮（用 `raw_shot` + 清洗前锚点在 `#film-json` 中 `setSelectionRange` + `focus()`）；当 `state.filmWarnings.length > 0` 时在底部 `#submit-hint` 追加轻量回响及跳回 `#film-warnings` 锚点。
  - 渲染按告警结构化字段通过 `workbench/static/messages/{zh-CN,en}.json` 的 `t(...)` 动态生成（未知 `code` 显示当前语言通用提示并将服务端 `w.message` 折叠在技术详情中，杜绝语言切换残留），切换 `zh-CN` ↔ `en` 时保持展开状态与焦点；`invalidateFilmReferences(clearBindings, clearSlots)` 仅在 `clearSlots === true`、空输入或解析失败时清空 `state.filmWarnings`，250ms 输入防抖与请求期间保留旧卡片并加 `.is-stale`（`opacity:.6` + `aria-busy="true"`）防按键跳动。
- **国际化职责边界（全部三套均经 `tests/test_locale_parity.py` 校验）**：
  - `h3ref/messages/{zh-CN,en}.json`：CLI 日志与 `report.md` 的 4 类告警文案（明确区分「超预算风险估算」与「偏长建议拆镜」因果，禁用「至少/保证念完」）、预检小节标题，以及 `chars`/`words` 单位本地化键。
  - `workbench/messages/{zh-CN,en}.json`：`/api/film/references` 服务端 `warnings[].message` 的 4 类告警文案，以及 `chars`/`words` 单位本地化键。
  - `workbench/static/messages/{zh-CN,en}.json`：创建页 Warning 卡片标题、非阻断标签、启发式/非中文未校准说明、4 类独立因果模板、定位/展开按钮、未知告警通用回退、部分检查提示（跳过/默认时长镜数）、底部 `#submit-hint` 回响及 `chars`/`words` 单位键。

---

### 项 2：分镜与视频防穿帮微规则（轻量化融入）

坚持 `composition v3` 精简路线，不新增 JSON 字段，只在提示词引导中增加 4 条经实片验证的硬约束：

1. **分镜构图规划（`h3ref/frames.py:describe_composition`）**：
   - **资产状态偏离覆盖**：在 `placement_and_pose` 规则补充——若本镜剧本要求角色摘下/脱下/放下参考图原有的穿戴或固定配饰（如摘眼镜、脱外套、摘头盔拿在手里），必须明确写出「已摘下/已脱下后的身体部位状态及内搭」，禁止用「仍穿着/照旧」含糊带过，防止生图模型将参考图穿戴与手持物品同时叠加画出两份；同时在 `h3ref/frames.py:209,217` 的参考图固定约束文案中增加单镜状态覆盖优先级说明（「服装与配饰默认照参考图，若当前镜头明确描述脱下、摘下或改动则以当前镜头描述为准」），消除与 `locked_look` / 参考图强锁文案的指令打架。
   - **强姿态显式终结**：若 `previous_composition` 中某角色处于显著姿态（趴桌、跪地、举物），本镜该角色仍在场但姿态变化时，须显式写清其当前已恢复的姿态（如「已坐直，双手垂放」），防止姿态滞留或错误转移到其他角色身上。
   - **群像静动分离**：画面含多名配角/路人时，明确仅主焦点角色做核心动作，其余背景人物保持静态或轻微静止状态，避免多人同步做重复动作。
   - **画面左右方位 + 过肩映射**：所有空间位置统一使用画面左/中/右与前/中/后景（禁用东南西北）；过肩镜头遵守物理映射——越过 A 右肩拍摄时，A 占画面左前景；越过 A 左肩拍摄时，A 占画面右前景。
2. **H3 英文改写（`h3ref/rewrite.py:RULES`）**：
   - 在规则 9 补充两句紧凑英文约束：
     - 当镜头动作涉及脱/摘参考图中的配饰或衣物（override reference state）时，明确写清替换后的当前外观，避免模型叠加出双份物品；
     - 多人同框时明确谁执行主动作、其余人保持静止/观察，且道具保持单实例不凭空复制。

---

### 项 3：物理合规安全转译 + 禁令词根正向化

#### 3.1 安全转译词表升级（`h3ref/frames.py:NEUTRAL_WORDS`）

保留现有所有含「死」字的词组与字符级兜底 `"死": "静止"`（严守 2026-11-08 教训），仅对冲突/打斗/受伤类词条做**更有影视张力的物理转译优化**（不消力、不引入高危词根）：

| 类别 | 操作 | 精确词条变更（阶段 2 Arm 3A 待测） |
|---|---|---|
| 击打/暴力类 | 修改 | `殴打`: `争执推搡` → `激烈交手推搡`；`暴打`: `争执冲突` → `重击逼退`（`暴力→激烈冲突` 保持不变） |
| 创伤/血迹类 | 修改 | `鲜血`: `深色污渍` → `暗红痕迹`；`伤口`: `衣物褶皱` → `破损擦痕`；`流血`: `衣物沾有污渍` → `沾染暗红痕迹`（不硬编码衣物部位） |
| 武器/致命类 | 修改 | `砍杀`: `对峙` → `兵刃交锋对峙`；`捅伤`/`刺伤`: `挥手逼退` → `招式逼近格挡`；`掐脖`/`勒脖`: `逼近对峙` → `逼近扣肩制伏` |
| 破坏/极端类 | 修改 / 新增 | 修改 `断肢`: `散落的道具` → `散落碎石与道具`；新增 `炸飞碎尸`: `气浪震散碎石尘土`（`肢解→画面切换` 保持不变） |

> 注：`describe_composition` 系统提示中的影视动作安全转译示例（沉劲压迫、受力后仰、气浪推散、地面裂纹、水珠汗珠飞溅）单列为阶段 2 **Arm 3B**，与 `NEUTRAL_WORDS`（Arm 3A）及项 2（Arm 2A）完全隔离测试。

> 注意：具体词条最终落地前，必须在阶段 2 用真实 APIMart GPT Image 2.6 做一轮审核通过率验证，确保不触发 `ContentPolicyError`。

#### 3.2 禁令词根正向化（`h3ref/rewrite.py` & 待测项 `h3ref/frames.py`）

- **阶段 2 待测改造（H3 英文改写侧 `h3ref/rewrite.py`，单列为阶段 2 Arm 3D）**：
  - H3 工作流无负面分支，扩展 `RULES` 第 5 条：除禁止音乐/字幕/水印的否定句外，要求 LLM **对高关联视觉概念的否定描述（如「面无笑意」「不发光」「不要空镜」「不流血」）一律做零词根正向改写**（改写为具体肌肉状态、哑光材质本色、人物主体开句），不把被禁词根带进英文提示词。
  - **暂定策略（Taste Decision `d1`）**：阶段 2/3 默认仅在 `RULES` 第 5 条通过正向化引导生效，**不默认向 `problems()` 添加视觉否定词硬拦截正则**（避免误杀合法英文句或手工 H3 覆盖导致 6 次重试耗尽）；仅当阶段 2 Arm 3D 实测显示模型持续忽视引导时，再补充零误报窄正则。
- **审慎验证项（生图侧 `h3ref/frames.py:TAIL`，单列为阶段 2 Arm 3C）**：
  - `frames.py:17` 的 `TAIL` 目前写着 `不增加额外人物。不出现字幕、文字、水印和任何真实品牌的标志。电影级布光。`；GPT Image 2.6 与 H3 机理不同，**禁止盲改**（注：`h3ref/film.py:RULES` 仅用于视频中文提示词，不进入生图请求，本次不改动）。
  - 在阶段 2 单独做一组 A/B 对照（对比原 `TAIL` vs 正向化表述，例如把泛泛的「电影级布光」拆为更明确的自然/戏剧光源描述，同时核对去掉「不出现文字」是否会诱发画中乱码），只保留实测变好的部分。

---

### 项 4：FACS 四区面部肌肉动作速查（治 H3「大情绪偏小」）

在 [h3ref/rewrite.py](../../h3ref/rewrite.py#L48-L50) 的 `RULES` 第 7 条中，把现有 3 行泛化原则扩充为一段精炼的英文「情绪 → 四区面部肌肉 + 自主神经/肢体动作」映射指引（控制在 8–10 行内）：

- **四区拆分原则**：大情绪与特写镜头按「眉区（brows）→ 眼区（eyes/eyelids）→ 颧颊（cheeks）→ 嘴与下颌（mouth/jaw）+ 呼吸/吞咽/肩颈」写具体可见形变，**严禁在输出中出现 `AU12` 等 FACS 编号**。
- **高频易混情绪锚点**：
  - **真笑 vs 假笑**：真笑必须带动眼尾眯起、脸颊上提与嘴角上扬；礼貌/冷笑只提嘴角（或单侧牵动），眼区保持冷峻不眯。
  - **惊讶 vs 恐惧**：惊讶 = 抬眉 + 睁大双眼 + 嘴微张/下颌放松；恐惧 = 在此基础上叠加眉心紧拧、肩膀后缩、急促倒吸气。
  - **疼痛 / 惊吓 / 崩溃（H3 重点弱项）**：明确写出强烈物理信号——五官骤然挤紧、眼眶泛红泪水滑落、喉结/胸口剧烈起伏、手部抓紧衣物或捂住痛处、身体蜷缩或猛地后撤，并给出「起势 → 峰值停顿 → 回落」节奏。
  - **隐忍 / 压抑 / 面无表情**：下颌咬肌绷紧、双唇抿成直线、眼睑压低、喉结轻滚，全程零「笑/哭」等反义词根。

---

## 分阶段实施与验证计划（合并分支后执行）

| 阶段 | 内容 | 验证方式 | 检查点 / 门禁 |
|---|---|---|---|
| **阶段 1** | 实现 **项 1（台词语速与镜长预算 WARN 预检）**：`h3ref/film.py` 纯函数（`normalize_shots` + `check_dialogue_budget`）+ CLI `run()`/`report()` 接入 + `workbench/references.py` & 创建页 Warning 卡片 + 全三套中英文 i18n | 新增 `tests/test_dialogue_budget.py`（覆盖中文/`ja`/`ko`/英文口径、常规/慢速含 `低声` 与英文词形/超长句/`dialogue_speech_unset`/`count == 0` 跳过、说话人前缀循环清洗与 `12:30` 保护、标点去重停顿、`normalize_shots` 非就地修改与草稿非字符串/非法时间容错、CLI `run()`/`report()` 集成、工作台 `/api/film/references` 与 `Accept-Language` 本地化），补充前端语言切换即时重绘与 `state.filmWarnings` 清空测试，并跑全量离线测试含 `tests/test_locale_parity.py` 全绿 | 零付费、确定性改动，测试全绿即可提交（可单独 commit） |
| **阶段 2** | **模态解耦单变量 A/B 实测（独立实验脚本 `scripts/ab_eval.py`，支持 `--arm`、`--fixture`、`--dry-run`、`--max-cny`、`--resume` 与 `ab_state.json` 断点防重付，按臂隔离改写缓存、仅共享冻结首帧并记录规则/模型指纹）**：① 纯生图臂（15–20 个对抗/常规镜头）——**Arm 3A**（`NEUTRAL_WORDS`）、**Arm 3B**（`describe_composition` 动作转译示例）、**Arm 3C-neg**（`TAIL` 否定句）与 **Arm 3C-light**（`TAIL` 布光）；② 纯视频臂（复用冻结首帧 + 常规对照镜）——**Arm 3D**（`rewrite.RULES` 第 5 条高激活视觉否定词 + 2 个正常否定对照镜）与 **Arm 4**（FACS 四区肌肉指引 + 克制表演对照镜），同步验证 `d2` few-shot 样例对比；③ 跨模态臂 **Arm 2A**（项 2 双态资产覆盖 + 图像/视频强锁对齐 + `rewrite.RULES` 规则 9）；LLM 步骤双测非 Claude 默认模型（DeepSeek/Gemini）与 Claude | 生成零依赖离线单页盲评板 `ab_review.html` + `answer_key.json`（随机匿名左右对照、先评画质/叙事忠实度再揭晓费用与审核结果），按「可交付验收总成本 + 重试次数 + 叙事忠实度 + 审核通过率（0 新增 `ContentPolicyError`）」评判；阶段 3 合入前执行 1 次组合态回归门禁（系统提示词累计增幅 ≤ +20%、零规则冲突） | **停下等用户确认**：逐臂决定合入或裁剪（含 `d1`/`d2`），严禁未通过单臂混入默认流程 |
| **阶段 3** | 仅合入通过阶段 2 验证的试验臂代码改动；补齐单测（含「零死字」回归、资产偏离覆盖文案、`RULES` 规则断言，以及若阶段 2 Arm 3D 决定启用时的窄负向词校验）；在 [SKILL.md](../../SKILL.md) 补充一句上下游解耦说明（支持外部编剧/分镜工具产出 `film.json` 交由本工作台出片，纳入本次范围） | 跑完整测试套件、工作台离线演示全流程冒烟验证，确认无破坏历史任务 `state.json` / `composition v2` 缓存兼容 | 全部测试与冒烟全绿后汇报交付 |

---

### 用户审批采纳的扩展与挑战（gate_rev=1: `uc1b` / `uc2b` / `uc3b`）

1. **`uc1b` · H3 历史成片语速校准前置步骤（防样本内过拟合 + 有效修复建议）**：
   - 在阶段 1 锁定默认 `DIALOGUE_RATE_CONFIG` 阈值前，按预设抽样与人工标注协议抽样 10–15 条历史 H3 成片（覆盖正常/超预算、常规/慢速样本，记录分段边界、实际生成秒数 `film.plan()` 与实际口播起止区间以将首帧惯性与纯语速解耦），区分调参集与留出验证集并报告混淆矩阵计数（`TP/FP/TN/FN`）；若本地样本不足无法形成独立留出验证，保留启发式初值并在 `meta.calibration_status` 显式标为 `"provisional"`（不伪装为已泛化校准）。
   - 区分长句可读性提示（`dialogue_long_line`）与超预算修复建议（`dialogue_over_budget` / `dialogue_slow_over_budget`）：同总时长内硬拆成两镜并不能增加有效口播时间，且原镜已达 15s（`MAX_SECONDS`）时延长原镜无法突破 `film.plan()` 上限——超预算建议必须明确「增加总有效生成预算（拆分为总预算足够的多段）或精简台词」，无可靠分句边界时不给具体拆句；绝不自动改写 `#film-json` 里的用户时间戳。未实测语种继续显式标为未校准。
2. **`uc2b` · 版本化黄金回归集与同模型退场规则（`evals/golden_shots/`）**：
   - 将阶段 2 的痛点镜头夹具（含 Arm 2A 多镜连续 `locked_look` 冲突夹具）、输入 manifest、盲评基线与结论沉淀为版本化目录 `evals/golden_shots/`（仅存轻量 JSON 夹具、脱敏规则指纹与文字评分记录，不把大体积二进制图片/视频塞进 git 仓库），由 `evals/golden_shots/manifest.json` 明确记录：
     - 不可变 `run_id`、完整生成配置（工作流/采样预设 `config.py:89-100`、`H3_REWRITER`、具体上游模型版本或显式 `"unpinned"`）、实际改写后提示词 `sha256` 摘要，以及仓库外参考图/冻结首帧/评分视频的相对路径与内容 `sha256`（版本未锁定或外部素材无法校验时显式标为 `reproducibility: "unreproducible"`）；
     - 每个夹具覆盖的缺陷类别与预期对照（防穿帮双态含多镜 `locked_look`、安全转译、视觉否定、FACS 大情绪、克制表演对照镜）；
     - **同模型退场条件（Retirement Rule）**：必须锁定**同一新模型 + 同一输入夹具 + 同一生成配置**，对比「当前完整补丁集」与「仅移除目标补丁」两组——仅当各缺陷类别均满足非劣标准（不允许总体胜率掩盖单类缺陷退化）、0 新增 `ContentPolicyError`、交付总成本不升且重跑组合态回归门禁通过后，才可安全移除该过期补丁。
3. **`uc3b` · CLI 独立零成本预检子命令（`python h3ref.py preflight`）**：
   - 在 [h3ref.py](../../h3ref.py#L181-L225) 新增只读子命令：`python h3ref.py preflight --film <path|-> [--json] [--fail-on-warn]`。
   - **导入隔离与零副作用保证**：`h3ref.py` 在顶层导入 `h3ref.config` / `h3ref.film` / `rh` / `comfy` **之前**抢先分派 `preflight` 子命令，仅导入纯标准库 `h3ref.preflight` 与去 `config` 化的 `h3ref.i18n`，绝不读取 `.env` 或任何 API Key、绝不解析 `H3_STEPS` 等无关生成环境变量（非法 `H3_STEPS` 不影响预检），仅从 `os.environ` 读取 `H3_DIALOGUE_RATE_CONFIG`（须满足 `lstrip().startswith("{")` 或常规文件 `S_ISREG` 且 `<= 64KB`）与 `H3_LANG`，不检查图片存在、不建目录、零网络/写盘；`--film -` 上限 32 MB 并将 `OSError`/`IsADirectoryError`/`UnicodeDecodeError` 转为结构化 `stderr` + 退出码 `1`。
   - **优先级与 Agent-Ready 退出码契约**：显式 `preflight` 子命令**始终忽略 `H3_SKIP_PREFLIGHT=1`**（该开关仅跳过流水线自动预检），同时通过 `meta.effective_config_sha256` 披露当前生效阈值；默认人类可读摘要输出至 `stdout`，`--json` 输出完整 `{"status", "warnings", "meta"}` 至 `stdout`，所有错误严格输出至 `stderr`；退出码：`0` = 校验完成且无触发失败条件、`1` = 输入/JSON/`FilmValidationError`/配置非法、`2` = `--fail-on-warn` 且存在告警或 `status != "complete"`。
   - **随仓库示例与文档同步**：新增零外部图片依赖示例 `examples/dialogue-budget.json`（验收测试锁定默认配置 `H3_DIALOGUE_RATE_CONFIG=""`），并在 `README.md`、`docs/workbench.md#dialogue-preflight` 与 `SKILL.md` 顶部补充 1 命令 Quickstart 与预期 JSON 输出，将 CLI/Agent TTHW 压缩至 `< 1 min`。

---

## 涉及文件清单（预计）

- 核心逻辑：`h3ref/preflight.py`、`h3ref/i18n.py`、`h3ref/film.py`、`h3ref/frames.py`、`h3ref/staging.py`、`h3ref/rewrite.py`、`h3ref.py`
- 工作台与界面：`workbench/references.py`、`workbench/server.py`、`workbench/jobs.py`、`workbench/storyboard.py`、`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`（复用 `.notice.warning` 并补齐 `border: 1px solid #eddfb9` 及列表间距）
- 国际化：`h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json`、`workbench/static/messages/{zh-CN,en}.json`
- 测试、示例与文档：`tests/test_dialogue_budget.py`、现有 `frames` / `rewrite` / `workbench` / `test_locale_parity.py` 测试、`examples/dialogue-budget.json`、`evals/golden_shots/`、`docs/workbench.md`（`#dialogue-preflight` 锚点与 `curl` 示例）、`README.md`、`SKILL.md`

## 评审采纳约束（Accepted Obligations）

<!-- autoplan-accepted:ceo -->
- Extract non-mutating pure helper `h3ref/film.py:normalize_shots(shots, *, strict=False)` matching `load()` filtering/rounding (`max(0.5, length)`, default `2.0s`, empty `action` skipped), safely handling draft/missing/invalid `shots` (non-list, non-dict, non-string `action`/`line`/`speech`, `bool`/`NaN`/`Inf` timestamps) when `strict=False`, and preserving `result["film"]["shots"]` untouched in `workbench/references.py:describe_references()`; verify CLI/workbench parity, draft resilience, and non-mutation in `tests/test_dialogue_budget.py`.
- Enforce deterministic `check_dialogue_budget()` specification: centralize speech-rate thresholds in a single top-level `DIALOGUE_RATE_CONFIG` dict in `h3ref/film.py`, frame i18n copy as heuristic speaking-duration risk estimates (noting `film.py:123` segment rounding and `README.md:117` opening-frame inertia, and marking non-`zh` defaults uncalibrated); normalize `speech` via `.strip().lower()` and match primary subtag `speech.split("-", 1)[0]` (non-str -> `""`); emit single `dialogue_speech_unset` (`shot: null, affected_shots: [1-based normalized indices]`) when `speech` is empty and any normalized shot has non-empty `line.strip()`; run slow-pace cue detection (CN including `低声`/`轻声` + EN `\b(?:whisper(?:s|ing|ed)?|murmur(?:s|ing|ed)?|softly|slowly|chok(?:e|es|ing|ed)|sob(?:s|bing|bed)?)\b`) on raw `action` + raw `line` BEFORE looped speaker-prefix stripping (protecting `12:30` numeric colons); branch non-space scripts (`zh`/`ja`/`ko`, `unit="chars"`) vs space-delimited scripts (`en`/`es`/`pt`/others, `unit="words"`, word regex `[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:'[0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*|[\u4e00-\u9fff]` excluding lone apostrophes); skip per-shot warnings when `count == 0` after prefix stripping; apply deduplicated regex punctuation pauses across all languages; enforce per-shot mutual exclusion `dialogue_slow_over_budget > dialogue_over_budget > dialogue_long_line` using `rate = slow_rate if is_slow else regular_rate` for `min_seconds`, `duration = round(end - start, 2)`, 1-based normalized `shot` indices, and prefix-stripped `line_preview` truncated to `text[:39] + "…"` when `> 40`; verify all 4 schemas and edge cases in `tests/test_dialogue_budget.py`.
- Wire CLI & workbench cleanly: `film.run()` logs warnings before `stage_frames()`; `film.report()` keeps its signature and calls `check_dialogue_budget(film)` directly (omitting the section when empty); `POST /api/film/references` passes `lang=language()` into `describe_references(script, *, lang="zh-CN")` which delegates directly to `film.check_dialogue_budget(script)` once and returns `warnings` with localized `message` plus structured fields; frontend `create.js` renders warnings dynamically from structured fields via `workbench/static/messages/{zh-CN,en}.json` `t(...)` (falling back to `w.message`) inside `.notice.warning` (with `border: 1px solid #eddfb9`) mounted after `#film-parse-status` so language switches repaint immediately, clearing `state.filmWarnings` on empty/error/reset; include `chars`/`words` unit localization keys across all 3 i18n suites and verify via `tests/test_locale_parity.py` and UI/API tests.
- Distinguish dual asset-override states in Item 2 (`h3ref/frames.py:describe_composition`, `h3ref/frames.py:209,217`, `h3ref/film.py:251`, `h3ref/rewrite.py:32`, `h3ref/rewrite.py:RULES` rule 9): when the character enters the shot already stripped/altered, specify the bare/inner state in `placement_and_pose`; when the removal action occurs DURING the shot, preserve initial wear in the opening frame (per `h3ref/frames.py:105`) and express removal progression + end state in `rewrite.py:RULES` rule 9; align `frames.py:209,217`, `film.py:251`, and `rewrite.py:32` so static reference-look constraints yield to explicit shot-level overrides; refine exact `NEUTRAL_WORDS` diff without hardcoded `衣袖` (`流血→沾染暗红痕迹`), remove misclassified `h3ref/film.py:RULES` from image-side Item 3.2, and retain the 1-sentence upstream/downstream decoupling note in `SKILL.md` in Phase 3 scope.
- Enforce modality-decoupled Phase 2 A/B screening + combined-state regression gate on an experimental branch/script: (a) pure-image arms — Arm 3A (`NEUTRAL_WORDS`), Arm 3B (`describe_composition` safety examples), Arm 3C-neg (`TAIL` negative clause) & Arm 3C-light (`TAIL` lighting wording) — tested image-only across 15–20 adversarial/normal shots (0 new `ContentPolicyError`, narrative fidelity preserved); (b) pure-video arms — Arm 3D (`rewrite.RULES` rule 5 scoped strictly to high-activation visual roots + 2 normal-negation control shots) and Arm 4 (FACS scoped to script-demanded high emotions + restrained-acting control shots) — reusing frozen cached frames; (c) cross-modal Arm 2A verifying both anti-duplication and action completion; evaluate LLM steps on both Workbench default non-Claude model (DeepSeek/Gemini) and Claude; judge by deliverable acceptance cost (cumulative cost + retries + narrative fidelity + moderation pass rate) and run one combined-state regression pass before Phase 3 merge enforcing cumulative system-prompt growth <= +20% and zero inter-rule conflict.
- Provisional Taste Decisions: (`d1`) keep visual-negation positive reframing in `rewrite.py:RULES` rule 5 only for Phase 2 Arm 3D A/B without adding default hard-fail regexes to `rewrite.py:problems()` unless Arm 3D empirically requires narrow zero-FP patterns; (`d2`) compare updating `prompts/h3_examples.md` few-shot demonstrations alongside `rewrite.RULES` expansion in Phase 2 Arm 3D/4 to check whether example-driven guidance achieves higher compliance at lower rule length.
<!-- /autoplan-accepted:ceo -->

<!-- autoplan-accepted:design -->
- Fix keystroke layout-thrashing bug and define full 7-state UI lifecycle in `workbench/static/create.js`: only clear `state.filmWarnings` when `clearSlots === true` (file import/reset), empty textarea, or JSON/API error; during 250ms input debounce and in-flight `/api/film/references` requests (`state.filmParsing === true`), keep the previous warning card rendered with `.is-stale` (`opacity: .6`, `aria-busy="true"`) and guard updates with `revision === state.filmRevision`; hide card cleanly on 0 warnings (announcing "preflight clean" once in `#film-parse-status` when transitioning from `>0` to `0`); keep `#film-warnings` inside `#film-fields` so `switchWorkflow()` / `clearNotices()` never wipes it; show warnings identically in Demo and Live modes; surface partial-check counts (`skipped_shots`, `default_duration_shots` returned by `describe_references` in optional `preflight_meta`) when non-zero so clean warnings are never misread as full-script validation.
- Close the edit-and-verify repair loop without auto-mutating JSON: include 1-based original array position `raw_shot` on per-shot warnings and `affected_raw_shots` on `dialogue_speech_unset` (removing redundant greenfield `chars` field — keep `count` + `unit` only), display `(JSON #raw_shot)` whenever `raw_shot != shot`, add a non-mutating "Locate in script" button per shot warning that selects the matching shot/line in `#film-json` via `setSelectionRange` + `focus()`, truncate `affected_shots` display cleanly when `> 6` (first 5 + total count), and include copyable `"speech": "zh"` guidance + cascade notice ("per-shot pacing checks skipped until `speech` is set") in `dialogue_speech_unset`.
- Eliminate `2.0s > 2.0s` rounding contradictions and lock heuristic copy framing: compute `min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10` so every over-budget warning strictly satisfies `min_seconds > duration` (enforced by unit test in `tests/test_dialogue_budget.py`); frame copy across all 3 i18n suites as heuristic pacing risk estimation ("估算约 {min_seconds}s / 当前镜长 {duration}s", forbidding "至少"/"保证念完"); separate causal phrasing for `dialogue_long_line` (too many chars/words in one shot -> split advice, not over-duration) vs over-budget warnings; append an uncalibrated-threshold note only when `speech` is non-`zh`; format `report.md` preflight section as a top Markdown table right after the basic summary and log uniform `preflight [WARN] shot#N ...` lines in `run.log`.
- Enforce `DESIGN.md` visual hierarchy, accessibility, and responsive contract: mount `<section id="film-warnings" class="notice warning film-warnings" aria-labelledby="film-warnings-title" hidden>` between `#film-parse-status` and `.film-reference-section`; add `border: 1px solid #eddfb9` and `.notice.warning .hint { color: #81612a }` (regression-checking `base.html:36,43` `#runtime-drive-box`/`#runtime-confirm`); sort warnings by risk tier (`dialogue_slow_over_budget` / `dialogue_over_budget` before `dialogue_long_line`, ascending `shot` within tier); format numeric counters in `--mono` tabular font; announce warning count changes only via `#film-parse-status` (`aria-live="polite"`, never `role="alert"` on the card); preserve `<details>` expand state and keyboard focus across `zh-CN` ↔ `en` switches; localize unknown warning `code` via generic current-language message with server `w.message` inside collapsible technical details; ensure `>= 44x44px` touch targets on narrow viewports, 4.5:1 text / 3:1 UI contrast, single-column wrapping down to `320px`/`420px` with zero nested scroll or horizontal overflow; append a quiet one-line reminder + jump link to `#film-warnings` inside `#submit-hint` when `state.filmWarnings.length > 0`.
- Specify a zero-dependency offline Phase 2 blind-evaluation packet (`ab_review.html` + `answer_key.json`) generated by the Phase 2 experiment script: tabbed by arm, randomized anonymous Left/Right A/B pairing with matched aspect/playback controls, target action brief, Better/Tie/Worse + failure-reason recording, cost/moderation reveal only after visual vote, and exportable JSON decision log (composable with `uc2` golden set if approved).
- Provisional Taste Decision (`d3`): Progressive disclosure shape for `#film-warnings` — compact 1-line header (`台词语速预检 · 共 N 条提醒` + `仅提示，不阻止继续` badge) + top **3** highest-priority warnings inline + native `<details>` for remaining items when `N > 3` (balancing immediate visibility for 1–3 warnings against keeping `.film-reference-section` above the fold for 15–25 warnings; alternatives: Codex's always-collapsed 1-line summary bar vs Native's top-5 inline, queued for Phase 4 Gate).
<!-- /autoplan-accepted:design -->

<!-- autoplan-accepted:dx -->
- Eliminate both DX CAP4 blockers (never signal malformed input or uncovered scripts as "preflight clean"): extract `h3ref/film.py:inspect_dialogue_budget(film, *, rate_config=None) -> dict` as the single source of truth returning `{"status": "complete"|"partial"|"unsupported"|"invalid", "warnings": [...], "meta": {"shots_invalid", "shots_missing", "total_raw_shots", "checked_shots", "skipped_shots", "default_duration_shots", "unsupported_script_shots"}}` (with `check_dialogue_budget()` as a thin wrapper); raise `FilmValidationError(ValueError)` on non-dict `film` in SDK, return HTTP 400 RFC 9457 when `film.shots` is present and non-list in `describe_references()`, emit `dialogue_shots_invalid` in `strict=False` fallback, emit `dialogue_unsupported_script` when non-empty post-strip dialogue has uncovered non-Latin/non-CJK letters (`count == 0`), require `status === "complete" && warnings.length === 0` before announcing "preflight clean", and restrict bare-colon speaker stripping so non-speaker imperatives like `记住：千万别回头` are not stripped.
- Preserve `raw_shot` / `raw_shot_index` (0-based) and default-duration provenance across the full CLI `load() -> run() -> report()` chain without leaking private keys into `state.json` or `result["film"]`: carry shot provenance from initial normalization through `load()` into `inspect_dialogue_budget()` so CLI and Workbench produce identical `raw_shot` and `preflight_meta` (verified by a cross-surface parity test where raw shot #1 has empty `action` and raw shot #2 is over budget); share `normalize_speech(raw)` across `inspect_dialogue_budget()`, `compile_prompt()`, and `stage_check()`; unify `rate_band` to `"regular"|"slow"` across all per-shot warnings; print partial/unsupported summary lines in CLI `run.log` and `report.md` when `skipped_shots`, `default_duration_shots`, or `unsupported_script_shots` > 0.
- Meet the agent-ready error contract (resolving all 6 CAP6 gaps): generalize `workbench/server.py:274` `segment_problem()` to `api_problem()` and onboard `POST /api/film/references` for both 400 validation errors (`code`, `field`, `fix`, `doc_anchor: "docs/workbench.md#dialogue-preflight"`, compatible `error`) and 500 unexpected errors (redacted local log + `instance` correlation ID + `recovery` with `side_effects: "none"` + `context`); introduce typed `FilmValidationError(ValueError)` (`code`, `shot_index`, `field`, `actual`, `fix`, `doc_anchor`) for `normalize_shots(..., strict=True)` (citing `H3_MAX_SHOTS` on limit errors, formatted cleanly to `stderr` + non-zero exit in `h3ref.py`); enrich `rewrite_exhausted` (`REWRITE_VALIDATION_EXHAUSTED`, backend/model context, attempt spend notice, actionable fix) and `ContentPolicyError` (`FRAME_MODERATION_BLOCKED`, `frame_key`, `IMAGE_MODEL`, both Workbench and CLI fixes); redact/sanitize `line_preview` control chars in logs; forbid any `d1` narrow regex from entering `cached_problems()` so manual H3 override stays intact; log `NEUTRAL_WORDS` hit summary in `run.log`.
- Harden Phase 2 experiment runner DX & upgrade contract: specify `scripts/ab_eval.py` (`--arm`, `--fixture`, `--dry-run`, `--max-cny`, `--resume`) with resumable `ab_state.json` (recording `(arm, fixture, provider)` before dispatch to prevent double-spend), strict per-arm rewrite cache isolation (sharing only frozen frames), recorded rule/model fingerprints, API-key redaction in logs/`ab_review.html`/`answer_key.json`, and `stderr` + non-zero exit error contract; explicitly extend `docs/workbench.md:163` upgrade guarantees so existing jobs preserve cached prompts/frames by default while documenting explicit opt-in re-rewrite/regenerate steps and cost impact (verified by legacy-job resume test).
- Ship complete public documentation & copy-paste examples alongside code: add `<a id="dialogue-preflight"></a>` in `docs/workbench.md` (4+ warning codes table, `complete`/`partial`/`unsupported` semantics, `POST /api/film/references` copy-paste `curl` + expected response), link from `README.md`, add preflight + `speech` + Item 2/3/4 authoring checklist to `SKILL.md`, and add a lightweight one-click "Copy" button for `"speech": "zh"` in `dialogue_speech_unset` UI guidance.
- Provisional Taste Decision (`d4`): Provide runtime escape hatches for `DIALOGUE_RATE_CONFIG` — Python `rate_config` kwarg plus optional `H3_DIALOGUE_RATE_CONFIG` (JSON/path) and `H3_SKIP_PREFLIGHT=1` env overrides so non-`zh` or fast-dialogue creators can calibrate/silence warnings without editing code (alternative: keep code-constant-only + Python kwarg to avoid extra env surface, queued for Phase 4 Gate).
<!-- /autoplan-accepted:dx -->

<!-- autoplan-accepted:eng -->
- Protect historical job H3 prompt cache from silent invalidation (reconciling prior DX `compile_prompt()` note with Eng F2 + Codex C7): keep `h3ref/film.py:compile_prompt()` (`film.py:33,251,264,299`) 100% byte-identical for existing/unversioned jobs (do NOT call `normalize_speech()` inside default `compile_prompt()`, so historical `"zh-CN"`, `"ZH"`, `"zh_CN"`, and `"   "` preserve exact `zh` hash and reuse `rec['h3']`); use `normalize_speech()` only in `h3ref/preflight.py:inspect_dialogue_budget()` and `film.stage_check()` (or future opt-in `prompt_compiler_version >= 2`); converge Item 2 video-side asset-override precedence into `h3ref/rewrite.py:32` & `rewrite.RULES` rule 9 (and `h3ref/frames.py:209,217` on image side); require Arm 2A to include multi-shot sequential fixtures (Shot 1 wearing -> Shot 2 removed/held) and, if `frames.py:95,104` / `film.py:444` / `staging.py:200` `locked_look` conflicts visually, resolve precedence without breaking cross-shot `locked_look` identity reuse; verify via legacy 0-spend resume regression tests covering non-canonical `speech` values.
- Isolate `uc3b` zero-cost CLI (`python h3ref.py preflight --film <path|-> [--json] [--fail-on-warn]`) from `.env`, API keys, and unrelated generation config (Codex C1/C9 + Native F5): implement pure-stdlib `h3ref/preflight.py` (re-exported by `h3ref/film.py` for full SDK compatibility), remove `from . import config` from `h3ref/i18n.py:7` (read `os.environ.get("H3_LANG", "").strip()`), and dispatch `preflight` in `h3ref.py` BEFORE importing `h3ref.config` / `h3ref.film` / `rh` / `comfy` so `.env` is never read and corrupt `H3_STEPS` / `RUNNINGHUB_*` never crash preflight; explicit `preflight` always ignores `H3_SKIP_PREFLIGHT=1` while reading/disclosing `H3_DIALOGUE_RATE_CONFIG`; cap `--film -` stdin at 32 MB and convert `OSError`/`IsADirectoryError`/`UnicodeDecodeError` to structured `stderr` + exit `1`; exit `0` default, `1` input/JSON/`FilmValidationError`/config error, `2` `--fail-on-warn` with any warning or `status != "complete"`; ship `examples/dialogue-budget.json` pinned to default config in acceptance tests + isolated subprocess test.
- Fix end-to-end provenance timing, API decoupling, and exception boundaries (Native F1/F3/F4 + Codex C6): in `workbench/jobs.py:502` `create()`, compute the full `PreflightResult` snapshot (including `meta.estimator_version`, `meta.effective_config_sha256`, `meta.calibration_status`) from raw `payload.get("film")` BEFORE `validate(payload, self.root)` (which already invokes `film.load()` via `workbench/inputs.py:148-152`), write `inputs/preflight.json` atomically in `_commit()`, and read/log it in `workbench/storyboard.py:298` (`operation="frames"`) as well as CLI `film.run()` and `film.report()` (legacy jobs without `inputs/preflight.json` fall back with `provenance_unknown` and `recomputed_with_config_sha256`); add `include_preflight=False` to `workbench/references.py:describe_references()` so `GET /api/film/example` and `bind_references()` -> `POST /api/jobs` remain decoupled from preflight and `H3_DIALOGUE_RATE_CONFIG`, enabling `include_preflight=True` ONLY on `POST /api/film/references`; generalize `workbench/server.py:274` `segment_problem()` into `api_problem(..., title_key, doc_anchor)` handling 400/413/415/500 (with redacted `storage/server.log` + `instance` + `error` compat key); widen `except (SystemExit, FilmValidationError)` across ALL `film.load()` callers (`workbench/demo.py:72`, `workbench/storyboard.py:25,73,317`, `workbench/jobs.py:584`, `workbench/server.py:715`, `h3ref.py`).
- Lock arithmetic invariants, bounds, script/prefix sanitization, and security hardening (Native F4/F5 + Codex C8): lock the over-budget invariant to `min_seconds > effective_duration` (`effective_duration = min(duration, float(MAX_SECONDS))` with `MAX_SECONDS = 15.0`, integer-ms pauses + `1e-9` tolerance), always emit `effective_duration` and dedicated 3-suite i18n copy when `effective_duration < duration` ("有效生成预算 15s（原镜长 20s）"); bound raw timestamps in `normalize_shots()` to `[0.0, 3600.0]` (`inputs.py:141`) and treat half-specified or out-of-range `start`/`end` as `2.0s` fallback counted in `default_duration_shots`; scan stripped text independently of `count == 0` for uncovered scripts (`"Привет 2026"`), normalize full-width ASCII BEFORE prefix matching, support `['’]`, CJK Ext A (`\u3400-\u4dbf`), `々` (`\u3005`), and `[-_]` locale tags; harden dynamic `cast` speaker stripping (validate `list`/`dict`/`aliases`, drop empty names, cap length `<= 64`, sort longest-first so `林医生` precedes `林`, case-insensitive ASCII role tags, strictly paired brackets capped at 5 loops); harden `H3_DIALOGUE_RATE_CONFIG` (inline JSON only when `val.lstrip().startswith("{")`; file path requires `stat.S_ISREG` and `st_size <= 65536`; finite positive non-bool floats; never echo file text; never add to `server.py:FIELDS`); bound `POST /api/film/references` `shots` to `max(2000, config.max_shots() * 2)`; add `sanitize_line_preview()` and `escape_markdown_cell()`.
- Harden frontend a11y, polling stability, and Phase 2 runner/calibration/golden-set contracts (Native F6/F7/F8/F9 + Codex C2/C3/C4/C5): mount dedicated `.sr-only` `#film-preflight-live` (`aria-live="polite"`) decoupled from `#film-parse-status`; render `#submit-hint` echo as a persistent child guarded by `state.kind === "film" && state.filmWarnings.length > 0` updating only when `(kind, baseKey, warningCount, lang)` changes (no focus loss on 1.8s polling); implement depth-aware JSON lexical scanner (`depth===1` `"shots"` -> `depth===2` `raw_shot_index` -> `depth===3` `"line"`, with `scrollTop` centering); scope warning list CSS under `.film-warnings` and cap `<details>` at 100 items; make `rewrite.backend()` read `H3_REWRITER` dynamically (or accept `rewriter_override`), isolate any future `d1` regex strictly to `rewrite.llm_rewrite_problems()` (protecting manual H3 at `film.py:527` + `workbench/segments.py:561,570,579`), reuse `frames.draw()` & `backend.estimate_cny()`, compile `frames.neutralize_text(text, *, neutral_words=None)` dynamically with zero logging inside pure `frames.prompt()`, enforce zero `innerHTML` + anonymized `blind/<pair_id>_L/_R` media paths + standalone 8-`SECRET_NAMES`/`Bearer`/`sk-` redaction + HTML-safe JSON escaping in `ab_review.html`, and atomic `ab_state.json` (`submission_unknown` + lock + in-flight `--max-cny` reserve + fingerprint check); enforce `uc1b` pre-specified sampling/labeling protocol + tuning/holdout split (or `"provisional"` stamp) + confusion matrix + budget-valid over-budget advice respecting 15s cap; enforce `uc2b` `evals/golden_shots/manifest.json` reproducibility metadata (`run_id`, workflow/sampling/model pin, prompt `sha256`, external asset pointers+hashes, `reproducibility` flag) and same-model per-defect-category non-inferiority retirement rule with combined-state gate re-run.
<!-- /autoplan-accepted:eng -->
## Review record

<!-- autoplan-baseline-edits:ceo {"sourceSha256":"3dcff904721310ffaac35b36664eb860abfb95618db4f4b40ce7a2342a2be376","replacements":[{"oldText":"确认画质/服从度净正向、审核通过率不降、费用无明显上涨后再升级为默认规则。","newText":"确认 APIMart 审核通过率不低于基线、3–5 镜人工盲评净正向、单镜/单段提示词字符与单镜估算费用增幅 ≤ 15% 后再升级为默认规则。"},{"oldText":"所有新增提示文案必须写入 `h3ref/messages/{zh-CN,en}.json` 与 `workbench/messages/{zh-CN,en}.json`，严禁硬编码。","newText":"所有新增提示文案必须写入全部三套语言包 `h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json` 与前端 `workbench/static/messages/{zh-CN,en}.json`，严禁硬编码。"},{"oldText":"- **输入前提**：接收经 `film.load()` 归一化时间后的 `film`（每个镜头具备 `start` / `end` / `action` / `line`），及 `speech` 语言代码。\n- **角色前缀清洗**：剥离常见说话人标记，避免把人名计入台词字数，例如：\n  - `（小林）这……是我的？` / `(小林) ...` / `【小林】...` / `小林：...`\n- **语言分流（防止英文误报）**：\n  - `speech == \"zh\"`（或未指定但文本含中文字符）：统计**可发音字符数**（汉字 + 数字 + 连续英文词按合理音节或单字口径，不含标点与空白）；\n  - 其他语种（如 `en` / `es` / `pt`）：按**单词数**估算（英语最快约 3.2 词/秒，慢速约 2.0 词/秒），不按字母数计算，避免严重误报；无 `speech` 或空 `line` 直接跳过。\n- **标点停顿预算**（中文口径）：\n  - `，` / `、` / `,`：+0.2s\n  - `。` / `？` / `！` / `.` / `?` / `!`：+0.4s\n  - `……` / `…`：+0.8s\n  - `——`：+0.6s\n- **双档语速判定（宁放过不误杀短剧快节奏）**：\n  - **常规/最快可行档（6.0 字/秒）**：`min_seconds = chars / 6.0 + pause`。仅当 `min_seconds > duration` 时发出 WARN（`code=\"dialogue_over_budget\"`），属于「真念不完」。\n  - **慢速语气触发档（3.5 字/秒）**：当该镜 `action` 或 `line` 明确包含慢速节奏词（`耳语|低语|气声|哽咽|一字一顿|一字一句|断续|拖长|放缓`）时，按 `min_seconds = chars / 3.5 + pause` 判定，超镜长发 WARN（`code=\"dialogue_slow_over_budget\"`）。\n  - **超长句拆镜提示**：中文单镜台词可发音字数 `> 24` 字（或英文 `> 18` 词）且未触发上面超预算时，发出轻量建议 WARN（`code=\"dialogue_long_line\"`），建议拆成正反打或插入反应镜。\n- **返回结构**（稳定、可序列化，便于前端渲染与测试）：\n  ```json\n  {\n    \"code\": \"dialogue_over_budget\",\n    \"severity\": \"warn\",\n    \"shot\": 3,\n    \"duration\": 2.0,\n    \"chars\": 16,\n    \"min_seconds\": 3.1,\n    \"rate_band\": \"fast\",\n    \"line_preview\": \"这……是我的？\"\n  }\n  ```","newText":"- **共享镜头归一化（非就地修改 + 草稿容错）**：在 `h3ref/film.py` 新增纯函数 `normalize_shots(shots, *, strict=False) -> list[dict]`，返回全新浅拷贝列表、**绝不就地修改入参**。沿用 `film.load()` 规则：过滤空 `action` 镜头、缺失 `start`/`end` 默按 `2.0s`、时长下限 `max(0.5, length)`、`round(..., 2)`。`strict=True`（供 `load()` 调用）对空 `shots`、超上限或非法时间抛错；`strict=False`（供 `check_dialogue_budget()` 调用）遇缺失/非 list `shots`、非 dict 项、非 `str` 的 `action`（视为无效并跳过）、或 `bool`/非有限数（`NaN`/`Inf`）/不可解析的 `start`/`end`（回退 `2.0s`）时安全容错，不抛 500。非 `str` 的 `line` 或 `speech` 统一安全视为 `\"\"`，`speech` 做 `.strip().lower()` 后再判空与分流。\n- **`speech` 缺失保护**：若归一化后 `speech` 为空/未设置且至少 1 个归一化镜头含非空 `line.strip()`（与 `film.py:293` `compile_prompt()` 行为一致），跳过逐镜语速计算，发出单条整片级告警 `code=\"dialogue_speech_unset\"`；若全片无非空 `line`，返回 `[]`。\n- **慢速语气检测顺序（先检测、后清洗前缀）**：慢速关键词检测必须在**原始 `action` 与原始 `line`** 上执行（早于剥离括号/冒号说话人前缀，防止 `（哽咽）...` 或 `（小林，低声）...` 中的表演提示被洗掉）。关键词覆盖中文（`耳语|低语|低声|轻声|气声|哽咽|一字一顿|一字一句|断续|拖长|放缓`）与英文（`\\b(?:whisper(?:s|ing|ed)?|murmur(?:s|ing|ed)?|softly|slowly|chok(?:e|es|ing|ed)|sob(?:s|bing|bed)?)\\b`，忽略大小写）。\n- **说话人前缀清洗（确定性循环）**：在行首循环剥离以下前缀直至不再匹配：① 成对全/半角括号及方括号标签（`^[（(\\[【][^）)\\]】]{1,20}[）)\\]】]\\s*`）；② 非数字开头的说话人冒号前缀（`^[^：:\\s\\d][^：:\\s]{0,11}[：:]\\s*`，避免误剥 `12:30` 等时间或句中冒号）。\n- **脚本族分流、确定性计数与 `count == 0` 跳过**：\n  - 非空格脚本（`speech` 为 `zh` / `ja` / `ko`，`unit=\"chars\"`）：汉字（`\\u4e00-\\u9fff`）、日文假名（`\\u3040-\\u30ff`）、韩文音节（`\\uac00-\\ud7af`）与单个数字（`[0-9]`）各计 `1`，每个连续拉丁词（`[A-Za-z]+`）计 `1`，不含标点与空白；常规档 `6.0` 字/秒、慢速档 `3.5` 字/秒、超长句阈值 `> 24` 字。\n  - 空格分词脚本（`speech` 为 `en` / `es` / `pt` 及其他非空代码，`unit=\"words\"`）：按 `re.findall(r\"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:'[0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*|[\\u4e00-\\u9fff]\", text)` 计词数（孤立单引号不计为词）；常规档 `3.2` 词/秒、慢速档 `2.0` 词/秒、超长句阈值 `> 18` 词。\n  - 清洗前缀并计数后若 `count == 0`（如纯标点 `\"……\"` 或纯标签 `\"（小林）\"`），直接跳过该镜语速与超长句告警（不误报 `dialogue_over_budget`）。\n- **标点停顿预算（全语种适用、正则 token 去重计次）**：按优先级一次扫描清洗后的文本，避免 `……` 或 `？！` 被重复累加：\n  - `……|\\.{3,}|…`：每次 +0.8s\n  - `——|--`：每次 +0.6s\n  - `[。？！.?!]+`：每次 +0.4s\n  - `[，、,]+`：每次 +0.2s\n- **单镜互斥优先级与数值口径**：同一镜头最多输出 1 条告警，优先级为 `dialogue_slow_over_budget > dialogue_over_budget > dialogue_long_line`。统一按 `rate = slow_rate if is_slow else regular_rate` 计算 `raw_min = count / rate + pause`、`min_seconds = round(raw_min, 1)`（包含 `dialogue_long_line`）；`duration = round(end - start, 2)`；超镜长判定按未四舍五入值 `raw_min > duration`；`line_preview` 取清洗前缀后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。\n- **4 类告警确定性 Schema**（`shot` 与 `affected_shots` 均为**归一化后镜头列表的 1-based 序号**，与 `frames.py:184`「第 N 镜」和 `film.py:243` `[Shot N]` 一致）：\n  - `dialogue_over_budget` / `dialogue_slow_over_budget`：`{\"code\", \"severity\": \"warn\", \"shot\": int, \"duration\": float, \"count\": int, \"unit\": \"chars\"|\"words\", \"chars\": int, \"min_seconds\": float, \"rate_band\": \"fast\"|\"slow\", \"line_preview\": str}`（`chars` 保留与 `count` 同值以向后兼容）。\n  - `dialogue_long_line`：同上字段，其中 `rate_band: \"normal\"`（表示未超语速预算但超过拆镜字/词数阈值）。\n  - `dialogue_speech_unset`：`{\"code\": \"dialogue_speech_unset\", \"severity\": \"warn\", \"shot\": null, \"affected_shots\": [int, ...]}`。"},{"oldText":"- **CLI（`h3ref/film.py`）**：\n  - `run()` 在规划完镜头后、开始任何付费调用前执行 `check_dialogue_budget(film)`；若有告警，逐条写入 `job.log(...)` 并汇总到 `report.md` 的「脚本预检提示」小节。**一律为 WARN，不阻断执行**。\n- **工作台后端（`workbench/references.py`、`workbench/server.py`）**：\n  - `POST /api/film/references`（调用 `describe_references`）在返回 `{\"film\": ..., \"slots\": ...}` 的基础上，补充 `\"warnings\": [...]`（复用 `film.load` 归一化后的镜头时长，调用 `film.check_dialogue_budget` 并按 `Accept-Language` 本地化 `message`）。\n  - 注意：`describe_references` 导入时 `cast[].picture` 尚未绑定真实文件，调用时长归一化与预检必须传 `check_images=False` 或复用纯镜头时间归一化逻辑，避免因未上传图片报错。\n- **工作台前端（`workbench/templates/index.html`、`workbench/static/create.js`）**：\n  - 导入或修改分镜 JSON 后，若 `warnings` 非空，在创建页分镜信息区以温和的 Warning 卡片（沿用 `DESIGN.md` 规范 `#81612a` on `#fff8e8`）列出具体镜号、字数、最短估算时长与建议，允许用户忽略并直接继续生成。\n- **国际化**：\n  - 同步补充 `h3ref/messages/zh-CN.json`、`h3ref/messages/en.json`、`workbench/messages/zh-CN.json`、`workbench/messages/en.json`。","newText":"- **CLI（`h3ref/film.py`）**：\n  - `run()` 在 `plan()` 之后、`stage_frames()` 之前调用 `check_dialogue_budget(film)`；若有告警，逐条用 `h3ref.i18n.message()` 写入 `job.log(\"preflight\", ...)`（即便后续 `stage_frames()` 部分失败提前返回，`run.log` 中也已留痕）。**一律为 WARN，不阻断执行**。\n  - `report()` 保持原函数签名不变，内部直接调用 `check_dialogue_budget(film)`；若有告警，在 `report.md` 中输出「脚本预检提示」小节（无告警时省略该小节）。\n- **工作台后端（`workbench/references.py`、`workbench/server.py`）**：\n  - `describe_references(script, *, lang=\"zh-CN\")` 保持 `lang` keyword-only 默认值以兼容 `GET /api/film/example`；`POST /api/film/references` 传入 `lang=language()`。\n  - 在返回 `{\"film\": ..., \"slots\": ...}` 的基础上补充 `\"warnings\": [...]`：内部直接将 `script` 传给 `film.check_dialogue_budget(script)`（由其内部调用一次非就地修改的 `normalize_shots(..., strict=False)`，绝不修改 `result[\"film\"][\"shots\"]`，防止 `create.js:167` 导入回写篡改用户草稿），并按 `workbench/messages/{zh-CN,en}.json` 附带本地化 `message` 字段。\n- **工作台前端（`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`）**：\n  - 复用 `.notice.warning`（补齐 `border: 1px solid #eddfb9`，符合 `DESIGN.md` `#81612a` on `#fff8e8`），挂载于 `#film-parse-status` 之后、`.film-reference-section` 之前。\n  - 渲染优先按告警结构化字段（`code`/`shot`/`duration`/`count`/`unit`/`min_seconds`/`affected_shots`）通过 `workbench/static/messages/{zh-CN,en}.json` 的 `t(...)` 动态生成（未知 `code` 时回退 `w.message`），确保顶部切换 `zh-CN` ↔ `en` 时 `renderPage()` 立即重绘而无旧语言残留；在 `invalidateFilmReferences()`、空输入或解析失败时同步清空 `state.filmWarnings`。\n- **国际化职责边界（全部三套均经 `tests/test_locale_parity.py` 校验）**：\n  - `h3ref/messages/{zh-CN,en}.json`：CLI 日志与 `report.md` 的 4 类告警文案、预检小节标题，以及 `chars`/`words` 单位本地化键。\n  - `workbench/messages/{zh-CN,en}.json`：`/api/film/references` 服务端 `warnings[].message` 的 4 类告警文案，以及 `chars`/`words` 单位本地化键。\n  - `workbench/static/messages/{zh-CN,en}.json`：创建页 Warning 卡片标题、辅助说明、单条告警模板（含 `chars`/`words` 单位本地化键）与忽略继续提示。"},{"oldText":"禁止用「仍穿着/照旧」含糊带过，防止生图模型将参考图穿戴与手持物品同时叠加画出两份。","newText":"禁止用「仍穿着/照旧」含糊带过，防止生图模型将参考图穿戴与手持物品同时叠加画出两份；同时在 `h3ref/frames.py:209,217` 的参考图固定约束文案中增加单镜状态覆盖优先级说明（「服装与配饰默认照参考图，若当前镜头明确描述脱下、摘下或改动则以当前镜头描述为准」），消除与 `locked_look` / 参考图强锁文案的指令打架。"},{"oldText":"| 类别 | 现行替换（偏平淡） | 计划升级方向（保留张力、降审核风险） |\n|---|---|---|\n| 击打/暴力类 | `暴力→激烈冲突`、`殴打→争执推搡`、`暴打→争执冲突` | 保留克制，补充动作感表达（如 `殴打→激烈交手推搡`、`暴打→重击逼退`），并完善 `describe_composition` 系统提示里的影视动作转译示例（沉劲压迫、受力后仰、气浪推散、地面裂纹、水珠汗珠飞溅） |\n| 创伤/血迹类 | `鲜血→深色污渍`、`伤口→衣物褶皱`、`流血→衣物沾有污渍` | 优化为更具戏剧感且安全的视觉状态（如 `鲜血→暗红痕迹`、`伤口→破损擦痕`、`流血→衣袖沾染暗红痕迹`） |\n| 武器/致命类 | `捅伤/刺伤→挥手逼退`、`砍杀→对峙` | 转为有压迫感的制伏/格挡画面（如 `砍杀→兵刃交锋对峙`、`捅伤/刺伤→招式逼近格挡`、`掐脖/勒脖→逼近扣肩制伏`） |\n| 破坏/极端类 | `断肢→散落的道具`、`肢解→画面切换` | `断肢→散落碎石与道具`、`炸飞碎尸→气浪震散碎石尘土` |","newText":"| 类别 | 操作 | 精确词条变更（阶段 2 Arm 3A 待测） |\n|---|---|---|\n| 击打/暴力类 | 修改 | `殴打`: `争执推搡` → `激烈交手推搡`；`暴打`: `争执冲突` → `重击逼退`（`暴力→激烈冲突` 保持不变） |\n| 创伤/血迹类 | 修改 | `鲜血`: `深色污渍` → `暗红痕迹`；`伤口`: `衣物褶皱` → `破损擦痕`；`流血`: `衣物沾有污渍` → `沾染暗红痕迹`（不硬编码衣物部位） |\n| 武器/致命类 | 修改 | `砍杀`: `对峙` → `兵刃交锋对峙`；`捅伤`/`刺伤`: `挥手逼退` → `招式逼近格挡`；`掐脖`/`勒脖`: `逼近对峙` → `逼近扣肩制伏` |\n| 破坏/极端类 | 修改 / 新增 | 修改 `断肢`: `散落的道具` → `散落碎石与道具`；新增 `炸飞碎尸`: `气浪震散碎石尘土`（`肢解→画面切换` 保持不变） |\n\n> 注：`describe_composition` 系统提示中的影视动作安全转译示例（沉劲压迫、受力后仰、气浪推散、地面裂纹、水珠汗珠飞溅）单列为阶段 2 **Arm 3B**，与 `NEUTRAL_WORDS`（Arm 3A）及项 2（Arm 2A）完全隔离测试。"},{"oldText":"- **确定改造（H3 英文改写侧 `h3ref/rewrite.py`）**：\n  - H3 工作流无负面分支，扩展 `RULES` 第 5 条：除禁止音乐/字幕/水印的否定句外，要求 LLM **对高关联视觉概念的否定描述（如「面无笑意」「不发光」「不要空镜」「不流血」）一律做零词根正向改写**（改写为具体肌肉状态、哑光材质本色、人物主体开句），不把被禁词根带进英文提示词。\n  - 评估在 `problems()` 增加对常见高危视觉否定句（如 `not glowing / no smile / without smiling / no empty room`）的提示性纠错，或在重试反馈中拦截。\n- **审慎验证项（生图侧 `h3ref/frames.py:TAIL` 与 `h3ref/film.py:RULES`）**：\n  - `frames.py:17` 的 `TAIL` 目前写着 `不增加额外人物。不出现字幕、文字、水印和任何真实品牌的标志。电影级布光。`；GPT Image 2.6 与 H3 机理不同，**禁止盲改**。","newText":"- **阶段 2 待测改造（H3 英文改写侧 `h3ref/rewrite.py`，单列为阶段 2 Arm 3D）**：\n  - H3 工作流无负面分支，扩展 `RULES` 第 5 条：除禁止音乐/字幕/水印的否定句外，要求 LLM **对高关联视觉概念的否定描述（如「面无笑意」「不发光」「不要空镜」「不流血」）一律做零词根正向改写**（改写为具体肌肉状态、哑光材质本色、人物主体开句），不把被禁词根带进英文提示词。\n  - **暂定策略（Taste Decision `d1`）**：阶段 2/3 默认仅在 `RULES` 第 5 条通过正向化引导生效，**不默认向 `problems()` 添加视觉否定词硬拦截正则**（避免误杀合法英文句或手工 H3 覆盖导致 6 次重试耗尽）；仅当阶段 2 Arm 3D 实测显示模型持续忽视引导时，再补充零误报窄正则。\n- **审慎验证项（生图侧 `h3ref/frames.py:TAIL`，单列为阶段 2 Arm 3C）**：\n  - `frames.py:17` 的 `TAIL` 目前写着 `不增加额外人物。不出现字幕、文字、水印和任何真实品牌的标志。电影级布光。`；GPT Image 2.6 与 H3 机理不同，**禁止盲改**（注：`h3ref/film.py:RULES` 仅用于视频中文提示词，不进入生图请求，本次不改动）。"},{"oldText":"| **阶段 1** | 实现 **项 1（台词语速与镜长预算 WARN 预检）**：`h3ref/film.py` 纯函数 + CLI 日志/报告接入 + `workbench/references.py` & 创建页展示 + 中英文 i18n | 新增 `tests/test_dialogue_budget.py`（覆盖中文常规/慢速/超长句、说话人前缀清洗、标点停顿、英文词数口径、工作台 `/api/film/references` 返回与 i18n）；跑现有全量离线测试全绿 | 零付费、确定性改动，测试全绿即可提交（可单独 commit） |\n| **阶段 2** | **小样本 A/B 实测（项 2 + 项 3 + 项 4）**：选取 3–5 个历史痛点镜头（摘镜/脱衣叠加、多人同框、动作冲突转译、惊吓/疼痛大情绪），单变量对比改动前后出图（APIMart）与出片（H3）效果、审核通过率与费用 | 输出对照记录（附实测图片/片段对比、是否触发审核、字符数与费用变化），由用户肉眼评判效果 | **停下等用户确认**：仅把实测净正向的条款合入默认流程；无效或负向的条款直接裁剪 |\n| **阶段 3** | 合入通过阶段 2 验证的 **项 2 + 项 3 + 项 4** 代码改动；补齐单测（含「零死字」回归、资产偏离与负向词拦截校验）；在 [SKILL.md](../../SKILL.md) 补充一句上下游解耦说明（支持外部编剧/分镜工具产出 `film.json` 交由本工作台出片） | 跑完整测试套件、工作台离线演示全流程冒烟验证，确认无破坏历史任务 `state.json` / `composition v2` 缓存兼容 | 全部测试与冒烟全绿后汇报交付 |\n\n---\n\n## 涉及文件清单（预计）\n\n- 核心逻辑：`h3ref/film.py`、`h3ref/frames.py`、`h3ref/rewrite.py`\n- 工作台与界面：`workbench/references.py`、`workbench/server.py`、`workbench/templates/index.html`、`workbench/static/create.js`\n- 国际化：`h3ref/messages/zh-CN.json`、`h3ref/messages/en.json`、`workbench/messages/zh-CN.json`、`workbench/messages/en.json`\n- 测试与文档：`tests/test_dialogue_budget.py`、现有 `frames` / `rewrite` / `workbench` 测试、`SKILL.md`","newText":"| **阶段 1** | 实现 **项 1（台词语速与镜长预算 WARN 预检）**：`h3ref/film.py` 纯函数（`normalize_shots` + `check_dialogue_budget`）+ CLI `run()`/`report()` 接入 + `workbench/references.py` & 创建页 Warning 卡片 + 全三套中英文 i18n | 新增 `tests/test_dialogue_budget.py`（覆盖中文/`ja`/`ko`/英文口径、常规/慢速含 `低声` 与英文词形/超长句/`dialogue_speech_unset`/`count == 0` 跳过、说话人前缀循环清洗与 `12:30` 保护、标点去重停顿、`normalize_shots` 非就地修改与草稿非字符串/非法时间容错、CLI `run()`/`report()` 集成、工作台 `/api/film/references` 与 `Accept-Language` 本地化），补充前端语言切换即时重绘与 `state.filmWarnings` 清空测试，并跑全量离线测试含 `tests/test_locale_parity.py` 全绿 | 零付费、确定性改动，测试全绿即可提交（可单独 commit） |\n| **阶段 2** | **小样本单变量 A/B 实测（独立实验分支/脚本，不污染默认提示词）**：基于 `examples/film.example.json` 与 3–5 个历史痛点镜头夹具（摘镜/脱衣叠加、多人同框、动作冲突转译、视觉否定句「面无笑意/不发光」、惊吓/疼痛大情绪），拆分为 6 个单变量试验臂独立对照基线——**Arm 2A**（项 2 构图规则 + `frames.py:209,217` 覆盖优先级 + `rewrite.RULES` 规则 9）、**Arm 3A**（`NEUTRAL_WORDS` 精确词表）、**Arm 3B**（`describe_composition` 动作安全转译示例）、**Arm 3C**（`frames.py:TAIL` 正向化对照）、**Arm 3D**（`rewrite.RULES` 第 5 条视觉否定词零词根正向化）、**Arm 4**（FACS 四区肌肉指引） | 输出对照记录（附实测图片/片段对比、APIMart 审核通过率、单镜/单段提示词字符数与单镜估算费用增幅），由用户肉眼评判；量化门槛：审核通过率 ≥ 基线、人工对照净正向、字符与费用增幅 ≤ 15% | **停下等用户确认**：逐臂决定合入或裁剪（含 `d1` 是否启用窄正则），严禁未通过单臂混入默认流程 |\n| **阶段 3** | 仅合入通过阶段 2 验证的试验臂代码改动；补齐单测（含「零死字」回归、资产偏离覆盖文案、`RULES` 规则断言，以及若阶段 2 Arm 3D 决定启用时的窄负向词校验）；在 [SKILL.md](../../SKILL.md) 补充一句上下游解耦说明（支持外部编剧/分镜工具产出 `film.json` 交由本工作台出片，纳入本次范围） | 跑完整测试套件、工作台离线演示全流程冒烟验证，确认无破坏历史任务 `state.json` / `composition v2` 缓存兼容 | 全部测试与冒烟全绿后汇报交付 |\n\n---\n\n## 涉及文件清单（预计）\n\n- 核心逻辑：`h3ref/film.py`、`h3ref/frames.py`、`h3ref/rewrite.py`\n- 工作台与界面：`workbench/references.py`、`workbench/server.py`、`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`（复用 `.notice.warning` 并补齐 `border: 1px solid #eddfb9` 及列表间距）\n- 国际化：`h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json`、`workbench/static/messages/{zh-CN,en}.json`\n- 测试与文档：`tests/test_dialogue_budget.py`、现有 `frames` / `rewrite` / `workbench` / `test_locale_parity.py` 测试、`SKILL.md`\n\n## 评审采纳约束（Accepted Obligations）"}]} -->

<!-- autoplan-accepted:ceo -->
- Extract non-mutating pure helper `h3ref/film.py:normalize_shots(shots, *, strict=False)` matching `load()` filtering/rounding (`max(0.5, length)`, default `2.0s`, empty `action` skipped), safely handling draft/missing/invalid `shots` (non-list, non-dict, non-string `action`/`line`/`speech`, `bool`/`NaN`/`Inf` timestamps) when `strict=False`, and preserving `result["film"]["shots"]` untouched in `workbench/references.py:describe_references()`; verify CLI/workbench parity, draft resilience, and non-mutation in `tests/test_dialogue_budget.py`.
- Enforce deterministic `check_dialogue_budget()` specification: centralize speech-rate thresholds in a single top-level `DIALOGUE_RATE_CONFIG` dict in `h3ref/film.py`, frame i18n copy as heuristic speaking-duration risk estimates (noting `film.py:123` segment rounding and `README.md:117` opening-frame inertia, and marking non-`zh` defaults uncalibrated); normalize `speech` via `.strip().lower()` and match primary subtag `speech.split("-", 1)[0]` (non-str -> `""`); emit single `dialogue_speech_unset` (`shot: null, affected_shots: [1-based normalized indices]`) when `speech` is empty and any normalized shot has non-empty `line.strip()`; run slow-pace cue detection (CN including `低声`/`轻声` + EN `\b(?:whisper(?:s|ing|ed)?|murmur(?:s|ing|ed)?|softly|slowly|chok(?:e|es|ing|ed)|sob(?:s|bing|bed)?)\b`) on raw `action` + raw `line` BEFORE looped speaker-prefix stripping (protecting `12:30` numeric colons); branch non-space scripts (`zh`/`ja`/`ko`, `unit="chars"`) vs space-delimited scripts (`en`/`es`/`pt`/others, `unit="words"`, word regex `[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:'[0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*|[\u4e00-\u9fff]` excluding lone apostrophes); skip per-shot warnings when `count == 0` after prefix stripping; apply deduplicated regex punctuation pauses across all languages; enforce per-shot mutual exclusion `dialogue_slow_over_budget > dialogue_over_budget > dialogue_long_line` using `rate = slow_rate if is_slow else regular_rate` for `min_seconds`, `duration = round(end - start, 2)`, 1-based normalized `shot` indices, and prefix-stripped `line_preview` truncated to `text[:39] + "…"` when `> 40`; verify all 4 schemas and edge cases in `tests/test_dialogue_budget.py`.
- Wire CLI & workbench cleanly: `film.run()` logs warnings before `stage_frames()`; `film.report()` keeps its signature and calls `check_dialogue_budget(film)` directly (omitting the section when empty); `POST /api/film/references` passes `lang=language()` into `describe_references(script, *, lang="zh-CN")` which delegates directly to `film.check_dialogue_budget(script)` once and returns `warnings` with localized `message` plus structured fields; frontend `create.js` renders warnings dynamically from structured fields via `workbench/static/messages/{zh-CN,en}.json` `t(...)` (falling back to `w.message`) inside `.notice.warning` (with `border: 1px solid #eddfb9`) mounted after `#film-parse-status` so language switches repaint immediately, clearing `state.filmWarnings` on empty/error/reset; include `chars`/`words` unit localization keys across all 3 i18n suites and verify via `tests/test_locale_parity.py` and UI/API tests.
- Distinguish dual asset-override states in Item 2 (`h3ref/frames.py:describe_composition`, `h3ref/frames.py:209,217`, `h3ref/film.py:251`, `h3ref/rewrite.py:32`, `h3ref/rewrite.py:RULES` rule 9): when the character enters the shot already stripped/altered, specify the bare/inner state in `placement_and_pose`; when the removal action occurs DURING the shot, preserve initial wear in the opening frame (per `h3ref/frames.py:105`) and express removal progression + end state in `rewrite.py:RULES` rule 9; align `frames.py:209,217`, `film.py:251`, and `rewrite.py:32` so static reference-look constraints yield to explicit shot-level overrides; refine exact `NEUTRAL_WORDS` diff without hardcoded `衣袖` (`流血→沾染暗红痕迹`), remove misclassified `h3ref/film.py:RULES` from image-side Item 3.2, and retain the 1-sentence upstream/downstream decoupling note in `SKILL.md` in Phase 3 scope.
- Enforce modality-decoupled Phase 2 A/B screening + combined-state regression gate on an experimental branch/script: (a) pure-image arms — Arm 3A (`NEUTRAL_WORDS`), Arm 3B (`describe_composition` safety examples), Arm 3C-neg (`TAIL` negative clause) & Arm 3C-light (`TAIL` lighting wording) — tested image-only across 15–20 adversarial/normal shots (0 new `ContentPolicyError`, narrative fidelity preserved); (b) pure-video arms — Arm 3D (`rewrite.RULES` rule 5 scoped strictly to high-activation visual roots + 2 normal-negation control shots) and Arm 4 (FACS scoped to script-demanded high emotions + restrained-acting control shots) — reusing frozen cached frames; (c) cross-modal Arm 2A verifying both anti-duplication and action completion; evaluate LLM steps on both Workbench default non-Claude model (DeepSeek/Gemini) and Claude; judge by deliverable acceptance cost (cumulative cost + retries + narrative fidelity + moderation pass rate) and run one combined-state regression pass before Phase 3 merge enforcing cumulative system-prompt growth <= +20% and zero inter-rule conflict.
- Provisional Taste Decisions: (`d1`) keep visual-negation positive reframing in `rewrite.py:RULES` rule 5 only for Phase 2 Arm 3D A/B without adding default hard-fail regexes to `rewrite.py:problems()` unless Arm 3D empirically requires narrow zero-FP patterns; (`d2`) compare updating `prompts/h3_examples.md` few-shot demonstrations alongside `rewrite.RULES` expansion in Phase 2 Arm 3D/4 to check whether example-driven guidance achieves higher compliance at lower rule length.
<!-- /autoplan-accepted:ceo -->

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Mode = SELECTIVE EXPANSION | Mechanical | Override rule | Autoplan CEO phase defaults to SELECTIVE EXPANSION | HOLD / REDUCTION / full EXPANSION |
| 2 | CEO | E1: Extract non-mutating `normalize_shots(shots, *, strict=False)` in `h3ref/film.py` | Mechanical | P2 Boil Lakes / P4 DRY | `load()` requires Path + `c["picture"]` (`h3ref/film.py:77-87`); `create.js:167` writes `result.film` back to editor so mutation corrupts drafts | In-place mutation or temp-file `film.load()` in `describe_references()` |
| 3 | CEO | E2: Emit `dialogue_speech_unset` (`shot: null, affected_shots`) when `speech` unset but `line` present | Mechanical | P1 Completeness | Plan lines 45 vs 46 contradicted each other; `compile_prompt()` (`h3ref/film.py:293`) silently drops lines when `speech` is unset | Silent skip or guessing `zh` |
| 4 | CEO | E3: Deterministic script-family counting (`zh`/`ja`/`ko` via BCP-47 primary subtag vs `en`/`es`/`pt`), dedup pauses, looped prefix strip, CN+EN slow cues (`低声`/`轻声` included), `count==0` skip, per-shot mutual exclusion, single `DIALOGUE_RATE_CONFIG` dict + heuristic risk wording | Mechanical | P1 Completeness / P5 Explicit | Eliminates `ja`/`ko` 1-word bug, `……` double-counting, `12:30` mis-strip, `count==0` false positives, and centralizes rates for post-calibration tuning | Ad-hoc ambiguous prose rules / scattered constants |
| 5 | CEO | E4: Update all 3 i18n suites (with `chars`/`words` unit keys in all 3) + dynamic frontend rendering via `static/messages` `t(...)` + server `Accept-Language` `message` | Mechanical | P1 Completeness / P2 Boil Lakes | `common.js:379-401` does not re-POST `/api/film/references` on language toggle; `test_locale_parity.py` enforces 3 suites | Server-only `w.message` (stale language on toggle) |
| 6 | CEO | E5: Modality-decoupled Phase 2 A/B screening (15–20 image shots for Arm 3A/3B/3C-neg/3C-light; frozen-frame video for Arm 3D/4 with control shots; cross-modal Arm 2A) + DeepSeek/Gemini coverage + combined-state regression gate (<= +20% prompt growth) + deliverable-cost & narrative-fidelity metrics | Mechanical | P1 Completeness / P5 Explicit | Both Native CEO and Codex (`gpt-6-astra`) flagged 3–5 shots as underpowered for moderation, `rh.py:93` duration-based pricing, single-arm stacking conflicts, and Workbench non-Claude LLM blind spot | 3–5 shot full-pipeline bundle with single-arm char-only gate |
| 7 | CEO | E6: Defer server-side `peakFilmReferences` refactor to TODOS.md | Mechanical | P3 Pragmatic | Outside blast radius of dialogue budget preflight | Refactoring `create.js:183-254` in this plan |
| 8 | CEO | E7: Defer auto-mutating JSON shot durations button to TODOS.md (pending `uc1` user review) | Mechanical | P3 Pragmatic / YAGNI | Mutating user storyboard timestamps risks beat misalignment; original plan is WARN-only; surfaced in `uc1` if user wants executable pacing | Silently adding auto-fix JSON mutation UI |
| 9 | CEO | E8 (`d1`): Keep visual-negation reframing prompt-only in `RULES` rule 5 first; do not add hard-fail regexes to `problems()` by default | Taste | P5 Explicit / P6 Action | `problems()` triggers 6x LLM retries and gates manual H3 overrides (`h3ref/film.py:527`); premature regex risks false-positive failures | Adding regexes (`not glowing / no smile / without smiling`) to `problems()` immediately |
| 10 | CEO | E9: Dual-state asset override (already-stripped opening vs in-shot removal action preserving `frames.py:105`) + align `frames.py:209,217`, `film.py:251`, and `rewrite.py:32` + drop `衣袖` hardcoding (`流血→沾染暗红痕迹`) | Mechanical | P1 Completeness / P2 Boil Lakes | 0H Spec Review + Native F2.3 + Codex F3 caught image/video lock conflict and action deletion if opening frame pre-strips glasses/coat | Leaving contradictory prompt instructions or pre-stripping active removal shots |
| 11 | CEO | E10: Retain 1-sentence `SKILL.md` decoupling note in Phase 3 scope | Mechanical | P3 Pragmatic | Already requested in source plan Phase 3; minimal 1-line doc update | Dropping without user approval |
| 12 | CEO | E11 (`d2`): Compare `prompts/h3_examples.md` few-shot updates alongside `rewrite.RULES` expansion in Phase 2 Arm 3D/4 | Taste | P3 Pragmatic / P5 Explicit | Native CEO F4.1 noted few-shot examples in `prompts/h3_examples.md` may give higher DeepSeek/Gemini compliance with fewer rule tokens | Ignoring `prompts/h3_examples.md` during Phase 2 |
| 13 | CEO | `uc1` (Queued User Challenge): Pre-calibrate Phase 1 pacing rates on 10–15 historical H3 clips + ROI prioritization / executable pacing helper vs direct Phase 1 ship | User Challenge | Cross-model challenge | Native CEO (F1.1, F2.1) & Codex (F1, F2) both challenged uncalibrated Seedance constants and passive WARN-only ROI; original plan direction retained until Phase 4 user decision | Auto-changing user's stated scope mid-run |
| 14 | CEO | `uc2` (Queued User Challenge): Persist Phase 2 pain-shot fixtures as versioned golden regression set with model-version retirement triggers | User Challenge | Cross-model challenge | Native CEO (F3.1, F5.1) & Codex (F8) both warned model-specific prompt patches decay in 3–6 months without a persistent eval harness | Auto-adding new eval infrastructure mid-run |

---

## Phase 1: CEO Review Record

### CEO Dual Voices — Consensus Table

- **Native CEO Voice**: Completed (`antigravity / gemini-4-neon`, `INPUT: ceo 2a36613dfcc7c2671a2ede481aef007f5b2c5a63742951477c2e68c5f08a3d47`)
- **Outside CEO Voice**: Completed (`provider=codex`, `model=gpt-6-astra`, `OUTSIDE_STATUS: completed provider=codex host=claude`, `VERDICT: FINDINGS`)

```text
CEO DUAL VOICES — CONSENSUS TABLE:
  Dimension                            Native (Gemini)   Codex (gpt-6-astra)   Consensus
  1. Premises valid?                   FINDINGS (Crit)   FINDINGS (High)       CONFIRMED (uc1: uncalibrated Seedance rates & H3 startup inertia; Arm 2A/3A flaws fixed mechanically)
  2. Right problem to solve?           FINDINGS (Crit)   FINDINGS (High)       CONFIRMED (uc1: external-repo-driven vs loss-ranked priorities & passive WARN vs executable helper)
  3. Scope calibration correct?        FINDINGS (High)   FINDINGS (High)       CONFIRMED (Phase 2 needs modality split + combined-state gate + deliverable-cost metric; applied)
  4. Alternatives sufficiently explored? FINDINGS (High) FINDINGS (High)       CONFIRMED (prompts/h3_examples.md few-shot -> d2; dual-state override -> fixed; executable pacing -> uc1)
  5. Competitive/market risks covered? FINDINGS (High)   FINDINGS (Med)        CONFIRMED (uc2: 3–6 month prompt-patch decay needs persistent golden eval set & retirement rule)
  6. 6-month trajectory sound?         FINDINGS (High)   FINDINGS (Med)        CONFIRMED (cumulative <= +20% system-prompt budget ceiling applied; golden eval set queued as uc2)
```

### Step 0I: Temporal Interrogation
- **HOUR 1 (Foundations, human: ~2h / CC: ~10min)**: Extract non-mutating `normalize_shots(shots, *, strict=False)` in `h3ref/film.py`, define `DIALOGUE_RATE_CONFIG`, verify `film.load()` preserves exact existing error behavior (`strict=True`).
- **HOUR 2–3 (Core Logic, human: ~4h / CC: ~20min)**: Implement `check_dialogue_budget(film)` with BCP-47 subtag split, raw slow-pace detection before looped prefix stripping, `count == 0` skip, deduplicated punctuation pauses, per-shot mutual exclusion, and all 4 schemas.
- **HOUR 4–5 (Integration & i18n, human: ~4h / CC: ~20min)**: Wire `film.run()`, `film.report()`, `workbench/references.py:describe_references(script, *, lang="zh-CN")`, `workbench/server.py`, `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, and all 3 i18n pairs (`h3ref/messages`, `workbench/messages`, `workbench/static/messages`).
- **HOUR 6+ (Verification & Phase 2/3 Gate, human: ~1d / CC: ~45min + live A/B)**: Add comprehensive `tests/test_dialogue_budget.py`, JS UI tests, `tests/test_locale_parity.py` parity verification; then run isolated modality-decoupled Phase 2 A/B screening & combined-state gate before Phase 3 merge.

### CEO Reality Rows (Step 0A/0B)

#### Premise Table (`unverified=0`)
| Premise | Claim | File:Line Receipt | Verdict |
|---|---|---|---|
| P1 | `h3ref/film.py:load()` normalizes shot durations (`max(0.5, length)`, default `2.0s`) without checking dialogue speed | `h3ref/film.py:77-103` | verified |
| P2 | `h3ref/film.py:compile_prompt()` omits dialogue when `speech` is empty/unset, and `plan()` rounds segment duration via `ceil(b - a - 0.25)` | `h3ref/film.py:123`, `h3ref/film.py:293-301` | verified |
| P3 | `h3ref/frames.py:NEUTRAL_WORDS` replaces conflict terms neutrally and keeps `"死": "静止"` fallback; `TAIL` has negative clauses | `h3ref/frames.py:17-31` | verified |
| P4 | `h3ref/frames.py:105` defines opening frame before action finishes; `frames.py:209,217`, `film.py:251`, `rewrite.py:32` lock reference wear strictly | `h3ref/frames.py:105,209,217`, `h3ref/film.py:251`, `h3ref/rewrite.py:32` | verified |
| P5 | `h3ref/rewrite.py:RULES` rule 5/7/9 & `NEGATIVE` only block audio/subtitle/text negatives, and `problems()` hard-gates retries (`max_retries=6`) and manual overrides | `h3ref/rewrite.py:44-56,206-218,302`, `h3ref/film.py:527` | verified |
| P6 | `README.md` documents H3 restrained big emotions and 1–2s opening-frame inertia; RunningHub pricing is duration-based (`rh.py:93`) | `README.md:117-118`, `h3ref/rh.py:93-94` | verified |
| P7 | Workbench `/api/film/references` calls `describe_references(payload.get("film"))`; `create.js:167` writes `result.film` back to editor; `common.js:379-401` does not re-POST on language toggle; `test_locale_parity.py` checks 3 locale suites | `workbench/server.py:330,657-662`, `workbench/static/create.js:133-167`, `workbench/static/common.js:379-401`, `tests/test_locale_parity.py:27-31` | verified |

REALITY: premise-table pass All 7 factual premises verified against codebase (`unverified=0`) h3ref/film.py:77-103
REALITY: already-done pass Checked `git log origin/main --since=2026-11-23` and grepped `check_dialogue_budget`/`dialogue_over_budget`; 0 hits (recent commits are ComfyUI/Colab only) h3ref/film.py:1
REALITY: surface-check pass Verified CLI `h3ref.py film`, HTTP `POST /api/film/references`, `GET /api/film/example`, `h3ref/film.py:load/run/report`, `h3ref/frames.py:describe_composition/prompt/TAIL/NEUTRAL_WORDS`, `h3ref/rewrite.py:RULES/problems` h3ref.py:192-216
REALITY: binding-decisions pass Checked `gstack-decision-search --scope branch --recent 50` (0 prior branch decisions) and `tasks/lessons.md` constraints (all 5 lessons honored) tasks/lessons.md:1-54
REALITY: numbers pass Derived `examples/film.example.json` shot 2 (`4/6.0 + 0.8 + 0.4 = 1.87s <= 2.0s` [measured: examples/film.example.json:17]); speech rates `6.0/3.5/3.2/2.0` tagged [estimated — queued in uc1 for H3 clip calibration]; `h3ref/rh.py:93` duration pricing verified [measured: h3ref/rh.py:93-94] examples/film.example.json:17
REALITY: deferred-asks pass No linked issue in this thread; all plan items mapped (`uc1`/`uc2` queued for Phase 4) docs/plans/2026-11-23-prompt-and-preflight-enhancements.md:1-154
REALITY: pr-count pass No `.gstack/ship-policy.json` present (default 1 PR per wave; Phase 1 can land independently as wave 1 before Phase 2/3) docs/plans/2026-11-23-prompt-and-preflight-enhancements.md:139-141

### Review Sections 1–11 (CEO Implementation-Ready Depth)

#### Section 1: Architecture Review
**Current scope**: `SELECTIVE EXPANSION` (Autoplan CEO override; governing rows: E1–E5, E9–E10 accepted; E6–E7 deferred; `d1`/`d2` provisional taste; `uc1`/`uc2` queued User Challenges retaining original plan direction).

```text
                    +-------------------------------------------+
                    |        film.json (CLI or Web Editor)      |
                    +---------------------+---------------------+
                                          |
                 +------------------------+------------------------+
                 v                                                 v
  +------------------------------+                 +-------------------------------+
  | h3ref/film.py (CLI Pipeline) |                 | workbench/server.py           |
  |  load() -> normalize_shots   |                 |  POST /api/film/references    |
  |             (strict=True)    |                 |  -> describe_references(lang) |
  |  run() -> check_dialogue_    |                 +---------------+---------------+
  |           budget(film)       |                                 |
  |    |- job.log("preflight")   |                                 v
  |    |- stage_frames()         |                 +-------------------------------+
  |    |- stage_prompts()        |                 | h3ref/film.py (Single Source) |
  |    |- stage_generate()       |                 |  normalize_shots(strict=False)|
  |    |- report() -> preflight  |                 |  check_dialogue_budget(film)  |
  +------------------------------+                 +---------------+---------------+
                                                                   |
                                                                   v
                                                   +-------------------------------+
                                                   | workbench/static/create.js    |
                                                   |  state.filmWarnings           |
                                                   |  .notice.warning via t(...)   |
                                                   +-------------------------------+

  Phase 2/3 Prompt Surface (Gated by Modality-Decoupled A/B + Combined Regression):
    frames.py (describe_composition, prompt L209/217, NEUTRAL_WORDS, TAIL)
    film.py   (compile_prompt L251)
    rewrite.py (RULES L32, #5, #7, #9; optional h3_examples.md via d2)
```
- **Data Flow (Happy / Nil / Empty / Error)**: Happy returns localized `warnings` list (`[]` if clean); Nil (`shots=None` or missing) returns `[]` under `strict=False` (and raises `SystemExit` under `strict=True` in `load()`); Empty (`shots=[]` or empty `line`) returns `[]`; Error (malformed JSON in editor) sets `state.filmError` and clears `state.filmWarnings`.
- **Coupling & Rollback**: `workbench/references.py` imports `h3ref.film.check_dialogue_budget` (existing dependency direction: `workbench` already imports `h3ref` in `workbench/inputs.py:10`). Rollback is a single git revert (<2 min), zero schema/DB migration, zero `state.json` breakage.
- **Value test**: No unconsumed process artifacts; Phase 2 A/B record directly gates Phase 3 merges and retires once Phase 3 lands (unless user approves `uc2` golden eval set).

#### Section 2: Error & Rescue Map (Error & Rescue Registry)
```text
METHOD/CODEPATH                       | WHAT CAN GO WRONG                  | EXCEPTION CLASS
--------------------------------------|------------------------------------|------------------
film.normalize_shots(strict=True)     | Empty shots / >max_shots           | SystemExit
film.normalize_shots(strict=True)     | Non-numeric / bool / NaN start/end | ValueError / TypeError
film.normalize_shots(strict=False)    | Draft missing/non-list/non-str/NaN | None (guarded)
film.check_dialogue_budget(film)      | Non-dict film / non-str speech/line| None (guarded)
references.describe_references()      | Invalid film root or cast[] entry  | InputError
frames.describe_composition() (Ph 2/3)| Longer prompt -> invalid JSON      | CompositionFormatError
frames.draw() (Ph 2/3 Arm 3A/3C)      | Upgraded NEUTRAL_WORDS / TAIL block| ContentPolicyError
rewrite.rewrite() (Ph 2/3 Arm 3D/4)   | Rule bloat -> retry exhaustion     | RewriteError
--------------------------------------|------------------------------------|------------------

EXCEPTION CLASS        | RESCUED? | RESCUE ACTION                                | USER SEES
-----------------------|----------|----------------------------------------------|-------------------------------------
SystemExit             | Y        | Existing CLI exit / inputs.py -> InputError  | Clear localized validation message
ValueError / TypeError | Y        | inputs.py:150 catches -> InputError          | Clear field validation error
InputError             | Y        | server.py HTTP 400 JSON error                | Red `#film-parse-status` error text
CompositionFormatError | Y        | film.py:450 retries 3x then raw fallback     | Logged fallback, frame still renders
ContentPolicyError     | Y        | film.py:390/481 persists & halts auto-retry  | Per-card moderation warning (manual edit)
RewriteError           | Y        | rewrite.py:325 bounded 6 retries then raise  | Segment error with concrete issues
```
No silent swallow gaps (`CRITICAL GAPS = 0`).

#### Section 3: Security & Threat Model
- **Attack surface**: No new HTTP endpoint, auth boundary, credential, or third-party dependency. Only adds `"warnings"` to existing local-only `POST /api/film/references`.
- **XSS / Injection**: `line_preview` originates from user-pasted `film.json` and renders inside `create.js`. **Mitigation (mandatory)**: Render all warning text strictly via DOM `textContent` / `element(...)` (matching existing `create.js` pattern), NEVER `innerHTML`.
- **ReDoS**: All new regexes in `check_dialogue_budget()` (prefix strip loop with bounded quantifiers `{1,20}` / `{0,11}`, punctuation scan, word scan, slow-pace alternation) are strictly linear O(N) with no nested quantifiers; add a long-input (10,000-char) linear-time test in `tests/test_dialogue_budget.py`.
- **Threat summary**: XSS via `line_preview` (Low likelihood / Med impact -> mitigated via `textContent`); ReDoS on pathological `line` (Low likelihood / Low impact -> mitigated via bounded non-backtracking regexes & unit test).

#### Section 4: Data Flow & Interaction Edge Cases
```text
film-json textarea (input/import)
  --[debounce/import]--> parseFilm() --[POST /api/film/references + Accept-Language]-->
  describe_references(script, lang) --> check_dialogue_budget(script)
    |-> normalize_shots(strict=False) [copy only; never mutates result.film.shots]
    |-> shadow: non-dict/missing/NaN/non-str -> safe skip / fallback 2.0s
    |-> shadow: speech empty + lines -> single dialogue_speech_unset
    |-> shadow: count == 0 -> skip shot
  --> JSON {film, slots, warnings}
  --> create.js checks (revision === state.filmRevision) [drops stale async response!]
  --> state.filmWarnings = result.warnings || []
  --> renderFilmReferences() -> .notice.warning via t(...) [language toggle repaints live!]
```
- **Async Ordering**: `create.js:104,134` already tracks `state.filmRevision` monotonically; `invalidateFilmReferences()` increments revision and clears `state.filmWarnings = []` so an older in-flight `POST /api/film/references` response can never overwrite warnings from a newer edit or cleared textarea.
- **Interaction Edge Cases**:
  | Interaction | Edge Case | Handled? | How |
  |---|---|---|---|
  | Rapid typing / double import | Out-of-order `/api/film/references` responses | Y | `revision !== state.filmRevision` discards stale response |
  | Language toggle (`zh-CN` ↔ `en`) | Warning card stays in old language | Y | Rendered dynamically from structured fields via `t(...)` in `renderPage()` |
  | Empty textarea / JSON syntax error | Old warning card lingers | Y | `invalidateFilmReferences()` & empty/catch branches reset `state.filmWarnings = []` |
  | 100+ shot film with 20+ warnings | Warning banner pushes upload slots off-screen | Y (handoff to Design Phase 2) | Cap/collapse long warning lists cleanly (exact UX specified in Design Phase 2) |
  | Clicking Start while warnings exist | Blocked submission | Y | Warnings are strictly non-blocking; `start-job` disabled state unchanged |

#### Section 5: Code Quality Review
- Follows existing repository structure (`h3ref/film.py` owns pure storyboard logic; `workbench/references.py` owns HTTP reference projection; `h3ref/i18n.py` & `workbench/i18n.py` own localization).
- Split `check_dialogue_budget()` into small cohesive helpers (`_strip_speaker_prefix`, `_count_speech_units`, `_pause_seconds`, `_is_slow_pace`, `_format_preview`) so no single function exceeds 5 branches / high cyclomatic complexity, and keep Python 3.10 AST compatibility (Prior learning applied: `python-310-pep701-fstring-ast-gate`, confidence 10/10, 2026-11-16).

#### Section 6: Test Review
- **Test pyramid**: Primarily fast deterministic unit tests (`tests/test_dialogue_budget.py`, ~20 cases) + Flask HTTP integration tests (`POST /api/film/references` with `Accept-Language: zh-CN` / `en`, `GET /api/film/example`) + Node JS DOM test (`tests/test_workbench_ui.js` for warning rendering, language toggle repaint, clearing on invalid/empty input, non-blocking submit) + `tests/test_locale_parity.py` + Phase 2 live A/B screening.
- **Assertion & `no_claim` discipline**:
  - Unit tests assert exact warning schemas, 1-based normalized indices, non-mutation of `result["film"]["shots"]`, `12:30` preservation, `count == 0` skip, `zh`/`ja`/`ko`/`en` counting, `低声`/`whispers` slow cues, `"死"` zero-occurrence regression (`no_claim=live APIMart moderation & H3 acting obedience require Phase 2 live A/B; unit tests verify deterministic logic & prompt assembly only`).
  - Hostile QA test: 10,000-char `line` with nested brackets/colons + `{"shots": [{"action": 123}, {"action": "ok", "start": true, "end": "NaN", "line": "……"}]}` must return HTTP 200 without crashing or hanging.

#### Section 7: Performance Review
- Zero DB queries, zero network calls in Phase 1. `check_dialogue_budget()` runs O(total line chars) in <1ms even for a 200-shot film (`~50KB` JSON). No new connections, no caching layer needed.

#### Section 8: Observability & Debuggability Review
- CLI logs each warning via `job.log("preflight", ...)` into `run.log` before `stage_frames()` and summarizes in `report.md` when non-empty.
- Phase 2 A/B records per-arm prompt length, APIMart moderation pass/fail, retry counts, and total cost so post-ship regressions can be traced back to the exact arm.

#### Section 9: Deployment & Rollout Review
- Local Python CLI + local Flask Workbench (Windows zip + macOS local venv). No migrations, no deploy-time skew window.
- Phased rollout: Wave 1 (Phase 1 deterministic preflight) ships independently with zero paid-API risk; Wave 2 (Phase 2 A/B screening + combined-state gate) gates Wave 3 (Phase 3 prompt merges + `SKILL.md`). Rollback = `git revert`.

#### Section 10: Long-Term Trajectory Review
- **Reversibility**: 5/5 (pure Python preflight + prompt strings + i18n keys; zero persisted state schema migration).
- **Debt & Guardrails**: System-prompt cumulative growth capped at `<= +20%` to prevent slow drift toward `manju-laoli-skill`; model-specific prompt patches queued in `uc2` for golden eval set + retirement conditions.

#### Section 11: Design & UX Review (UI Scope = YES)
- **Information Architecture**: `#film-json` editor -> `#film-parse-status` (hard syntax/API errors) -> `#film-dialogue-warnings` (`.notice.warning`, soft non-blocking preflight advice) -> `.film-reference-section` (required cast/board uploads) -> mode & submit.
- **State Coverage Map**:
  | Feature | LOADING | EMPTY | ERROR | SUCCESS (Clean) | SUCCESS (Warnings) | PARTIAL/LARGE (15+ warns) |
  |---|---|---|---|---|---|---|
  | Dialogue Preflight Card | Hidden / cleared while `filmParsing` | Hidden (`state.filmWarnings=[]`) | Hidden (`state.filmError` shown instead) | Hidden (zero noise!) | Amber `.notice.warning` card (`#81612a` on `#fff8e8`, `border: 1px solid #eddfb9`) | Hand off exact collapsible/summary cap to Phase 2 Design Review |
- **DESIGN.md & Accessibility**: Matches `DESIGN.md` warning tokens (`#81612a` on `#fff8e8`), zero external fonts, `role="status"` / `aria-live="polite"` without stealing focus, responsive vertical stack down to `420px`. Detailed pixel/interaction audit deferred to Phase 2 (`/plan-design-review`).

### NOT in scope
- **Deferred to TODOS.md (E6)**: Moving `peakFilmReferences` shot-time normalization out of `workbench/static/create.js:183-254` to the server (separate existing UI feature, outside dialogue preflight blast radius).
- **Deferred to TODOS.md (E7, subject to `uc1`)**: One-click auto-mutating JSON shot durations in the editor (mutating user timestamps risks beat drift; WARN-only explicitly requested unless user approves `uc1`).
- **Rejected / Out of scope**: Modifying `h3ref/film.py:RULES` (video-side, misclassified in draft), expanding `composition v3` JSON fields back toward v2, adding default hard-fail visual-negation regexes to `rewrite.py:problems()` (provisional `d1`).

### What already exists
- `h3ref/film.py:77-103` (`load()`): Existing shot filtering & duration normalization — reused by extracting `normalize_shots()`.
- `h3ref/frames.py:18-31` (`NEUTRAL_WORDS` & `NEUTRAL_PATTERN`): Existing length-sorted moderation sanitizer — upgraded in-place after Arm 3A verification.
- `workbench/static/style.css:223` (`.notice.warning`): Existing `DESIGN.md` warning surface — reused with `border: 1px solid #eddfb9`.
- `tests/test_locale_parity.py`: Existing 3-suite i18n key & placeholder parity gate — extended to cover new keys.

### Dream state delta
```text
CURRENT STATE                     THIS PLAN                               12-MONTH IDEAL
No preflight pacing check;        Zero-cost CLI + Workbench preflight     Audio/TTS-calibrated pacing +
prompt flaws discovered only      + modality-decoupled A/B-gated          versioned golden eval suite +
after spending GPU/API credits    lightweight prompt upgrades (v3 kept)   automated burned-subtitle/visual QA
```

### Failure Modes Registry
```text
CODEPATH                        | FAILURE MODE                         | RESCUED? | TEST? | USER SEES?                     | LOGGED?
--------------------------------|--------------------------------------|----------|-------|--------------------------------|--------
film.normalize_shots(False)     | Draft non-list/non-str/bool/NaN      | Y        | Y     | Safe skip / 2.0s fallback      | N (pure)
film.check_dialogue_budget()    | speech unset with lines              | Y        | Y     | dialogue_speech_unset WARN     | Y (CLI)
film.check_dialogue_budget()    | count == 0 ("……" / "(小林)")          | Y        | Y     | Clean skip (no false alarm)    | N
references.describe_references()| In-place mutation corrupts editor    | Y        | Y     | result.film.shots untouched    | N
create.js language toggle       | Stale server warning language        | Y        | Y     | Instant repaint via t(...)     | N
create.js render warnings       | XSS via line_preview                 | Y        | Y     | Escaped text via textContent   | N
frames.py + film.py + rewrite.py| Image/video lock fights asset override| Y       | Y     | Dual-state precedence (Arm 2A) | Y (A/B)
frames.py NEUTRAL_WORDS (Arm3A) | APIMart ContentPolicyError spike     | Y        | Y     | 15-20 shot A/B gate blocks     | Y (A/B)
rewrite.py RULES (Arm 3D/4)     | DeepSeek/Gemini rule bloat regression| Y        | Y     | Combined <=+20% & non-Claude gate | Y (A/B)
```
**CRITICAL GAPS**: 0.

### Stale Diagram Audit
- Inspected `h3ref/film.py`, `h3ref/frames.py`, `h3ref/rewrite.py`, `workbench/references.py`: 0 existing ASCII diagrams found (0 stale diagrams).

### Implementation Tasks (CEO Review)
- [ ] **T1 (P1, human: ~2h / CC: ~10min)** — `h3ref/film.py` — Extract non-mutating `normalize_shots(shots, *, strict=False)` and centralize `DIALOGUE_RATE_CONFIG`
  - Surfaced by: CEO Step 0 & Section 1 — `load()` requires Path and mutates `shots`; `describe_references()` receives draft dicts without `cast[].picture`
  - Files: `h3ref/film.py`, `tests/test_dialogue_budget.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_offline.py`
- [ ] **T2 (P1, human: ~4h / CC: ~20min)** — `h3ref/film.py` — Implement deterministic `check_dialogue_budget()` and wire into `run()` and `report()`
  - Surfaced by: CEO Step 0 & Section 2/5 — 4 warning schemas, BCP-47 subtag split, raw slow-pace detection before looped prefix stripping, `count == 0` skip, dedup pauses, mutual exclusion, Python 3.10 AST compatibility
  - Files: `h3ref/film.py`, `h3ref/messages/zh-CN.json`, `h3ref/messages/en.json`, `tests/test_dialogue_budget.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_locale_parity.py tests/test_dual_llm_and_security.py`
- [ ] **T3 (P1, human: ~4h / CC: ~20min)** — `workbench/` — Wire `describe_references(script, *, lang="zh-CN")`, `POST /api/film/references`, create-page `.notice.warning` card, dynamic `t(...)` rendering, XSS-safe DOM, and all 3 i18n suites
  - Surfaced by: CEO 0H Spec Review & Section 3/4/11 — non-mutation of `result.film.shots`, instant language toggle repaint, `state.filmWarnings` cleanup, `.notice.warning` border
  - Files: `workbench/references.py`, `workbench/server.py`, `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, `workbench/messages/zh-CN.json`, `workbench/messages/en.json`, `workbench/static/messages/zh-CN.json`, `workbench/static/messages/en.json`, `tests/test_dialogue_budget.py`, `tests/test_workbench_ui.js`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_reference_bindings.py tests/test_locale_parity.py && node tests/test_workbench_ui.js`
- [ ] **T4 (P1, human: ~1d / CC: ~45min + live A/B)** — `h3ref/frames.py`, `h3ref/film.py`, `h3ref/rewrite.py` — Execute modality-decoupled Phase 2 A/B screening (Arm 2A dual-state + image/video lock alignment, Arm 3A/3B/3C-neg/3C-light 15–20 image shots, Arm 3D/4 frozen-frame video + control shots, DeepSeek/Gemini + Claude, combined-state <= +20% gate) before Phase 3 merge
  - Surfaced by: CEO Dual Voices (Native F2.2/F2.3/F4.1 + Codex F3/F4/F5/F6) — statistical power, action preservation, video-side lock consistency, deliverable-cost & narrative-fidelity gating
  - Files: `h3ref/frames.py`, `h3ref/film.py`, `h3ref/rewrite.py`, `prompts/h3_examples.md`, `SKILL.md`
  - Verify: Phase 2 A/B report reviewed by user + full offline test suite + workbench demo smoke test in Phase 3

### CEO Completion Summary
```text
  +====================================================================+
  |            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
  +====================================================================+
  | Mode selected        | SELECTIVE EXPANSION                         |
  | System Audit         | 3 i18n suites verified; no prior preflight  |
  | Step 0               | SELECTIVE EXPANSION; 0H Spec Loop PASS 9/10 |
  | Section 1  (Arch)    | 2 issues found (resolved via E1/E4)         |
  | Section 2  (Errors)  | 8 error paths mapped, 0 GAPS                |
  | Section 3  (Security)| 2 issues found (XSS/ReDoS), 0 High unmitig. |
  | Section 4  (Data/UX) | 5 edge cases mapped, 0 unhandled            |
  | Section 5  (Quality) | 2 issues found (helpers + Py3.10 AST)       |
  | Section 6  (Tests)   | Diagram/matrix produced, 0 gaps             |
  | Section 7  (Perf)    | 0 issues found                              |
  | Section 8  (Observ)  | 0 gaps found                                |
  | Section 9  (Deploy)  | 0 risks flagged (5/5 reversibility)         |
  | Section 10 (Future)  | Reversibility: 5/5, debt items: 2 (uc2)     |
  | Section 11 (Design)  | 1 handoff item (15+ warning collapse -> P2) |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (2 deferred, 3 rejected/out)        |
  | What already exists  | written (4 existing components mapped)      |
  | Dream state delta    | written                                     |
  | Error/rescue registry| 8 rows, 0 CRITICAL GAPS                     |
  | Failure modes        | 9 total, 0 CRITICAL GAPS                    |
  | TODOS.md updates     | 2 items proposed (E6, E7)                   |
  | Scope proposals      | 11 proposed, 9 accepted, 2 deferred         |
  | CEO plan             | written (~/.gstack/projects/.../ceo-plans/) |
  | Outside voice        | codex (gpt-6-astra) completed               |
  | Lake Score           | N/A (scope/kind choices; 0 coverage polls)  |
  | Diagrams produced    | 3 (Architecture, Data Flow, Dream State)    |
  | Stale diagrams found | 0                                           |
  | Unresolved decisions | 0 mid-run (2 Taste d1/d2 + 2 UC uc1/uc2 -> P4) |
  | Reality rows         | 7/7 rows (verdict=complete), 0 unverified   |
  +====================================================================+
```

---

## Phase 2: Design Review

<!-- autoplan-baseline-edits:design {"sourceSha256":"581cc4226afa468b4e05b78f18cc30e6ae258caf4307acf1dd0dc0484ec6dce2","replacements":[{"oldText":"`min_seconds = round(raw_min, 1)`（包含 `dialogue_long_line`）；`duration = round(end - start, 2)`；超镜长判定按未四舍五入值 `raw_min > duration`；`line_preview` 取清洗前缀后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。","newText":"`min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10`（向上进位到 0.1 秒，确保所有超预算告警在界面展示时严格满足 `min_seconds > duration`，避免出现「2.0 秒超出 2.0 秒」的舍入穿帮；`dialogue_long_line` 同口径估算）；`duration = round(end - start, 2)`；超镜长判定按未四舍五入值 `raw_min > duration`；`line_preview` 取清洗前缀后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。"},{"oldText":"- **4 类告警确定性 Schema**（`shot` 与 `affected_shots` 均为**归一化后镜头列表的 1-based 序号**，与 `frames.py:184`「第 N 镜」和 `film.py:243` `[Shot N]` 一致）：\n  - `dialogue_over_budget` / `dialogue_slow_over_budget`：`{\"code\", \"severity\": \"warn\", \"shot\": int, \"duration\": float, \"count\": int, \"unit\": \"chars\"|\"words\", \"chars\": int, \"min_seconds\": float, \"rate_band\": \"fast\"|\"slow\", \"line_preview\": str}`（`chars` 保留与 `count` 同值以向后兼容）。\n  - `dialogue_long_line`：同上字段，其中 `rate_band: \"normal\"`（表示未超语速预算但超过拆镜字/词数阈值）。\n  - `dialogue_speech_unset`：`{\"code\": \"dialogue_speech_unset\", \"severity\": \"warn\", \"shot\": null, \"affected_shots\": [int, ...]}`。","newText":"- **4 类告警确定性 Schema**（`shot` 与 `affected_shots` 均为**归一化后镜头列表的 1-based 序号**，与 `frames.py:184`「第 N 镜」和 `film.py:243` `[Shot N]` 一致；同时附带原始 JSON 数组位次 `raw_shot` / `affected_raw_shots` 以消除草稿跳过空镜导致的编辑器定位错位；移除绿色新建接口中无消费者且易误导英文词数的冗余 `chars` 字段）：\n  - `dialogue_over_budget` / `dialogue_slow_over_budget`：`{\"code\", \"severity\": \"warn\", \"shot\": int, \"raw_shot\": int, \"duration\": float, \"count\": int, \"unit\": \"chars\"|\"words\", \"min_seconds\": float, \"rate_band\": \"fast\"|\"slow\", \"line_preview\": str}`。\n  - `dialogue_long_line`：同上字段，其中 `rate_band: \"normal\"`（表示未超语速预算但超过拆镜字/词数阈值）。\n  - `dialogue_speech_unset`：`{\"code\": \"dialogue_speech_unset\", \"severity\": \"warn\", \"shot\": null, \"affected_shots\": [int, ...], \"affected_raw_shots\": [int, ...]}`。"},{"oldText":"`report()` 保持原函数签名不变，内部直接调用 `check_dialogue_budget(film)`；若有告警，在 `report.md` 中输出「脚本预检提示」小节（无告警时省略该小节）。","newText":"`report()` 保持原函数签名不变，内部直接调用 `check_dialogue_budget(film)`；若有告警，在 `report.md` 顶部基础摘要之后、逐段提示词详情之前输出「脚本预检提示」Markdown 表格（`镜头 | 类型 | 镜长 / 估算 | 字/词数 | 台词片段`，无告警时省略该小节）。"},{"oldText":"- **工作台前端（`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`）**：\n  - 复用 `.notice.warning`（补齐 `border: 1px solid #eddfb9`，符合 `DESIGN.md` `#81612a` on `#fff8e8`），挂载于 `#film-parse-status` 之后、`.film-reference-section` 之前。\n  - 渲染优先按告警结构化字段（`code`/`shot`/`duration`/`count`/`unit`/`min_seconds`/`affected_shots`）通过 `workbench/static/messages/{zh-CN,en}.json` 的 `t(...)` 动态生成（未知 `code` 时回退 `w.message`），确保顶部切换 `zh-CN` ↔ `en` 时 `renderPage()` 立即重绘而无旧语言残留；在 `invalidateFilmReferences()`、空输入或解析失败时同步清空 `state.filmWarnings`。\n- **国际化职责边界（全部三套均经 `tests/test_locale_parity.py` 校验）**：\n  - `h3ref/messages/{zh-CN,en}.json`：CLI 日志与 `report.md` 的 4 类告警文案、预检小节标题，以及 `chars`/`words` 单位本地化键。\n  - `workbench/messages/{zh-CN,en}.json`：`/api/film/references` 服务端 `warnings[].message` 的 4 类告警文案，以及 `chars`/`words` 单位本地化键。\n  - `workbench/static/messages/{zh-CN,en}.json`：创建页 Warning 卡片标题、辅助说明、单条告警模板（含 `chars`/`words` 单位本地化键）与忽略继续提示。","newText":"- **工作台前端（`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`）**：\n  - 复用 `.notice.warning`（补齐 `border: 1px solid #eddfb9` 与 `.notice.warning .hint{color:#81612a}`，符合 `DESIGN.md` `#81612a` on `#fff8e8`，并回归核对 `base.html:36,43`），作为 `<section id=\"film-warnings\" class=\"notice warning film-warnings\" aria-labelledby=\"film-warnings-title\" hidden>` 挂载于 `#film-parse-status` 之后、`.film-reference-section` 之前。\n  - 采用风险分层 + 渐进披露（超预算类置前、`dialogue_long_line` 置后，组内按镜序；1 行紧凑摘要头含「仅提示，不阻止继续」标签；默认内联展示前 3 条，超 3 条用原生 `<details>` 折叠其余项，待 Phase 4 `d3` 终审确认），不设「永久忽略/关闭」按钮，且对每条逐镜告警提供轻量非修改「定位到脚本」按钮（用 `raw_shot` + 清洗前锚点在 `#film-json` 中 `setSelectionRange` + `focus()`）；当 `state.filmWarnings.length > 0` 时在底部 `#submit-hint` 追加轻量回响及跳回 `#film-warnings` 锚点。\n  - 渲染按告警结构化字段通过 `workbench/static/messages/{zh-CN,en}.json` 的 `t(...)` 动态生成（未知 `code` 显示当前语言通用提示并将服务端 `w.message` 折叠在技术详情中，杜绝语言切换残留），切换 `zh-CN` ↔ `en` 时保持展开状态与焦点；`invalidateFilmReferences(clearBindings, clearSlots)` 仅在 `clearSlots === true`、空输入或解析失败时清空 `state.filmWarnings`，250ms 输入防抖与请求期间保留旧卡片并加 `.is-stale`（`opacity:.6` + `aria-busy=\"true\"`）防按键跳动。\n- **国际化职责边界（全部三套均经 `tests/test_locale_parity.py` 校验）**：\n  - `h3ref/messages/{zh-CN,en}.json`：CLI 日志与 `report.md` 的 4 类告警文案（明确区分「超预算风险估算」与「偏长建议拆镜」因果，禁用「至少/保证念完」）、预检小节标题，以及 `chars`/`words` 单位本地化键。\n  - `workbench/messages/{zh-CN,en}.json`：`/api/film/references` 服务端 `warnings[].message` 的 4 类告警文案，以及 `chars`/`words` 单位本地化键。\n  - `workbench/static/messages/{zh-CN,en}.json`：创建页 Warning 卡片标题、非阻断标签、启发式/非中文未校准说明、4 类独立因果模板、定位/展开按钮、未知告警通用回退、部分检查提示（跳过/默认时长镜数）、底部 `#submit-hint` 回响及 `chars`/`words` 单位键。"}]} -->

<!-- autoplan-accepted:design -->
- Fix keystroke layout-thrashing bug and define full 7-state UI lifecycle in `workbench/static/create.js`: only clear `state.filmWarnings` when `clearSlots === true` (file import/reset), empty textarea, or JSON/API error; during 250ms input debounce and in-flight `/api/film/references` requests (`state.filmParsing === true`), keep the previous warning card rendered with `.is-stale` (`opacity: .6`, `aria-busy="true"`) and guard updates with `revision === state.filmRevision`; hide card cleanly on 0 warnings (announcing "preflight clean" once in `#film-parse-status` when transitioning from `>0` to `0`); keep `#film-warnings` inside `#film-fields` so `switchWorkflow()` / `clearNotices()` never wipes it; show warnings identically in Demo and Live modes; surface partial-check counts (`skipped_shots`, `default_duration_shots` returned by `describe_references` in optional `preflight_meta`) when non-zero so clean warnings are never misread as full-script validation.
- Close the edit-and-verify repair loop without auto-mutating JSON: include 1-based original array position `raw_shot` on per-shot warnings and `affected_raw_shots` on `dialogue_speech_unset` (removing redundant greenfield `chars` field — keep `count` + `unit` only), display `(JSON #raw_shot)` whenever `raw_shot != shot`, add a non-mutating "Locate in script" button per shot warning that selects the matching shot/line in `#film-json` via `setSelectionRange` + `focus()`, truncate `affected_shots` display cleanly when `> 6` (first 5 + total count), and include copyable `"speech": "zh"` guidance + cascade notice ("per-shot pacing checks skipped until `speech` is set") in `dialogue_speech_unset`.
- Eliminate `2.0s > 2.0s` rounding contradictions and lock heuristic copy framing: compute `min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10` so every over-budget warning strictly satisfies `min_seconds > duration` (enforced by unit test in `tests/test_dialogue_budget.py`); frame copy across all 3 i18n suites as heuristic pacing risk estimation ("估算约 {min_seconds}s / 当前镜长 {duration}s", forbidding "至少"/"保证念完"); separate causal phrasing for `dialogue_long_line` (too many chars/words in one shot -> split advice, not over-duration) vs over-budget warnings; append an uncalibrated-threshold note only when `speech` is non-`zh`; format `report.md` preflight section as a top Markdown table right after the basic summary and log uniform `preflight [WARN] shot#N ...` lines in `run.log`.
- Enforce `DESIGN.md` visual hierarchy, accessibility, and responsive contract: mount `<section id="film-warnings" class="notice warning film-warnings" aria-labelledby="film-warnings-title" hidden>` between `#film-parse-status` and `.film-reference-section`; add `border: 1px solid #eddfb9` and `.notice.warning .hint { color: #81612a }` (regression-checking `base.html:36,43` `#runtime-drive-box`/`#runtime-confirm`); sort warnings by risk tier (`dialogue_slow_over_budget` / `dialogue_over_budget` before `dialogue_long_line`, ascending `shot` within tier); format numeric counters in `--mono` tabular font; announce warning count changes only via `#film-parse-status` (`aria-live="polite"`, never `role="alert"` on the card); preserve `<details>` expand state and keyboard focus across `zh-CN` ↔ `en` switches; localize unknown warning `code` via generic current-language message with server `w.message` inside collapsible technical details; ensure `>= 44x44px` touch targets on narrow viewports, 4.5:1 text / 3:1 UI contrast, single-column wrapping down to `320px`/`420px` with zero nested scroll or horizontal overflow; append a quiet one-line reminder + jump link to `#film-warnings` inside `#submit-hint` when `state.filmWarnings.length > 0`.
- Specify a zero-dependency offline Phase 2 blind-evaluation packet (`ab_review.html` + `answer_key.json`) generated by the Phase 2 experiment script: tabbed by arm, randomized anonymous Left/Right A/B pairing with matched aspect/playback controls, target action brief, Better/Tie/Worse + failure-reason recording, cost/moderation reveal only after visual vote, and exportable JSON decision log (composable with `uc2` golden set if approved).
- Provisional Taste Decision (`d3`): Progressive disclosure shape for `#film-warnings` — compact 1-line header (`台词语速预检 · 共 N 条提醒` + `仅提示，不阻止继续` badge) + top **3** highest-priority warnings inline + native `<details>` for remaining items when `N > 3` (balancing immediate visibility for 1–3 warnings against keeping `.film-reference-section` above the fold for 15–25 warnings; alternatives: Codex's always-collapsed 1-line summary bar vs Native's top-5 inline, queued for Phase 4 Gate).
<!-- /autoplan-accepted:design -->

### Decision Audit Trail (Phase 2: Design)
| # | Phase | Decision | Classification | Principle | Rationale |
|---|---|---|---|---|---|
| 12 | Design | Fix `invalidateFilmReferences(false, false)` keystroke flicker via `.is-stale` retention + full 7-state UI lifecycle | Mechanical (Auto-fix) | P5 | Clearing `state.filmWarnings` on every keystroke causes 150–300px vertical page jumps (`create.js:685-693`); both Native (Critical F2.1) and Codex (High #4) caught it |
| 13 | Design | Add `raw_shot` / `affected_raw_shots` + non-mutating "Locate in script" (`setSelectionRange` + `focus()`) & remove redundant greenfield `chars` field | Mechanical (Auto-fix) | P1 / P5 | `normalize_shots` skips empty actions causing index drift vs `#film-json` (Native Critical F3.1 + Codex High #2); `chars` on `unit="words"` is a greenfield bug trap (Native F5.2) |
| 14 | Design | Ceiling rounding `min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10` + heuristic estimation copy + distinct `dialogue_long_line` causal template | Mechanical (Auto-fix) | P5 | Prevents `2.0s > 2.0s` contradiction on `examples/film.example.json` +1 char (`2.033s`) and separates over-budget vs long-line causes (Native F3.2/F5.1 + Codex High #3) |
| 15 | Design | Semantic `<section aria-labelledby>`, `#film-parse-status` polite count announce, focus/expand persistence on locale toggle, localized generic fallback for unknown `code`, `44px` touch targets, `320px`/`420px` wrap, `.notice.warning .hint` color + `#submit-hint` echo | Mechanical (Auto-fix) | P5 | Completes `DESIGN.md` alignment, WCAG contrast/a11y, mobile responsiveness, and zero stale-language fallback (Native F2.2/F4.1/F4.2/F5.3 + Codex #5/#6/#7) |
| 16 | Design | Zero-dependency offline Phase 2 blind-review HTML board (`ab_review.html` + `answer_key.json`) | Mechanical (Auto-fix) | P1 | Makes 7+ arm human blind evaluation actually executable without file-name leakage or review fatigue (Native F3.3 + Codex #8) |
| 17 | Design | `d3`: Default disclosure shape for `#film-warnings` (compact header + top 3 inline + `<details>` rest vs always-collapsed summary vs top 5 inline) | **TASTE DECISION** | P3 / P6 | Valid UX trade-off between Native (top 5 inline) and Codex (collapsed summary bar); provisional default picks top 3 inline + `<details>` and queues `d3` for Phase 4 Gate |

### Step 0: Design Scope Assessment
- **Initial Plan Design Completeness**: **4 / 10** → **Post-Fix: 9 / 10** (algorithm/API was 9/10, but UI states, keystroke stability, edit-locate bridge, progressive disclosure, a11y, responsive CSS, and Phase 2 blind evaluation were unspecified).
- **DESIGN.md Alignment**: Audited [DESIGN.md](../../DESIGN.md) (calm offline studio desk, `#f3f5f2` canvas, `#ffffff` cards, Warning `#81612a` on `#fff8e8` + `#eddfb9`, `--mono` counters, `68ch` prose cap, `1060px`/`760px`/`420px` breakpoints, `2px solid var(--accent)` focus ring, `prefers-reduced-motion: reduce`, Paid-action clarity).
- **Existing UI Patterns Reused**: `.notice.warning` ([style.css:223](../../workbench/static/style.css#L223)), `.segments-rewrite-toolbar.live-paid .hint` ([style.css:194](../../workbench/static/style.css#L194)), `#film-parse-status` `aria-live="polite"` ([index.html:30](../../workbench/templates/index.html#L30)), `state.filmRevision` ([create.js:104,134](../../workbench/static/create.js#L104-L134)), `clearSlots` preservation pattern ([create.js:103-107](../../workbench/static/create.js#L103-L107)), native `<details>` styling ([style.css:208-210](../../workbench/static/style.css#L208-L210)), `#submit-hint` ([index.html:45](../../workbench/templates/index.html#L45)).

### Step 0.5: Dual Voices (Design)

#### Claude SUBAGENT (design — independent review, `gemini-4-neon`)
- **Snapshot Verified**: `INPUT: design ef09c26293cb076f44df9d6a5a18f00dfc34f9131a2588e6b34d824dcc636be4` (exact match).
- **Key Findings (11 issues: 2 Critical, 4 High, 5 Medium)**:
  - **Critical F2.1**: Unconditionally clearing `state.filmWarnings` in `invalidateFilmReferences()` causes 150–300px layout bounce on every keystroke (`create.js:685-693`); clear only on `clearSlots === true`, keep `.is-stale` while `filmParsing`.
  - **Critical F3.1**: `normalize_shots` skips empty-action drafts so `shot` drifts from `#film-json` object order; add `raw_shot` + one-click locate button in `#film-json`.
  - **High F1.1**: 10–25 warnings push `.film-reference-section` off-screen; risk-tier sorting + compact header with non-blocking badge + cap top 5 with collapse.
  - **High F2.2 & F4.1**: 5 missing UI states (loading/importing, clean-after-fix, `dialogue_speech_unset` cascade notice, Tab switch vs `clearNotices()`, Demo vs Live), `aria-live="polite"` (never `role="alert"`), `affected_shots` truncation > 6, no permanent Dismiss button.
  - **High F3.2**: `raw_min=2.033` rounds to `2.0s` against `2.0s` duration on `film.example.json` +1 char; use ceiling `math.ceil(raw_min*10-1e-9)/10`.
  - **High F3.3**: Phase 2 7+ arm blind evaluation needs an offline zero-dependency single-page HTML board (`ab_review.html` + `answer_key.json`).
  - **Medium F1.2 / F4.2 / F5.1–F5.3**: `report.md` top Markdown table + `run.log` prefix + Step 03 non-goal; `.notice.warning .hint` amber color & `base.html:36,43` border regression check; separate `dialogue_long_line` causal copy; drop greenfield `chars` field; `#submit-hint` quiet echo linking back to `#film-warnings`.

#### Codex SAYS (design — UX challenge, `gpt-6-astra`)
```tool-output
CODEX_MODEL: gpt-6-astra (exec; role: plan-review, tier: frontier; source: gstack catalog frontier/openai (verified 2026-10-07))
OUTSIDE_STATUS: completed provider=codex host=claude
VERDICT: FINDINGS (4 High, 4 Medium; FINDINGS: P1)

1. High — 告警列表会挤占主任务，且与付费提示争夺注意力：默认只显示“发现 N 项台词风险，可继续创建”的紧凑摘要，展开后先显示最多 3 条并提供“查看全部”；预算不足排在单纯长句之前；付费提示留在提交区。
2. High — 有告警，没有可完成的修复路径；镜头编号还会指错编辑位置：返回原始数组位置用于定位；每条显示台词预览并提供“定位到脚本”；为四类告警分别给出具体修改建议；WARN 不加确认弹窗、不禁用提交、不自动改时长。
3. High — 启发式说明已采纳，但界面的数值承诺仍未定义：锁定文案为“台词时长风险估算”，显示“估算约 X 秒／当前镜长 Y 秒”，禁止“至少/保证念完”；消除 2.0s 超出 2.0s 舍入边界矛盾；非中文标未校准。
4. High — 清空数组不等于定义状态，部分检查尤其容易被误读：明确空输入/检查中(.is-stale)/失败/无告警/部分检查（跳过镜数与默认时长镜数）完整状态表；保留 revision 校验防旧响应覆盖。
5. Medium — 响应式设计没有落到这个组件：规定窄屏单列、操作按钮移至下一行、自然换行与断词、无嵌套滚动；验收覆盖 320px/桌面/200% 缩放/中英文/25 条告警。
6. Medium — 可访问性只有颜色，没有新增交互的契约：使用具名区域、语义列表及原生 details/summary；仅在检查完成后通过 polite 状态区播报数量；语言切换与折叠保留焦点与展开状态；>=44x44px 触控目标；4.5:1 正文与 3:1 控件对比度。
7. Medium — 未知告警的回退方式违背“切换语言无残留”的承诺：未知 code 显示当前语言通用提示，原服务端文本放入技术详情；新增状态/计数/按钮全部进语言包。
8. Medium — 人工盲评被列为门禁，却没有设计评审者的判断流程：固定离线评审包，随机匿名 A/B，一致播放条件，记录更好/持平/更差及原因，视觉判断后再揭示成本与审核结果并版本化。
```

#### Design Litmus Scorecard (Dual-Voice Consensus)
| # | Design Dimension | Native Design Voice | Codex Design Voice (`gpt-6-astra`) | Consensus | Resolution |
|---|---|---|---|---|---|
| 1 | Information Hierarchy & Progressive Disclosure | High (F1.1: risk-tiered, non-blocking badge, top 5 inline + collapse) | High (#1: risk-tiered, collapsed 1-line summary + top 3 + view all) | **CONFIRMED** on hierarchy; **DISAGREE** on default cap (`5` vs `0/3`) | Auto-fix hierarchy/sorting/badge; provisional top-3 inline + `<details>` rest, queued as **TASTE DECISION `d3`** |
| 2 | Actionable Repair & Index Bridging | Critical (F3.1: `raw_shot` + locate in `#film-json`) | High (#2: raw index + locate + 4-type guidance, no auto-mutate) | **CONFIRMED** | Auto-fix (`raw_shot` / `affected_raw_shots` + non-mutating Locate button) |
| 3 | Heuristic Framing & Rounding Precision | High (F3.2 ceiling rounding + F5.1 `long_line` copy) | High (#3 estimation copy + boundary contradiction + non-zh tag) | **CONFIRMED** | Auto-fix (`math.ceil(raw_min*10-1e-9)/10`, forbid "至少", distinct `long_line` copy) |
| 4 | Interaction State Machine & Flicker | Critical (F2.1 `.is-stale` + F2.2 5 missing states) | High (#4 full state table + partial check skipped/default counts) | **CONFIRMED** | Auto-fix (clear only on `clearSlots`/empty/error, `.is-stale`, `preflight_meta`) |
| 5 | Responsive & Visual System (`DESIGN.md`) | Medium (F4.2 `.hint` color, `base.html` check, `420px`) | Medium (#5 `320px`/200% zoom/no nested scroll/25 warnings) | **CONFIRMED** | Auto-fix (full CSS tokens + responsive test matrix) |
| 6 | Accessibility & Unknown-Code i18n | High (F2.2/F4.1 `aria-live="polite"`, `affected_shots` cap) | Medium (#6/#7 semantic list, focus/expand keep, 44px, generic unknown fallback) | **CONFIRMED** | Auto-fix (all a11y + locale fallback rules locked) |
| 7 | Phase 2 Human Blind Evaluation UX | High (F3.3 offline `ab_review.html` + `answer_key.json`) | Medium (#8 fixed anonymous randomized A/B packet + post-vote reveal) | **CONFIRMED** | Auto-fix (zero-dependency offline blind-review HTML packet) |

**Consensus Summary**: **6/7** dimensions CONFIRMED across both voices; **1/7** UX disclosure disagreement → **TASTE DECISION `d3`** for Phase 4 Gate.

### Passes 1–7 (Plan Design Review)

- **Pass 1 — Information Architecture (`4/10 → 9/10`)**:
  - Primary task order preserved: `#film-json` → `#film-parse-status` (blocking syntax/API status) → `#film-warnings` (compact, non-blocking preflight card) → `.film-reference-section` (primary upload task) → Demo/Live & Submit (`#submit-hint` quiet echo).
  - Risk-tier ordering inside `#film-warnings`: `dialogue_speech_unset` (top if present) → `dialogue_slow_over_budget` / `dialogue_over_budget` (sorted by `shot`) → `dialogue_long_line` (sorted by `shot`).
  - `report.md` preflight table placed right after top basic summary, before per-segment prompt breakdown; `run.log` uses uniform `preflight [WARN] shot#N ...` lines.
- **Pass 2 — Interaction State Coverage (`3/10 → 9/10`)**:
  | State | Trigger | `#film-warnings` Visual & DOM | `#film-parse-status` (`aria-live="polite"`) |
  |---|---|---|---|
  | **Empty / Unexamined** | Blank `#film-json` | `hidden`, cleared | Default hint |
  | **Importing** | `#film-import` change (`reset: true`) | `hidden`, cleared immediately | Existing importing status |
  | **Debouncing / Parsing** | Typing in `#film-json` (`filmParsing=true`, `clearSlots=false`) | Keeps previous card dimmed (`.is-stale`, `opacity:.6`, `aria-busy="true"`) — zero layout jump! | Updating status (no alert spam) |
  | **Error** | JSON syntax error or `/api/film/references` 4xx/5xx | `hidden`, cleared | Existing red error message |
  | **Clean (0 warnings)** | Response `warnings: []` & `revision === state.filmRevision` | `hidden` (quiet desk) | If `prevCount > 0`, announce once: "未发现此类风险 / No pacing risks found"; if partial (`skipped_shots > 0` or `default_duration_shots > 0`), append concise partial-check note |
  | **Warnings (`1..N`)** | Response `warnings.length > 0` | Visible `<section>`, top 3 inline + `<details>` for `N > 3` (`d3` provisional); each row has `--mono` metric + preview + Locate button; `#submit-hint` adds quiet echo | Announce count only ("发现 N 条台词语速提醒") |
  | **Tab Switch / Demo vs Live** | `one` ↔ `film`, Demo ↔ Live | Preserved inside `#film-fields` (untouched by `clearNotices()`); shown identically in Demo & Live | Unchanged |
- **Pass 3 — User Journey & Emotional Arc (`4/10 → 9/10`)**:
  ```text
  Paste/Import 25-shot JSON
    -> [Relief] Compact amber header ("3 条提醒 · 不阻止继续") — upload cards still visible!
    -> [Clarity] Top over-budget shot shows "--mono 3.0s · 估需约 5.2s (28 字)" + [定位到脚本]
    -> [Flow] Click [定位] -> cursor jumps right to raw_shot in #film-json -> edit line -> card dims (.is-stale, NO page jump!)
    -> [Closure] 250ms later -> warning disappears cleanly -> upload portraits -> bottom #submit-hint confirms 0 remaining reminders
  ```
  Phase 2 Blind Review Journey: Run experiment script → open local offline `ab_review.html` → compare randomized anonymous Left/Right frames & videos → record Better/Tie/Worse + failure tag → export JSON & reveal `answer_key.json` cost/moderation table.
- **Pass 4 — AI Slop Risk & Aesthetic Craft (`7/10 → 9/10`)**:
  - Strictly preserves `DESIGN.md` calm studio aesthetic: no toast popups, no modal confirmations, no pulsing badges, no gradients or icon bloat. Numeric metrics use `var(--mono)` with `font-variant-numeric: tabular-nums`; prose stays `<= 68ch`; amber `#81612a` on `#fff8e8` is clearly disambiguated from bottom paid `.run-settings.live` via the explicit "仅提示，不阻止继续" inline tag.
- **Pass 5 — Design System Alignment (`5/10 → 9/10`)**:
  - Fix `.notice.warning` ([style.css:223](../../workbench/static/style.css#L223)) with `border: 1px solid #eddfb9` and add `.notice.warning .hint { color: #81612a }` (matching `.segments-rewrite-toolbar.live-paid .hint` at [style.css:194](../../workbench/static/style.css#L194)); verify `#runtime-drive-box` and `#runtime-confirm` ([base.html:36,43](../../workbench/templates/base.html#L36-L43)) gain crisp consistent borders without regression.
- **Pass 6 — Responsive & Accessibility (`4/10 → 9/10`)**:
  - **Responsive**: Single-column flex/grid layout inside `.film-warnings-item` with `flex-wrap: wrap; min-width: 0; overflow-wrap: anywhere`; Locate button wraps cleanly to its own row on `<= 760px` / `420px` / `320px`; zero nested scrollbars; verified at `320px`, `420px`, `760px`, desktop `1380px`, `200%` browser zoom, both `zh-CN` and `en`, with 25 warnings.
  - **Accessibility**: `<section id="film-warnings" aria-labelledby="film-warnings-title">` + semantic `<ul>` + native `<details><summary>` (keyboard Enter/Space built-in); never use `role="alert"`; count announcements routed through `#film-parse-status` (`aria-live="polite"`); preserve `<details>` open state and active focus across `renderPage()` locale switches; `>= 44×44px` tap targets on mobile; `#81612a` on `#fff8e8` verified `>= 4.5:1` text contrast and `>= 3:1` focus/control contrast (`2px solid var(--accent)`).
- **Pass 7 — Unresolved Design Decisions (`4/10 → 9/10`)**:
  - Resolved mechanically: `dialogue_long_line` independent causal copy; remove redundant `chars` key; `affected_shots` truncation `> 6` (`前 5 镜 + 共 N 镜`); localized generic fallback + collapsible technical detail for unknown `code`; `#submit-hint` quiet echo; Step 03 (`job.html`) explicitly out of scope for Phase 1.
  - Queued for Phase 4 Gate: **TASTE DECISION `d3`** (default disclosure cap: top 3 inline + `<details>` vs always-collapsed summary vs top 5 inline).

### NOT in scope (Design)
- **Step 03 (`workbench/templates/job.html`) preflight summary banner**: Explicitly excluded from Phase 1 (reviewers can inspect `report.md` / `run.log`); logged below as an optional future TODO.
- **Visual storyboard timeline editor / auto-duration fixer**: Rejected for Phase 1 (keep `#film-json` + non-mutating Locate bridge; auto-duration rewrite stays behind `uc1` / E7).
- **Web-hosted A/B evaluation platform**: Out of scope; Phase 2 uses a zero-dependency local static `ab_review.html` file only.

### What already exists (Design)
- [DESIGN.md](../../DESIGN.md): Complete token, typography, breakpoint (`1060px`/`760px`/`420px`), focus-ring, and paid-action clarity spec.
- [style.css:194,208-210,223](../../workbench/static/style.css#L194-L223): Existing amber `.hint`, native `<details>` styling, and `.notice.warning` surface.
- [create.js:103-107,134,685-693](../../workbench/static/create.js#L103-L134): Existing `clearSlots` stability pattern and monotonic `state.filmRevision` race guard.
- [index.html:30,45](../../workbench/templates/index.html#L30-L45): Existing `#film-parse-status` (`aria-live="polite"`) and `#submit-hint`.

### TODOS.md updates (Design)
- **D-TODO-1 (Optional Design Debt)**: *Step 03 (`job.html`) preflight echo when `frames="none"`* — What: Show a collapsed 1-line preflight warning count above `#script-plan` on the Step 03 review page if warnings existed. Why: Users running via CLI or skipping warnings on Step 02 won't see them before approving Step 03 paid video generation. Pros: Closes Step 02 → Step 03 visibility gap. Cons: Requires reading/persisting preflight summary in `job` payload. Context: `workbench/templates/job.html` + `workbench/static/job.js`. Depends on: Phase 1 `check_dialogue_budget()`.

### Implementation Tasks (Design Review)
- [ ] **T5 (P1, human: ~2h / CC: ~10min)** — `h3ref/film.py`, `workbench/references.py` — Upgrade warning schema & rounding (`math.ceil(raw_min * 10 - 1e-9) / 10`, `raw_shot`/`affected_raw_shots`, drop `chars`, add `preflight_meta` skipped/default counts, top Markdown table in `report.md`, `preflight [WARN] shot#N` in `run.log`)
  - Surfaced by: Design Dual Voices (Native F1.2/F3.1/F3.2/F5.2 + Codex #2/#3/#4)
  - Files: `h3ref/film.py`, `workbench/references.py`, `tests/test_dialogue_budget.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_offline.py`
- [ ] **T6 (P1, human: ~4h / CC: ~20min)** — `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, `workbench/static/messages/*.json` — Implement `#film-warnings` 7-state UI, `.is-stale` anti-flicker, risk-tiered progressive disclosure (`d3`), non-mutating `#film-json` Locate bridge, `#submit-hint` echo, unknown-code localized fallback, responsive `320px`/`420px` + WCAG a11y + `base.html:36,43` border check
  - Surfaced by: Design Dual Voices (Native F1.1/F2.1/F2.2/F3.1/F4.1/F4.2/F5.1/F5.3 + Codex #1/#2/#4/#5/#6/#7)
  - Files: `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, `workbench/static/messages/zh-CN.json`, `workbench/static/messages/en.json`, `h3ref/messages/*.json`, `workbench/messages/*.json`, `tests/test_workbench_ui.js`, `tests/test_locale_parity.py`
  - Verify: `.venv/bin/python -m unittest tests/test_locale_parity.py tests/test_reference_bindings.py && node tests/test_workbench_ui.js`
- [ ] **T7 (P2, human: ~3h / CC: ~15min)** — Phase 2 experiment script — Generate zero-dependency offline blind-evaluation packet (`ab_review.html` + `answer_key.json`) with randomized anonymous A/B, synced media controls, Better/Tie/Worse + failure-reason recording, and post-vote cost/moderation reveal
  - Surfaced by: Design Dual Voices (Native F3.3 + Codex #8)
  - Files: Phase 2 experiment branch script (unmerged until Phase 3 / `uc2` decision)
  - Verify: Open generated `ab_review.html` offline (no network requests), confirm labels hidden until reveal, export JSON ratings

### Design Review Completion Summary
```text
  +====================================================================+
  |            DESIGN PLAN REVIEW — COMPLETION SUMMARY                 |
  +====================================================================+
  | Initial / Final Score| 4/10 -> 9/10                                |
  | DESIGN.md Alignment  | Verified (amber tokens, --mono, 68ch, a11y) |
  | Pass 1 (Info Arch)   | 4/10 -> 9/10 (risk-tiered + non-block badge)|
  | Pass 2 (States)      | 3/10 -> 9/10 (7-state table + .is-stale)    |
  | Pass 3 (Journey)     | 4/10 -> 9/10 (Locate bridge + ab_review)    |
  | Pass 4 (AI Slop)     | 7/10 -> 9/10 (calm desk, zero toast/modal)  |
  | Pass 5 (System)      | 5/10 -> 9/10 (border + .hint + base.html)   |
  | Pass 6 (Resp & A11y) | 4/10 -> 9/10 (320px/420px, polite, 44px)    |
  | Pass 7 (Unresolved)  | 4/10 -> 9/10 (long_line copy, -chars, d3)   |
  +--------------------------------------------------------------------+
  | Dual Voices          | Native (11 issues) + Codex (8 concerns)     |
  | Litmus Consensus     | 6/7 CONFIRMED; 1 DISAGREE -> Taste d3       |
  | Tasks Persisted      | 3 tasks (T5, T6, T7) via gstack-tasks       |
  | Visual Mockups       | Skipped (autoplan non-interactive mid-run)  |
  | Unresolved Decisions | 0 mid-run (1 Taste d3 -> Phase 4 Gate)      |
  +====================================================================+
```

---

## Phase 2.5: DX Review

<!-- autoplan-baseline-edits:dx {"sourceSha256":"046e362ff84f8522ed15dd308dd5e2ed39432b67b2f93406bed605f55360b363","replacements":[{"oldText":"  - 清洗前缀并计数后若 `count == 0`（如纯标点 `\"……\"` 或纯标签 `\"（小林）\"`），直接跳过该镜语速与超长句告警（不误报 `dialogue_over_budget`）。","newText":"  - 清洗前缀并计数后若 `count == 0`：若清洗后不含任何字母/文字字符（如纯标点 `\"……\"` 或纯标签 `\"（小林）\"`），安全跳过该镜（不误报 `dialogue_over_budget`）；若含未覆盖脚本文字（如俄语/阿拉伯语/泰语等非拉丁非 CJK 字母），计入 `meta.unsupported_script_shots`、将检查状态标为 `\"unsupported\"`/`\"partial\"` 并发出 `dialogue_unsupported_script`，绝不静默伪报「检查通过」。"},{"oldText":"- **4 类告警确定性 Schema**（`shot` 与 `affected_shots` 均为**归一化后镜头列表的 1-based 序号**，与 `frames.py:184`「第 N 镜」和 `film.py:243` `[Shot N]` 一致；同时附带原始 JSON 数组位次 `raw_shot` / `affected_raw_shots` 以消除草稿跳过空镜导致的编辑器定位错位；移除绿色新建接口中无消费者且易误导英文词数的冗余 `chars` 字段）：\n  - `dialogue_over_budget` / `dialogue_slow_over_budget`：`{\"code\", \"severity\": \"warn\", \"shot\": int, \"raw_shot\": int, \"duration\": float, \"count\": int, \"unit\": \"chars\"|\"words\", \"min_seconds\": float, \"rate_band\": \"fast\"|\"slow\", \"line_preview\": str}`。\n  - `dialogue_long_line`：同上字段，其中 `rate_band: \"normal\"`（表示未超语速预算但超过拆镜字/词数阈值）。\n  - `dialogue_speech_unset`：`{\"code\": \"dialogue_speech_unset\", \"severity\": \"warn\", \"shot\": null, \"affected_shots\": [int, ...], \"affected_raw_shots\": [int, ...]}`。","newText":"- **单一事实源返回与确定性 Schema**：新增 `inspect_dialogue_budget(film: dict, *, rate_config: dict | None = None) -> dict` 返回 `{\"status\": \"complete\"|\"partial\"|\"unsupported\"|\"invalid\", \"warnings\": [...], \"meta\": {\"shots_invalid\": bool, \"shots_missing\": bool, \"total_raw_shots\": int, \"checked_shots\": int, \"skipped_shots\": int, \"default_duration_shots\": int, \"unsupported_script_shots\": int}}`，`check_dialogue_budget(film, *, rate_config=None) -> list[dict]` 作为薄包装返回 `warnings`。`shot`/`affected_shots` 为归一化 1-based 序号，`raw_shot`/`affected_raw_shots` 为原始数组 1-based 位次，并附 0-based `raw_shot_index` 供自动化 Agent 回写 JSON；`rate_band` 统一为实际估算语速档 `\"regular\"|\"slow\"`：\n  - `dialogue_over_budget` / `dialogue_slow_over_budget`：`{\"code\", \"severity\": \"warn\", \"shot\": int, \"raw_shot\": int, \"raw_shot_index\": int, \"duration\": float, \"count\": int, \"unit\": \"chars\"|\"words\", \"min_seconds\": float, \"rate_band\": \"regular\"|\"slow\", \"line_preview\": str}`。\n  - `dialogue_long_line`：同上字段（`rate_band` 反映该镜实际使用的 `\"regular\"|\"slow\"` 档位，告警类别由 `code` 区分）。\n  - `dialogue_speech_unset`：`{\"code\": \"dialogue_speech_unset\", \"severity\": \"warn\", \"shot\": null, \"affected_shots\": [int, ...], \"affected_raw_shots\": [int, ...]}`。\n  - 结构/覆盖诊断：`dialogue_shots_invalid`（`strict=False` 收到非 list `shots` 时由 `inspect_dialogue_budget` 发出，防止静默返回 `[]`）与 `dialogue_unsupported_script`（未覆盖文字脚本时发出）。"},{"oldText":"| **阶段 2** | **小样本单变量 A/B 实测（独立实验分支/脚本，不污染默认提示词）**：基于 `examples/film.example.json` 与 3–5 个历史痛点镜头夹具（摘镜/脱衣叠加、多人同框、动作冲突转译、视觉否定句「面无笑意/不发光」、惊吓/疼痛大情绪），拆分为 6 个单变量试验臂独立对照基线——**Arm 2A**（项 2 构图规则 + `frames.py:209,217` 覆盖优先级 + `rewrite.RULES` 规则 9）、**Arm 3A**（`NEUTRAL_WORDS` 精确词表）、**Arm 3B**（`describe_composition` 动作安全转译示例）、**Arm 3C**（`frames.py:TAIL` 正向化对照）、**Arm 3D**（`rewrite.RULES` 第 5 条视觉否定词零词根正向化）、**Arm 4**（FACS 四区肌肉指引） | 输出对照记录（附实测图片/片段对比、APIMart 审核通过率、单镜/单段提示词字符数与单镜估算费用增幅），由用户肉眼评判；量化门槛：审核通过率 ≥ 基线、人工对照净正向、字符与费用增幅 ≤ 15% | **停下等用户确认**：逐臂决定合入或裁剪（含 `d1` 是否启用窄正则），严禁未通过单臂混入默认流程 |","newText":"| **阶段 2** | **模态解耦单变量 A/B 实测（独立实验脚本 `scripts/ab_eval.py`，支持 `--arm`、`--fixture`、`--dry-run`、`--max-cny`、`--resume` 与 `ab_state.json` 断点防重付，按臂隔离改写缓存、仅共享冻结首帧并记录规则/模型指纹）**：① 纯生图臂（15–20 个对抗/常规镜头）——**Arm 3A**（`NEUTRAL_WORDS`）、**Arm 3B**（`describe_composition` 动作转译示例）、**Arm 3C-neg**（`TAIL` 否定句）与 **Arm 3C-light**（`TAIL` 布光）；② 纯视频臂（复用冻结首帧 + 常规对照镜）——**Arm 3D**（`rewrite.RULES` 第 5 条高激活视觉否定词 + 2 个正常否定对照镜）与 **Arm 4**（FACS 四区肌肉指引 + 克制表演对照镜），同步验证 `d2` few-shot 样例对比；③ 跨模态臂 **Arm 2A**（项 2 双态资产覆盖 + 图像/视频强锁对齐 + `rewrite.RULES` 规则 9）；LLM 步骤双测非 Claude 默认模型（DeepSeek/Gemini）与 Claude | 生成零依赖离线单页盲评板 `ab_review.html` + `answer_key.json`（随机匿名左右对照、先评画质/叙事忠实度再揭晓费用与审核结果），按「可交付验收总成本 + 重试次数 + 叙事忠实度 + 审核通过率（0 新增 `ContentPolicyError`）」评判；阶段 3 合入前执行 1 次组合态回归门禁（系统提示词累计增幅 ≤ +20%、零规则冲突） | **停下等用户确认**：逐臂决定合入或裁剪（含 `d1`/`d2`），严禁未通过单臂混入默认流程 |"},{"oldText":"- 测试与文档：`tests/test_dialogue_budget.py`、现有 `frames` / `rewrite` / `workbench` / `test_locale_parity.py` 测试、`SKILL.md`","newText":"- 测试与文档：`tests/test_dialogue_budget.py`、现有 `frames` / `rewrite` / `workbench` / `test_locale_parity.py` 测试、`docs/workbench.md`（`#dialogue-preflight` 锚点与 `curl` 示例）、`README.md`、`SKILL.md`"}]} -->

<!-- autoplan-accepted:dx -->
- Eliminate both DX CAP4 blockers (never signal malformed input or uncovered scripts as "preflight clean"): extract `h3ref/film.py:inspect_dialogue_budget(film, *, rate_config=None) -> dict` as the single source of truth returning `{"status": "complete"|"partial"|"unsupported"|"invalid", "warnings": [...], "meta": {"shots_invalid", "shots_missing", "total_raw_shots", "checked_shots", "skipped_shots", "default_duration_shots", "unsupported_script_shots"}}` (with `check_dialogue_budget()` as a thin wrapper); raise `FilmValidationError(ValueError)` on non-dict `film` in SDK, return HTTP 400 RFC 9457 when `film.shots` is present and non-list in `describe_references()`, emit `dialogue_shots_invalid` in `strict=False` fallback, emit `dialogue_unsupported_script` when non-empty post-strip dialogue has uncovered non-Latin/non-CJK letters (`count == 0`), require `status === "complete" && warnings.length === 0` before announcing "preflight clean", and restrict bare-colon speaker stripping so non-speaker imperatives like `记住：千万别回头` are not stripped.
- Preserve `raw_shot` / `raw_shot_index` (0-based) and default-duration provenance across the full CLI `load() -> run() -> report()` chain without leaking private keys into `state.json` or `result["film"]`: carry shot provenance from initial normalization through `load()` into `inspect_dialogue_budget()` so CLI and Workbench produce identical `raw_shot` and `preflight_meta` (verified by a cross-surface parity test where raw shot #1 has empty `action` and raw shot #2 is over budget); share `normalize_speech(raw)` across `inspect_dialogue_budget()`, `compile_prompt()`, and `stage_check()`; unify `rate_band` to `"regular"|"slow"` across all per-shot warnings; print partial/unsupported summary lines in CLI `run.log` and `report.md` when `skipped_shots`, `default_duration_shots`, or `unsupported_script_shots` > 0.
- Meet the agent-ready error contract (resolving all 6 CAP6 gaps): generalize `workbench/server.py:274` `segment_problem()` to `api_problem()` and onboard `POST /api/film/references` for both 400 validation errors (`code`, `field`, `fix`, `doc_anchor: "docs/workbench.md#dialogue-preflight"`, compatible `error`) and 500 unexpected errors (redacted local log + `instance` correlation ID + `recovery` with `side_effects: "none"` + `context`); introduce typed `FilmValidationError(ValueError)` (`code`, `shot_index`, `field`, `actual`, `fix`, `doc_anchor`) for `normalize_shots(..., strict=True)` (citing `H3_MAX_SHOTS` on limit errors, formatted cleanly to `stderr` + non-zero exit in `h3ref.py`); enrich `rewrite_exhausted` (`REWRITE_VALIDATION_EXHAUSTED`, backend/model context, attempt spend notice, actionable fix) and `ContentPolicyError` (`FRAME_MODERATION_BLOCKED`, `frame_key`, `IMAGE_MODEL`, both Workbench and CLI fixes); redact/sanitize `line_preview` control chars in logs; forbid any `d1` narrow regex from entering `cached_problems()` so manual H3 override stays intact; log `NEUTRAL_WORDS` hit summary in `run.log`.
- Harden Phase 2 experiment runner DX & upgrade contract: specify `scripts/ab_eval.py` (`--arm`, `--fixture`, `--dry-run`, `--max-cny`, `--resume`) with resumable `ab_state.json` (recording `(arm, fixture, provider)` before dispatch to prevent double-spend), strict per-arm rewrite cache isolation (sharing only frozen frames), recorded rule/model fingerprints, API-key redaction in logs/`ab_review.html`/`answer_key.json`, and `stderr` + non-zero exit error contract; explicitly extend `docs/workbench.md:163` upgrade guarantees so existing jobs preserve cached prompts/frames by default while documenting explicit opt-in re-rewrite/regenerate steps and cost impact (verified by legacy-job resume test).
- Ship complete public documentation & copy-paste examples alongside code: add `<a id="dialogue-preflight"></a>` in `docs/workbench.md` (4+ warning codes table, `complete`/`partial`/`unsupported` semantics, `POST /api/film/references` copy-paste `curl` + expected response), link from `README.md`, add preflight + `speech` + Item 2/3/4 authoring checklist to `SKILL.md`, and add a lightweight one-click "Copy" button for `"speech": "zh"` in `dialogue_speech_unset` UI guidance.
- Provisional Taste Decision (`d4`): Provide runtime escape hatches for `DIALOGUE_RATE_CONFIG` — Python `rate_config` kwarg plus optional `H3_DIALOGUE_RATE_CONFIG` (JSON/path) and `H3_SKIP_PREFLIGHT=1` env overrides so non-`zh` or fast-dialogue creators can calibrate/silence warnings without editing code (alternative: keep code-constant-only + Python kwarg to avoid extra env surface, queued for Phase 4 Gate).
<!-- /autoplan-accepted:dx -->

### Decision Audit Trail (Phase 2.5: DX)
| # | Phase | Decision | Classification | Principle | Rationale |
|---|---|---|---|---|---|
| 18 | DX | Fix both CAP4 blockers: `inspect_dialogue_budget()` returning `status: complete|partial|unsupported|invalid`, HTTP 400 on non-list `shots`, `dialogue_unsupported_script` for uncovered scripts, safe colon-prefix stripping | Mechanical (Auto-fix) | P1 / P5 | Both Native (CAP4 non-list `shots`) and Codex (CAP4 unsupported scripts like `ru`) caught malformed/uncovered inputs silently signalling "preflight clean" |
| 19 | DX | Preserve `raw_shot` / `raw_shot_index` & default-duration provenance across `film.load() -> run() -> report()`, unify `rate_band` (`regular|slow`), share `normalize_speech()` | Mechanical (Auto-fix) | P5 | Codex #3 proved `load()` drops empty shots before `run()`/`report()`, breaking `raw_shot` parity; Native F4/F5 caught `rate_band` & `compile_prompt()` drift |
| 20 | DX | Onboard `POST /api/film/references` to existing RFC 9457 `api_problem()` + `instance` trace, add typed `FilmValidationError`, enrich `rewrite_exhausted` / `ContentPolicyError` & `scripts/ab_eval.py` error/recovery/cache isolation | Mechanical (Auto-fix) | P1 / P5 | Resolves all 6 CAP6 findings from Native + Codex #4/#6 and reuses `workbench/server.py:274-312` infrastructure |
| 21 | DX | Add `#dialogue-preflight` doc anchor + `curl` example in `docs/workbench.md`, update `README.md` & `SKILL.md`, unify Phase 2 table spec | Mechanical (Auto-fix) | P1 / P5 | Both Native F15 and Codex #5/#7 flagged missing public docs, doc anchors, and Phase 2 table drift |
| 22 | DX | `d4`: Runtime escape hatches for `DIALOGUE_RATE_CONFIG` (`H3_DIALOGUE_RATE_CONFIG` / `H3_SKIP_PREFLIGHT=1` env + Python `rate_config` kwarg vs Python-only) | **TASTE DECISION** | P3 / P5 | Balances opinionated defaults against uncalibrated non-`zh`/fast-comedy creator flexibility; queued for Phase 4 Gate |
| 23 | DX | `uc3`: Add standalone zero-cost CLI subcommand `python h3ref.py preflight --film <path|-> [--json] [--fail-on-warn]` + zero-asset example fixture | **USER CHALLENGE** | P6 | Both Native (High F1) and Codex (High #2) independently flagged missing zero-cost CLI preflight as the #1 TTHW blocker; new CLI surface queued for Phase 4 Gate (original `film.run()`/`report()` preserved as baseline) |

### Step 0: DX Investigation (0A–0G)

#### Product Type & Mode
- **Auto-Detected Product Type**: **CLI Tool + Local Web API/Workbench + Claude Code Skill** (primary: CLI Tool + Local API).
- **Selected Mode**: **DX POLISH** (Autoplan override — maximize rigor across every touchpoint without unapproved surface expansion; `uc3` CLI subcommand queued as User Challenge).

#### 0A. Developer Persona Card
```text
TARGET DEVELOPER PERSONA
========================
Who:       Independent AI short-drama builder / developer (and AI coding agent via SKILL.md)
Context:   Exports Seedance-style film.json from upstream scriptwriters and runs H3 locally via CLI or Workbench to save 10x GPU cost
Tolerance: 3–5 minutes to verify zero-cost value before entering paid API keys; abandons if silent failures waste cloud credits
Expects:   Zero-cost offline validation, copy-paste JSON/curl examples, agent-readable error codes with exact fixes, safe resumable reruns
```

#### 0B. Developer Empathy Narrative (Developer Perspective)
> I just cloned `h3-ref-video` because my Seedance bill is eating my short-drama budget, and the README promises a zero-cost dialogue-pacing preflight before spending a cent on frames or video. I read `README.md` and `h3ref.py`: there's `one`, `film`, `rewrite`, and `account` — wait, where is the command to just check my `film.json`? If I run `python h3ref.py film jobs/test --film examples/film.example.json`, `film.load()` immediately fails because `cast/xiaolin.jpg` doesn't exist, and if I supply dummy images it rushes straight into paid GPT Image 2.6 calls! Fine, I spin up the local Workbench: create `.venv`, activate, `pip install -r requirements-web.txt`, launch `python workbench.py`, open `http://127.0.0.1:7860`, switch to Demo mode, download the example, and import it — 8 steps later, the warning card finally works. Then my scriptwriter sends a Russian draft (`"speech": "ru"`) or a typo (`"shots": {}`) — the UI silently shows zero warnings and tells me "preflight clean"! And when I call `POST /api/film/references` from my automation script with bad JSON, I get a localized Chinese string without a stable error code or `fix` hint. I want a fast, predictable check that tells me honestly when something wasn't checked and exactly what to fix.

#### 0C. Competitive DX Benchmark
- **Benchmark Clock**: Cloned repo + Python 3.10+ installed → first understood, zero-cost dialogue preflight diagnostic on a sample script (observed/estimated from `README.md:64-73` and `h3ref.py:181-225`; web search skipped per offline benchmark disclosure — using direct repo inspection and standard reference CLI benchmarks).

| Tool | Start → Result | Time + Evidence Type | DX Choice | Source |
|---|---|---|---|---|
| `h3-ref-video` (Current plan — Workbench path) | Cloned repo → `.venv` + install + start + Demo toggle + download + import → warning card | ~5–8 min (8 steps, estimated from `README.md:64-73`) | Local web UI with zero-key Demo mode, but 8-step setup | [README.md:64-73](../../README.md#L64-L73) |
| `h3-ref-video` (Current plan — CLI/Agent path) | Cloned repo → run CLI preflight without API key/images | Blocked (requires images + runs paid `film.run()`, observed in `h3ref.py:192-216`, `film.py:83`) | Embedded WARN in `film.run()` only | [h3ref.py:192-216](../../h3ref.py#L192-L216) |
| `h3-ref-video` (Post-DX with `uc3` CLI `preflight`) | Cloned repo → `python h3ref.py preflight --film examples/film.example.json --json` | < 1 min (1–2 steps, estimated; target **Competitive: 2–5 min** for full Workbench, **Champion: < 2 min** for CLI) | Zero-key, zero-image stdout JSON preflight | Proposed via `uc3` |
| Peer reference: `ruff check` / `ffprobe` | Installed CLI → single-command structured JSON report | < 1 min (reference benchmark) | Standalone read-only check subcommand + `--output-format json` + exit codes | Reference standard |
| Peer reference: `manju-laoli-skill` (V7.5.0-SLIM) | 155-file rule library + 14.8k-line regex checker | > 15 min (estimated from repo structure) | Heavyweight upfront ceremony; high cognitive load | Plan background |

- **Target Tier**: **Competitive (2–5 min)** on Workbench zero-key Demo path (and **Champion < 2 min** on CLI/Agent if `uc3` is approved at Phase 4 Gate).

#### 0D. Magical Moment Specification
- **Magical Moment**: Pasting or importing an existing Seedance-style `film.json` (zero API keys, zero image files) and instantly seeing exact pacing risks with a 1-click "Locate in script" jump right into the offending line — before spending a single cent on cloud GPU credits.
- **Delivery Vehicle (DX POLISH)**: Workbench Demo mode + copy-paste `POST /api/film/references` `curl` in `docs/workbench.md#dialogue-preflight` (and zero-key `python h3ref.py preflight --film ... --json` if user approves `uc3`).

#### 0F. Developer Journey Map (6-Stage Trace)
```text
STAGE           | DEVELOPER DOES                           | FRICTION POINTS                                   | STATUS
----------------|------------------------------------------|---------------------------------------------------|--------
1. Discover     | Reads README.md / SKILL.md               | No mention of preflight codes, curl, or anchor    | Fixed (docs/workbench.md#dialogue-preflight + README + SKILL.md)
2. Install      | Sets up .venv & requirements-web.txt     | 8-step Workbench path; Windows has 1-click cmd    | Documented; CLI zero-install path queued in uc3
3. Hello World  | Runs preflight on film.example.json      | CLI film.run() requires cast/xiaolin.jpg + keys   | Workbench Demo works; CLI preflight queued in uc3
4. Real Usage   | Integrates upstream JSON / non-zh script | CAP4 on non-list shots & ru/ar scripts; raw_shot  | Fixed (inspect_dialogue_budget status + raw_shot provenance + rate_config)
5. Debug        | Handles 400/500 API & rewrite/policy err | /api/film/references lacks RFC 9457; 6 CAP6 gaps  | Fixed (api_problem + FilmValidationError + context/fix/recovery)
6. Upgrade      | Pulls Phase 3 prompt upgrades on old job | Cache reuse ignores RULES version; unclear opt-in | Fixed (ab_eval arm isolation + docs/workbench.md:163 opt-in contract)
```

#### 0G. First-Time Developer Confusion Report
```text
FIRST-TIME DEVELOPER REPORT
============================
Persona: Independent AI short-drama builder / AI coding agent
Attempting: Zero-cost dialogue pacing preflight on a 20-shot script

CONFUSION LOG:
T+0:00  Reads README.md & runs `python h3ref.py --help`. Sees `one`, `film`, `rewrite`, `account` — no preflight command. [Queued: uc3]
T+0:45  Tries `python h3ref.py film jobs/test --film examples/film.example.json`. Fails at film.py:83: missing `cast/xiaolin.jpg`. [Queued: uc3]
T+1:30  Starts Workbench, hits `POST /api/film/references` with `{"film": {"shots": "bad"}}` via curl. Gets HTTP 200 `warnings: []`! [FIXED: CAP4 -> 400 RFC 9457 / shots_invalid]
T+2:15  Tests Russian script (`"speech": "ru"`). Regex counts 0 words -> skipped -> UI says "preflight clean"! [FIXED: CAP4 -> status="unsupported" + dialogue_unsupported_script]
T+2:45  Has empty shot #1 in draft, runs CLI `film` -> `report.md` says `raw_shot=1` instead of `2` because `load()` dropped shot #1. [FIXED: provenance carried through load()]
T+3:00  After fixes: clear RFC 9457 errors, honest `status`, exact `raw_shot` parity, and `#dialogue-preflight` docs.
```

### Step 0.5: Dual Voices (DX)

#### Claude SUBAGENT (DX — independent review, `gemini-4-neon`)
- **Snapshot Verified**: `INPUT: dx ff7e84acfefd3081acd1eb5c29a3880b02161c3c556435221e585821da69cf8b` (exact match).
- **Summary (19 findings: 1 Critical CAP4, 9 High incl. 6 CAP6, 9 Medium)**:
  - **CAP4 (Critical #8)**: Non-dict `film` / non-list `shots` silently returns `[]` & `skipped_shots=0` → falsely announces "preflight clean".
  - **CAP6 (High #9–#14)**: `POST /api/film/references` 400/500 not using repo's existing RFC 9457 `segment_problem()` + `instance` trace (`server.py:274-312`); `normalize_shots(strict=True)` throws untyped `SystemExit`; Phase 2 `ab_eval.py` missing resumable `ab_state.json` & error contract; `rewrite_exhausted` & `ContentPolicyError` missing stable codes/context/CLI fix.
  - **High (#1, #3, #15, #17)**: Missing CLI zero-cost `preflight` subcommand; `check_dialogue_budget` vs `preflight_meta` single-source-of-truth contradiction (`inspect_dialogue_budget()`); missing `docs/workbench.md#dialogue-preflight` + `curl` + `README`/`SKILL.md` updates; no runtime escape hatch for `DIALOGUE_RATE_CONFIG`.
  - **Medium (#2, #4–#7, #16, #18–#19)**: Phase 2 `--dry-run`/`--max-cny`; `rate_band` `"fast"`/`"normal"` confusion → `"regular"|"slow"`; `normalize_speech()` drift vs `compile_prompt()`; 0-based `raw_shot_index`; CLI `preflight_meta` logging; 1-click copy for `"speech": "zh"`; forbid `d1` regex in `cached_problems()`; log `NEUTRAL_WORDS` hits.

#### Codex SAYS (DX — developer experience challenge, `gpt-6-astra`)
```tool-output
CODEX_MODEL: gpt-6-astra (exec; role: plan-review, tier: frontier; source: gstack catalog frontier/openai (verified 2026-10-07))
OUTSIDE_STATUS: completed provider=codex host=claude
VERDICT: FINDINGS (4 High, 3 Medium; FINDINGS: P1)

1. High — 未覆盖的文字会被当成“没有风险”，触发 CAP4（如 speech="ru" 俄语计数 0 跳过后宣称 preflight clean；且“记住：千万别回头”会被说话人冒号正则误剥）。
   Recommendation: 明确区分 complete、partial、unsupported 检查状态，对未覆盖文字返回稳定诊断码，并仅剥离可确认的说话人标签 because “未能计数”和“检查通过”必须有不同的机器及界面语义。
2. High — 零费用预检没有独立 CLI 入口，五分钟试用目标缺少可执行路径（h3ref.py:192 无 preflight 子命令，film.load():83 强校验参考图存在；工作台首次需 8 个动作）。
   Recommendation: 增加 python h3ref.py preflight --film examples/dialogue-budget.json --format json，提供随仓库附带的无外部资产示例，禁止该命令调用生成服务 because 开发者应能在配置付费后端之前独立验证核心价值。
3. High — 原始镜头定位契约缺少跨归一化的保存机制（film.run():1223 先调用 load() 过滤空镜并替换 film["shots"]，导致 CLI 路径丢失 raw_shot 与默认时长来源）。
   Recommendation: 在首次归一化时保存明确的原始索引映射，并测试“原始 JSON → load → run/report”与工作台的结果一致 because 定位字段必须指向用户实际编辑的输入，不能依赖已经丢失的信息。
4. Medium — 错误输出尚不满足 agent-ready 契约，单独触发 CAP6（/api/film/references 未走 RFC 9457 缺 code/field/instance；CLI 缺机器输出通道与台词摘要日志脱敏规则）。
   Recommendation: 复用现有问题响应结构，为该接口补齐 code/field/instance 和恢复说明；CLI 将结构化结果放 stdout、诊断放 stderr，并明确预览文本处理规则 because 自动化调用者不应解析中英文句子来决定修什么、怎么重试或去哪里查日志。
5. Medium — 用户文档交付不足，无法保证两分钟内找到完整接入示例（README/docs 未规划更新，缺完整命令/请求/响应/修复示例）。
   Recommendation: 在 README 增加预检入口，并在 docs/ 提供一页完整 CLI/Python/HTTP 示例、四种诊断的修复方式及部分检查语义 because 内部实施计划不能承担公共接口文档的职责。
6. High — 升级后哪些提示词生效不明确，A/B 也可能复用旧产物（film.py:538 H3 缓存未含 rewrite.RULES 或模型版本，Arm 3D/4 续跑可能跳过新规则；旧任务保留与主动重跑边界未明）。
   Recommendation: 为实验隔离每个 arm 的改写缓存，只共享明确冻结的图片；记录实际模型和规则指纹，并补充旧任务保留策略及显式重新改写步骤 because “兼容旧任务”与“验证新规则确实执行”是两项不同的验收条件。
7. Medium — Phase 2 有两套操作规格，实验交接无法直接执行（阶段表仍写 3-5 镜/6 臂，采纳约束已扩为 15-20 镜/拆分 3C/双模型/组合回归）。
   Recommendation: 将采纳约束回写为唯一阶段规格，并提供固定实验命令、输入 manifest、预算限制和续跑规则 because 后续开发者应能复现同一组实验，而不是重新解释评审记录。
```

#### DX DUAL VOICES — CONSENSUS TABLE
```text
DX DUAL VOICES — CONSENSUS TABLE:
  Dimension                           Claude  Codex  Consensus
  1. Getting started < 5 min?          FAIL    FAIL   CONFIRMED (CLI blocked; Workbench ~8 steps -> uc3)
  2. API/CLI naming guessable?         GAP     GAP    CONFIRMED (inspect_dialogue_budget, raw_shot provenance, rate_band)
  3. Error messages actionable?        CAP4    CAP4   CONFIRMED (CAP4 silent-clean + CAP6 RFC 9457/CLI contracts)
  4. Docs findable & complete?         FAIL    FAIL   CONFIRMED (missing #dialogue-preflight, curl, README/SKILL sync)
  5. Upgrade path safe?                GAP     FAIL   CONFIRMED (cache version isolation + legacy opt-in contract)
  6. Dev environment friction-free?    GAP     GAP    CONFIRMED (Phase 2 ab_eval.py spec/resume/budget + d4 escape hatch)
CONFIRMED = native + outside agree; primary cannot replace outside. DISAGREE -> taste.
Missing/disabled voice = N/A, never CONFIRMED. Flag any single-voice critical finding: None (CAP4 confirmed by both!).
```

### Passes 1–8 (Plan DevEx Review)

- **Pass 1 — Getting Started (`5/10 → 8/10`)**: Workbench zero-key Demo path documented cleanly with `#dialogue-preflight` quickstart + copy-paste `curl`; CLI zero-key standalone `python h3ref.py preflight` queued as **User Challenge `uc3`** (if approved, rises to `9/10`, TTHW `< 1 min`).
- **Pass 2 — API / CLI / SDK Design (`5/10 → 9/10`)**: Single-source `inspect_dialogue_budget()` (`status`, `warnings`, `meta`) with `check_dialogue_budget()` wrapper; provenance preserved across `load() -> run() -> report()`; `normalize_speech()` shared with `compile_prompt()`; `rate_band` unified to `"regular"|"slow"`; `raw_shot_index` (0-based) alongside 1-based `raw_shot`; `rate_config` + `H3_DIALOGUE_RATE_CONFIG` escape hatch (`d4`).
- **Pass 3 — Error Messages & Debugging (`4/10 [CAP4] → 9/10`, agent-ready-v1)**:
  ```text
  ERROR PATH MATRIX (agent-ready-v1)
  Path                               | Class      | what | fix  | recovery | code | context | trace | redaction | channel | Cap (Before -> After)
  -----------------------------------|------------|------|------|----------|------|---------|-------|-----------|---------|----------------------
  1. check/inspect non-dict / bad    | validation | pass | pass | N/A      | pass | N/A     | N/A   | pass      | pass    | CAP4 -> NONE
     shots / unsupported script      |            |      |      |          |      |         |       |           |         |
  2. POST /api/film/references 400   | validation | pass | pass | N/A      | pass | N/A     | N/A   | pass      | pass    | CAP6:fix -> NONE
  3. POST /api/film/references 500   | unexpected | pass | pass | pass     | pass | pass    | pass  | pass      | pass    | CAP6:trace -> NONE
  4. normalize_shots(strict=True)    | validation | pass | pass | N/A      | pass | N/A     | N/A   | pass      | pass    | CAP6:fix -> NONE
  5. scripts/ab_eval.py paid failure | dependency | pass | pass | pass     | pass | pass    | pass  | pass      | pass    | CAP6:recovery -> NONE
  6. rewrite_exhausted / PolicyError | dependency | pass | pass | pass     | pass | pass    | N/A   | pass      | pass    | CAP6:context -> NONE
  ```
  - **Corrected example (`POST /api/film/references` 400 on non-list `shots`)**: `HTTP 400 application/problem+json` `{"type":"urn:workbench:error:dialogue_shots_invalid","title":"Invalid film script","status":400,"code":"dialogue_shots_invalid","field":"film.shots","detail":"film.shots must be a JSON array, got str","error":"...","fix":"Set top-level \"shots\" to a JSON array of shot objects (see examples/film.example.json).","doc_anchor":"docs/workbench.md#dialogue-preflight"}`.
  - **Corrected example (`normalize_shots(strict=True)` limit exceeded)**: `FilmValidationError(code="SHOTS_LIMIT_EXCEEDED", field="shots", actual=120, fix="Split the script below 60 shots or set H3_MAX_SHOTS=120, then rerun.")`, printed to `stderr` with non-zero exit in CLI.
- **Pass 4 — Documentation & Learning (`4/10 → 9/10`)**: Add `<a id="dialogue-preflight"></a>` in `docs/workbench.md` (warning code table, `complete`/`partial`/`unsupported` semantics, copy-paste `curl` + JSON response), link from `README.md`, update `SKILL.md` (upstream decoupling, `speech`, Item 2/3/4 rules), consolidate Phase 2 single canonical spec via `autoplan-baseline-edits:dx`, and add 1-click Copy button for `"speech": "zh"`.
- **Pass 5 — Upgrade & Migration Path (`5/10 → 9/10`)**: Extend `docs/workbench.md:163` upgrade contract: existing jobs preserve cached frames/H3 prompts untouched by default (zero surprise re-spend), document explicit opt-in re-rewrite/regenerate commands + cost scope, isolate Phase 2 per-arm rewrite caches with recorded rule/model fingerprints, and add legacy-job resume compatibility test.
- **Pass 6 — Developer Environment & Tooling (`6/10 → 9/10`)**: `scripts/ab_eval.py` gets `--dry-run`, `--arm`, `--fixture`, `--max-cny`, `--resume` + `ab_state.json`, zero-dependency offline `ab_review.html`, and full offline unittest + Node UI test parity.
- **Pass 7 — Community & Ecosystem (`7/10 → 8/10`)**: MIT-licensed repo with clear cost transparency (`¥0.17/frame`, `¥0.13/15s`), upstream-decoupled `film.json` interchange documented in `SKILL.md`, and zero-key offline Demo mode.
- **Pass 8 — DX Measurement & Feedback Loops (`5/10 → 8/10`)**: Track Phase 2 deliverable cost/retries/blind-win-rate per arm, log `NEUTRAL_WORDS` hits in `run.log` for false-positive spotting, and define boomerang check via `/devex-review` on live CLI/Workbench after Phase 1 ships (no telemetry added).

### Appendix: Claude Code Skill DX Checklist (`SKILL.md`)
- [x] **Error recovery**: Resumable `state.json` + new `ab_state.json` for Phase 2 (`--resume`, no double-spend).
- [x] **Agent-readable errors**: All 6 touched error paths upgraded to `agent-ready-v1` (zero CAP4/CAP6 remaining).
- [x] **Bounded autonomy**: Non-blocking WARN preflight; paid A/B gated by `--max-cny` + human blind review before Phase 3 merge.
- [x] **Skill guidance sync**: `SKILL.md` updated with upstream `film.json` decoupling, mandatory `speech` check, and Item 2/3/4 writing cues (and `preflight` command if `uc3` approved).
- [ ] **Standalone zero-key CLI preflight for Agents**: Queued in **`uc3`** for Phase 4 Gate.

### DX Review Scorecard
```text
+====================================================================+
|              DX PLAN REVIEW — SCORECARD                             |
+====================================================================+
| Dimension            | Score  | Prior  | Trend  |
|----------------------|--------|--------|--------|
| Getting Started      |  8/10  |  5/10  | +3 ↑   |
| API/CLI/SDK          |  9/10  |  5/10  | +4 ↑   |
| Error Messages       |  9/10  |  4/10  | +5 ↑   |
| Documentation        |  9/10  |  4/10  | +5 ↑   |
| Upgrade Path         |  9/10  |  5/10  | +4 ↑   |
| Dev Environment      |  9/10  |  6/10  | +3 ↑   |
| Community            |  8/10  |  7/10  | +1 ↑   |
| DX Measurement       |  8/10  |  5/10  | +3 ↑   |
+--------------------------------------------------------------------+
| TTHW                 | 3 min  | 8 min  | -5m ↑  |
| Competitive Rank     | Competitive (Champion < 1 min w/ uc3)        |
| Magical Moment       | Designed via Demo + Locate + curl (+ uc3)    |
| Product Type         | CLI Tool + Local Web API + Claude Code Skill |
| Mode                 | DX POLISH                                    |
| Overall DX           | 8.6/10 | 5.1/10 | +3.5 ↑ |
+====================================================================+
| DX PRINCIPLE COVERAGE                                               |
| Zero Friction               | covered (Champion if uc3 approved)    |
| Learn by Doing              | covered (Demo + copy-paste curl)      |
| Fight Uncertainty           | covered (agent-ready-v1, 0 CAP4/CAP6) |
| Opinionated + Escape Hatches| covered (d4 rate_config + cached H3)  |
| Code in Context             | covered (docs/workbench.md examples)  |
| Magical Moments             | covered (instant preflight + Locate)  |
| Agents Are Users            | covered (RFC 9457, raw_shot_index)    |
+====================================================================+
```

### DX Implementation Checklist
```text
DX IMPLEMENTATION CHECKLIST
============================
[ ] TTHW <= 3 min on Workbench Demo (<= 1 min on CLI if uc3 approved)
[ ] inspect_dialogue_budget() single source of truth returning status (complete|partial|unsupported|invalid) + warnings + meta
[ ] raw_shot / raw_shot_index & default-duration provenance preserved across film.load() -> run() -> report()
[ ] Shared normalize_speech() across preflight, compile_prompt(), and stage_check()
[ ] POST /api/film/references onboarded to RFC 9457 api_problem() (400 code/field/fix/doc_anchor + 500 instance/log/recovery)
[ ] FilmValidationError(ValueError) for normalize_shots(strict=True) + enriched rewrite_exhausted & ContentPolicyError
[ ] scripts/ab_eval.py supports --arm, --fixture, --dry-run, --max-cny, --resume, ab_state.json, per-arm rewrite cache isolation & key redaction
[ ] docs/workbench.md#dialogue-preflight anchor + 4+ code table + curl example, linked from README.md & SKILL.md
[ ] Legacy job upgrade policy documented & tested (cached prompts/frames preserved by default)
[ ] d1 narrow regex explicitly excluded from cached_problems(); NEUTRAL_WORDS hit summary logged in run.log
```

### NOT in scope (DX)
- **Hosted web playground / cloud sandbox**: Local-first studio tool; offline Demo mode already provides zero-key sandbox.
- **Automated telemetry / SaaS analytics**: Strictly offline/local privacy posture; no usage tracking added.
- **Standalone CLI `preflight` subcommand (pending `uc3`)**: Preserved out of baseline until user approves `uc3` at Phase 4 Gate.

### What already exists (DX)
- [workbench/server.py:274-312](../../workbench/server.py#L274-L312): Existing RFC 9457 `segment_problem()` + redacted traceback + `instance` correlation logging — generalize to `api_problem()` instead of reinventing.
- [docs/workbench.md:118-167](../../docs/workbench.md#L118-L167): Existing `#video-prompt-errors` anchor, recovery table, `curl` examples, and upgrade preservation note (`L163`).
- [h3ref/rewrite.py:206-218](../../h3ref/rewrite.py#L206-L218): Existing separation of `problems()` vs `cached_problems()` — preserve for manual H3 escape hatch.

### TODOS.md updates (DX)
- None beyond `uc3` (CLI `preflight` subcommand, queued for Phase 4 Gate) and `d4` (runtime `H3_DIALOGUE_RATE_CONFIG` escape hatch, queued for Phase 4 Gate).

### Implementation Tasks (DX Review)
- [ ] **T8 (P1, human: ~3h / CC: ~15min)** — `h3ref/film.py` — Extract `inspect_dialogue_budget()` (`status: complete|partial|unsupported|invalid`, `warnings`, `meta`), `normalize_speech()`, `FilmValidationError`, safe colon-prefix stripping, `rate_band: "regular"|"slow"`, `raw_shot_index`, and cross-`load()` provenance preservation + parity test
  - Surfaced by: DX Dual Voices (Native CAP4 #8 / #3–#7 / #11 + Codex #1 / #3)
  - Files: `h3ref/film.py`, `h3ref.py`, `tests/test_dialogue_budget.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_offline.py`
- [ ] **T9 (P1, human: ~2h / CC: ~10min)** — `workbench/server.py`, `workbench/references.py`, `h3ref/rewrite.py`, `h3ref/frames.py` — Generalize `api_problem()` RFC 9457 + `instance` trace for `POST /api/film/references` (400/500), enrich `rewrite_exhausted` & `ContentPolicyError`, forbid `d1` regex in `cached_problems()`, log `NEUTRAL_WORDS` hits, add `rate_config` / `d4` escape hatch
  - Surfaced by: DX Dual Voices (Native CAP6 #9/#10/#13/#14/#17–#19 + Codex #4)
  - Files: `workbench/server.py`, `workbench/references.py`, `h3ref/rewrite.py`, `h3ref/frames.py`, `h3ref/messages/*.json`, `workbench/messages/*.json`, `tests/test_dialogue_budget.py`, `tests/test_workbench_api.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_reference_bindings.py tests/test_locale_parity.py`
- [ ] **T10 (P1, human: ~2h / CC: ~10min)** — `scripts/ab_eval.py` — Add resumable `ab_state.json`, `--arm`/`--fixture`/`--dry-run`/`--max-cny`/`--resume`, strict per-arm rewrite cache isolation (frozen frames only), rule/model fingerprints, key redaction, and `stderr` + non-zero exit error contract
  - Surfaced by: DX Dual Voices (Native #2/#12 + Codex #6/#7)
  - Files: `scripts/ab_eval.py` (Phase 2 experiment branch)
  - Verify: `.venv/bin/python scripts/ab_eval.py --dry-run --arm 3A` (0 API calls, valid manifest & budget estimate)
- [ ] **T11 (P2, human: ~1.5h / CC: ~8min)** — `docs/workbench.md`, `README.md`, `SKILL.md`, `workbench/static/create.js` — Add `#dialogue-preflight` doc anchor + warning code table + `curl` example + legacy upgrade opt-in policy, link in `README.md`, update `SKILL.md`, and add 1-click Copy button for `"speech": "zh"`
  - Surfaced by: DX Dual Voices (Native #15/#16 + Codex #5/#6)
  - Files: `docs/workbench.md`, `README.md`, `SKILL.md`, `workbench/static/create.js`, `tests/test_workbench_ui.js`
  - Verify: Verify `#dialogue-preflight` anchor & copy-paste `curl` against local test client + `node tests/test_workbench_ui.js`

---

## Phase 3: Eng Review (Cycle 1 — Archived Applied Baseline & Obligations)

```text
<!-- autoplan-baseline-edits:eng {"sourceSha256":"4c4f91172ec70ce526fb73f17ca798fc1419d87a539405f0aa997bb805816028","replacements":[{"oldText":"- **说话人前缀清洗（确定性循环）**：在行首循环剥离以下前缀直至不再匹配：① 成对全/半角括号及方括号标签（`^[（(\\[【][^）)\\]】]{1,20}[）)\\]】]\\s*`）；② 非数字开头的说话人冒号前缀（`^[^：:\\s\\d][^：:\\s]{0,11}[：:]\\s*`，避免误剥 `12:30` 等时间或句中冒号）。","newText":"- **说话人前缀清洗（确定性循环，最多 5 轮）**：在行首循环剥离以下前缀直至不再匹配或达到 5 轮上限：① 严格成对全/半角括号及方括号标签（`^(?:（[^）]{1,20}）|\\([^)]{1,20}\\)|\\[[^\\]]{1,20}\\]|【[^】]{1,20}】)\\s*`，拒绝 `(你好】` 跨类型错配）；② 显式说话人冒号前缀（仅匹配 `film.cast` 角色名/别名或常见显式说话人标签 `旁白|画外音|路人[甲乙丙0-9]?|众人|Narrator|VO|OS`，防止误剥 `12:30`、`记住：千万别回头` 或 `Wait: don't go`）。"},{"oldText":"  - 清洗前缀并计数后若 `count == 0`：若清洗后不含任何字母/文字字符（如纯标点 `\"……\"` 或纯标签 `\"（小林）\"`），安全跳过该镜（不误报 `dialogue_over_budget`）；若含未覆盖脚本文字（如俄语/阿拉伯语/泰语等非拉丁非 CJK 字母），计入 `meta.unsupported_script_shots`、将检查状态标为 `\"unsupported\"`/`\"partial\"` 并发出 `dialogue_unsupported_script`，绝不静默伪报「检查通过」。","newText":"  - **独立未覆盖脚本检测与 `count == 0` 跳过**：计数前将全角 ASCII 标准化、英文词正则支持弯引号 `['’]`、CJK 补齐扩展 A 区 `\\u3400-\\u4dbf` 与 `々`（`\\u3005`）、`normalize_speech()` 按 `re.split(r\"[-_]\", cleaned, maxsplit=1)[0]` 切分。对清洗后文本**独立扫描**是否存在未覆盖脚本字母（如西里尔/阿拉伯/泰文/天城文等 `ch.isalpha()`）——不以 `count == 0` 为前提，防止 `\"Привет 2026\"` 等混合脚本绕过——命中即计入 `meta.unsupported_script_shots` 并发出 `dialogue_unsupported_script`（`status` 为 `\"unsupported\"`/`\"partial\"`）；仅当无未覆盖脚本且 `count == 0`（如纯标点 `\"……\"` 或纯标签）时安全跳过该镜。"},{"oldText":"超镜长判定按未四舍五入值 `raw_min > duration`；`line_preview` 取清洗前缀后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。","newText":"超镜长判定按 `raw_min > effective_duration + 1e-9`（使用整数毫秒累加停顿或 `1e-9` 容差消除 `0.2+0.2+0.2=0.6000000000000001` 导致的 `1.6s > 1.6s` IEEE-754 穿帮，且 `effective_duration = min(duration, float(config.MAX_SECONDS))` 防止 20 秒单镜被 `film.plan()` 截断至 15 秒时漏报，超上限时在告警中附带 `effective_duration`）；`line_preview` 取清洗前缀并脱敏控制字符后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。"},{"oldText":"- **CLI（`h3ref/film.py`）**：\n  - `run()` 在 `plan()` 之后、`stage_frames()` 之前调用 `check_dialogue_budget(film)`；若有告警，逐条用 `h3ref.i18n.message()` 写入 `job.log(\"preflight\", ...)`（即便后续 `stage_frames()` 部分失败提前返回，`run.log` 中也已留痕）。**一律为 WARN，不阻断执行**。\n  - `report()` 保持原函数签名不变，内部直接调用 `check_dialogue_budget(film)`；若有告警，在 `report.md` 顶部基础摘要之后、逐段提示词详情之前输出「脚本预检提示」Markdown 表格（`镜头 | 类型 | 镜长 / 估算 | 字/词数 | 台词片段`，无告警时省略该小节）。\n- **工作台后端（`workbench/references.py`、`workbench/server.py`）**：\n  - `describe_references(script, *, lang=\"zh-CN\")` 保持 `lang` keyword-only 默认值以兼容 `GET /api/film/example`；`POST /api/film/references` 传入 `lang=language()`。\n  - 在返回 `{\"film\": ..., \"slots\": ...}` 的基础上补充 `\"warnings\": [...]`：内部直接将 `script` 传给 `film.check_dialogue_budget(script)`（由其内部调用一次非就地修改的 `normalize_shots(..., strict=False)`，绝不修改 `result[\"film\"][\"shots\"]`，防止 `create.js:167` 导入回写篡改用户草稿），并按 `workbench/messages/{zh-CN,en}.json` 附带本地化 `message` 字段。","newText":"- **CLI（`h3ref/film.py`）**：\n  - `run()` 在 `load()` 之前读取原始 `job.state[\"film_path\"]`（工作台创建的任务优先读取 `jobs.py:530-532` 在 `load()` 前落盘的不可变 `inputs/preflight.json`，绝不污染 `film.json` 或 `state.json`），统一调用一次 `inspect_dialogue_budget()`，将告警与部分/未覆盖摘要写入 `job.log(\"preflight\", ...)`（`line_preview` 经 `sanitize_line_preview()` 去除控制字符/ANSI 并折叠换行）。**一律为 WARN，不阻断执行**。\n  - `report()` 保持原函数签名不变，优先读取 `inputs/preflight.json`（或未归一化原始 `film_path`），缺失时回退调用 `inspect_dialogue_budget(film)` 并标记来源未知；若有告警或部分检查摘要，在 `report.md` 顶部基础摘要之后、逐段提示词详情之前输出「脚本预检提示」Markdown 表格（单元格经 `escape_markdown_cell()` 转义 `|` 与换行，无告警时省略该小节）。\n- **工作台后端（`workbench/references.py`、`workbench/server.py`、`workbench/jobs.py`、`workbench/storyboard.py`）**：\n  - `describe_references(script, *, lang=\"zh-CN\")` 保持 `lang` keyword-only 默认值以兼容 `GET /api/film/example`；`POST /api/film/references` 传入 `lang=language()`，统一调用一次 `film.inspect_dialogue_budget(script)` 并固定返回 `{\"film\", \"slots\", \"warnings\", \"preflight_status\", \"preflight_meta\"}`（非可选；绝不修改 `result[\"film\"][\"shots\"]`）。\n  - 锁定 `shots` 四态与 10x 上限：`shots` 缺失/`null` → 200 `preflight_status=\"invalid\"`（`shots_missing=True`）；存在且非 list 或长度超过 `max(2000, config.max_shots() * 2)` → HTTP 400 RFC 9457 `api_problem()`；`[]` 或 `checked_shots == 0` → `preflight_status=\"partial\"`；正常返回 `complete`/`partial`/`unsupported`。`POST /api/film/references` 的 400/415 `HTTPException` 与 500 意外异常统一走 `api_problem()`（无任务端点 500 脱敏日志写入 `storage/server.log` + `instance` 关联号，保留 `error` 兼容键）。"},{"oldText":"- 工作台与界面：`workbench/references.py`、`workbench/server.py`、`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`（复用 `.notice.warning` 并补齐 `border: 1px solid #eddfb9` 及列表间距）","newText":"- 工作台与界面：`workbench/references.py`、`workbench/server.py`、`workbench/jobs.py`、`workbench/storyboard.py`、`workbench/templates/index.html`、`workbench/static/create.js`、`workbench/static/style.css`（复用 `.notice.warning` 并补齐 `border: 1px solid #eddfb9` 及列表间距）"}]} -->

<!-- autoplan-accepted:eng -->
(Superseded by Eng Cycle 2 after gate_rev=1 reply="rec" adopting uc1b, uc2b, uc3b; all 5 Cycle 1 Eng obligations remain retained in Cycle 2.)
<!-- /autoplan-accepted:eng -->
```

### Decision Audit Trail (Phase 3: Eng)
| # | Phase | Decision | Classification | Principle | Rationale |
|---|---|---|---|---|---|
| 24 | Eng | Keep `compile_prompt()` (`film.py:33,251,264,299`) byte-identical for existing jobs (or version-gate if Arm 2A demands it) + zero-spend legacy resume test | Mechanical (Auto-fix) | P5 | Both Native (Critical A1) and Codex (High #5/#6) proved modifying `compile_prompt()` busts `rec['zh']==zh` H3 cache (`film.py:535`) |
| 25 | Eng | Persist immutable `inputs/preflight.json` before `film.load()` in `workbench/jobs.py:530-532` & CLI `film.run()`; unify all 3 callers on `inspect_dialogue_budget()` & non-optional response keys | Mechanical (Auto-fix) | P1 / P5 | Both Native (Critical A2, High A4) and Codex (High #2/#3) proved `inputs.py:148` → `jobs.py:532` erases `raw_shot` provenance and §1.2 dropped `status`/`meta` |
| 26 | Eng | Widen `except (SystemExit, FilmValidationError)` in `jobs.py`/`server.py`/`storyboard.py`; isolate any future `d1` regex to `llm_rewrite_problems()` outside both `problems()` & `cached_problems()` | Mechanical (Auto-fix) | P5 | Native A3/A5 + Codex #7 caught real `film.py:527` manual-H3 path calling `problems()` and workbench callers only catching `SystemExit` |
| 27 | Eng | Fix IEEE-754 `1e-9` tolerance + `MAX_SECONDS` clamp check, non-`count==0` mixed-script scan, paired brackets + cast-gated colon strip, 10x shot/DOM caps, `rate_config` & `ab_review.html` security hardening | Mechanical (Auto-fix) | P1 / P5 | Native E1–E4/S1–S2 + Codex #1/#8 closed subtle float/mixed-script/DoS/XSS/div-by-zero defects before code |
| 28 | Eng | Render `#submit-hint` echo in `renderMode()`, lexical JSON Locate scanner, focus/details snapshot, `frames.neutralize_text()`, keyword-only overrides & atomic `submission_unknown` state machine in `ab_eval.py` | Mechanical (Auto-fix) | P5 | Native C1–C4 + Codex #4 eliminated polling wipeouts, duplicate-line locate bugs, and Phase 2 double-spend risks |

### Step 0: Scope Challenge, Complexity Check & Reality Verification
- **Scope Challenge Result**: `scope accepted as-is` (Autoplan Eng override: never reduce scope, P2).
- **Complexity Check**:
  - Proposed touched files across Waves 1–3: **16 existing files + 2 new files** (`h3ref/film.py`, `h3ref/frames.py`, `h3ref/rewrite.py`, `h3ref.py`, `workbench/references.py`, `workbench/server.py`, `workbench/jobs.py`, `workbench/storyboard.py`, `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, 6 i18n JSON files, `docs/workbench.md`, `README.md`, `SKILL.md`, plus new `tests/test_dialogue_budget.py` & Phase 2 experimental `scripts/ab_eval.py`).
  - New classes/services: **1 lightweight exception class** (`FilmValidationError(ValueError)`) and **0 new services/daemons/DB tables**.
  - Arrangement verdict: Original module arrangement preserved (pure storyboard functions in `h3ref/film.py`, API projection in `workbench/references.py`, RFC 9457 error formatting in `workbench/server.py`, UI in `workbench/static/create.js`); naturally splits into Wave 1 (zero-paid deterministic preflight) → Wave 2 (experimental `scripts/ab_eval.py`) → Wave 3 (passing prompt arms + docs).
- **Search Check**: All patterns use Python stdlib (`re`, `math`, `json`, `pathlib`) and existing repo patterns (`config.atomic_replace`, `segment_problem` RFC 9457, `create.js` `state.filmRevision`) — **[Layer 1]** proven built-ins, zero external dependencies added.
- **TODOS Cross-Reference**: No existing root `TODOS.md` in repository; all deferred items consolidated in `TODOS.md updates` below.
- **Distribution Check**: Local Python CLI + Flask Workbench + packaged Windows zip (`start-windows.cmd`); no new pip dependencies, no new binary/OS build steps, pure Python 3.10+ & vanilla ES6 JS (zero Node runtime required in production).

#### Eng Reality Premise Table (`unverified=0`)
| # | Premise | Claim | File:Line Receipt | Verdict |
|---|---|---|---|---|
| E-P1 | `film.load()` mutates & drops empty shots before `run()` | `load()` filters empty `action`, assigns continuous times, replaces `film["shots"]`; `run()` calls `load()` at L1223 | `h3ref/film.py:77-103,1223` | verified |
| E-P2 | `compile_prompt()` text changes invalidate cached `h3` | `stage_prompts()` checks `rec.get('zh') == zh` to reuse `rec['h3']`; `film.py:33,251,264,299` feed `zh` | `h3ref/film.py:33,251,264,299,535-540` | verified |
| E-P3 | Manual H3 override calls `rewrite.problems()` | `prompt_override.format == 'h3'` validates via `rewrite.problems()`, NOT `cached_problems()` | `h3ref/film.py:526-527` | verified |
| E-P4 | Workbench `inputs.py` / `jobs.py` strip raw shots pre-CLI | `inputs.py:148-152` runs `film.load()` before `jobs.py:532` writes `inputs/film.json`; `jobs.py:584` catches `SystemExit` only | `workbench/inputs.py:148-152`, `workbench/jobs.py:530-532,584` | verified |
| E-P5 | `workbench/server.py` already implements RFC 9457 + `instance` | `segment_problem()` builds `application/problem+json`; `unexpected_error()` writes redacted traceback only when `identifier in jobs.records` | `workbench/server.py:274-313,657-662` | verified |
| E-P6 | `create.js:20` overwrites `#submit-hint` & `film.plan()` clamps 15s | `renderMode()` sets `$("submit-hint").textContent`; `film.plan()` clamps segment seconds to `max_len` (`config.MAX_SECONDS=15`) | `workbench/static/create.js:20,103-179,685-693`, `h3ref/film.py:106-127` | verified |
| E-P7 | Existing offline test suite covers locale parity & bindings | `tests/test_locale_parity.py` checks 3 locale pairs; `tests/test_reference_bindings.py` tests `describe_references`/`bind_references`/`POST /api/film/references` | `tests/test_locale_parity.py:26-86`, `tests/test_reference_bindings.py:18-120` | verified |

REALITY: premise-table pass All 7 engineering premises verified directly against source files; unverified=0 h3ref/film.py:77-127
REALITY: already-done pass Checked `git log --oneline -8 -- h3ref/film.py h3ref/frames.py h3ref/rewrite.py workbench/references.py workbench/server.py`; only ComfyUI/Colab commits exist, 0 preflight/FACS/NEUTRAL_WORDS changes landed h3ref/film.py:1
REALITY: surface-check pass Verified `h3ref.py:181-225` subcommands, `POST /api/film/references` (`workbench/server.py:657`), `POST /api/jobs` (`workbench/server.py:645`), `describe_references()` (`workbench/references.py:9`), `film.load/plan/compile_prompt/stage_prompts/report/run` (`h3ref/film.py`) workbench/server.py:645-663
REALITY: binding-decisions pass Checked `tasks/lessons.md` (5 project lessons honored: v3 simplicity, A/B before merge, zero-死 fallback, locked_look separation, 3-suite i18n) and prior Autoplan Phase 1/2/2.5 obligations tasks/lessons.md:1-54
REALITY: numbers pass Verified `MAX_SECONDS=15` clamp [measured: h3ref/film.py:106-123]; 2000-shot cap [measured: workbench/inputs.py:124]; `film.example.json` shot 2 `1.87s<=2.0s` [measured: examples/film.example.json:17]; IEEE-754 float-pause rounding `0.2*3` [measured: h3ref/film.py:99-100] h3ref/film.py:106-127
REALITY: deferred-asks pass No linked tracker issue; all CEO/Design/DX deferrals (E6, E7, D-TODO-1) and Gate items (`uc1`–`uc3`, `d1`–`d4`) preserved explicitly docs/plans/2026-11-23-prompt-and-preflight-enhancements.md:1-190
REALITY: pr-count pass Default 1 PR per wave (Wave 1 deterministic preflight independently shippable; Wave 2 experimental A/B branch; Wave 3 passing prompt arms + docs) docs/plans/2026-11-23-prompt-and-preflight-enhancements.md:152-157

### Step 0.5: Dual Voices (Eng)

#### Claude SUBAGENT (eng — independent review, `gemini-4-neon`)
- **Snapshot Verified**: `INPUT: eng d97494188dc53f1f607f3fe4aae7bdd02dc9a39ea26527602b93c8f488bdb018` (exact match).
- **Summary (15 findings: 2 Critical, 8 High, 4 Medium, 1 Low)**:
  - **Critical A1**: Modifying `film.py:251` / `compile_prompt()` busts `rec['zh'] == zh` cache on legacy jobs (`film.py:535-540`, `inputs.py:470-480`).
  - **Critical A2**: Workbench `inputs.py:148-152` → `jobs.py:532` writes normalized `inputs/film.json`, erasing `raw_shot` & `default_duration_shots` before child CLI runs.
  - **High A3–A5**: `jobs.py:584`/`server.py:715`/`storyboard.py:73` only catch `SystemExit`, not `FilmValidationError`; `describe_references()` response schema/4-state `shots` & 500 `storage/server.log` + `HTTPException` gaps; manual H3 at `film.py:527` calls `rewrite.problems()`, not `cached_problems()`.
  - **High E1–E2, S1, C1**: IEEE-754 `0.2+0.2+0.2` recreates `1.6s > 1.6s`; `"Привет 2026"` bypasses `count == 0` unsupported-script check (+ full-width ASCII, `’`, CJK Ext A, `zh_CN`); `H3_DIALOGUE_RATE_CONFIG` div-by-zero/file-leak/`server.py:FIELDS` ban; `create.js:20` `renderMode()` wipes `#submit-hint` every 1.8s & `replaceChildren()` drops `<details open>`/focus.
  - **Medium/Low E3–E4, S2, C2–C4**: Strict paired bracket regex + cast-gated colon strip; 10x shot cap (`2000`) & 100-item DOM cap + finite duration guard; `report.md` Markdown `|` escape & `ab_review.html` `\u003c` JSON XSS escape; ~50-line JSON lexical Locate scanner; keyword-only prompt overrides + atomic `submission_unknown` state machine in `ab_eval.py`; `frames.neutralize_text()`.

#### Codex SAYS (eng — architecture challenge, `gpt-6-astra`)
```tool-output
CODEX_MODEL: gpt-6-astra (exec; role: plan-review, tier: frontier; source: gstack catalog frontier/openai (verified 2026-10-07))
OUTSIDE_STATUS: completed provider=codex host=claude
VERDICT: FINDINGS (5 High, 3 Medium; FINDINGS: P1)

1. High — 混合脚本仍会被误报为完整检查（如俄语+数字 "1" 或阿拉伯语+"OK" 使 count!=0 绕过检测）。
   Recommendation: 对每条清洗后的台词独立检查未被计数规则覆盖的文字，并明确混合脚本的 partial/unsupported 判定 because “至少识别一个词”不等于“整句已被检查”。
2. High — 完整检查结果在集成边界被丢弃（§1.2 仍调列表版 check_dialogue_budget()，API preflight_meta 为可选且未锁 status 字段）。
   Recommendation: 三个集成入口统一调用一次 inspect_dialogue_budget()，API 固定返回完整预检对象，薄包装只保留给兼容调用者 because 状态和覆盖率属于结果契约，不能从空告警列表推导。
3. High — 原始镜头来源信息会在工作台持久化之前丢失（workbench/inputs.py:148 先调 load() 再由 jobs.py:532 写入 inputs/film.json）。
   Recommendation: 明确保留原始输入及独立的来源映射，覆盖“工作台提交→保存→新进程恢复→报告”的测试，并为旧任务定义来源未知的行为 because 已被过滤和补齐的数据无法在下游无损重建。
4. High — 实验运行器的“提交前记一条记录”不足以保证防重付（缺 submission_unknown 状态机、原子写、运行锁、在途 --max-cny 预留、恢复指纹校验）。
   Recommendation: 定义持久化提交状态机、原子写入及运行锁；未知提交禁止自动重发；预算包含已花费和在途预留，并在恢复时校验输入与规则指纹 because 本地记录、远端提交和计费不是一个原子事务。
5. High — “默认保留旧任务缓存”的承诺与现有缓存判断冲突（film.py:535 以旧 zh == 新 zh 复用 h3，改 compile_prompt 固定文案会触发旧任务静默重改写）。
   Recommendation: 明确旧任务沿用已保存提示词的条件，将采用新规则设为显式操作，并覆盖“已有 h3、尚无视频 task”的升级恢复测试 because 修改生成源文本会直接触发现有缓存失效，文档无法改变运行行为。
6. Medium — 双态资产覆盖仍遗漏一条全局服装锁定指令（h3ref/film.py:33 RULES 要求“发型、妆容和服装前后一致”并在分镜后追加）。
   Recommendation: 在全局连续性规则中明确身份稳定与显式衣物状态变化的优先级，并测试跨镜脱衣、重新穿回及镜内脱衣 because 只修改局部参考图说明，不能消除最后追加的全局冲突指令。
7. Medium — 禁止窄正则进入 cached_problems()，仍保护不了手工覆盖（手工 H3 覆盖走 film.py:527 rewrite.problems()）。
   Recommendation: 将结构有效性检查与自动改写质量规则分开，明确手工覆盖只调用前者，并增加手工否定句回归用例 because 手工覆盖实际没有经过计划所保护的缓存校验入口。
8. Medium — 台词预算使用脚本镜长，可能超过实际生成时长上限（film.py:106 plan() 将 20 秒单镜限幅到 15 秒 MAX_SECONDS）。
   Recommendation: 对单镜超过生成上限提供独立诊断，或在预算结果中同时反映实际可生成时长，并覆盖 20 秒单镜案例 because 用户填入的镜长不一定是模型实际获得的播放时间。
```

#### ENG DUAL VOICES — CONSENSUS TABLE
```text
ENG DUAL VOICES — CONSENSUS TABLE:
  Dimension                           Claude  Codex  Consensus
  1. Architecture sound?               FAIL    FAIL   CONFIRMED (A1 cache bust, A2/C3 provenance strip, A4/C2 boundary drop, A5/C7 problems() split)
  2. Test coverage sufficient?         GAP     GAP    CONFIRMED (legacy 0-spend resume, provenance E2E, IEEE-754, mixed-script, 20s clamp, UI races)
  3. Perf risks + budget/bench named?  GAP     OK     Single-voice Medium (E4: 2000-shot cap & 100-item DOM cap — auto-fixed)
  4. Security threats covered?         GAP     OK     Single-voice High (S1 rate_config & S2 ab_review XSS/Markdown cell — auto-fixed)
  5. Error paths handled?              GAP     FAIL   CONFIRMED (A3 SystemExit callers, A4 500 server.log, C3/C4 ab_eval submission_unknown)
  6. Deployment risk manageable?       FAIL    FAIL   CONFIRMED (A1/C5 silent legacy H3 cache invalidation — auto-fixed!)
CONFIRMED = native + outside agree; primary cannot replace outside. DISAGREE -> taste.
Missing/disabled voice = N/A, never CONFIRMED. Single-voice critical findings: None (both Critical A1 & A2 confirmed by Codex!).
```

### Section 1: Architecture Review

#### Architecture Dependency Graph (ASCII)
```text
                   +----------------------------------------------------+
                   |          Raw film.json (CLI or Web Editor)         |
                   +-----------+----------------------------+-----------+
                               |                            |
            [POST /api/film/references]            [POST /api/jobs / CLI film]
                               v                            v
                +------------------------------+   +------------------------------+
                | workbench/server.py          |   | workbench/jobs.py:530 OR     |
                |  api_problem() RFC 9457      |   | h3ref/film.py:run() pre-load |
                |  storage/server.log (500)    |   +--------------+---------------+
                +--------------+---------------+                  |
                               |                                  | 1) raw inspect once
                               v                                  v
                +------------------------------+   +------------------------------+
                | workbench/references.py      |   | inputs/preflight.json        |
                |  describe_references(lang)   |   | (immutable raw provenance;   |
                |  -> {film, slots, warnings,  |   |  film.json stays JSON-clean) |
                |      preflight_status, meta} |   +--------------+---------------+
                +--------------+---------------+                  |
                               |                                  |
                               +----------------+-----------------+
                                                v
                           +----------------------------------------+
                           | h3ref/film.py (Single Source of Truth) |
                           |  - normalize_speech(raw)               |
                           |  - normalize_shots(shots, strict=bool) |
                           |  - inspect_dialogue_budget(film, cfg)  |
                           |  - FilmValidationError(ValueError)     |
                           |  - check_dialogue_budget() [thin wrap] |
                           +--------------------+-------------------+
                                                |
                 +------------------------------+------------------------------+
                 v                                                             v
  +------------------------------+                              +------------------------------+
  | workbench/static/create.js   |                              | h3ref/film.py:report()       |
  |  - #film-warnings (.is-stale)|                              |  - reads inputs/preflight    |
  |  - lexical JSON Locate       |                              |  - escape_markdown_cell()    |
  |  - renderMode() #submit-hint |                              |  - top preflight table       |
  +------------------------------+                              +------------------------------+

  Phase 2/3 Prompt & Cache Isolation Boundary:
    frames.py (describe_composition + neutralize_text + keyword-only overrides) -> stage_frames
    film.py:compile_prompt() -> BYTE-IDENTICAL for legacy jobs (zero silent H3 cache bust!)
    rewrite.py (RULES #5/#7/#9 + problems()[structural] vs llm_rewrite_problems()[retry-only])
    scripts/ab_eval.py (ab_state.json atomic lock, submission_unknown guard, --max-cny reserve, ab_review.html)
```

#### Two Shapes Before One (Design-Twice) & 8 Red Flags Screen
- **New Boundary Evaluated**: `h3ref/film.py:inspect_dialogue_budget()` + `inputs/preflight.json` provenance boundary.
- **Shape A (Rejected — Private Keys Inside `film["shots"]`)**: Attach `_raw_shot` and `_default_duration` dicts onto each normalized shot returned by `film.load()`. Shallow leak: `workbench/jobs.py:532` serializes `film` into `inputs/film.json` and `create.js:167` writes `result.film` into `#film-json`, leaking internal keys or forcing every serializer to strip `_`-prefixed fields.
- **Shape B (Chosen — Pure `inspect_dialogue_budget()` + Immutable `inputs/preflight.json`)**: `film.load()` stays 100% pure JSON-clean; `inspect_dialogue_budget(raw_film)` runs once on the raw script before `load()` and writes an immutable `inputs/preflight.json` snapshot (for jobs) or returns `{warnings, preflight_status, preflight_meta}` directly (for `/api/film/references`). Deep module: hides normalization, script detection, pause math, and provenance behind 1 call; zero serializer leakage.
- **8 Red Flags Screen**:
  1. Shallow module: Pass — 1 function `inspect_dialogue_budget()` encapsulates all pacing & coverage policy.
  2. Information leakage: Fixed (`A2`/`A4`) — no `_raw_shot` keys leak into `film.json`; external JSON parsed cleanly at boundary.
  3. Temporal decomposition: Fixed (`A2`) — preflight runs on raw input once rather than re-deriving discarded data after `load()`.
  4. Pass-through method: Fixed (Codex #2) — all 3 callers call `inspect_dialogue_budget()` directly; `check_dialogue_budget()` kept only as minimal list convenience wrapper.
  5. Split ownership: Fixed (`C1`) — `renderMode()` is the sole owner of `#submit-hint`; `api_problem()` is the sole owner of RFC 9457 responses.
  6. Two ways to do one task: Fixed (`A5`) — structural checks (`problems()`/`cached_problems()`) strictly separated from LLM retry quality checks (`llm_rewrite_problems()`).
  7. Importable internals: Pass — internal helpers (`_strip_speaker_prefix`, `_scan_uncovered_script`, `_pause_ms`) prefixed `_`.
  8. Hand-synced list: Fixed — `DIALOGUE_RATE_CONFIG` is the single dict driving rate math and tests; `tests/test_locale_parity.py` mechanically fails build on 3-suite i18n key/placeholder drift.

### Section 2: Code Quality Review & Shared-Code Rubric
- **Findings (all auto-fixed)**:
  - `[CRITICAL] (confidence: 10/10) h3ref/film.py:535-540` — `compile_prompt()` text changes bust legacy `rec['zh'] == zh` cache → **Accepted** (keep `compile_prompt()` byte-identical for legacy jobs; converge Item 2 into `rewrite.py`/`frames.py` or version-gate).
  - `[CRITICAL] (confidence: 10/10) workbench/inputs.py:148-152, workbench/jobs.py:530-532` — `load()` erases raw provenance before child CLI → **Accepted** (persist `inputs/preflight.json` pre-`load()`).
  - `[INFORMATIONAL] (confidence: 9/10) workbench/jobs.py:584, workbench/server.py:715, workbench/storyboard.py:73` — `except SystemExit` misses `FilmValidationError` → **Accepted** (widen catch clauses).
  - `[INFORMATIONAL] (confidence: 9/10) workbench/static/create.js:20,312` — `renderMode()` wipes `#submit-hint` on 1.8s polling & `#film-parse-status` re-announces → **Accepted** (move echo into `renderMode()` + one-shot `state.filmPreflightCleanAnnounced`).
- **Shared-Code Rubric**:
  - `h3ref/film.py:normalize_shots()`: 2 verified callers (`film.load()` at `h3ref/film.py:91` and proposed `inspect_dialogue_budget()` / `describe_references()`). Removes ~12 duplicated lines, adds ~28 guarded lines (net +16 impl lines, huge reliability gain: eliminates CLI vs Workbench normalization drift).
  - `workbench/server.py:api_problem()`: generalizes existing `segment_problem()` (`workbench/server.py:274-284`) across `regenerate_segment`, `rewrite_segment`, `rewrite_prompts`, `get_job`, and `film_references` (net ~+8 lines, zero duplicate RFC 9457 builders).
  - `h3ref/frames.py:neutralize_text()`: extracts pure tuple helper from `frames.py:222` so `frames.prompt()` keeps its `str` signature for `workbench/storyboard.py:55` while `stage_frames()` logs hit summaries cleanly.

### Section 3: Test Review
- **Detected Test Frameworks**: Python `unittest` (`.venv/bin/python -m unittest`), Node.js JS runner (`node tests/test_workbench_ui.js`, `tests/test_results.js`, `tests/test_video_segment_prompts.js`). 34 existing test files verified in `tests/`.

#### Codepath & User-Flow Coverage Diagram (ASCII)
```text
CODE PATHS                                               USER FLOWS
[+] h3ref/film.py                                        [+] Workbench Create Page (/)
  ├── normalize_shots(strict=True/False)                   ├── [GAP] Paste/import -> 7-state #film-warnings lifecycle
  │   ├── [GAP] draft malformed/NaN/bool/inf/2000-cap      ├── [GAP] Typing debounce -> .is-stale (zero layout jump)
  │   └── [GAP] strict=True -> FilmValidationError         ├── [GAP] Click Locate -> lexical JSON scanner (dups/escapes)
  ├── inspect_dialogue_budget()                            ├── [GAP] Locale zh-CN <-> en -> preserve <details> & focus
  │   ├── [GAP] zh/ja/ko vs en/es/pt + full-width/’/ExtA   ├── [GAP] 1.8s polling renderMode() keeps #submit-hint echo
  │   ├── [GAP] IEEE-754 1e-9 + 20s MAX_SECONDS clamp      └── [GAP] Copy "speech":"zh" & 320px/200% zoom layout
  │   ├── [GAP] mixed-script ("Привет 2026") unsupported
  │   ├── [GAP] paired brackets & cast-gated bare-colon  [+] Cross-Surface & Upgrade Flows
  │   └── [GAP] rate_config validation (0/neg/NaN/file)    ├── [GAP] [→E2E] Workbench -> inputs/preflight -> CLI -> report
  ├── compile_prompt() & stage_prompts()                   ├── [GAP] [→E2E] Legacy state.json resume -> 0 LLM/APIMart calls
  │   └── [GAP] CRITICAL legacy cache 0-spend resume       └── [GAP] [→E2E] Manual H3 override w/ negation (film.py:527)
  └── report() -> escape_markdown_cell() top table
                                                         [+] Error & Security States
[+] workbench/references.py & server.py                    ├── [GAP] POST /api/film/references 400/415/500 RFC 9457
  ├── [★★ TESTED] valid cast/board — test_ref_bind:36      └── [GAP] XSS (<img/script>) in UI & ab_review.html
  ├── [GAP] {warnings, preflight_status, preflight_meta}
  ├── [GAP] 4 shots states (missing/bad/empty/valid)     [+] Phase 2 Prompt A/B (LLM/Image/Video)
  └── [GAP] jobs.py/storyboard.py FilmValidationError      ├── [GAP] [→EVAL] Pure-image 15-20 shots (3A/3B/3C-neg/3C-light)
                                                           ├── [GAP] [→EVAL] Frozen-frame video + controls (3D/4/d2)
[+] h3ref/frames.py & rewrite.py & scripts/ab_eval.py      └── [GAP] [→EVAL] Cross-modal 2A + combined <= +20% gate
  ├── [★★★ TESTED] "死" fallback — test_frame_mod
  └── [GAP] neutralize_text(), ab_state.json submission_unknown

COVERAGE (Pre-Implementation): 2/27 paths covered (7%)  |  New/Changed Paths: 25 GAPS (3 E2E, 3 EVAL)
QUALITY: ★★★:1 ★★:1 ★:0  |  All 25 GAPS mapped to tests/test_dialogue_budget.py, test_workbench_ui.js, test_locale_parity.py & Phase 2 eval
```
Legend: `★★★` behavior + edge + error | `★★` happy path | `★` smoke check | `[→E2E]` integration test | `[→EVAL]` live LLM/media eval.

- **Test Plan Artifact Persisted**: [qiaoyu-main-eng-review-test-plan-20261124-190657.md](file:///Users/qiaoyu/.gstack/projects/garycaogan-h3-ref-video/qiaoyu-main-eng-review-test-plan-20261124-190657.md) (all Critical Paths & Edge Cases include full `Value:` cards and `no_claim` boundaries).

### Section 4: Performance Review
- **Hot Path**: `POST /api/film/references` fires on every 250ms typing pause in `#film-json`.
  - **Scale Budget**: Capped at `max(2000, config.max_shots() * 2)` shots (`inputs.py:124` parity); O(total line chars) linear regexes (zero nested quantifiers, bracket strip capped at 5 iterations); target `< 10ms` backend response on 200-shot script, `< 50ms` on 2000-shot max script.
  - **Frontend Render Budget**: Top 3 warnings inline + `<details>` capped at **100 rendered DOM items** (+ "M more omitted") so 2000-warning scripts never freeze main thread on render or `zh-CN` ↔ `en` toggle.
  - **Bench / Verification**: Include a 10,000-char single-line ReDoS test and a 500-shot timing assertion (`< 100ms`) in `tests/test_dialogue_budget.py`.
- **Memory & I/O**: Zero DB queries, zero network calls in Phase 1; `inputs/preflight.json` written once (< 50 KB) via `config.atomic_replace()`.

### NOT in scope (Eng)
- **Modifying `h3ref/film.py:compile_prompt()` for legacy jobs without version gating**: Explicitly avoided in Wave 1/3 default path to guarantee zero silent H3 cache invalidation.
- **Replacing `state.json` or migrating historical job directories**: Read-only fallback (`provenance_unknown`) preserves 100% backward compatibility.
- **Automated browser/Playwright CI harness**: Local `node tests/test_workbench_ui.js` + manual responsive check covers UI without adding Chromium CI bloat.

### What already exists (Eng)
- `h3ref/config.py:atomic_replace()`: Atomic file persistence — reused for `inputs/preflight.json` and `ab_state.json`.
- `h3ref/frames.py:327-332` & `h3ref/film.py:418,730`: Proven `submission_unknown` two-phase anti-double-spend pattern — reused in `scripts/ab_eval.py`.
- `workbench/server.py:274-313`: Existing RFC 9457 `segment_problem()` + redacted traceback logger — generalized to `api_problem()`.
- `workbench/jobs.py:181-195`: Existing secret-redaction logic — reused for `storage/server.log` and `ab_eval.py` / `ab_review.html`.
- `tests/test_locale_parity.py` & `tests/test_workbench_ui.js`: Existing 3-suite locale parity & Node DOM test harnesses — extended in-place.

### Failure Modes Registry (Eng)
| Codepath | Realistic Production Failure | Error Handling / Guard | Test Coverage | User Sees | Critical Gap? |
|---|---|---|---|---|---|
| `film.compile_prompt()` / `stage_prompts()` | Legacy job resumes after upgrade -> silent paid LLM re-rewrite | Keep `compile_prompt()` byte-identical (or version-gate) | Legacy 0-spend resume test | Cached `h3` reused, ¥0 extra spend | No |
| `workbench/jobs.py:530` → child CLI | `load()` drops empty shot -> wrong `raw_shot` in `report.md` | Immutable `inputs/preflight.json` written pre-`load()` | Cross-surface E2E parity test | Exact matching `raw_shot` everywhere | No |
| `film.inspect_dialogue_budget()` | IEEE-754 `0.2*3` -> `1.6s > 1.6s` false alarm | `raw_min > effective_duration + 1e-9` + `MAX_SECONDS` clamp | Float & 20s-clamp unit tests | Never `min_seconds <= duration` | No |
| `film.inspect_dialogue_budget()` | Mixed script `"Привет 2026"` -> false `"complete"` | Independent uncovered-script scan (not `count == 0`) | Mixed-script unit test | `dialogue_unsupported_script` WARN | No |
| `POST /api/film/references` | Malformed JSON / non-list `shots` / 500 crash | `api_problem()` RFC 9457 + `storage/server.log` + `instance` | Flask HTTP 400/415/500 tests | Clear localized error + code + log ID | No |
| `workbench/jobs.py:584` / `storyboard.py:73` | `FilmValidationError` escapes as 500 | Widen `except (SystemExit, FilmValidationError)` | Workbench error-handling test | Clean HTTP 400 validation error | No |
| `create.js:20` `renderMode()` | 1.8s polling wipes `#submit-hint` warning echo | Render echo inside `renderMode()` + lexical Locate | `tests/test_workbench_ui.js` | Persistent echo & accurate Locate | No |
| `scripts/ab_eval.py` | Crash mid-API-call -> double-spend on `--resume` | Atomic `ab_state.json` + lock + `submission_unknown` + `--max-cny` reserve | Dry-run & state-machine unit test | Halts & reconciles unknown task | No |

**CRITICAL GAPS FLAGGED**: **0**.

### Worktree Parallelization Strategy
| Step | Modules Touched | Depends On |
|---|---|---|
| **Step 1 (Core Preflight Engine)** | `h3ref/film.py`, `h3ref/messages/`, `tests/test_dialogue_budget.py` | — |
| **Step 2 (Workbench Backend & Error Contract)** | `workbench/references.py`, `workbench/server.py`, `workbench/jobs.py`, `workbench/storyboard.py`, `workbench/messages/` | Step 1 |
| **Step 3 (Workbench Frontend UI & CSS)** | `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, `workbench/static/messages/`, `tests/test_workbench_ui.js` | Step 2 (API contract locked) |
| **Step 4 (Docs & `SKILL.md`)** | `docs/workbench.md`, `README.md`, `SKILL.md` | Step 1–3 |
| **Step 5 (Phase 2 A/B Runner + Prompt Overrides)** | `h3ref/frames.py`, `h3ref/rewrite.py`, `scripts/ab_eval.py` | Independent of Steps 2–4 |

- **Parallel Lanes**:
  - **Lane A (Wave 1 Preflight)**: Step 1 → Step 2 → Step 3 → Step 4 (shared `workbench/` + `messages/`).
  - **Lane B (Wave 2 A/B Scaffolding)**: Step 5 (`h3ref/frames.py`, `h3ref/rewrite.py`, `scripts/ab_eval.py` — disjoint from `workbench/`).
- **Execution Order**: Run Step 1 first (or launch Lane B in parallel with Lane A after Step 1 locks `h3ref/film.py`). Merge Lane A (Wave 1 PR). Run Phase 2 live A/B on Lane B, obtain user blind-review approval, then merge passing Wave 3 prompt arms + `SKILL.md`.
- **Conflict Flags**: `h3ref/messages/{zh-CN,en}.json` and `tests/test_locale_parity.py` are touched by Steps 1, 2, 3, and 5 (`ContentPolicyError`/`rewrite_exhausted`) — keep i18n JSON edits sequential per wave.

### TODOS.md Updates (Consolidated Across All Phases)
1. **E6 (CEO — Deferred)**: *Server-side `peakFilmReferences` shot normalization* — What: Move client-side `create.js:183-254` shot-time normalization to server so peak-reference estimation shares `normalize_shots()`. Why: Eliminates duplicate JS normalization logic outside dialogue preflight scope. Pros: Single normalization implementation. Cons: Widens Wave 1 blast radius into existing cast-grouping UI. Context: `workbench/static/create.js:183-254`. Depends on: Wave 1 `normalize_shots()`.
2. **E7 (CEO — Deferred, linked to `uc1`)**: *Optional non-destructive pacing assistant / one-click split suggestion* — What: Provide calibrated shot-split / duration suggestions after H3 clip calibration. Why: Helps creators resolve over-budget warnings faster without blind timestamp auto-mutation. Pros: Closes fix loop faster. Cons: Risky without H3 calibration (`uc1`). Context: `#film-warnings` in `create.js`. Depends on: `uc1` empirical calibration.
3. **D-TODO-1 (Design — Deferred)**: *Step 03 (`job.html`) preflight summary echo when `frames="none"`* — What: Display a collapsed 1-line preflight warning count above `#script-plan` on Step 03. Why: Surfaces skipped or CLI-origin warnings before approving paid Step 03 video generation. Pros: Closes Step 02 → Step 03 gap. Cons: Requires exposing `inputs/preflight.json` in job snapshot. Context: `workbench/templates/job.html`, `workbench/static/job.js`. Depends on: Wave 1 `inputs/preflight.json`.

### Implementation Tasks (Eng Review)
- [ ] **T12 (P1, human: ~2h / CC: ~10min)** — `h3ref/film.py`, `workbench/jobs.py`, `workbench/storyboard.py`, `workbench/server.py` — Persist immutable `inputs/preflight.json` pre-`load()`, keep `compile_prompt()` byte-identical for legacy jobs, widen `except (SystemExit, FilmValidationError)`, and add legacy 0-spend resume + cross-surface provenance E2E tests
  - Surfaced by: Eng Dual Voices (Native Critical A1/A2/High A3 + Codex #3/#5/#6)
  - Files: `h3ref/film.py`, `workbench/jobs.py`, `workbench/storyboard.py`, `workbench/server.py`, `tests/test_dialogue_budget.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_reference_bindings.py tests/test_offline.py`
- [ ] **T13 (P1, human: ~2h / CC: ~10min)** — `h3ref/film.py`, `workbench/references.py`, `workbench/server.py` — Unify all 3 callers on `inspect_dialogue_budget()`, fix IEEE-754 `1e-9` + `MAX_SECONDS` clamp + non-`count==0` mixed-script scan + paired brackets/cast-gated colon strip + 2000-shot cap + `rate_config` schema validation + `storage/server.log` 500 & 400/415 `HTTPException` RFC 9457 routing
  - Surfaced by: Eng Dual Voices (Native A4/E1–E4/S1 + Codex #1/#2/#8)
  - Files: `h3ref/film.py`, `workbench/references.py`, `workbench/server.py`, `tests/test_dialogue_budget.py`
  - Verify: `.venv/bin/python -m unittest tests/test_dialogue_budget.py tests/test_workbench.py`
- [ ] **T14 (P1, human: ~2.5h / CC: ~12min)** — `workbench/static/create.js`, `h3ref/film.py` — Render `#submit-hint` echo inside `renderMode()`, implement ~50-line JSON lexical Locate scanner, snapshot/restore `<details open>` & `data-focus-key`, cap `<details>` at 100 items, add `sanitize_line_preview()` & `escape_markdown_cell()`, and add full UI race/polling/XSS tests
  - Surfaced by: Eng Native Voice (High C1, Med C2/E4/S2)
  - Files: `workbench/static/create.js`, `h3ref/film.py`, `tests/test_workbench_ui.js`, `tests/test_dialogue_budget.py`
  - Verify: `node tests/test_workbench_ui.js && .venv/bin/python -m unittest tests/test_dialogue_budget.py`
- [ ] **T15 (P1, human: ~2.5h / CC: ~12min)** — `h3ref/frames.py`, `h3ref/rewrite.py`, `scripts/ab_eval.py` — Add default-`None` keyword-only prompt overrides & `frames.neutralize_text()`, isolate any future `d1` check to `llm_rewrite_problems()` (protecting `film.py:527` manual H3), and implement atomic `ab_state.json` (`submission_unknown` + lock + in-flight `--max-cny` reserve + fingerprint check + HTML-safe JSON escaping in `ab_review.html`)
  - Surfaced by: Eng Dual Voices (Native High A5, Med C3/S2, Low C4 + Codex #4/#7)
  - Files: `h3ref/frames.py`, `h3ref/rewrite.py`, `scripts/ab_eval.py`, `tests/test_manual_h3_rewrite.py`, `tests/test_frame_moderation.py`
  - Verify: `.venv/bin/python -m unittest tests/test_manual_h3_rewrite.py tests/test_frame_moderation.py && .venv/bin/python scripts/ab_eval.py --dry-run --arm 3A`

### Eng Completion Summary
- **Step 0: Scope Challenge** — `scope accepted as-is` (16 existing + 2 new files, 1 lightweight exception class, 0 new services)
- **Reality rows** — 7/7 applicable rows emitted (`PLAN_REALITY: phase=eng rows=7/7 missing=none unverified=0 verdict=complete`), 0 unverified premises
- **Architecture Review** — 5 issues found (A1–A5), all resolved mechanically
- **Code Quality Review** — 4 issues found (C1–C4), all resolved mechanically
- **Test Review** — ASCII codepath/user-flow diagram produced, 25 gaps identified & mapped; Test Plan Artifact written to [qiaoyu-main-eng-review-test-plan-20261124-190657.md](file:///Users/qiaoyu/.gstack/projects/garycaogan-h3-ref-video/qiaoyu-main-eng-review-test-plan-20261124-190657.md)
- **Performance Review** — 1 scale risk found (E4 2000-shot & 100-item DOM cap), resolved with explicit budgets
- **NOT in scope** — written
- **What already exists** — written
- **TODOS.md updates** — 3 consolidated items proposed (E6, E7, D-TODO-1)
- **Failure modes** — 0 critical gaps flagged (8 failure modes mapped & mitigated)
- **Unresolved decisions** — 0 in Eng review (4 Taste `d1`–`d4` + 3 User Challenges `uc1`–`uc3` queued for Phase 4 Final Approval Gate)
- **Outside voice** — Codex (`gpt-6-astra`), completed (8 findings: 5 High, 3 Medium)
- **Parallelization** — 2 lanes (Lane A Wave 1 Preflight sequential; Lane B Wave 2 A/B runner parallel after Step 1)
- **Lake Score** — N/A (Autoplan mechanical auto-decisions; 0 completeness polls)





---

## Phase 3: Eng Review (Cycle 2 — Post-`uc1b`/`uc2b`/`uc3b`)

<!-- autoplan-baseline-edits:eng {"sourceSha256":"9ebb4175002f7482c47e29a1301f07956e37c9e828bcdf8a9cc7ddd0143bd734","replacements":[{"oldText":"#### 1.1 纯函数设计（`h3ref/film.py`）\n\n新增纯函数 `check_dialogue_budget(film: dict) -> list[dict]`，供 CLI 与工作台共同调用：\n\n- **共享镜头归一化（非就地修改 + 草稿容错）**：在 `h3ref/film.py` 新增纯函数 `normalize_shots(shots, *, strict=False) -> list[dict]`，返回全新浅拷贝列表、**绝不就地修改入参**。沿用 `film.load()` 规则：过滤空 `action` 镜头、缺失 `start`/`end` 默按 `2.0s`、时长下限 `max(0.5, length)`、`round(..., 2)`。`strict=True`（供 `load()` 调用）对空 `shots`、超上限或非法时间抛错；`strict=False`（供 `check_dialogue_budget()` 调用）遇缺失/非 list `shots`、非 dict 项、非 `str` 的 `action`（视为无效并跳过）、或 `bool`/非有限数（`NaN`/`Inf`）/不可解析的 `start`/`end`（回退 `2.0s`）时安全容错，不抛 500。非 `str` 的 `line` 或 `speech` 统一安全视为 `\"\"`，`speech` 做 `.strip().lower()` 后再判空与分流。","newText":"#### 1.1 纯函数设计（纯标准库 `h3ref/preflight.py`，由 `h3ref/film.py` 原样再导出）\n\n将纯标准库预检引擎独立为 `h3ref/preflight.py`（零 `h3ref.config` / `.env` / 网络副作用），由 `h3ref/film.py` 原样再导出 `normalize_shots`、`normalize_speech`、`inspect_dialogue_budget`、`check_dialogue_budget`、`FilmValidationError`、`DIALOGUE_RATE_CONFIG`、`sanitize_line_preview`、`escape_markdown_cell`，同时移除 `h3ref/i18n.py:7` 对 `config` 的顶层导入（改读 `os.environ.get(\"H3_LANG\", \"\").strip()`），供 CLI、独立 `preflight` 子命令与工作台共同调用：\n\n- **共享镜头归一化（非就地修改 + 草稿容错 + 时间戳有界）**：新增纯函数 `normalize_shots(shots, *, strict=False) -> list[dict]`，返回全新浅拷贝列表、**绝不就地修改入参**。沿用 `film.load()` 规则：过滤空 `action` 镜头、缺失或仅单边填写 `start`/`end`（或超出 `[0.0, 3600.0]` 上界，防 `1e308` 触发 `t + length` 溢出）默按 `2.0s` 并计入 `default_duration_shots`、时长下限 `max(0.5, length)`、`round(..., 2)`。`strict=True`（供 `load()` 调用）对空 `shots`、超上限或非法时间抛 `FilmValidationError`；`strict=False`（供 `inspect_dialogue_budget()` 调用）遇缺失/非 list `shots`、非 dict 项、非 `str` 的 `action`（视为无效并跳过）、或 `bool`/非有限数（`NaN`/`Inf`）/越界/不可解析的 `start`/`end`（回退 `2.0s`）时安全容错，不抛 500。非 `str` 的 `line` 或 `speech` 统一安全视为 `\"\"`，`normalize_speech()` 仅用于预检与 `stage_check()`，**绝不改动 `film.compile_prompt()`（`film.py:264,293,299`）对旧任务非规范 `speech` 的字节级输出**。"},{"oldText":"- **说话人前缀清洗（确定性循环，最多 5 轮）**：在行首循环剥离以下前缀直至不再匹配或达到 5 轮上限：① 严格成对全/半角括号及方括号标签（`^(?:（[^）]{1,20}）|\\([^)]{1,20}\\)|\\[[^\\]]{1,20}\\]|【[^】]{1,20}】)\\s*`，拒绝 `(你好】` 跨类型错配）；② 显式说话人冒号前缀（仅匹配 `film.cast` 角色名/别名或常见显式说话人标签 `旁白|画外音|路人[甲乙丙0-9]?|众人|Narrator|VO|OS`，防止误剥 `12:30`、`记住：千万别回头` 或 `Wait: don't go`）。","newText":"- **说话人前缀清洗（确定性循环，最多 5 轮）**：先做全角 ASCII 标准化，再在行首循环剥离以下前缀直至不再匹配或达到 5 轮上限：① 严格成对全/半角括号及方括号标签（`^(?:（[^）]{1,20}）|\\([^)]{1,20}\\)|\\[[^\\]]{1,20}\\]|【[^】]{1,20}】)\\s*`，拒绝 `(你好】` 跨类型错配）；② 显式说话人冒号前缀（仅匹配经清洗的 `film.cast` 角色名/别名——校验 `cast` 为 `list`、项为 `dict`、`aliases` 为 `list[str]`、过滤空串、限制长度 `<= 64`、按长度降序排列使 `林医生` 优先于 `林`——或常见显式说话人标签 `旁白|画外音|路人[甲乙丙0-9]?|众人|Narrator|VO|OS`（ASCII 忽略大小写），防止误剥 `12:30`、`记住：千万别回头` 或 `Wait: don't go`）。"},{"oldText":"- **单镜互斥优先级与数值口径**：同一镜头最多输出 1 条告警，优先级为 `dialogue_slow_over_budget > dialogue_over_budget > dialogue_long_line`。统一按 `rate = slow_rate if is_slow else regular_rate` 计算 `raw_min = count / rate + pause`、`min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10`（向上进位到 0.1 秒，确保所有超预算告警在界面展示时严格满足 `min_seconds > duration`，避免出现「2.0 秒超出 2.0 秒」的舍入穿帮；`dialogue_long_line` 同口径估算）；`duration = round(end - start, 2)`；超镜长判定按 `raw_min > effective_duration + 1e-9`（使用整数毫秒累加停顿或 `1e-9` 容差消除 `0.2+0.2+0.2=0.6000000000000001` 导致的 `1.6s > 1.6s` IEEE-754 穿帮，且 `effective_duration = min(duration, float(config.MAX_SECONDS))` 防止 20 秒单镜被 `film.plan()` 截断至 15 秒时漏报，超上限时在告警中附带 `effective_duration`）；`line_preview` 取清洗前缀并脱敏控制字符后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。\n- **单一事实源返回与确定性 Schema**：新增 `inspect_dialogue_budget(film: dict, *, rate_config: dict | None = None) -> dict` 返回 `{\"status\": \"complete\"|\"partial\"|\"unsupported\"|\"invalid\", \"warnings\": [...], \"meta\": {\"shots_invalid\": bool, \"shots_missing\": bool, \"total_raw_shots\": int, \"checked_shots\": int, \"skipped_shots\": int, \"default_duration_shots\": int, \"unsupported_script_shots\": int}}`，`check_dialogue_budget(film, *, rate_config=None) -> list[dict]` 作为薄包装返回 `warnings`。","newText":"- **单镜互斥优先级与数值口径**：同一镜头最多输出 1 条告警，优先级为 `dialogue_slow_over_budget > dialogue_over_budget > dialogue_long_line`。统一按 `rate = slow_rate if is_slow else regular_rate` 计算 `raw_min = count / rate + pause`、`min_seconds = math.ceil(raw_min * 10 - 1e-9) / 10`（向上进位到 0.1 秒）；`duration = round(end - start, 2)`，`effective_duration = min(duration, float(MAX_SECONDS))`（`MAX_SECONDS = 15.0`）；超镜长判定按 `raw_min > effective_duration + 1e-9`（整数毫秒停顿 + `1e-9` 容差），所有超预算告警严格满足不变量 **`min_seconds > effective_duration`**（当 `effective_duration < duration` 即单镜超过 15s 上限时，告警携带 `effective_duration` 并由全部三套语言包渲染专用文案「估算约 `{min_seconds}s` / 有效生成预算 `{effective_duration}s`（原镜长 `{duration}s`）」，避免 `16s > 20s` 矛盾展示）；`line_preview` 取清洗前缀并脱敏控制字符后的文本，长度 `> 40` 时截为 `text[:39] + \"…\"`（总长 ≤ 40）。\n- **单一事实源返回与可复现元数据 Schema**：新增 `inspect_dialogue_budget(film: dict, *, rate_config: dict | None = None) -> dict` 返回 `{\"status\": \"complete\"|\"partial\"|\"unsupported\"|\"invalid\", \"warnings\": [...], \"meta\": {\"estimator_version\": str, \"effective_config_sha256\": str, \"calibration_status\": \"provisional\"|\"calibrated\"|\"custom\", \"shots_invalid\": bool, \"shots_missing\": bool, \"total_raw_shots\": int, \"checked_shots\": int, \"skipped_shots\": int, \"default_duration_shots\": int, \"unsupported_script_shots\": int}}`，`check_dialogue_budget(film, *, rate_config=None) -> list[dict]` 作为薄包装返回 `warnings`。"},{"oldText":"- **CLI（`h3ref/film.py`）**：\n  - `run()` 在 `load()` 之前读取原始 `job.state[\"film_path\"]`（工作台创建的任务优先读取 `jobs.py:530-532` 在 `load()` 前落盘的不可变 `inputs/preflight.json`，绝不污染 `film.json` 或 `state.json`），统一调用一次 `inspect_dialogue_budget()`，将告警与部分/未覆盖摘要写入 `job.log(\"preflight\", ...)`（`line_preview` 经 `sanitize_line_preview()` 去除控制字符/ANSI 并折叠换行）。**一律为 WARN，不阻断执行**。\n  - `report()` 保持原函数签名不变，优先读取 `inputs/preflight.json`（或未归一化原始 `film_path`），缺失时回退调用 `inspect_dialogue_budget(film)` 并标记来源未知；若有告警或部分检查摘要，在 `report.md` 顶部基础摘要之后、逐段提示词详情之前输出「脚本预检提示」Markdown 表格（单元格经 `escape_markdown_cell()` 转义 `|` 与换行，无告警时省略该小节）。\n- **工作台后端（`workbench/references.py`、`workbench/server.py`、`workbench/jobs.py`、`workbench/storyboard.py`）**：\n  - `describe_references(script, *, lang=\"zh-CN\")` 保持 `lang` keyword-only 默认值以兼容 `GET /api/film/example`；`POST /api/film/references` 传入 `lang=language()`，统一调用一次 `film.inspect_dialogue_budget(script)` 并固定返回 `{\"film\", \"slots\", \"warnings\", \"preflight_status\", \"preflight_meta\"}`（非可选；绝不修改 `result[\"film\"][\"shots\"]`）。\n  - 锁定 `shots` 四态与 10x 上限：`shots` 缺失/`null` → 200 `preflight_status=\"invalid\"`（`shots_missing=True`）；存在且非 list 或长度超过 `max(2000, config.max_shots() * 2)` → HTTP 400 RFC 9457 `api_problem()`；`[]` 或 `checked_shots == 0` → `preflight_status=\"partial\"`；正常返回 `complete`/`partial`/`unsupported`。`POST /api/film/references` 的 400/415 `HTTPException` 与 500 意外异常统一走 `api_problem()`（无任务端点 500 脱敏日志写入 `storage/server.log` + `instance` 关联号，保留 `error` 兼容键）。","newText":"- **CLI 与工作台任务链（`h3ref/film.py`、`workbench/jobs.py`、`workbench/storyboard.py`、`workbench/demo.py`）**：\n  - `workbench/jobs.py:502` `create()` 必须在调用 `validate(payload, self.root)` **之前**（`validate()` 内部 `workbench/inputs.py:148-152` 已运行 `film.load()` 覆盖 `request[\"film\"]`）对原始 `payload.get(\"film\")` 计算完整 `PreflightResult` 快照（含 `estimator_version`/`effective_config_sha256`/`calibration_status`），并在 `_commit()` 中原子写入不可变 `inputs/preflight.json`（绝不污染 `film.json` 或 `state.json`）；新建工作台任务默认走 `workbench/storyboard.py:298`（`operation=\"frames\"`），同样读取 `inputs/preflight.json`（在 `H3_SKIP_PREFLIGHT != \"1\"` 时）并写入 `job.log(\"preflight\", ...)`。\n  - `film.run()` 在 `load()` 之前优先读取 `inputs/preflight.json` 快照（无快照时读取原始 `film_path` 调用一次 `inspect_dialogue_budget()` 并落盘），将告警与部分/未覆盖摘要写入 `job.log(\"preflight\", ...)`（`line_preview` 经 `sanitize_line_preview()` 脱敏）。**一律为 WARN，不阻断执行**。\n  - `film.report()` 保持原函数签名不变，优先按创建时落盘的 `inputs/preflight.json` 快照原样展示（保证配置升级后历史报告结果可复现），缺失时回退调用 `inspect_dialogue_budget(film)` 并显式标注 `provenance_unknown` 与 `recomputed_with_config_sha256`；若有告警或部分检查摘要，在 `report.md` 顶部基础摘要之后、逐段提示词详情之前输出「脚本预检提示」Markdown 表格（单元格经 `escape_markdown_cell()` 转义 `|` 与换行，无告警时省略该小节）。\n  - 全量 `film.load()` 调用点（`workbench/demo.py:72`、`workbench/storyboard.py:25,73,317`、`workbench/jobs.py:584`、`workbench/server.py:715`、`h3ref.py`）统一扩宽捕获 `except (SystemExit, FilmValidationError)`。\n- **工作台参考图与校验接口（`workbench/references.py`、`workbench/server.py`）**：\n  - `describe_references(script, *, lang=\"zh-CN\", include_preflight=False)` 默认不运行预检与 `H3_DIALOGUE_RATE_CONFIG` 加载，保证 `GET /api/film/example` 与 `bind_references()` → `POST /api/jobs` 不被预检配置耦合或崩溃；仅 `POST /api/film/references` 传入 `lang=language(), include_preflight=True`，调用一次 `inspect_dialogue_budget(script)` 并固定返回 `{\"film\", \"slots\", \"warnings\", \"preflight_status\", \"preflight_meta\"}`（绝不修改 `result[\"film\"][\"shots\"]`）。\n  - 锁定 `shots` 四态与 10x 上限：`shots` 缺失/`null` → 200 `preflight_status=\"invalid\"`（`shots_missing=True`）；存在且非 list 或长度超过 `max(2000, config.max_shots() * 2)` → HTTP 400 RFC 9457 `api_problem()`；`[]` 或 `checked_shots == 0` → `preflight_status=\"partial\"`；正常返回 `complete`/`partial`/`unsupported`。将 `workbench/server.py:274` `segment_problem()` 泛化为 `api_problem(..., title_key, doc_anchor, ...)`，覆盖 `POST /api/film/references` 的 400 / 413（`RequestEntityTooLarge`）/ 415（`UnsupportedMediaType`）`HTTPException` 与 500 意外异常（脱敏日志写入 `storage/server.log` + `instance` 关联号，保留 `error` 兼容键）。"},{"oldText":"### 用户审批采纳的扩展与挑战（gate_rev=1: `uc1b` / `uc2b` / `uc3b`）\n\n1. **`uc1b` · H3 历史成片语速校准前置步骤**：\n   - 在阶段 1 锁定默认 `DIALOGUE_RATE_CONFIG` 阈值前，抽样 10–15 条历史 H3 成片（综合 `README.md:117` 1–2s 首帧惯性与 `h3ref/film.py:123` 分段取整），记录各镜台词字数、标点停顿、镜长、实际成片口播是否加速/吞字，统计误报率与漏报率后校准中文默认 `regular_rate` / `slow_rate` / `long_line_limit`（校准数据与结论记录在 `docs/plans/` 或 `evals/golden_shots/` 中；若本地无历史成片样本，先以现有启发式初值落盘并把中文一并标注为待实片校准）。\n   - 基于校准结果评估是否补充**非破坏式**分镜拆句建议（只给文本拆句/延时长建议，绝不自动改写 `#film-json` 里的用户时间戳）。未实测语种（`ja`/`ko`/`en`/`es`/`pt`）继续显式标为未校准。\n2. **`uc2b` · 版本化黄金回归集与模型退场规则（`evals/golden_shots/`）**：\n   - 将阶段 2 的痛点镜头夹具、输入 manifest、盲评基线与结论沉淀为版本化目录 `evals/golden_shots/`（仅存轻量 JSON 夹具、脱敏规则指纹与文字评分记录，不把大体积二进制图片/视频塞进 git 仓库），由 `evals/golden_shots/manifest.json` 明确记录：\n     - 每个夹具覆盖的缺陷类别与预期对照（防穿帮双态、安全转译、视觉否定、FACS 大情绪、克制表演对照镜）；\n     - 当前生效的模型版本（`gpt-image-2.6-sunburst`、`minimax-h3`、`deepseek-v4-pro` / `gemini` / `claude`）；\n     - **模型退场条件（Retirement Rule）**：当上游模型升级后，在不带某条提示词补丁的基线对照中即可达到同等或更优的盲评胜率、0 新增 `ContentPolicyError` 且交付总成本不升时，即判定该补丁过期并可安全移除。\n3. **`uc3b` · CLI 独立零成本预检子命令（`python h3ref.py preflight`）**：\n   - 在 [h3ref.py](../../h3ref.py#L181-L225) 新增只读子命令：`python h3ref.py preflight --film <path|-> [--json] [--fail-on-warn]`。\n   - **零成本与零资产保证**：不读取或要求任何 API Key，不检查 `cast[].picture` / `board` 本地文件是否存在，不创建任务目录，绝不调用任何网络或生成服务；直接读取原始 JSON（`--film -` 支持从 `stdin` 读入）并调用 `film.inspect_dialogue_budget(raw_film)`，完整保留 `raw_shot` / `raw_shot_index` 与 `meta`。\n   - **Agent-Ready 通道与退出码契约**：\n     - 默认人类可读摘要与告警表输出至 `stdout`；`--json` 将完整 JSON（`{\"status\", \"warnings\", \"meta\"}`）输出至 `stdout`，所有错误/诊断信息严格输出至 `stderr`；\n     - 退出码：`0` = 校验完成且无触发失败条件（默认即使有 WARN 也返回 `0`）；`1` = 输入文件不可读、JSON 语法错或 `FilmValidationError`（在 `stderr` 首/末行打印 `code` 与可执行 `fix`）；`2` = 指定 `--fail-on-warn` 且存在任意告警或 `status != \"complete\"`（含 `partial`/`unsupported`/`invalid`）。\n   - **随仓库示例与文档同步**：新增零外部图片依赖示例 `examples/dialogue-budget.json`（含可复现的正常镜、超预算镜、慢速镜与空 `action` 镜），并在 `README.md`、`docs/workbench.md#dialogue-preflight` 与 `SKILL.md` 顶部补充 1 命令 Quickstart 与预期 JSON 输出，将 CLI/Agent TTHW 压缩至 `< 1 min`。","newText":"### 用户审批采纳的扩展与挑战（gate_rev=1: `uc1b` / `uc2b` / `uc3b`）\n\n1. **`uc1b` · H3 历史成片语速校准前置步骤（防样本内过拟合 + 有效修复建议）**：\n   - 在阶段 1 锁定默认 `DIALOGUE_RATE_CONFIG` 阈值前，按预设抽样与人工标注协议抽样 10–15 条历史 H3 成片（覆盖正常/超预算、常规/慢速样本，记录分段边界、实际生成秒数 `film.plan()` 与实际口播起止区间以将首帧惯性与纯语速解耦），区分调参集与留出验证集并报告混淆矩阵计数（`TP/FP/TN/FN`）；若本地样本不足无法形成独立留出验证，保留启发式初值并在 `meta.calibration_status` 显式标为 `\"provisional\"`（不伪装为已泛化校准）。\n   - 区分长句可读性提示（`dialogue_long_line`）与超预算修复建议（`dialogue_over_budget` / `dialogue_slow_over_budget`）：同总时长内硬拆成两镜并不能增加有效口播时间，且原镜已达 15s（`MAX_SECONDS`）时延长原镜无法突破 `film.plan()` 上限——超预算建议必须明确「增加总有效生成预算（拆分为总预算足够的多段）或精简台词」，无可靠分句边界时不给具体拆句；绝不自动改写 `#film-json` 里的用户时间戳。未实测语种继续显式标为未校准。\n2. **`uc2b` · 版本化黄金回归集与同模型退场规则（`evals/golden_shots/`）**：\n   - 将阶段 2 的痛点镜头夹具（含 Arm 2A 多镜连续 `locked_look` 冲突夹具）、输入 manifest、盲评基线与结论沉淀为版本化目录 `evals/golden_shots/`（仅存轻量 JSON 夹具、脱敏规则指纹与文字评分记录，不把大体积二进制图片/视频塞进 git 仓库），由 `evals/golden_shots/manifest.json` 明确记录：\n     - 不可变 `run_id`、完整生成配置（工作流/采样预设 `config.py:89-100`、`H3_REWRITER`、具体上游模型版本或显式 `\"unpinned\"`）、实际改写后提示词 `sha256` 摘要，以及仓库外参考图/冻结首帧/评分视频的相对路径与内容 `sha256`（版本未锁定或外部素材无法校验时显式标为 `reproducibility: \"unreproducible\"`）；\n     - 每个夹具覆盖的缺陷类别与预期对照（防穿帮双态含多镜 `locked_look`、安全转译、视觉否定、FACS 大情绪、克制表演对照镜）；\n     - **同模型退场条件（Retirement Rule）**：必须锁定**同一新模型 + 同一输入夹具 + 同一生成配置**，对比「当前完整补丁集」与「仅移除目标补丁」两组——仅当各缺陷类别均满足非劣标准（不允许总体胜率掩盖单类缺陷退化）、0 新增 `ContentPolicyError`、交付总成本不升且重跑组合态回归门禁通过后，才可安全移除该过期补丁。\n3. **`uc3b` · CLI 独立零成本预检子命令（`python h3ref.py preflight`）**：\n   - 在 [h3ref.py](../../h3ref.py#L181-L225) 新增只读子命令：`python h3ref.py preflight --film <path|-> [--json] [--fail-on-warn]`。\n   - **导入隔离与零副作用保证**：`h3ref.py` 在顶层导入 `h3ref.config` / `h3ref.film` / `rh` / `comfy` **之前**抢先分派 `preflight` 子命令，仅导入纯标准库 `h3ref.preflight` 与去 `config` 化的 `h3ref.i18n`，绝不读取 `.env` 或任何 API Key、绝不解析 `H3_STEPS` 等无关生成环境变量（非法 `H3_STEPS` 不影响预检），仅从 `os.environ` 读取 `H3_DIALOGUE_RATE_CONFIG`（须满足 `lstrip().startswith(\"{\")` 或常规文件 `S_ISREG` 且 `<= 64KB`）与 `H3_LANG`，不检查图片存在、不建目录、零网络/写盘；`--film -` 上限 32 MB 并将 `OSError`/`IsADirectoryError`/`UnicodeDecodeError` 转为结构化 `stderr` + 退出码 `1`。\n   - **优先级与 Agent-Ready 退出码契约**：显式 `preflight` 子命令**始终忽略 `H3_SKIP_PREFLIGHT=1`**（该开关仅跳过流水线自动预检），同时通过 `meta.effective_config_sha256` 披露当前生效阈值；默认人类可读摘要输出至 `stdout`，`--json` 输出完整 `{\"status\", \"warnings\", \"meta\"}` 至 `stdout`，所有错误严格输出至 `stderr`；退出码：`0` = 校验完成且无触发失败条件、`1` = 输入/JSON/`FilmValidationError`/配置非法、`2` = `--fail-on-warn` 且存在告警或 `status != \"complete\"`。\n   - **随仓库示例与文档同步**：新增零外部图片依赖示例 `examples/dialogue-budget.json`（验收测试锁定默认配置 `H3_DIALOGUE_RATE_CONFIG=\"\"`），并在 `README.md`、`docs/workbench.md#dialogue-preflight` 与 `SKILL.md` 顶部补充 1 命令 Quickstart 与预期 JSON 输出，将 CLI/Agent TTHW 压缩至 `< 1 min`。"},{"oldText":"- 核心逻辑：`h3ref/film.py`、`h3ref/frames.py`、`h3ref/rewrite.py`、`h3ref.py`","newText":"- 核心逻辑：`h3ref/preflight.py`、`h3ref/i18n.py`、`h3ref/film.py`、`h3ref/frames.py`、`h3ref/staging.py`、`h3ref/rewrite.py`、`h3ref.py`"}]} -->

<!-- autoplan-accepted:eng -->
- Protect historical job H3 prompt cache from silent invalidation (reconciling prior DX `compile_prompt()` note with Eng F2 + Codex C7): keep `h3ref/film.py:compile_prompt()` (`film.py:33,251,264,299`) 100% byte-identical for existing/unversioned jobs (do NOT call `normalize_speech()` inside default `compile_prompt()`, so historical `"zh-CN"`, `"ZH"`, `"zh_CN"`, and `"   "` preserve exact `zh` hash and reuse `rec['h3']`); use `normalize_speech()` only in `h3ref/preflight.py:inspect_dialogue_budget()` and `film.stage_check()` (or future opt-in `prompt_compiler_version >= 2`); converge Item 2 video-side asset-override precedence into `h3ref/rewrite.py:32` & `rewrite.RULES` rule 9 (and `h3ref/frames.py:209,217` on image side); require Arm 2A to include multi-shot sequential fixtures (Shot 1 wearing -> Shot 2 removed/held) and, if `frames.py:95,104` / `film.py:444` / `staging.py:200` `locked_look` conflicts visually, resolve precedence without breaking cross-shot `locked_look` identity reuse; verify via legacy 0-spend resume regression tests covering non-canonical `speech` values.
- Isolate `uc3b` zero-cost CLI (`python h3ref.py preflight --film <path|-> [--json] [--fail-on-warn]`) from `.env`, API keys, and unrelated generation config (Codex C1/C9 + Native F5): implement pure-stdlib `h3ref/preflight.py` (re-exported by `h3ref/film.py` for full SDK compatibility), remove `from . import config` from `h3ref/i18n.py:7` (read `os.environ.get("H3_LANG", "").strip()`), and dispatch `preflight` in `h3ref.py` BEFORE importing `h3ref.config` / `h3ref.film` / `rh` / `comfy` so `.env` is never read and corrupt `H3_STEPS` / `RUNNINGHUB_*` never crash preflight; explicit `preflight` always ignores `H3_SKIP_PREFLIGHT=1` while reading/disclosing `H3_DIALOGUE_RATE_CONFIG`; cap `--film -` stdin at 32 MB and convert `OSError`/`IsADirectoryError`/`UnicodeDecodeError` to structured `stderr` + exit `1`; exit `0` default, `1` input/JSON/`FilmValidationError`/config error, `2` `--fail-on-warn` with any warning or `status != "complete"`; ship `examples/dialogue-budget.json` pinned to default config in acceptance tests + isolated subprocess test.
- Fix end-to-end provenance timing, API decoupling, and exception boundaries (Native F1/F3/F4 + Codex C6): in `workbench/jobs.py:502` `create()`, compute the full `PreflightResult` snapshot (including `meta.estimator_version`, `meta.effective_config_sha256`, `meta.calibration_status`) from raw `payload.get("film")` BEFORE `validate(payload, self.root)` (which already invokes `film.load()` via `workbench/inputs.py:148-152`), write `inputs/preflight.json` atomically in `_commit()`, and read/log it in `workbench/storyboard.py:298` (`operation="frames"`) as well as CLI `film.run()` and `film.report()` (legacy jobs without `inputs/preflight.json` fall back with `provenance_unknown` and `recomputed_with_config_sha256`); add `include_preflight=False` to `workbench/references.py:describe_references()` so `GET /api/film/example` and `bind_references()` -> `POST /api/jobs` remain decoupled from preflight and `H3_DIALOGUE_RATE_CONFIG`, enabling `include_preflight=True` ONLY on `POST /api/film/references`; generalize `workbench/server.py:274` `segment_problem()` into `api_problem(..., title_key, doc_anchor)` handling 400/413/415/500 (with redacted `storage/server.log` + `instance` + `error` compat key); widen `except (SystemExit, FilmValidationError)` across ALL `film.load()` callers (`workbench/demo.py:72`, `workbench/storyboard.py:25,73,317`, `workbench/jobs.py:584`, `workbench/server.py:715`, `h3ref.py`).
- Lock arithmetic invariants, bounds, script/prefix sanitization, and security hardening (Native F4/F5 + Codex C8): lock the over-budget invariant to `min_seconds > effective_duration` (`effective_duration = min(duration, float(MAX_SECONDS))` with `MAX_SECONDS = 15.0`, integer-ms pauses + `1e-9` tolerance), always emit `effective_duration` and dedicated 3-suite i18n copy when `effective_duration < duration` ("有效生成预算 15s（原镜长 20s）"); bound raw timestamps in `normalize_shots()` to `[0.0, 3600.0]` (`inputs.py:141`) and treat half-specified or out-of-range `start`/`end` as `2.0s` fallback counted in `default_duration_shots`; scan stripped text independently of `count == 0` for uncovered scripts (`"Привет 2026"`), normalize full-width ASCII BEFORE prefix matching, support `['’]`, CJK Ext A (`\u3400-\u4dbf`), `々` (`\u3005`), and `[-_]` locale tags; harden dynamic `cast` speaker stripping (validate `list`/`dict`/`aliases`, drop empty names, cap length `<= 64`, sort longest-first so `林医生` precedes `林`, case-insensitive ASCII role tags, strictly paired brackets capped at 5 loops); harden `H3_DIALOGUE_RATE_CONFIG` (inline JSON only when `val.lstrip().startswith("{")`; file path requires `stat.S_ISREG` and `st_size <= 65536`; finite positive non-bool floats; never echo file text; never add to `server.py:FIELDS`); bound `POST /api/film/references` `shots` to `max(2000, config.max_shots() * 2)`; add `sanitize_line_preview()` and `escape_markdown_cell()`.
- Harden frontend a11y, polling stability, and Phase 2 runner/calibration/golden-set contracts (Native F6/F7/F8/F9 + Codex C2/C3/C4/C5): mount dedicated `.sr-only` `#film-preflight-live` (`aria-live="polite"`) decoupled from `#film-parse-status`; render `#submit-hint` echo as a persistent child guarded by `state.kind === "film" && state.filmWarnings.length > 0` updating only when `(kind, baseKey, warningCount, lang)` changes (no focus loss on 1.8s polling); implement depth-aware JSON lexical scanner (`depth===1` `"shots"` -> `depth===2` `raw_shot_index` -> `depth===3` `"line"`, with `scrollTop` centering); scope warning list CSS under `.film-warnings` and cap `<details>` at 100 items; make `rewrite.backend()` read `H3_REWRITER` dynamically (or accept `rewriter_override`), isolate any future `d1` regex strictly to `rewrite.llm_rewrite_problems()` (protecting manual H3 at `film.py:527` + `workbench/segments.py:561,570,579`), reuse `frames.draw()` & `backend.estimate_cny()`, compile `frames.neutralize_text(text, *, neutral_words=None)` dynamically with zero logging inside pure `frames.prompt()`, enforce zero `innerHTML` + anonymized `blind/<pair_id>_L/_R` media paths + standalone 8-`SECRET_NAMES`/`Bearer`/`sk-` redaction + HTML-safe JSON escaping in `ab_review.html`, and atomic `ab_state.json` (`submission_unknown` + lock + in-flight `--max-cny` reserve + fingerprint check); enforce `uc1b` pre-specified sampling/labeling protocol + tuning/holdout split (or `"provisional"` stamp) + confusion matrix + budget-valid over-budget advice respecting 15s cap; enforce `uc2b` `evals/golden_shots/manifest.json` reproducibility metadata (`run_id`, workflow/sampling/model pin, prompt `sha256`, external asset pointers+hashes, `reproducibility` flag) and same-model per-defect-category non-inferiority retirement rule with combined-state gate re-run.
<!-- /autoplan-accepted:eng -->

### Decision Audit Trail (Phase 3: Eng Cycle 2 — Post-`uc1b`/`uc2b`/`uc3b`)
| # | Phase | Decision | Classification | Principle | Rationale |
|---|---|---|---|---|---|
| 29 | Eng-R2 | Extract pure-stdlib `h3ref/preflight.py` (re-exported by `h3ref/film.py`), remove `config` import from `h3ref/i18n.py:7`, and dispatch `preflight` in `h3ref.py` before importing `config`/`film` | Mechanical (Auto-fix) | P5 | Codex C1 proved `h3ref.py:23` -> `config.py:63,94-96` loads `.env` (API keys) and parses `H3_STEPS` at import time, breaking `uc3b`'s zero-`.env`/zero-API-key promise |
| 30 | Eng-R2 | Compute `inputs/preflight.json` snapshot in `workbench/jobs.py:502` BEFORE `validate()` (`inputs.py:148` runs `film.load()`) and log preflight in `workbench/storyboard.py:298` (`operation="frames"`) | Mechanical (Auto-fix) | P1 / P5 | Native F1 (Critical) proved `jobs.py:530` was already post-`film.load()`, and newly created Workbench film jobs stop at `operation="frames"` without calling `film.run()` |
| 31 | Eng-R2 | Reconcile DX vs Eng contradiction: keep `compile_prompt()` raw-speech byte-identical for legacy jobs; use `normalize_speech()` only in `inspect_dialogue_budget()` & `stage_check()` | Mechanical (Auto-fix) | P5 | Both Native F2 and Codex C7 caught that calling `normalize_speech()` inside `compile_prompt()` busts `rec['zh']==zh` cache on legacy `"zh-CN"`/`"ZH"`/`"   "` scripts |
| 32 | Eng-R2 | Add `include_preflight=False` to `describe_references()` (enabled only on `POST /api/film/references`), generalize `api_problem()` for 400/413/415/500, and widen `except (SystemExit, FilmValidationError)` across all 6 `film.load()` callers | Mechanical (Auto-fix) | P1 / P5 | Native F3/F4 proved `describe_references()` is shared with `GET /api/film/example` & `POST /api/jobs`, and `demo.py:72`/`storyboard.py:25,317` also call `film.load()` |
| 33 | Eng-R2 | Lock over-budget invariant to `min_seconds > effective_duration` with dedicated 3-suite clamped copy (`effective_duration < duration`), bound timestamps to `[0.0, 3600.0]`, and stamp `estimator_version`/`effective_config_sha256`/`calibration_status` in `PreflightResult["meta"]` | Mechanical (Auto-fix) | P1 / P5 | Both Native F4 and Codex C8 proved `duration=20, raw_min=16, effective=15` violated `min_seconds > duration`; Codex C6 proved config changes would silently drift historical reports without config fingerprints |
| 34 | Eng-R2 | Explicit `python h3ref.py preflight` always ignores `H3_SKIP_PREFLIGHT=1`; harden `cast` prefix matching, `H3_DIALOGUE_RATE_CONFIG` (`S_ISREG` & `<=64KB`), `--film -` 32MB cap, and `ab_review.html` (zero `innerHTML`, anonymized media, 8-`SECRET_NAMES` redaction) | Mechanical (Auto-fix) | P5 | Codex C9 + Native F5 closed silent CLI bypass, FIFO/OOM file-read DoS, dirty `cast` crashes, and DOM XSS / blind-label leaks |
| 35 | Eng-R2 | Frontend: dedicated `.sr-only` `#film-preflight-live`, signature-guarded `#submit-hint` echo, depth-aware JSON lexical scanner + `scrollTop` centering, scoped `.film-warnings` CSS | Mechanical (Auto-fix) | P5 | Native F6 prevented `#film-parse-status` announcement clobbering, 1.8s polling focus loss, nested-key scanner mismatches, and `.runtime-subbox` regressions |
| 36 | Eng-R2 | Phase 2 runner & multi-shot `locked_look`: dynamic `rewrite.backend()`, reuse `frames.draw()`/`estimate_cny()`, zero logging in pure `frames.prompt()`, Arm 2A multi-shot `locked_look` test | Mechanical (Auto-fix) | P1 / P5 | Native F7/F8 closed import-time `config.REWRITER` freeze, pure-function log spam on `GET /api/jobs/<id>` polls, and cross-shot `locked_look` duplication |
| 37 | Eng-R2 | Harden `uc1b` (pre-specified sampling/holdout/`"provisional"` stamp, confusion matrix, budget-valid 15s-aware fix advice) and `uc2b` (reproducibility manifest + same-model per-category non-inferiority retirement rule + combined-state gate) | Mechanical (Auto-fix) | P1 / P5 | Codex C2/C3/C4/C5 eliminated in-sample calibration overfitting, ineffective split hints, irreproducible golden manifests, and premature cross-model patch deletion |

### Step 0: Cycle 2 Scope & Reality Verification
- **Scope Re-Challenge (Post-`uc1b`/`uc2b`/`uc3b`)**:
  - New modules/files added by `uc1b`/`uc2b`/`uc3b` + import isolation: `h3ref/preflight.py` (stdlib-only), `examples/dialogue-budget.json`, `evals/golden_shots/manifest.json` (plus surgical 1-line `config` removal in `h3ref/i18n.py:7`).
  - Zero new external packages, zero new services/daemons/DB tables; all 3 user-approved extensions cleanly land within existing 3-wave delivery plan.

#### Eng Cycle 2 Reality Premise Table (`unverified=0`)
| # | Premise | Claim | File:Line Receipt | Verdict |
|---|---|---|---|---|
| E2-P1 | `h3ref.py:23` eagerly loads `.env` & parses `H3_STEPS` | `h3ref.py:23` imports `config`; `config.py:63` loads `.env`; `config.py:94-96` runs `int()`/`float()` at import time; `h3ref/i18n.py:7` also imports `config` | `h3ref.py:23`, `h3ref/config.py:63,94-96`, `h3ref/i18n.py:7` | verified |
| E2-P2 | `jobs.create()` mutates `request["film"]` at L502 before L530 | `jobs.py:502` calls `validate(payload, self.root)` -> `inputs.py:148-152` calls `film.load()`; L509 sets `operation="frames"` | `workbench/jobs.py:501-533`, `workbench/inputs.py:148-152` | verified |
| E2-P3 | Workbench new jobs run `storyboard.py:298`, not `film.run()` | `storyboard.py:298-307` handles `operation == "frames"` directly via `film.stage_frames()` without invoking `film.run()` | `workbench/storyboard.py:298-307` | verified |
| E2-P4 | `describe_references()` is shared by 3 callers | `references.py:9` is called by `POST /api/film/references`, `bind_references()` (`references.py:37` -> `POST /api/jobs`), and `GET /api/film/example` | `workbench/references.py:9,37` | verified |
| E2-P5 | `h3ref/__init__.py` is empty (safe lazy import) | Importing `h3ref.preflight` + config-free `h3ref.i18n` triggers zero package-init side effects | `h3ref/__init__.py:1` | verified |
| E2-P6 | `config.py:89-100` defines workflow & resolution presets | `RH_WORKFLOW_ID`, `RH_GRAPH`, `COMFY_GRAPH`, `H3_STEPS`, `SIZES`, `COMFY_RESOLUTIONS` control video generation output | `h3ref/config.py:89-100` | verified |
| E2-P7 | `film.plan()` caps segments at `MAX_SECONDS = 15` | `plan()` clamps `seconds` to `min(max_len, ...)` where `max_len = config.MAX_SECONDS` (`15`) | `h3ref/film.py:106-127` | verified |

REALITY: premise-table pass All 7 Cycle 2 engineering premises verified directly against live files; unverified=0 h3ref/config.py:63-100
REALITY: already-done pass Checked `h3ref.py`, `h3ref/i18n.py`, `workbench/jobs.py`, `workbench/references.py`, `workbench/storyboard.py` — 0 preflight/uc1b/uc2b/uc3b changes landed h3ref.py:1-25
REALITY: surface-check pass Verified `h3ref.py:23` eager config import, `h3ref/i18n.py:7` config import, `workbench/jobs.py:502` validate()-before-_commit(), `workbench/storyboard.py:298` operation=frames, `workbench/references.py:9` describe_references(), `h3ref/frames.py:95` locked_look workbench/jobs.py:501-541
REALITY: binding-decisions pass Honored `tasks/lessons.md` (all 5 project rules) plus `gate_rev=1` user reply `rec` adopting `uc1b`/`uc2b`/`uc3b` and confirming `d1a`–`d4a` tasks/lessons.md:1-54
REALITY: numbers pass Verified `h3ref/config.py:94-96` import-time int/float parsing [measured: h3ref/config.py:94-96]; `MAX_SECONDS=15` clamp [measured: h3ref/film.py:106-123]; `workbench/inputs.py:141` 3600s bound [measured: workbench/inputs.py:141]; `H3_DIALOGUE_RATE_CONFIG` 64KB cap & 32MB stdin cap [measured: workbench/server.py:34] h3ref/config.py:63-100
REALITY: deferred-asks pass No linked tracker issue; E6/E7/D-TODO-1 preserved; `uc1b`/`uc2b`/`uc3b` now integrated into Implementation plan and verified in Eng Cycle 2 docs/plans/2026-11-23-prompt-and-preflight-enhancements.md:153-170
REALITY: pr-count pass 1 PR per wave (Wave 1 deterministic preflight + `uc3b` CLI; Wave 2 experimental `ab_eval.py` + `uc2b` golden set; Wave 3 passing prompt arms + docs) docs/plans/2026-11-23-prompt-and-preflight-enhancements.md:145-150

### Step 0.5: Dual Voices (Eng Cycle 2 — Post-`uc1b`/`uc2b`/`uc3b`)

#### Claude SUBAGENT (eng Cycle 2 — independent review, `gemini-4-neon`)
- **Snapshot Verified**: `INPUT: eng 562728c877e76cd346d6b0e43a88230b83e58f94b79a49e3ce922eb54efd98ea` (exact match).
- **Summary (9 findings: 1 Critical, 6 High, 2 Medium)**:
  - **Critical F1**: `workbench/jobs.py:502` calls `validate()` (which already runs `film.load()` via `workbench/inputs.py:148-152`) before `jobs.py:530-532`; new Workbench jobs default to `operation="frames"` (`workbench/storyboard.py:298`) and do not call `film.run()`.
  - **High F2–F7**: DX vs Eng `compile_prompt()` `normalize_speech()` contradiction (`h3ref/film.py:264,293,299`); `describe_references()` shared across `/api/film/example` & `POST /api/jobs` + `api_problem()` 413/415/500 generalization; `effective_duration` vs `min_seconds > duration` invariant + `1e308` timestamp overflow + remaining `film.load()` callers (`workbench/demo.py:72`, `workbench/storyboard.py:25,317`); `cast` sanitization + `H3_DIALOGUE_RATE_CONFIG` `S_ISREG`/`64KB` + `ab_review.html` zero `innerHTML` & 8-`SECRET_NAMES` redaction + `--film -` 32MB cap; UI dedicated `.sr-only` live node + `#submit-hint` signature guard + depth-aware lexical scanner + scoped CSS; multi-shot `locked_look` conflict (`h3ref/frames.py:95,104`, `h3ref/staging.py:200`).
  - **Medium F8–F9**: Dynamic `rewrite.backend()`, `frames.draw()`/`estimate_cny()` reuse, anonymized blind media filenames, pure `frames.prompt()` zero-logging, and 6 new regression test groups.

#### Codex SAYS (eng Cycle 2 — architecture challenge, `gpt-6-astra`)
```tool-output
CODEX_MODEL: gpt-6-astra (exec; role: plan-review, tier: frontier; source: gstack catalog frontier/openai (verified 2026-10-07))
OUTSIDE_STATUS: completed provider=codex host=claude
VERDICT: FINDINGS (3 High, 6 Medium; FINDINGS: P1)

1. High — uc3b 的“不读取 API Key”承诺被导入链提前破坏（h3ref.py:23 导入 config -> config.py:63 读取 .env 并解析 H3_STEPS 等无关参数）。
   Recommendation: 明确预检的导入隔离方案，并用独立子进程验证禁止读取 .env、禁止网络、禁止写文件且无关生成配置损坏仍可运行 because 零成本保证必须覆盖命令启动全过程，而不只是 inspect_dialogue_budget() 函数体。
2. High — uc2b 的退场规则没有定义正确的比较对象，可能误删仍有效的补丁（跨模型历史对比和总体胜率会掩盖单类缺陷退化）。
   Recommendation: 将退场比较锁定为同一新模型、同一输入及生成配置下的“当前完整补丁集”对“仅移除目标补丁”，预先定义分缺陷类别的非劣标准并重跑组合态门禁 because 跨模型历史比较和总体平均分都无法证明某条补丁可以安全移除。
3. Medium — uc2b 的版本化目录尚不足以重建评测证据（缺不可变 run_id、完整工作流/采样配置、实际改写提示词摘要、外部素材定位及哈希与不可复现标记）。
   Recommendation: 给每次评测保存不可变 run ID、完整生成配置与实际提示词摘要，以及外部素材的相对定位和内容哈希；无法确定上游版本或找回素材时标记证据不可复现 because Git 中的 manifest 版本并不等于模型和评测素材都已固定。
4. Medium — uc1b 容易把校准样本上的拟合结果误当成验证结果（10-15 条样本内调参+评估存在乐观偏差，且未解耦首帧惯性与分段取整）。
   Recommendation: 预先固定抽样和人工标签规则，记录片段边界、生成秒数及实际口播区间；区分调参与留出验证，没有足够独立验证时仍标为暂定校准，并报告混淆矩阵计数 because 小样本内误报率下降不能证明默认阈值具备泛化能力。
5. Medium — uc1b 的拆句建议可能无破坏性，却完全无效（同总时长拆两镜不增口播时间，超 15s 原镜无法继续延长）。
   Recommendation: 区分长句可读性建议与超预算修复建议；后者明确需要增加有效生成时间或减少台词，跨越上限时建议拆成具有足够总预算的片段，无可靠切分点时不生成具体拆句 because 不修改原始数据不代表建议在实际生成流程中可行。
6. Medium — 校准及 d4 配置覆盖让“相同输入、相同预检结果”的保证失效（inputs/preflight.json 需固定估算器版本、有效配置指纹与校准状态）。
   Recommendation: 在预检元数据中定义估算器版本、有效配置指纹和校准状态，并明确历史报告展示当次结果还是重新检查结果；需要重算时显式标注 because 原始 JSON 的不可变性只能保证输入来源，不能保证算法输出可复现。
7. High — 共享语言归一化与历史提示词字节一致仍存在明确冲突（film.py:264,299 直接用原始 speech，归一化会改动历史 "zh_CN"/"ZH"/空白任务输出）。
   Recommendation: 明确旧编译器保留原始语言行为，共享归一化仅用于预检或显式启用的新编译器版本，并增加上述非规范语言值的历史缓存回归用例 because “统一 helper”不能同时满足所有既有输入的字节一致承诺。
8. Medium — 已采纳的 15 秒截断规则与告警显示不变量不可同时满足（duration=20, raw_min=16, effective_duration=15 时 16 <= 20）。
   Recommendation: 将超预算不变量改为 min_seconds > effective_duration，截断场景明确显示“有效生成预算 15s，原镜长 20s”，同步更新全部展示端和测试 because 判定使用的预算必须与告警解释使用的预算一致。
9. Medium — uc3b 的显式预检与 d4 的跳过开关没有优先级契约（H3_SKIP_PREFLIGHT=1 下显式 preflight --fail-on-warn 应检查而非静默放行）。
   Recommendation: 规定显式 preflight 命令忽略流水线跳过开关，继续支持并披露有效阈值覆盖；示例验收使用固定配置，并为跳过开关与 --fail-on-warn 的组合增加命令级测试 because 用户显式请求检查时不能被隐含环境设置静默绕过。
```

#### ENG DUAL VOICES — CONSENSUS TABLE (Cycle 2)
```text
ENG DUAL VOICES — CONSENSUS TABLE (Cycle 2):
  Dimension                           Claude  Codex  Consensus
  1. Architecture sound?               FAIL    FAIL   CONFIRMED (F1 jobs.py:502 timing, C1 h3ref.py:23 import isolation, F2/C7 compile_prompt() conflict, F3 describe_references() decoupling)
  2. Test coverage sufficient?         GAP     GAP    CONFIRMED (uc3b subprocess isolation, H3_SKIP_PREFLIGHT=1 precedence, non-canonical speech 0-spend resume, 20s effective_duration, uc1b holdout/confusion matrix)
  3. Perf risks + budget/bench named?  GAP     OK     Single-voice High (F4 1e308 overflow bound, F5 64KB config / 32MB stdin caps, F6 1.8s polling signature guard — auto-fixed)
  4. Security threats covered?         GAP     GAP    CONFIRMED (C1 zero-.env leak, F5 S_ISREG/64KB config, dirty cast, ab_review.html zero-innerHTML & 8-secret redaction — auto-fixed)
  5. Error paths handled?              GAP     GAP    CONFIRMED (F3 413/415/500 api_problem, F4 all 6 film.load() callers, C9 H3_SKIP_PREFLIGHT precedence, F5 stdin OSError exit 1 — auto-fixed)
  6. Deployment risk manageable?       FAIL    FAIL   CONFIRMED (F2/C7 legacy speech cache bust, C2/C3 uc2b retirement & manifest reproducibility, C6 config-fingerprint drift — auto-fixed!)
CONFIRMED = native + outside agree; primary cannot replace outside. DISAGREE -> taste.
Missing/disabled voice = N/A, never CONFIRMED. Single-voice critical finding: F1 (jobs.py:502 pre-validate() timing) — auto-fixed directly into baseline & accepted obligations.
```

### Updated Failure Modes Registry (Eng Cycle 2 Additions)
| Codepath | Realistic Production Failure | Error Handling / Guard | Test Coverage | User Sees | Critical Gap? |
|---|---|---|---|---|---|
| `h3ref.py preflight` startup | Eager `config` import loads `.env` / crashes on corrupt `H3_STEPS` | Lazy dispatch before `config`/`film` import -> pure-stdlib `h3ref/preflight.py` + config-free `h3ref/i18n.py` | Isolated subprocess test w/ poisoned `.env` & `H3_STEPS=bad` | Clean zero-cost preflight exit `0`/`2` | No |
| `workbench/jobs.py:502` & `storyboard.py:298` | `validate()` mutates `film` before snapshot; `frames` skips `film.run()` | Compute `PreflightResult` snapshot BEFORE `validate()`; log snapshot in `storyboard.py:298` | Workbench `jobs.create()` -> `storyboard` -> `report` E2E | Exact creation-time warnings in `run.log` & `report.md` | No |
| `h3ref/film.py:compile_prompt()` | Legacy `"zh-CN"`/`"ZH"`/`"   "` cache busted by `normalize_speech()` | Keep `compile_prompt()` raw-speech byte-identical; `normalize_speech()` only in preflight/`stage_check()` | Legacy non-canonical `speech` 0-spend resume test | Cached `h3` reused, ¥0 extra spend | No |
| `preflight` 20s shot & `1e308` timestamp | `16s <= 20s` contradictory UI / `OverflowError` on `t + length` | Invariant `min_seconds > effective_duration` + clamped 3-suite copy + `[0.0, 3600.0]` bound | 20s clamp & `1e308` unit tests | `"有效生成预算 15s（原镜长 20s）"` | No |
| `GET /api/film/example` / `H3_DIALOGUE_RATE_CONFIG` | Bad config breaks example download or blocks on FIFO `/dev/zero` | `describe_references(..., include_preflight=False)` + `stat.S_ISREG` & `<= 64KB` + `lstrip().startswith("{")` | Example & security bounds tests | Example works; bad config cleanly rejected on preflight | No |
| `python h3ref.py preflight --fail-on-warn` | Ambient `H3_SKIP_PREFLIGHT=1` silently passes CI gate | Explicit `preflight` always ignores `H3_SKIP_PREFLIGHT=1` & stamps `effective_config_sha256` | CLI flag + env precedence test | Deterministic exit `2` on warnings | No |
| `evals/golden_shots/` patch retirement | Cross-model comparison retires still-needed patch or hides single-category regression | Same-new-model `"full"` vs `"minus-1"` + per-category non-inferiority + combined-state gate + full reproducibility manifest | `uc2b` manifest schema & retirement checklist | Auditable, reproducible patch retirement | No |

**CRITICAL GAPS FLAGGED (Cycle 2)**: **0**.

### Implementation Tasks (Eng Review — Cycle 2 Reconciled)
- [ ] **T12 (P1, carried from Cycle 1, human: ~2.5h / CC: ~12min)** — `h3ref/film.py`, `h3ref/preflight.py`, `workbench/jobs.py`, `workbench/storyboard.py`, `workbench/demo.py`, `workbench/server.py` — Compute `inputs/preflight.json` snapshot (with `estimator_version`/`effective_config_sha256`/`calibration_status`) BEFORE `validate()` in `jobs.create()`, log preflight in `storyboard.py:298` (`operation="frames"`), keep `compile_prompt()` raw-speech byte-identical for legacy jobs, widen `except (SystemExit, FilmValidationError)` across all 6 callers
- [ ] **T13 (P1, carried from Cycle 1, human: ~2.5h / CC: ~12min)** — `h3ref/preflight.py`, `h3ref/film.py`, `workbench/references.py`, `workbench/server.py` — Decouple `describe_references(include_preflight=False)`, generalize `api_problem()` for 400/413/415/500, enforce `min_seconds > effective_duration` clamped 3-suite copy & `[0, 3600]` bounds, harden `cast` prefix sanitization & `H3_DIALOGUE_RATE_CONFIG` `S_ISREG`/`64KB` checks
- [ ] **T14 (P1, carried from Cycle 1, human: ~2.5h / CC: ~12min)** — `workbench/templates/index.html`, `workbench/static/create.js`, `workbench/static/style.css`, `h3ref/preflight.py` — Add dedicated `.sr-only` `#film-preflight-live`, signature-guarded `#submit-hint` echo, depth-aware JSON lexical Locate scanner + `scrollTop` centering, scoped `.film-warnings` CSS & 100-item DOM cap
- [ ] **T15 (P1, carried from Cycle 1, human: ~3h / CC: ~15min)** — `h3ref/frames.py`, `h3ref/rewrite.py`, `h3ref/staging.py`, `scripts/ab_eval.py` — Dynamic `rewrite.backend()`, reuse `frames.draw()`/`estimate_cny()`, dynamic `frames.neutralize_text()` (zero logs in pure `frames.prompt()`), zero-`innerHTML`/anonymized-media/8-`SECRET_NAMES` redaction in `ab_review.html`, Arm 2A multi-shot `locked_look` conflict test & manual H3 negation tests (`film.py:527` + `segments.py`)
- [ ] **T16 (P1, New — `uc3b`, human: ~2h / CC: ~10min)** — `h3ref/preflight.py`, `h3ref/i18n.py`, `h3ref.py`, `examples/dialogue-budget.json`, docs — Implement zero-`.env`/zero-API-key `python h3ref.py preflight`, ignore `H3_SKIP_PREFLIGHT=1`, 32MB stdin cap, exit codes `0`/`1`/`2`, `examples/dialogue-budget.json` & subprocess isolation tests
- [ ] **T17 (P2, New — `uc1b`, human: ~2h / CC: ~10min)** — `docs/plans/`, `h3ref/preflight.py`, 3 i18n suites — Execute `uc1b` H3 calibration protocol (pre-specified sampling/labeling, tuning vs holdout split or `"provisional"` stamp, confusion matrix) & separate long-line readability hints from budget-increasing/trim over-budget advice (respecting 15s cap)
- [ ] **T18 (P2, New — `uc2b`, human: ~1.5h / CC: ~8min)** — `evals/golden_shots/manifest.json`, `scripts/ab_eval.py` — Create versioned `evals/golden_shots/manifest.json` with immutable `run_id`, full generation config/workflow/model pin, prompt `sha256`, external asset pointers+hashes, `reproducibility` flag & same-model per-category non-inferiority retirement rule

### Eng Cycle 2 Completion Summary
- **Scope Re-Challenge** — `uc1b` / `uc2b` / `uc3b` integrated cleanly (`h3ref/preflight.py` stdlib-only isolation, `examples/dialogue-budget.json`, `evals/golden_shots/manifest.json`)
- **Reality rows** — 7/7 applicable rows emitted (`PLAN_REALITY: phase=eng rows=7/7 missing=none unverified=0 verdict=complete`), 0 unverified premises
- **Dual Voices (Cycle 2)** — Native (`gemini-4-neon`, 9 findings: 1 Critical, 6 High, 2 Medium) + Codex (`gpt-6-astra`, `OUTSIDE_STATUS: completed`, 9 findings: 3 High, 6 Medium); 5/6 consensus dimensions confirmed, 1 single-voice perf/bounds gap auto-fixed; 0 disagreements
- **Test Plan Artifact** — Updated [qiaoyu-main-eng-review-test-plan-20261124-190657.md](file:///Users/qiaoyu/.gstack/projects/garycaogan-h3-ref-video/qiaoyu-main-eng-review-test-plan-20261124-190657.md) with 6 Cycle 2 Critical Path groups (`CP-C2-1`–`CP-C2-6`)
- **Failure modes** — 0 critical gaps flagged across Cycle 1 + Cycle 2
- **Unresolved decisions** — 0 (all `gate_rev=1` user decisions `uc1b`/`uc2b`/`uc3b` + `d1a`–`d4a` absorbed and verified; ready for `gate_rev=2` final sign-off)
