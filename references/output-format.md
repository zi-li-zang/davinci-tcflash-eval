# 统一结果格式（schema_version: 2）

新批次使用以下结构。历史目录保持原样，不创建兼容的重复报告。

```text
batch_YYYYMMDD_HHMMSS/
  report.md                       人读总入口，运行期间更新
  report.json                     机器状态与结论来源
  manifest.json                   本批输入声明
  control.json                    仅在有控制请求时创建
  logs/                           批次、项目盘点与恢复证据
    001_project/
      inventory.json
      events.jsonl
      worker.log
      run_status.json
      operations/001_preflight.input.json ...result.json ...log
  items/
    001_project__t01/
      report.md                   当前项详细报告，固定八节
      result.json
      observations.json           Agent 补充观察，有内容时创建
      render/attempt_01/           实际渲染产物
      comparison/attempt_01/       实际比较产物（有比较时）
      logs/attempt_01/
        events.jsonl
        worker.log
        run_status.json
        plugin_presence.json
        output_inventory.json
        operations/
        diagnostics/              Agent 有诊断证据时创建
```

项目编号与时间线编号稳定；真实名称写入 JSON，文件夹仅做安全字符处理。各次尝试分别保存；文件内保留实际时间，用户目录不使用纳秒随机名。没有取到的证据不创建空文件冒充完成。

## 声明与证据

按以下类别记录，禁止把配置存在、API 返回成功或模板说明当作最终产物已验证：

| 类别 | 存放位置与用途 |
|---|---|
| 输入声明 | 本批 `manifest.json`、每项 `declared`；记录用户/基准说明及请求参数 |
| 实测证据 | 每项 `evidence`、operations 结果、日志和产物；写明观察时点及实际核验范围 |
| 补充观察或假设 | `observations.json`；说明来源、证据路径、是否待验证，不覆盖执行器原始结论 |
| 未知或未测量 | JSON 用 null，文字写“未知/未测量”，不从文件名推测 GPU、JA 或根因 |

`observed_before_render.format_codec` 和 `render_mode` 是设置前的观察值；`export_rules` 是本次解析后的导出请求。它们不能混称为最终文件规格。格式/编码回读校验的含义及其他导出参数的验证边界见批次说明，产物规格须另行检查。

## 机器数据

批次字段：schema_version、created_at、updated_at、batch_state（RUNNING / PAUSED / COMPLETE）、reason、projects、items、counts。PAUSED 是可恢复调度状态；COMPLETE 仅表示本轮调度结束，不表示全部通过。

每项字段：item_id、project_id、drp_path、drp_sha256、timeline、timeline_index、status、render_status、compare_status、failure_code、reason、run_dir、paths、declared、evidence、integrity、attempts、selected_attempt 和耗时。

- status：PASS / FAIL / SKIPPED / ERROR / NOT_RUN / RENDERED_NOT_COMPARED。PASS 仅用于完成所需比较及证据检查；RENDERED_NOT_COMPARED 表示完成导出但未取得有效比较结论，压力测试也不生成像素 PASS。
- 父项目：子项状态一致时沿用，混合时 PARTIAL。未导入项目只占一行，时间线数量标未知。
- attempts 保存首次渲染错误、各次结果、日志/产物目录与选用尝试；重试成功不能擦除首次失败。
- evidence.render 保留原始 failure_code；host_state 和 recovery_reason 分别记录后续宿主状态，不能覆盖原错误。
- render_elapsed_sec：独立渲染引擎耗时，当前没有可靠测量，null。
- render_poll_wall_sec：StartRendering 后轮询墙钟，有实际返回才记录。
- executor_total_sec：该次渲染/盘点/比较的总墙钟，不包含整个项目导入。各次均在 attempts 保留。
- integrity 文件数不等于视频解码帧数；decoded_frame_count 未检测时 null。比较覆盖见 comparison/coverage.json。

## 总报告五节

1. 批次概要：输入、目录、策略、时间和可用环境证据。
2. 进度与计数：项目、已发现项、各状态数量；不把未知时间线算成已覆盖。
3. 每项结果：渲染、比较、结论、尝试、耗时、原因及详细报告链接，父项状态。
4. 失败与恢复：原始错误、当前恢复状态及证据位置。
5. 结论与待处理：实测范围、未比较/未执行、下一步。

## 当前项详细报告八节

1. 结果概要：本项状态、覆盖限制、失败原因。
2. 输入与配置：DRP 哈希、SDK、时间线、插件位置；实测、声明、未知分开。
3. 渲染与完整性：实际产物、零字节、解码状态、数量、尺寸/通道、各类耗时。
4. 可比性与映射：映射范围、缺失/额外/边界帧、像素域和 alpha，先说明能否比较。
5. 严格指标：整体及 RGB/alpha、逐帧最差值、完整数据路径；SSIM/PSNR 仅辅助。
6. 图像证据：可用并排、差异倍率、ROI、时序图；说明是否实际人工/Agent 目视。
7. 异常与恢复：首次错误、尝试、操作、验证、日志；不得把恢复成功写成首次成功。
8. 事实、待验证和证据索引：不凭经验确认根因，不凭输出完整性证明无泄漏。

脚本根据 result/evidence 和 `observations.json` 生成报告。Agent 将补充诊断与视觉观察写入当前项 `observations.json`（JSON 对象），例如：

```json
{
  "visual_review": "未目视，等待检查差异图",
  "host_diagnosis": {
    "conclusion": "待验证的判断或已确认事实，并注明类别",
    "evidence_paths": ["<本次尝试 diagnostics 中的实际证据绝对路径>"]
  }
}
```

截图、日志摘录等附件放在对应 attempt 的 `logs/diagnostics/`，不再创建第二份结论文件。续跑生成报告会读取 `observations.json`；仅手工修改 report.md 的内容不会保留，因此先记录观察再使用执行器的报告生成函数更新报告。保持五节/八节结构，不另建 final、summary、detailed 等重复入口。
