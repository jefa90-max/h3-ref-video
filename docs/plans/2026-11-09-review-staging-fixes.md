# 单帧实施审查问题修复

用户已确认修复审查报告的全部四项问题，并补永久回归。起点 `57684cf`，当前分支 `feat/video-segment-review`；不重做已经完成的单帧实施。

1. **F01，关键检查点：** 先复现完成段 redo 时缺文字 Key 仍通过预检。抽出 worker 已有的段失效记录计算，供预检生成只读状态使用；覆盖 shot 仅目标段、first/none 后续扩散、reset_applied 恢复与直接 H3。失败预检必须保持原 state 和付费视频不变。
2. **F02：** 先复现合法 composition 附加 camera 字段时的 KeyError 与整镜不开画；渲染仅使用既定四字段，不增加翻译字段或扩展 schema。
3. **F03，关键检查点：** 先复现缩图／参考图上传失败后被锁成接单未知；标记移至实际付费 create 前。覆盖批次、单图编辑、create 响应丢失、明确拒绝和已有 task 恢复；不清理历史 unknown，不猜测旧任务是否已接单。独立复核追加了非 JSON HTTP 401 鉴权拒绝仍误锁的原有遗漏：保留 HTTP 错误状态供图片提交边界识别，仅将明确的 401/403 鉴权拒绝归为未接单；其它不确定结果仍锁定。该项也先 RED→GREEN，在同一修复 pass 处理，不再派第二审查者。
4. **F04：** 只给 storyboard 卡片提示提高前景色；静态对比度至少 4.5:1，页面文案不变。本项以静态计算和用户浏览器反馈验证，不增加镜像 CSS 的测试。
5. **最终检查点：** 定向回归 RED→GREEN、完整 Python/Node、编译、JS 语法、locale key/占位符校验和独立 Codex 复核；功能修复与永久回归分开本地 commit，保留原有用户改动。

## 验证与范围

- Python 一律使用仓库 `.venv/bin/python`；测试使用临时目录与 provider/network mock，不调用付费服务，不启停用户服务器。
- 先写出失败回归，再修源码；保留永久回归，只删除本轮临时验证脚本。
- Context7 再次返回月度额度耗尽；测试边界核对使用 [Python unittest.mock 官方文档](https://docs.python.org/3.14/library/unittest.mock.html)。沿用项目标准库测试，不安装依赖。
- 不推送、合并或部署；CSS 实施后交付用户反馈，真实模型画质、Windows 真机与浏览器 UAT 不冒充已验证。

## Review

四项均已修复。产品改动限于共享 redo 失效投影、既定相机字段渲染、图片 create 边界及明确鉴权拒绝分类、storyboard 提示颜色；没有新增界面文字、依赖、服务或生成请求。

| 问题 | RED 证据 | 修复后验证 |
| --- | --- | --- |
| F01 续生成预检 | 缺文字 Key 的目标段与 first/none 后继段共3处未拒绝 | `RedoPreflight` 4项通过；检查 state/旧片不变，reset_applied 恢复及直接 H3 仍可用 |
| F02 相机额外字段 | 中英渲染均 KeyError，stage_frames 返回 s1-1 失败且 draw=0 | 两条新回归通过；原隔离探针重放 draw=1、failures=[] |
| F03 生成前失败误锁 | 缩图、上传、缺 Key、单图上传4条新回归失败 | 生成前错误可编辑／恢复，旧像素不变；原上传探针 create=0、editable=true；create 丢响应仍锁定 |
| F03 复核追加鉴权边界 | 真实 urllib `HTTPError(401/403)` 纯文本响应被误归 unknown | 保留非 JSON HTTP 错误的状态；图片明确鉴权拒绝允许手工重试，原响应丢失保护通过 |
| F04 提示对比度 | 卡片2.959:1，其它提示背景低至2.641:1 | 相同前景色在三种相关背景分别5.989 / 5.614 / 5.344:1 |

新增11项永久回归，最终 `.venv/bin/python -m unittest discover -s tests -v`：**216项，212通过、4项既有 Windows 条件跳过**；Node CI 三文件 **40项通过**。compileall、自有与测试 JS 语法、四组 locale 递归 key/占位符、diff whitespace 检查通过；没有新增 skip 或放宽断言。

独立 Codex 对本轮修复做了一次只读复核：F01/F02/F04到位，F03缩图/上传原问题已修；另指出上述鉴权遗漏，按实际影响列 Important，在唯一后续修复 pass 中 RED→GREEN 并跑完整 suite。没有第二次独立复审，不把回归验证称为跨模型或复审通过。源码与网络边界的测试使用合成夹具/mock，未声称真实生成画质或浏览器/Windows真机已通过。

功能修复使用 `fix(...)`，永久回归使用 `test(fix):`，分别本地提交；低影响颜色项按批准方案使用静态计算，无镜像 CSS 测试。旧 unknown 状态无法据现有记录可靠判断是否真的已接单，因此未自动清除。用户原有 `tasks/lessons.md` 与三份研究文档改动未纳入本轮提交。

Python 后端在工作台进程中已导入的模块需要用户自行重启工作台才能生效；本轮没有启停服务。临时 fixture 自动清理，没有遗留一次性测试脚本；永久回归保留。完整日志和修复前后探针结果保存在忽略目录 `.workbench/reviews/20261109-single-frame-fixes/`。
