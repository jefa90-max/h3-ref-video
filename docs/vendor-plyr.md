# Plyr 播放器依赖

- 版本：3.8.5（固定随仓库提供）。
- 官方项目：https://github.com/sampotts/plyr
- npm 包：https://registry.npmjs.org/plyr/-/plyr-3.8.5.tgz
- 包 SHA-1：`47d6a20f125c8b40940441f813411771d9367c27`（已与 npm 元数据核对）。
- 使用文件：`workbench/static/vendor/plyr/` 下的 JS、CSS、SVG。
- 许可：MIT，完整许可保存在同目录 `LICENSE.md`。

工作台从本地静态目录加载播放器及图标，无运行时 CDN 请求，不增加 Node 构建步骤。播放器控制文案由工作台的中英 JSON 字典提供；关闭播放器自己的本地存储。

播放器销毁时使用的空白媒体也已本地化：`blank.mp4` 为本地 ffmpeg 生成的 2×2 黑色空白视频（官方默认资源下载返回 403，因此未复制），避免播放或切换任务时请求 CDN。
