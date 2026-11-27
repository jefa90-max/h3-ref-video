# 显式 `cast_refs` 角色解析与创建页分镜截断告警实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**日期：** 2026-11-26  
**状态：** 计划已拟定，待用户确认后进入实施  
**Goal:** 支持分镜脚本逐镜显式声明 `cast_refs` / `offscreen_cast_refs`，停止对显式空角色段误触发全员兜底，并在工作台创建页实时告警 `shot` 模式下会被九图上限省略的分镜图。

**Architecture:** 在 `h3ref/film.py` 建立唯一的角色出场解析入口 `resolve_cast()`，显式 `cast_refs` 优先于原有正文子串匹配并自动剔除 `offscreen_cast_refs`，仅对未声明 `cast_refs` 的段落保留 `or all_cast` 兜底；后端生图、构图规划、审图接口与预检统一消费该语义，前端 `workbench/static/create.js` 将峰值计算升级为完整参考图预算分析器，使截断与 0 图问题在创建页即时可见。

**Tech Stack:** Python 3.10+（标准库 + Flask）、Vanilla JS（零构建）、`unittest` + `node:test`。

---

## Global Constraints

- **九图硬上限与最小参考图约束**：`config.MAX_IMAGES = 9` 为单段/单镜参考图硬上限；ComfyUI/H3 单段视频生成要求 `1 <= len(pics) <= 9`（`h3ref/comfy.py:298`）。
- **零构建前端与安全策略**：不引入打包器，保持 `default-src 'self'` CSP 兼容。
- **三套中英双语同步**：所有新增错误码、预检告警与界面文案必须成对写入 `h3ref/messages/{zh-CN,en}.json`、`workbench/messages/{zh-CN,en}.json`、`workbench/static/messages/{zh-CN,en}.json`，通过 `tests/test_locale_parity.py`。
- **历史脚本与缓存兼容**：未提供 `cast_refs` 的脚本行为保持 100% 不变；不破坏已落盘的 `state.json`、`composition v2/v3` 缓存与历史任务续跑。
- **单一数据源决策**：只读取 `shot.cast_refs` 与 `shot.offscreen_cast_refs`，**不读取冗余的 `cast[].shot_ids`**，避免用户增删改镜头后两处字段不同步。
- **与 UX 改造计划正交**：本计划前端改动限定在 `workbench/static/create.js` 的参考图分析与提示区及 locale 文件，不依赖也不冲突于 `docs/plans/2026-11-26-workbench-ux-overhaul.md`。
- **提交规范**：修 bug 与对应回归测试遵循清晰分离的 commit 习惯；实现前先写失败测试（TDD）。

## 待确认设计取舍（实施前请拍板）

1. **`cast_refs` 中出现未定义角色名**（例如全角/半角括号打错 `"周屿安(童年)"`，或写了不在 `cast` 中的路人 `"伴娘"`）：
   - **默认推荐**：在 `preflight` 中发出可一键定位的告警 `cast_ref_unknown`（运行时忽略未知项，不阻断导入），同时对非法类型（非数组、非字符串元素）在边界直接 Fail Fast 报错。
   - **备选**：连未知角色名也在 `inputs.validate` / `film.load` 直接报错拦截。
2. **创建页分镜截断提示的阻断级别**：
   - **默认推荐**：强提示但不硬阻断提交（与结果页既有“后部 N 镜沿用文字分镜”语义一致，让用户自行决定合图、切模式或继续）。

## Review Focus

以下 5 类高风险边界必须在对应 Task 的测试中显式锁定：

