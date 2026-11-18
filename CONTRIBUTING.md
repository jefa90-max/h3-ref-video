# 参与贡献

欢迎提 issue 和 PR。开发测试使用 Python 虚拟环境和 Node.js 22；安装完整测试依赖后，改代码前先跑离线测试（不联网、不要 key、不花钱）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-web.txt
python -m unittest discover -s tests -v
node --test tests/test_results.js tests/test_frame_character_picker.js tests/test_video_segment_prompts.js tests/test_workbench_ui.js
```

Windows 使用 `python -m venv .venv` 创建环境，再用 `.venv\Scripts\python.exe` 执行安装和 Python 测试命令，无需运行 `source`。测试覆盖切段、每段参考图的顺序和 9 张上限、多角色合图去重、中文提示词、精简构图 v3 与有界重试、改写格式检查（六段非空顺序、否定句、图片编号与包装剥离）、工作流节点图的输入，以及工作台审核与恢复、分镜编辑、手动英文提示词改写、双文字供应商配置、多语言键一致性和分页交互。CI 在 Ubuntu 的 Python 3.10、3.12、3.13 和 Windows 的 Python 3.12 上运行 Python 与四组 JavaScript 测试。

动了生成、审片或拼片这些要花钱的环节，PR 里写一下真跑的结果：用 `one` 跑一条 5 秒片段（约 ¥0.05），动了分镜图或整片流程就用 `examples/film.example.json` 配一张自己的角色图跑一遍 `film`（约 ¥1.5）。

不要把 key、任务目录、生成的视频和别人的素材提交进来（`docs/media/` 里的演示素材除外，它们不进打包）；节点图只放 `workflows/` 里注明了作者和出处的那种。
