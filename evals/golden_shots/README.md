# 提示词增强的预注册镜头

[manifest.json](manifest.json) 记录了 30 个合成夹具，共 34 镜。剧本和盲评目标可以直接用于离线试算。素材位置、真实生成结果和盲评分数待填写，当前标记为 `unreproducible`。

| 实验臂 | 夹具 | 核对事项 |
|---|---:|---|
| 3A、3B、3C-neg、3C-light | 共用一组 16 个独立镜头 | 10 个边界场景、6 个常规对照。每次仅比较一种图片规则，分别核对画面、审核和成本。 |
| 3D | 6 个镜头 | 4 个高关联视觉否定和 2 个正常剧情否定对照。每对使用同一张冻结首帧。 |
| 4 | 5 个镜头 | 4 个大幅表情和 1 个克制表演对照。每对使用同一张冻结首帧。 |
| 2A | 3 组连续场景、7 镜 | 镜头开场已摘下、镜内摘脱、跨镜结束姿态，以及反打时的空间和背景连续性。 |

`asset:<key>` 指向同一夹具的 `assets[key]`。开跑前为所选夹具补齐外部媒体的相对路径和内容 SHA-256。两边使用同一组原始参考图，纯视频臂额外共用冻结首帧。一镜对应一张冻结首帧。每次实片运行保存实际模型、采样配置、工作流和改写后提示词的摘要，连同盲评结果与评分媒体摘要一起归档。模型未锁定或素材无法核验的结果继续标记为 `unreproducible`。

评估补丁能否退场时，用同一新模型和配置分别生成全补丁版与仅移除目标补丁的版本。逐个缺陷类别比较盲评和叙事忠实度。还需核对审核拦截、包含重试的交付总成本、组合提示词长度以及规则之间有无冲突。全部通过后才退场。

## 离线走查

在仓库根目录使用已安装依赖的 Python 虚拟环境运行：

```bash
.venv/bin/python scripts/ab_eval.py --dry-run --out jobs/ab-wave2-preview
.venv/bin/python scripts/ab_eval.py --arm 3A --pilot --dry-run --out jobs/ab-wave2-3a-preview
```

产物包括 `dry_run.json` 和 `ab_review.html`。离线预览只验证输入结构、调用数量和提示词长度，费用为零，画面留空。它绕过 `.env`，可用当前 shell 中预设的模型配置走查。付费前要核对 `provenance.settings` 与实际账号设置一致。

## 付费运行和恢复

开始前确认试跑额度与模型设置。可用 `--arm` 重复选择实验臂，`--fixture` 重复选择夹具 ID，或用 `--pilot` 按预设种子缩小到每臂最多三组。需要参考图的夹具，可用 `--assets assets.json --asset-root /path/to/verified-assets` 提供本地素材，结构为 `{"fixture-id": {"asset-key": {"path": "frame.png", "sha256": "…"}}}`。视频组用 `--guide` 指向本地冻结的官方 H3 提示词指南。

`--quotes` 指向一份按当前供应商已确认的人民币报价 JSON。按计划中的调用填入所需的 `image`、`composition.llm`、`composition.claude`、`rewrite.llm`、`rewrite.claude` 单次费用上界。视频用 `video_per_second`，包含外部 GPU 租用成本时也按秒计入。脚本用 `--max-cny` 拦住超过已批准额度的请求。图片实际用量与远程任务账单可能和估算不同。收不到实付金额时，继续保留该次预占，待核对账单。

执行时使用独立的输出目录。需要续跑时，带上与第一次相同的参数，并加 `--resume`。保存了任务 ID 的步骤从原任务查询继续。接单状态为 `submission_unknown` 的步骤应先核实供应商订单。实验每步只发一次生成请求。

出片后把 `ab_review.html` 与同目录的 `blind/` 交给评审。完成视觉投票后再导入运行方保存的 `answer_key.json` 查看费用和审核结果。评价结果可导出为 JSON，缺素材的对照只记录状态。提示词指纹、素材摘要、报价和任务进度归档在 `run_manifest.json` 与 `ab_state.json`。