1. **段内混合显式与隐式镜头**：同一视频段中镜 A 写 `cast_refs: []`、镜 B 省略 `cast_refs` 且正文无角色名 → 因存在未显式声明镜头且整段 0 匹配，保留旧 `or all_cast` 兜底；仅当**整段全部镜头均显式提供 `cast_refs`** 且解析为空时才跳过兜底（锁定于 Task 1）。
2. **`frames="none"` + 无 `board` + 首段全显式 `cast_refs: []`（0 参考图）**：`film.refs()` 必须在创建校验期立即失败并给出中文/英文可执行提示，而不是等到 `comfy.graph()` 或 `rewrite.py` 运行期才崩溃（锁定于 Task 1 + Task 4）。
3. **画外音仅单镜出声 vs 全段仅出声**：`offscreen_cast_refs` 按镜剔除后再做段级并集——同段其他镜露脸的角色仍进入段级参考图，但整段只出声不露脸的角色不占用 `<Picture N>` 视觉位（锁定于 Task 1）。
4. **`cast_refs` 非法类型与拼写错误**：`cast_refs: "周屿安"`、`cast_refs: [123]`、`offscreen_cast_refs: true` 在 `inputs.validate` 与 `film.load` 立即抛具体字段错；未知角色名（如半角括号拼写错）由 `preflight` 输出 `cast_ref_unknown` 并可在前端「定位到脚本」（锁定于 Task 2 + Task 4）。
5. **前后端预算一致性与单角色截断盲区**：即使 `castSlots.length < 2`（不显示多角色合图工具条），只要 `shot` 模式会省略分镜图，创建页也必须展示告警；且 JS `analyzeFilmReferences` 与 Python `film.refs` 在同一跨年龄 60 镜夹具上的段数、可生成分镜总数、截断段号 100% 一致（锁定于 Task 4）。

---

## File Structure

| 文件 | 职责与改动边界 |
|---|---|
| `h3ref/film.py` | 新增 `resolve_cast()` 作为角色解析唯一入口；改造 `refs()`、`frame_reference_paths()`、`stage_frames()`；增加全显式空段免兜底与 0 图保护 |
| `h3ref/preflight.py` | `normalize_shots` 校验 `cast_refs`/`offscreen_cast_refs` 类型；`inspect_with_env` 增加 `cast_ref_unknown` 告警 |
| `h3ref/frames.py` | `describe_composition` 对显式声明 `cast_refs` 的镜头在上下文中透传已解析的 `active_cast` |
| `h3ref/staging.py` | `_actor_sentences` 支持可选 `shot` 单人显式归属，使 `changing_wearables` 兼容带括号阶段名的角色 |
| `workbench/inputs.py` | `validate()` 在边界校验 `shots[i].cast_refs` 与 `shots[i].offscreen_cast_refs` |
| `workbench/storyboard.py` | `plan()` 改用 `film.resolve_cast(..., exclude_offscreen=True)` 计算单镜默认角色图 |
| `workbench/server.py` | `frame_reference()` 改用 `film.resolve_cast(..., exclude_offscreen=True)` |
| `workbench/static/create.js` | 将 `peakFilmReferences` 升级为 `analyzeFilmReferences`，支持 `cast_refs`/`offscreen_cast_refs` 解析、分镜截断提示、0 图提示与 `cast_ref_unknown` 定位 |
| `h3ref/messages/{zh-CN,en}.json` | 新增 `film_segment_no_references`、`film_cast_refs_invalid`、`cast_ref_unknown` |
| `workbench/messages/{zh-CN,en}.json` | 新增 `cast_ref_unknown`（供 `/api/film/references` 预检文案复用） |
| `workbench/static/messages/{zh-CN,en}.json` | 新增 `cast_ref_unknown`、`filmShotFramesOmittedHint`、`filmZeroReferencesHint` |
| `tests/test_cast_refs.py` | 新建：覆盖核心解析、60 镜真实回归、边界校验、构图/审图/可变穿戴与前后端一致性 |
| `tests/test_workbench_ui.js` | 新增创建页截断告警、合图实时消警、0 图告警、`cast_ref_unknown` 定位与 Python 对照测试 |
| `SKILL.md`、`docs/workbench.md` | 补充 `cast_refs` / `offscreen_cast_refs` 字段契约与跨年龄角色写法 |

---

## Tasks

### Task 1: 核心角色解析 `resolve_cast()` 与段级空角色 / 0 图保护

