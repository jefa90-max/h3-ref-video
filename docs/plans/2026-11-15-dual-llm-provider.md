# APIMart / Gemini 官方语言模型双接入

状态：用户已确认推荐方案；双提供方与两阶段独立模型设置已实现，通过离线验证和独立代码审查，等待用户重启工作台验收界面。

## 目标与范围

- 逐镜构图规划和 H3 提示词改写可选择 APIMart 或 Gemini 官方 API。
- 工作台和 CLI 使用同一套提供方配置，默认 APIMart，保留原有 APIMart / 自定义 OpenAI 兼容配置。
- 分镜图片仍由 APIMart 生成；RunningHub 视频生成、已暂停的字幕检查保持现状。
- 不自动跨提供方重试；不重跑已有模型产物或付费任务。

## 接入方案

1. **推荐：Gemini 官方 OpenAI 兼容接口。** 官方地址为 `https://generativelanguage.googleapis.com/v1beta/openai/`，支持现有的 `messages`、Bearer API key 和 `choices[0].message.content`；复用现有 HTTP 实现，无需新增 SDK。
2. **备选：Gemini 原生 generateContent。** 需要增加请求、鉴权、返回值转换。当前只使用文字生成，暂无必须使用原生接口的能力需求，改动和维护成本更高。

参考：
- https://ai.google.dev/gemini-api/docs/openai
- https://docs.apimart.ai/cn/api-reference/texts/general/chat-completions-nostream
- Context7：`/googleapis/python-genai`、`/websites/apimart_ai_cn`。

## 拟议行为

- 增加明确的 `LLM_PROVIDER=apimart|gemini` 配置和工作台提供方选择。
- APIMart 保留 `LLM_API_KEY` 优先、`APIMART_API_KEY` 回退，以及现有自定义模型 / 地址能力。
- Gemini 使用单独的 `GEMINI_API_KEY`，避免遗留 APIMart 凭据被发送给 Google。模型支持自定义；切换提供方时匹配对应地址和默认模型。
- 两处文字调用入口共享配置结果，普通任务和单段重生成均按所选提供方检查密钥。
- 新配置可保存到本机 `.env`，浏览器仅接收密钥是否存在；子进程使用当前配置快照。
- 所有新增或调整的 UI 文案进入现有中英文 JSON。
- 两个平台分别保存构图规划 / 提示词改写模型：APIMart 使用 `LLM_COMPOSITION_MODEL` / `LLM_REWRITE_MODEL`，Gemini 使用 `GEMINI_COMPOSITION_MODEL` / `GEMINI_REWRITE_MODEL`。未单独配置时分别回退 `LLM_MODEL` / `GEMINI_MODEL`，再使用平台默认值。

## 实施接口与文件分工

### 任务 1：核心配置与任务预检

文件：`h3ref/config.py`、`h3ref/frames.py`、`h3ref/film.py`、`workbench/server.py`、`workbench/inputs.py`、`workbench/segments.py`。

- [x] 先用临时离线测试重现 Gemini 被拒绝、独立模型未生效的缺口。
- [x] 增加 `config.llm_settings(environ=None) -> dict`，返回 `provider/base_url/composition_model/rewrite_model/key_names`，不返回密钥值。
- [x] 现有 `config.LLM_BASE` / `config.LLM_MODEL` 分别成为所选平台地址 / 改写模型；新增 `config.COMPOSITION_MODEL` 用于构图请求和规划错误报告。
- [x] 工作台配置 API 增加提供方、Gemini key 和两平台独立模型字段；bootstrap 返回 `llm_providers` 的已解析设置供表单使用。
- [x] 两处文字预检使用同一解析器的 `key_names`；新构图需要文字密钥，缓存提示词或已提交图片任务无需重新规划。
- [x] 离线验证通过，保留已有任务缓存与已提交任务语义。

### 任务 2：设置表单与说明

文件：`workbench/templates/base.html`、`workbench/static/common.js`、中英文 UI JSON、`.env.example`、`README.md`、`docs/workbench.md`。

- [x] 表单增加提供方、Gemini key、构图规划模型 / 提示词改写模型；模型 ID 可自由填写。
- [x] Gemini 显示固定官方地址；APIMart 保留自定义地址。模型字段随提供方使用不同配置名，切换不覆盖另一平台设置。
- [x] 验证选择、保存、刷新、重建应用后的模型和密钥状态一致，任何响应均无密钥明文。
- [x] 更新双语文案、环境变量和操作说明。

### 任务 3：最终验证与交付

- [x] `.venv/bin/python -m unittest discover -s tests -q`，预期全部通过（平台 skip 保留）。
- [x] `node --test tests/test_results.js tests/test_frame_character_picker.js tests/test_video_segment_prompts.js`，预期 40 项通过。
- [x] Python 编译、自有 JS 语法、中英文键 / 占位符、模板渲染和 diff whitespace 检查通过。
- [x] 独立审查密钥隔离、旧配置兼容、两阶段模型与缓存预检；按用户要求移除临时功能测试，不自动 commit。

重点检查：遗留 `LLM_API_KEY` 不覆盖 Gemini key；遗留 APIMart URL 不覆盖官方 URL；模型设置跨提供方互不污染；切换后两处真实请求使用不同模型；缓存任务不因新增预检而被迫重新调用模型。

## 实施与验证

1. 确认接入方案和范围（关键检查点）。
2. 调整核心配置、工作台配置 API 与两处任务预检，验证 APIMart 旧配置、Gemini 独立密钥及错误配置拒绝。
3. 调整设置表单、中英文文案、`.env.example` 和使用说明，验证切换、保存、刷新与子进程环境一致。
4. 在现有 `.venv` 中运行离线 Python 测试和编译检查，执行项目 Node 测试、JS 语法及语言包校验；移除本轮临时验证代码。

不启动或停止开发服务器；不调用付费模型；不自动提交或推送。项目没有前端构建命令，以 Python 编译、离线测试和 JS 检查作为构建等效验证。

## 验收标准

- 未配置提供方时，现有 APIMart 调用行为保持兼容。
- Gemini 配置同时作用于构图规划与 H3 改写，不要求 APIMart 文字密钥；需要出图时仍要求 APIMart 图片密钥。
- 缺少 Gemini key 不得回退到 APIMart；未知提供方在边界校验时报错。
- 已缓存改写和已提交任务继续跳过不必要的文字密钥检查。
- 密钥不回传浏览器、不进入任务 JSON、不写入日志。


## Review

- APIMart / Gemini 官方共用现有 Chat Completions 调用；两平台密钥隔离，构图规划和提示词改写分别设置模型。
- 工作台设置 API、两处任务预检、子进程配置、双语表单及使用说明已同步。旧 `LLM_MODEL` / 自定义 URL 和已有任务缓存保持兼容。
- 12 项临时 Python 功能验收与 2 项临时 Node 表单验收先失败后通过；按用户约定已删除临时测试文件和对应缓存，日志保存于 `.workbench/verification/dual-llm-provider/`。
- 清理后原 Python 219 项通过（4 项平台跳过），Node 40 项通过；Python 编译、自有 JS 语法、四组语言包键 / 占位符、两页面模板渲染及 diff whitespace 检查通过。
- 独立代码审查未发现阻塞问题。未调用真实模型接口，远端 key / 模型可用性尚未实测；未启动或停止服务器、未修改本机 `.env`、未提交或推送。
- 用户需自行重启工作台并刷新页面验收本轮 UI。配置保存后只影响新启动任务，已有构图 / H3 提示词仍按现有缓存逻辑复用。
