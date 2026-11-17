# 安全说明

- key 从环境变量或仓库根目录的 `.env` 读取（`.env` 已在 `.gitignore` 里）。工作台可选择将填写的 key 保存到本机 `.env`，或只保存在当前服务内存；已保存的 key 不回传浏览器、不写入浏览器存储或任务 JSON。日志和报告中的已知 key 会被脱敏；任务状态只记录任务号，不记录 key。
- 上传的视频和图片会传到 RunningHub 和 APIMart，按它们自己的保存规则存放（APIMart 上传的图保留 72 小时）。别传你没有权利处理的素材。
- 发现安全问题（比如 key 出现在日志、报告或任务目录里），请在 GitHub 上用 [Security Advisory](https://github.com/garycaogan/gary-h3-ref-video/security/advisories/new) 私下报告，不要开公开 issue。