**Files:**
- Modify: `h3ref/film.py:131-165`, `h3ref/film.py:348-355`, `h3ref/film.py:425-430`
- Modify: `h3ref/messages/zh-CN.json`, `h3ref/messages/en.json`
- Test: `tests/test_cast_refs.py`

**Interfaces:**
- Consumes: `h3ref.config.MAX_IMAGES`, `h3ref.i18n.message`, `h3ref.staging.group_cast_by_picture`
- Produces:
  - `h3ref.film.resolve_cast(all_cast: list[dict], shots: list[dict], *, exclude_offscreen: bool = True, fallback_all: bool = False) -> list[dict]`
  - 保留 `h3ref.film.in_shots(c: dict, shots: list[dict]) -> bool` 兼容旧调用与单测，但 `refs()`、`frame_reference_paths()`、`stage_frames()` 全部改调 `resolve_cast()`

- [ ] **Step 1: 编写失败测试 `tests/test_cast_refs.py`（核心解析与 60 镜回归）**

在 `tests/test_cast_refs.py` 中编写 `unittest.TestCase`，断言以下场景：
- `test_explicit_cast_refs_matches_parenthetical_names_without_text_match`：角色名为 `周屿安（童年）`、`周屿安（成年）`，镜头正文只写 `周屿安`，通过 `cast_refs` 精确匹配对应年龄段，不跨年龄串图。
- `test_cast_refs_supports_aliases_and_preserves_cast_order`：`cast_refs` 中写角色 `aliases` 可解析回对应角色，且返回顺序与 `film["cast"]` 定义顺序一致、无重复。
- `test_offscreen_cast_refs_excluded_per_shot_before_segment_union`：同段内镜 2 可见 `周念夏（童年）`、镜 6 写 `offscreen_cast_refs: ["周念夏（童年）"]` → 单镜 6 解析结果不含她，但段级结果含她；若整段每一镜都在 `offscreen_cast_refs` 中，段级结果也不含她。
- `test_explicit_empty_cast_refs_skips_all_cast_fallback`：整段所有镜头均为 `cast_refs: []`（如素人纪实 3 镜段）→ `refs(..., "shot")` 返回 `0` 张角色图 + `board` + 全部 3 张 `frame`，不再塞入全部 8 个角色。
- `test_mixed_explicit_empty_and_legacy_shot_keeps_fallback`：同段内镜 1 写 `cast_refs: []`、镜 2 省略 `cast_refs` 且正文无角色名 → 保留原有 `all_cast` 兜底行为。
- `test_zero_reference_segment_fails_fast`：`frames="none"`、`board=None`、首段全显式 `cast_refs: []` → `film.refs()` 抛出 `SystemExit`，错误信息包含 `第 1 段`（中文）或 `Segment 1`（英文）。
- `test_legacy_script_without_cast_refs_unchanged`：不含 `cast_refs` 的传统脚本与原行为完全一致（含 `GROUP_WORDS` 触发）。
- `test_multi_stage_60_shot_regression_reproduces_29_vs_59_and_60`：构造与实际 60 镜剧本同结构（8 角色含 6 个带括号跨阶段名、第 12 段 6 人 4 镜、第 15–16 段 `cast_refs: []`）的夹具：未合图 `refs` 统计为 59/60（仅第 12 段省略 1 镜），合并 1 对角色后为 60/60，且每镜角色参考图零错配。

- [ ] **Step 2: 运行新测试确认失败**

Run: `.venv/bin/python -m unittest tests/test_cast_refs.py -v`  
Expected: FAIL with `AttributeError: module 'h3ref.film' has no attribute 'resolve_cast'`

- [ ] **Step 3: 实现 `resolve_cast()` 并接入 `refs()`、`frame_reference_paths()`、`stage_frames()`**

