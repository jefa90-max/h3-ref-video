# 安全说明

- key 从环境变量或仓库根目录的 `.env` 读取（`.env` 已在 `.gitignore` 里）。工作台可选择将填写的 key 保存到本机 `.env`，或只保存在当前服务内存；已保存的 key 不回传浏览器、不写入浏览器存储或任务 JSON。日志和报告中的已知 key 会被脱敏；任务状态（`state.json`）与 Colab 节点记录（`.workbench/colab-nodes.json`）不保存明文 key，仅记录任务号、节点元数据以及用于密钥隔离的非敏感 SHA-256 指纹前缀（`comfyui_key_fp` / `key_fp`）。
- 上传的视频和图片会传到所选的视频后端（**ComfyUI / Colab 节点**或 **RunningHub**）以及 **APIMart**（画图/改写；若配置 `LLM_PROVIDER=gemini` 则改写发往 Gemini），按各自服务的保存规则存放（APIMart 上传的图保留 72 小时）。别传你没有权利处理的素材。
- ComfyUI 远端鉴权默认 fail-closed：非本机回环的明文 `http://` 地址会拒绝携带 `COMFYUI_API_KEY`，公网节点须使用 `https://`；Colab 一键启动（`scripts/start_colab.py`）的反向代理对公网隧道强制校验 `COMFYUI_API_KEY`，且隧道令牌与鉴权密钥只从用户个人 Google Drive 私有隐藏文件（`.ngrok_token`、`.comfyui_api_key`、`.ngrok_domain`）读取，不经命令行参数或本地日志传递。
- 发现安全问题（比如 key 出现在日志、报告或任务目录里），请在 GitHub 上用 [Security Advisory](https://github.com/garycaogan/gary-h3-ref-video/security/advisories/new) 私下报告，不要开公开 issue。
