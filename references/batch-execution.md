# 批次执行与恢复

本文件维护执行、导出和恢复规则。比较判定见 [SDK 比较规则](sdk-equivalence.md)，状态、证据与交付格式见 [报告规范](output-format.md)。

## 输入与工程保护

- 使用已安装、能连接 Resolve 的同一 Python，不安装依赖或搭建服务。比较需要 numpy/OpenCV；缺依赖时先使用明确的独立导出方案渲染，保留结果待比较。
- 原始 DRP、SDK、素材和历史结果保持只读。新批次创建全新 `batch_YYYYMMDD_HHMMSS`，输出与输入目录隔离。测试副本使用 `EVAL_RUN_...` 名称避免项目库冲突，不重命名 NAS 的 DRP。
- 工程、节点和插件语义由 DRP 固定；不擅改算法、JA、timestep、时间线帧率、项目色彩或裁剪。导出表示按下节准备，正式比较不用于反向调整参数让结果通过。
- Color 节点确认没有 TrueCut Flash 时跳过；插件探测不完整须诊断，不能当作不存在。不要求渲染前具有 case 认证或完整帧映射。

### 本地保存偏好

[local-execution-policy.json](local-execution-policy.json) 的 `project_policy` 是机器读取的默认值，本批 manifest 同名字段优先。`save_then_switch` 表示先调用 SaveProject 并检查成功、工程身份和空闲状态，再导入测试副本。空的未命名工程有单独处理；保存失败或身份变化时暂停，不丢弃修改。

`authorization_source` 和 `scope` 仅为来源与范围说明，程序不解析它们作为权限规则。当前实现只有 `save_then_switch` 会启用自动保存；其他值（包括 `require_confirmation`）不自动保存，也不会自动弹出确认界面，有活动工程时会阻断切换。用户明确禁止保存时，Agent 应覆盖本批策略并处理该阻断。

## 渲染入口

```sh
"$PYTHON" scripts/render_first.py --manifest /absolute/manifest.json --output /absolute/batch_YYYYMMDD_HHMMSS
```

执行器将 manifest 复制到批次内；也可用 `run_full_eval_pipeline.py --drp-dir ... --sdk-dir ... --output-base ...` 盘点同名项目。后者的同名目录只是项目级基准线索，不推导帧映射；默认按 SDK 图像格式准备导出。非同名关系在 manifest 明确填写。

最小 manifest：

```json
{
  "timeline_policy": "all",
  "max_attempts": 2,
  "export": {"settings_source": "sdk_aligned"},
  "projects": [
    {"id": "sample", "drp_path": "/absolute/sample.drp", "sdk_dir": "/absolute/sdk/sample", "render_timeout_sec": 3600}
  ]
}
```

`timeline_policy` 支持 `all`、`single`、明确且唯一的名称列表。默认逐条执行全部时间线；无时间线、选择不存在或名称不唯一时说明原因。特殊时间线可明确指定名称独立处理。

### SDK 对齐导出

DRP 用于固定工程、时间线和插件设置，不作为 Deliver 设置已保存的依据。SDK 比较默认 `settings_source: sdk_aligned`：先盘点对应 SDK 图像目录，再配置同类图像序列。JPEG → JPEG，EXR → EXR，TIFF → TIFF；`.jpg/.jpeg` 和 `.tif/.tiff` 分别视为同一格式。导出设置写在本批 manifest，不修改原始 DRP。

Agent 在启动前查询 `GetRenderFormats()` / `GetRenderCodecs()`，结合基准实际规格及已有说明，选择并记录 `format`、`codec`。执行器只在候选唯一时自动选择；存在多种 EXR 位深/通道编码时，Agent 应核验后填入真实 API codec，不能因为需要准备设置就停止整个任务，也不能猜一个编码让流程通过。混合格式基准先明确对应子目录；不存在基准时可用显式独立导出方案先渲染，比较仍待补。

例如（占位值须替换为本机 API 返回值）：

```json
{"settings_source": "sdk_aligned", "format": "exr", "codec": "<已核验的 API codec>",
 "render_mode": "single_clip", "image_settings": {"ExportAlpha": true, "AlphaMode": 1}}
```

`image_settings` 支持 `FormatWidth`、`FormatHeight`、`ExportAlpha`、`AlphaMode`、`VideoQuality`；只填写有基准证据的值。AlphaMode 0/1 分别为 premultiplied/straight。位深/通道编码由真实 codec 或明确预设控制，不假设任意 API 字段都可用。尺寸应核对基准及工程意图；出现不一致时先诊断原因，不自动拉伸或裁剪来使比较通过。无法通过 API 表达的设置，由 Agent 在独占控制时配置并保存已验证预设，记录证据。

JPEG 需要记录可确认的质量/编码信息；相同扩展名与质量数字不能证明不同编码器的解码像素一致。EXR 需要区分 half/float、RGB/RGBA 和 alpha 语义。先完成可确定的导出配置，编码差异按 SDK 比较规则判定。