在 `h3ref/film.py` 中：
1. 实现 `_cast_lookup(all_cast: list[dict]) -> dict[str, int]`：按 `all_cast` 顺序将 `name.strip()` 与 `aliases` 各项映射到索引（首次出现优先）。
2. 实现 `resolve_cast(all_cast: list[dict], shots: list[dict], *, exclude_offscreen: bool = True, fallback_all: bool = False) -> list[dict]`：
   - 逐镜判断 `explicit = ("cast_refs" in s and s["cast_refs"] is not None)`。
   - 若 `explicit`：取 `s["cast_refs"]` 中清洗后的名字集合；当 `exclude_offscreen=True` 时剔除 `s.get("offscreen_cast_refs") or []`；通过 `_cast_lookup` 收集命中索引。
   - 否则（非显式镜头）：沿用 `in_shots(c, [s])` 收集命中索引。
   - 按 `all_cast` 原序去重返回命中列表；仅当结果为空且 `fallback_all=True` 且**并非所有镜头都显式声明了 `cast_refs`**（`not (shots and all("cast_refs" in s and s["cast_refs"] is not None for s in shots))`）时，回退返回 `list(all_cast)`。
3. 在 `refs()` 中：
   - `active_cast = resolve_cast(all_cast, seg["shots"], exclude_offscreen=True, fallback_all=True)`。
   - 在 `if len(out) > config.MAX_IMAGES:` 之后增加空图保护：`if not out: raise SystemExit(message("film_segment_no_references", number=seg["idx"] + 1))`。
4. 在 `frame_reference_paths()` 和 `stage_frames()` 中将 `[c for c in ... if in_shots(c, [shot])]` 替换为 `resolve_cast(film.get("cast") or [], [shot], exclude_offscreen=True, fallback_all=False)`。
5. 在 `h3ref/messages/zh-CN.json` 与 `h3ref/messages/en.json` 添加 `film_segment_no_references`（含 `{number}` 占位符）。

- [ ] **Step 4: 运行测试验证通过且无回归**

Run: `.venv/bin/python -m unittest tests/test_cast_refs.py tests/test_grouped_cast.py tests/test_locale_parity.py -v`  
Expected: PASS

- [ ] **Step 5: Commit（按需执行时）**

```bash
git add h3ref/film.py h3ref/messages/zh-CN.json h3ref/messages/en.json tests/test_cast_refs.py
git commit -m "feat(film): resolve explicit shot cast_refs and skip fallback on explicit empty segments"
```

---

### Task 2: 边界类型校验与未知角色预检告警

**Files:**
- Modify: `h3ref/preflight.py:83-118`, `h3ref/preflight.py:227-295`, `h3ref/preflight.py:312-326`
- Modify: `workbench/inputs.py:126-143`
- Modify: `h3ref/messages/zh-CN.json`, `h3ref/messages/en.json`
- Modify: `workbench/messages/zh-CN.json`, `workbench/messages/en.json`
- Modify: `workbench/static/messages/zh-CN.json`, `workbench/static/messages/en.json`
- Test: `tests/test_cast_refs.py`, `tests/test_dialogue_budget.py`, `tests/test_locale_parity.py`

**Interfaces:**
- Consumes: `h3ref.preflight.FilmValidationError`, `workbench.inputs.invalid`, `workbench.references.describe_references`
- Produces:
  - `h3ref.preflight._validate_cast_ref_list(raw, field, index, *, strict: bool) -> list[str] | None`
  - 预检告警对象：`{"code": "cast_ref_unknown", "severity": "warn", "shot": int, "raw_shot": int, "raw_shot_index": int, "names": list[str]}`（`warning_message` 将 `names` 拼为逗号/顿号分隔字符串填入 `{names}`）

- [ ] **Step 1: 在 `tests/test_cast_refs.py` 追加校验与预检失败测试**

