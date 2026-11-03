# 视频片段审阅与重生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在结果页用分镜图 / 视频片段 Tab，支持只重生成目标片段，以及不调用模型的独立拼接。

**Architecture:** 继续复用 film.gen_segment 与 media.join。工作台用隔离的片段尝试目录保存新任务和产物，成功后发布到原 clips；assembly.json 记录成片使用的片段版本。页面用现有原生 JS / Plyr，图和视频独立分页。

**Tech Stack:** Python 3.10+、Flask 3.1、FFmpeg、原生 JavaScript、已有本地 Plyr；无新增依赖。

**Spec:** docs/design/video-segment-review.md

## Global Constraints

- 严格只生成选中片段，first / none 提示复核衔接；沿用原提示词、画幅、时长和参考图。
- 临时产物成功前保留旧片段和旧成片；任务号持久化，续跑不得重复提交。
- 所有新增 UI 和工作台错误文案来自中英 JSON；不修改不相关代码。
- Python 使用现有 .venv；不调用 Claude 或真实付费接口，不启动或重启用户服务。
- 用户已确认设计和 Tab 修订，连续执行；一次 UI 交付后等待反馈。

## Review Focus

- 提交成功后网络中断：保留任务号，恢复查询相同任务。
- 发布视频或尾帧途中中断：保留暂存文件，恢复发布而非重新生成。
- 老任务没有 assembly.json：显示现有视频，并能正确识别后续替换造成的待拼接。
- 单纯拼接时原角色图被移动或模型 key 不存在：仍可以拼接已完成视频。
- 进入视频阶段后用户手动返回分镜 Tab：轮询不抢焦点，不重建未变化播放器。

### Task 1: 片段操作与文件事务

**Files:** 新增 workbench/segments.py；修改 h3ref/film.py、workbench/demo.py；临时验证 tests/test_segments_feature.py。

**Interfaces:** segments.view(request, artifacts, editable) 返回列表（number/start/end/seconds/shots/url/version/can_regenerate/chained_next）；segments.assembly_status(request) 返回 version/ready/pending；prepare_regeneration(request, number, version) 写 pending；regenerate(request) 执行；prepare_assembly(request, version) 写 pending；assemble(request) 执行；record_assembly(request) 记录完整流水线产物。gen_segment 增加可选 reference_paths，默认不变。

- [x] 先通过公共 HTTP 验证新路由当前不存在，再为单段失败保留/恢复、只拼接失败保留写离线用例。
- [x] 实现片段视图与版本计算、独立暂存 Job 复用 gen_segment、发布状态和备份。
- [x] 实现 FFmpeg 临时拼接、发布后更新报告和 assembly 版本；演示复用单段 render_clip。
- [x] 验证三模式单段范围、重试任务复用、半发布恢复、数值段序及只拼接零模型调用。

### Task 2: 任务/API 集成

**Files:** workbench/jobs.py、workbench/server.py、workbench/inputs.py、workbench/messages/{zh-CN,en}.json。

**Interfaces:** POST /api/jobs/<id>/segments/<number>/regenerate（version）；POST /api/jobs/<id>/assemble（version）。operation 为 segment / assemble，Jobs.restart 和 resume 复用。快照增加 segments、assembly、can_assemble、pending_assembly。

- [x] 写拒绝过期版本、重复操作、未审图和越界片段的 HTTP 用例。
- [x] 集成 task 操作与子进程分发，创建入口禁止用户绕过审核直接提交内部操作。
- [x] 重生成预检只需目标引用、原 H3 和 RunningHub；拼接及其恢复不验证源图或要求模型 key。
- [x] 完整 pipeline 成功记录 assembly；片段操作不清除分镜 dirty 标记；跑原全量回归。

### Task 3: 结果页 Tab 与播放器

**Files:** workbench/templates/results.html、workbench/static/results.js、style.css、messages/{zh-CN,en}.json。

- [x] 实现语义化 Tab，初始基于阶段选择；进入视频操作时切一次，手动切换保留到下一次操作。
- [x] 视频片段每页 6 项，与分镜分页独立；每卡片 Plyr、下载、状态、重生成，视频 Tab 容纳成片与拼接按钮。
- [x] 用文件 URL 和语言管理播放器生命周期，普通状态轮询仅更新文本和按钮；离开 Tab 暂停播放，离开页销毁。
- [x] 验证键盘、125 图 / 多段分页、自动切换后手动返回、英文 JSON 键与占位符一致。

### Task 4: 集成与交付

**Files:** docs/workbench.md、docs/workbench-verification.md、tasks/todo.md。

- [x] Flask test_client + 真实 FFmpeg 在临时目录跑分镜 → 审核 → 成片 → 第 2 段重生成 → 手动拼接，比较其它文件字节与时间戳。
- [x] Python compileall、全量 unittest、JS node --check、模板与 JSON 检查；无实际服务器或付费生成。
- [x] 独立代码审查新边界，若发现 bug 则复现失败再修复，并按用户规则独立回归提交。
- [x] 清理临时功能验证脚本，保留原测试与实际 bug 的回归；更新说明、验证证据和实际未测范围。

## 执行记录

- 用户已确认方案；本轮在现有工作目录的新功能分支执行，便于用户现有服务器后续自行刷新，不切换到另一份代码目录。
- 计划由本会话实施；最终由独立代理审查。功能验证脚本按用户规则 9 清理，实际 bug 回归按规则 15 保留。

- 验证完成：65 项（包含 6 项临时功能检查）全部通过；临时测试清理后保留 59 项。真实 FFmpeg 三模式与离线 Chromium 已完成，见 docs/workbench-verification.md。
- 独立审查的备份中断、未知提交和旧任务状态问题已通过失败条件回归修复；未知提交采用阻止自动重提的保守行为，需用户核对远端任务后恢复记录。