格式/编码在提交队列前回读校验；其他参数的 setter 成功不等于逐字段验证，最终还须检查实际产物。请求和观察值的存储位置、含义见报告规范。

显式 `render_preset` / `format` + `codec` 仍可用；`imported_current` 仅适合用户明确接受当前设置的独立渲染/压力测试，不作为 SDK 比较默认值。默认单时间线整段导出；显式 individual_clips 需基准对应关系，不自动拆猜 clip。

## 控制权与持续监测

执行器在 `/tmp/davinci_tcflash_eval_executor.lock` 持独占锁；直接调用 Python 函数的集成方也必须持此锁。并发只允许读取文件，不向 Resolve 发第二条控制链。

```sh
"$PYTHON" scripts/batch_control.py status --batch /absolute/batch
"$PYTHON" scripts/batch_control.py pause --batch /absolute/batch
"$PYTHON" scripts/batch_control.py stop-current --batch /absolute/batch
"$PYTHON" scripts/batch_control.py recovery-check --batch /absolute/batch
```

- `status` 只读取报告与当前项快照，含进度、轮询耗时、已记录错误；快照过期不是宿主死亡证据。
- `pause` 在安全边界停止下一项；正在运行的渲染继续到返回。
- `stop-current` 请求当前 worker 调用 StopRendering，并暂停后续调度；请求写入不等于停止完成，须核验。
- `recovery-check` 需要获得同一锁，查询宿主/项目库和渲染空闲状态；执行器未退出时拒绝抢占。
- API 阻塞时控制请求可能暂时无法处理。外层超时结束 Python worker 不等于结束 Resolve 渲染，必须继续诊断，不能立刻启动其他宿主操作。

Agent 在用户要求持续执行时负责持续观察至终态。根据耗时合理轮询，不重复启动同一批次。每轮状态更新同时读取报告、快照和相关错误证据；日志心跳只证明等待进程活着。

## 诊断与恢复策略

| 情形 | 处理 |
|---|---|
| 输入缺失、明确没有插件 | 记 SKIPPED 与具体原因，继续 |
| 插件探测 UNKNOWN | 暂停；用 MCP/API/可见 Color 节点补充证据，不能当作 ABSENT |
| 比较配置、通道或 alpha 不明 | 保留渲染，比较 NOT_READY/BLOCKED，继续渲染其他项 |
| 像素超差/NaN/Inf | FAIL，保留证据，继续 |
| 已知渲染失败、已确认空闲 | 保留失败，继续下一项；必要重试由 Agent 明确发起 |
| 超时、宿主未知、导入/关闭后状态不明 | PAUSED；先诊断再恢复，不能继续切项目 |
| 产物为空/缺失但宿主已确认空闲 | ERROR，保留实际证据，可继续其他项 |

允许的诊断：读取本次日志、Resolve 日志、输出数量及文件变化，读取工程/队列状态，查看当前 UI。已知、明确属于本次任务的错误提示可关闭，但先保存截图/文本，操作后核验原工程与空闲状态。未知弹窗不能盲点。

NAS 素材已正确绑定时不操作。仅在确实离线且有明确新旧路径绑定时重链接同一素材，记录身份与前后路径并核验，不使用 ReplaceClip 猜测替换。

Agent 接管 MCP/UI 操作前确认执行器已退出并取得同一锁，在诊断后核验旧渲染已结束及宿主空闲，再交还控制权。`run()` 不提供 UI 或重链接能力，这部分由 Agent 执行；证据保存方式见报告规范。

不自动杀死或重启 Resolve，不盲重试。默认每项最多 2 次渲染尝试（执行器的 `max_attempts`）；每次恢复诊断默认最多 2 轮、累计 120 秒，由 Agent 管理，Python 不计量 Agent 的 MCP/UI 操作。已有日志可解释的长任务观察不计入故障恢复预算。超预算时交付阻断证据和剩余项，用户指定的预算优先。

## 续跑与保留首次失败

```sh
"$PYTHON" scripts/render_first.py --manifest /absolute/batch/manifest.json --output /absolute/batch --resume
"$PYTHON" scripts/render_first.py --manifest /absolute/batch/manifest.json --output /absolute/batch --resume --retry-item 012_example__t01
```

`--resume` 先核验空闲，使用本批原 manifest，继续 NOT_RUN 项；不自动重做 PASS/FAIL/ERROR。明确重试用 `--retry-item`，写新 attempt，首次失败保留。中断的 RUNNING 项转为 ERROR，须显式重试。原 DRP 哈希变化不继续该项。恢复不是新测试，不覆盖历史；新测试另建批次。

若未发现时间线前导入失败，恢复对象是报告中的项目占位 item_id。尚未导入的项目时间线数量未知，不能虚报完整时间线覆盖。

渲染完成后的比较接入与条件核实见 [SDK 比较规则](sdk-equivalence.md)，不要在此另行定义映射或判定标准。