新增测试方法：
- `test_invalid_cast_refs_type_fails_fast_in_load_and_validate`：分别测试 `cast_refs="周屿安"`、`cast_refs=[123]`、`cast_refs=[""]`、`offscreen_cast_refs=True` 在 `preflight.normalize_shots(..., strict=True)`、`film.load()` 与 `workbench.inputs.validate()` 均抛出具体字段错（包含 `shots[0].cast_refs` / `shots[0].offscreen_cast_refs`）。
- `test_unknown_cast_ref_emits_preflight_warning_with_shot_location`：第 2 镜写 `cast_refs: ["周屿安(童年)"]`（半角括号拼写错），`preflight.inspect_dialogue_budget()` 与 `workbench.references.describe_references(..., include_preflight=True)` 返回 `code == "cast_ref_unknown"`、`shot == 2`、`raw_shot_index == 1`、`names == ["周屿安(童年)"]`，且消息文本同时支持中文和英文。
- `test_known_cast_ref_and_alias_emit_no_unknown_warning`：命中 `cast[].name` 或 `cast[].aliases`、或显式 `cast_refs: []` 时不产生 `cast_ref_unknown` 告警。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest tests/test_cast_refs.py -v`  
Expected: FAIL（`cast_refs` 未校验类型且未生成 `cast_ref_unknown` 告警）

- [ ] **Step 3: 实现边界校验、预检扫描与三套中英 locale 文案**

1. `h3ref/preflight.py`：
   - 新增 `_validate_cast_ref_list(raw, field: str, index: int, *, strict: bool) -> list[str] | None`：
     - `raw is None` 返回 `None`；
     - 非 `list` 或含非非空字符串元素时：`strict=True` 抛 `FilmValidationError("film_cast_refs_invalid", field=f"shots[{index}].{field}", actual=repr(raw)[:80], shot_index=index)`；`strict=False` 返回 `None`（跳过而非崩溃）。
     - 合法时返回去首尾空白的字符串列表（保留空列表 `[]`）。
   - `_normalized_shots` 对每镜检查并标准化 `cast_refs` / `offscreen_cast_refs`（仅当原字段存在且不为 `None` 时保留）。
   - `inspect_with_env` 构建已知角色名集合（`cast[].name` 与 `cast[].aliases` 去首尾空白），逐镜扫描 `cast_refs` 与 `offscreen_cast_refs` 中的未知项，去重保序后产出 `_warning("cast_ref_unknown", number, raw_shot=index + 1, raw_shot_index=index, names=unknown)`。
   - `warning_message` 对 `code == "cast_ref_unknown"` 将 `values["names"]` 格式化为本地化分隔字符串。
2. `workbench/inputs.py`：
   - 在 `validate()` 的 `for index, shot in enumerate(shots):` 循环中，对存在的 `cast_refs` / `offscreen_cast_refs`（非 `None`）校验为数组且每项为非空字符串，与 `cast[i].aliases` 校验风格保持一致。
3. 在 `h3ref/messages/{zh-CN,en}.json` 添加 `film_cast_refs_invalid` 与 `cast_ref_unknown`；在 `workbench/messages/{zh-CN,en}.json` 与 `workbench/static/messages/{zh-CN,en}.json` 添加 `cast_ref_unknown`（确保 `{shot}` / `{names}` 占位符中英一致）。

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/python -m unittest tests/test_cast_refs.py tests/test_dialogue_budget.py tests/test_preflight_review_regressions.py tests/test_locale_parity.py -v`  
Expected: PASS

- [ ] **Step 5: Commit（按需执行时）**

```bash
git add h3ref/preflight.py workbench/inputs.py h3ref/messages/ workbench/messages/ workbench/static/messages/ tests/test_cast_refs.py
git commit -m "feat(preflight): validate shot cast_refs and warn on unknown character references"
```

---

### Task 3: 全链路消费收口（审图接口、构图上下文、可变穿戴归属）

**Files:**
- Modify: `workbench/storyboard.py:45-53`
- Modify: `workbench/server.py:823-832`
- Modify: `h3ref/frames.py:102-117`
- Modify: `h3ref/staging.py:49-102`, `h3ref/staging.py:156-163`
- Test: `tests/test_cast_refs.py`

