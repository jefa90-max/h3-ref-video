# H3 本地工作台 Implementation Plan

此文是首轮实现计划快照。后续 key 持久化、审图与 CLI 对齐方案见 [本轮增强计划](workbench-storyboard-review.md)，当前行为以 docs/workbench.md 为准。

**Goal:** 在浏览器里跑通现有 one / film，提供无 key 演示。
**Architecture:** Flask API + 单页静态界面 + 子进程运行现有 CLI。
**Tech Stack:** Python 3.10+、Flask 3.1、Pillow、原生 JavaScript。
**Spec:** docs/design/local-workbench.md

本轮已授权自主决定可逆技术选择并实施；任务不创建提交，不启动 pnpm dev。

## 全局约束
- 不改变 h3ref.py 或包内现有流水线；Python 均在 .venv 内运行。
- UI 新增文本来自 zh-CN/en JSON；key 不落盘、不回显；仅本机。
- 临时验证脚本执行完删除，现有测试保留；发现既有 bug 另行记录。

## 检查重点
- 输入 JSON 非对象、镜头时长非法、参考图缺失必须在付费前失败。
- 新任务不可覆盖旧 state；演示不可误触远端。
- 配置仅影响随后启动的子进程，不污染正在运行任务。
- 未授权路径不可通过文件服务下载，key 不出现在响应或落盘日志。
- 浏览器刷新或服务重启后能找回任务，不能假报成功。

## 1. 服务与运行器
- [x] 新增 workbench.py 入口、workbench/server.py HTTP/配置/上传，workbench/jobs.py 任务/子进程，workbench/inputs.py 输入校验，workbench/demo.py 离线产物，workbench/instance.py 单实例锁。
- [x] GET /api/bootstrap 返回配置状态、任务、原始示例；POST /api/config 更新内存配置；POST /api/uploads 返回绝对图路径。
- [x] POST /api/jobs 输入 kind/demo/output_dir/prompt/images/seconds/aspect/film/frames；GET /api/jobs/<id> 轮询日志与产物；POST /api/jobs/<id>/resume 续跑。
- [x] 临时 unittest / HTTP 检查覆盖缺 key、非法 JSON、占位图、命令、续跑、越界下载。

## 2. 界面
- [x] 新增 workbench/static/index.html、app.js、style.css 与 messages/zh-CN.json、en.json。
- [x] 实现配置、一条片段、整条片、运行监控；异步轮询，错误可见，任务执行时禁止重复提交。
- [x] 浏览器点击演示与错误路径，窄屏无横向溢出。

## 3. 文档与交付验证
- [x] requirements-web.txt 为可选 UI 依赖；README 新增启动与使用；docs/workbench.md 记录路径和边界。
- [x] .venv/bin/python -m compileall -q h3ref.py h3ref workbench.py workbench。
- [x] .venv/bin/python -m unittest discover -s tests -v；node --check workbench/static/app.js。
- [x] 临时验证使用内联脚本与自动清理目录，保留 docs/workbench-verification.md 验证结论；未做付费验证。