**Interfaces:**
- Consumes: `h3ref.film.resolve_cast`
- Produces:
  - `workbench/storyboard.py:plan` 与 `workbench/server.py:frame_reference` 输出与 `film.frame_reference_paths` 完全一致的单镜角色参考图
  - `h3ref/frames.py:describe_composition` 在发送给 LLM 的 `context["current_shot"]` 中，当镜头显式声明 `cast_refs` 时附带 `"active_cast": [c["name"] for c in visible_cast]`
  - `h3ref/staging.py:_actor_sentences(film: dict, action: str, *, shot: dict | None = None)`

- [ ] **Step 1: 在 `tests/test_cast_refs.py` 追加链路集成失败测试**

新增测试方法：
- `test_storyboard_plan_and_server_frame_reference_use_cast_refs`：通过 `workbench.storyboard.plan` 和 Flask `test_client` 请求 `/api/jobs/<id>/frames/s1-1/references/1` 及 `s1-6`（含 `offscreen_cast_refs`），断言审图列表与 HTTP 接口返回的角色参考图严格按 `cast_refs - offscreen_cast_refs` 解析。
- `test_describe_composition_includes_active_cast_for_explicit_shot`：mock `infer`，检查传给构图 LLM 的 JSON 上下文中 `current_shot["active_cast"] == ["周屿安（童年）"]`（剔除画外音角色），而无 `cast_refs` 的传统镜头不强塞无关字段。
- `test_changing_wearables_attributes_single_explicit_cast_member`：角色名 `周念夏（童年）`、镜头写 `cast_refs: ["周念夏（童年）"]`、`action: "她脱下校服外套"` → `staging.changing_wearables(script)` 返回 `{"周念夏（童年）": {"外套"}}`。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest tests/test_cast_refs.py -v`  
Expected: FAIL（`storyboard.plan` 与 `server.frame_reference` 仍调用 `in_shots`、`active_cast` 缺失、`changing_wearables` 返回 `{}`）

- [ ] **Step 3: 逐处收口实现**

1. `workbench/storyboard.py:48` 与 `workbench/server.py:826`：将 `[c for c in ... if film.in_shots(c, [shot])]` 替换为 `film.resolve_cast(all_cast, [shot], exclude_offscreen=True, fallback_all=False)`。
2. `h3ref/frames.py:describe_composition`：当 `"cast_refs" in shot and shot["cast_refs"] is not None` 时，导入/复用 `film.resolve_cast`（或放在 `staging` / 无环位置避免循环导入——注意：`frames.py` 不导入 `film.py`，因此将纯函数 `resolve_cast` 放在无副作用底层模块如 `h3ref/preflight.py` 或 `h3ref/staging.py` 再由 `h3ref/film.py` 重导出，可彻底避免循环导入！）。在 `context["current_shot"]` 附加 `"active_cast": [c["name"] for c in resolved]`。
   > **实施注意（防循环导入）：** `h3ref/film.py` 顶层导入 `frames`、`preflight`、`staging`。将 `resolve_cast()` 与 `GROUP_WORDS` 的纯解析实现放在 `h3ref/staging.py`（或 `h3ref/preflight.py`），并在 `h3ref/film.py` 重导出 `resolve_cast`，既保证 `film.resolve_cast` 公开接口不变，又让 `frames.py` 与 `staging.py` 无环调用。
3. `h3ref/staging.py`：`_actor_sentences(film, action, *, shot=None)` 当某句文本未匹配到任何 `name`/`aliases` 且 `shot` 显式解析后仅有 1 名可见角色时，将该句归属给该唯一角色；`changing_wearables` 与 `scope_wearable_look` 传入 `shot=shot`。

- [ ] **Step 4: 运行测试验证通过**

Run: `.venv/bin/python -m unittest tests/test_cast_refs.py tests/test_frame_characters.py tests/test_wearable_continuity.py tests/test_composition_v3_and_retries.py tests/test_workbench.py -v`  
Expected: PASS

- [ ] **Step 5: Commit（按需执行时）**

```bash
git add h3ref/film.py h3ref/frames.py h3ref/staging.py workbench/storyboard.py workbench/server.py tests/test_cast_refs.py
git commit -m "feat(storyboard): wire explicit cast_refs through frame review, composition, and wearables"
```

---

### Task 4: 创建页前端 `cast_refs` 解析与分镜截断实时告警

**Files:**
- Modify: `workbench/static/create.js:185-260`, `workbench/static/create.js:375-398`, `workbench/static/create.js:549-575`
- Modify: `workbench/static/messages/zh-CN.json`, `workbench/static/messages/en.json`
- Test: `tests/test_workbench_ui.js`, `tests/test_cast_refs.py`

**Interfaces:**
- Consumes: `state.film.shots`, `state.film.cast`, `state.filmGroups`, `state.filmBindings`, `$("#frames").value`
- Produces:
  - `analyzeFilmReferences(castSlots, groupBySlot, boardCount)` 返回 `{ peak, hardLimitExceeded, omittedFrames, omittedSegments, plannedFrames, totalShots, omittedBoardSegments, zeroRefSegment }`
  - 保留 `peakFilmReferences(...)` 包装返回 `analyzeFilmReferences(...).peak` 以兼容现有测试
  - 新 i18n key：`filmShotFramesOmittedHint`（`{totalShots}`, `{plannedFrames}`, `{omittedFrames}`, `{segments}`, `{max}`）、`filmZeroReferencesHint`（`{segment}`）、`cast_ref_unknown`（`{shot}`, `{names}`）

- [ ] **Step 1: 在 `tests/test_workbench_ui.js` 新增失败测试**

新增 5 个 `node:test` 用例：
1. `create.js warns on shot-mode frame truncation and clears warning after grouping cast`：导入 60 镜跨年龄剧本（含 `cast_refs`、第 12 段 6 人 4 镜、第 15–16 段 `cast_refs: []`），未合图时 `#film-reference-list` 显示截断提示（含 `59`、`60`、第 `12` 段、省略 `1` 镜）；合并其中 2 个角色后截断提示消失。
2. `create.js warns on legacy script silent truncation (29/60) without cast_refs`：无 `cast_refs`、6 个带括号名字匹配不到时，创建页立即展示截断告警（`29` / `60`，省略 `31` 镜）。
3. `create.js warns on truncation even with single cast slot`：仅 1 个角色、单段 10 个 1.5s 镜头 → 即使无多角色工具条，仍显示分镜截断提示。
4. `create.js warns on zero-reference first segment in none mode`：`frames="none"`、无总表、首段 `cast_refs: []` → 显示 `filmZeroReferencesHint`。
5. `create.js renders cast_ref_unknown preflight warning with locate button`：后端返回 `cast_ref_unknown` 告警时，渲染可读提示并支持点击「定位到脚本」选中对应 `shots[raw_shot_index]`。

- [ ] **Step 2: 运行 JS 测试确认失败**

Run: `node --test tests/test_workbench_ui.js`  
Expected: 新增 5 个用例 FAIL

- [ ] **Step 3: 实现 `analyzeFilmReferences()`、创建页告警渲染与双语文案**

1. `workbench/static/create.js`：
   - 在 `analyzeFilmReferences` 中实现与 `resolve_cast` 同构的 `activeIndicesInShots(shotList, { excludeOffscreen = true, fallbackAll = false })`：
     - 建 `name` / `aliases` 到 cast 下标的查找表；
     - 对每镜若 `Array.isArray(s.cast_refs)`：按 `cast_refs` 剔除 `offscreen_cast_refs` 解析；否则走原 `FILM_GROUP_WORDS` + `name`/`aliases` 文本匹配；
     - 仅当结果为空、`fallbackAll` 为真、且**并非所有镜头都显式提供 `Array.isArray(s.cast_refs)`** 时才回退全员。
   - 遍历 `segments` 计算：
     - `shot` 模式：`room = 9 - segSheets - segBoard`；若 `room < segShots.length && segBoard` 则丢 `board`（记录 `omittedBoardSegments`）并 `room += 1`；`frameCount = Math.min(segShots.length, Math.max(0, room))`；累加 `plannedFrames += frameCount`，若 `frameCount < segShots.length` 记录 `omittedFrames += segShots.length - frameCount` 与 `omittedSegments.push(segIdx + 1)`；同时检查单镜 `boardCount + shotSheets > 9` 置 `hardLimitExceeded = true`、`peak = Math.max(peak, segSheets + segShots.length)`。
     - `first`/`none` 模式：计算 `segTotal = boardCount + segSheets + tailCount`，`segTotal > 9` 时置 `hardLimitExceeded = true`；若 `segTotal === 0` 记录 `zeroRefSegment = segIdx + 1`。
   - `renderFilmReferences()`：
     - 无论 `castSlots.length` 是否 `>= 2`，只要 `analysis.omittedFrames > 0`、`analysis.hardLimitExceeded` 或 `analysis.zeroRefSegment`，都在工具条（`>= 2` 角色）或独立提示块（`< 2` 角色）内渲染对应提示（段号列表超过 6 个时按已有 `dialogue_shots_more` 风格截断显示）。
   - `FILM_WARNING_PRIORITY` 中加入 `cast_ref_unknown: 1`，并在 `filmWarningItem()` 将 `warning.names` 数组用 `t("listSeparator")` 拼接传入 `t("cast_ref_unknown", values)`。
2. `workbench/static/messages/{zh-CN,en}.json` 补充 `filmShotFramesOmittedHint`、`filmZeroReferencesHint`、`cast_ref_unknown`。
3. 在 `tests/test_cast_refs.py` 增加一个 Python/Node 交叉一致性测试（用同一份 60 镜夹具对比 `film.refs()` 与 `create.js:analyzeFilmReferences()` 的 `plannedFrames`、`omittedFrames`、`omittedSegments`）。

- [ ] **Step 4: 运行 JS 与 Python 测试验证通过**

Run: `node --test tests/test_workbench_ui.js && .venv/bin/python -m unittest tests/test_cast_refs.py tests/test_locale_parity.py -v`  
Expected: PASS

- [ ] **Step 5: Commit（按需执行时）**

```bash
git add workbench/static/create.js workbench/static/messages/zh-CN.json workbench/static/messages/en.json tests/test_workbench_ui.js tests/test_cast_refs.py
git commit -m "feat(workbench): surface shot-mode 9-image truncation warnings and support cast_refs in create UI"
```

---

### Task 5: 文档更新与全量回归验证

**Files:**
- Modify: `SKILL.md:22`
- Modify: `docs/workbench.md`（脚本格式与九图预检小节）

- [ ] **Step 1: 同步文档说明**

在 `SKILL.md` 与 `docs/workbench.md` 中简明记录：
- `shots[].cast_refs`（优先于文本匹配，推荐用于跨年龄/多阶段同名角色；显式空数组 `[]` 表示无主演、不触发全员兜底）；
- `shots[].offscreen_cast_refs`（仅出声不出镜角色，不带入视觉参考图）；
- 创建页会在 `shot` 模式存在分镜图被九图上限省略时实时提示受影响段号。

- [ ] **Step 2: 全量回归测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_workbench_ui.js tests/test_results.js tests/test_video_segment_prompts.js tests/test_frame_character_picker.js
```
Expected: 全部测试退出码为 0，0 失败。

- [ ] **Step 3: 复核真实 60 镜脚本验收**

用原始 60 镜脚本跑一遍 `workbench.references.describe_references(..., include_preflight=True)` + `film.plan()` + `film.refs()`：
- 未合图：`59 / 60`（仅第 12 段 6 人 + 4 镜提示省略 1 镜，第 15–16 段不再强塞 8 位主演，镜 6 首帧不含门内周念夏）；
- 合图（合并任意 1 对第 12 段角色）：`60 / 60`，0 截断，0 `cast_ref_unknown` 告警。

- [ ] **Step 4: Commit（按需执行时）**

```bash
git add SKILL.md docs/workbench.md
git commit -m "docs: document explicit cast_refs and offscreen_cast_refs schema"
```
