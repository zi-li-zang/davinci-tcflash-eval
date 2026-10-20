---
name: davinci-tcflash-eval
description: 本地批量导入 DRP、检测 TrueCut Flash Color 节点、持续渲染并比较已有外部 SDK 输出。用于 DaVinci Resolve 插件回归、像素一致性测试及压力测试；由 Agent 负责监测、诊断、恢复和统一报告。
---

# TrueCut Flash 批次评估

首要目标是比较已有外部 SDK 输出与插件经 Resolve 导出后的解码像素；压力测试可以只渲染。Agent 负责准备、执行、持续监测、诊断恢复与交付，启动脚本后仍须跟进至终态或有证据的阻断。

## 按任务读取

| 文档 | 唯一维护的内容 | 何时读取 |
|---|---|---|
| [批次执行与恢复](references/batch-execution.md) | 输入准备、导出策略、宿主控制、失败处理和续跑 | 执行或恢复批次 |
| [SDK 比较规则](references/sdk-equivalence.md) | 帧映射、像素域、容差及一致性判定 | 比较 SDK |
| [报告规范](references/output-format.md) | 输出目录、状态、耗时、事实标记及报告结构 | 执行或读取报告 |
| [通用提示词](references/prompt-templates.md) | 可复制的任务输入模板 | 用户需要提示词 |
| [本地保存偏好](references/local-execution-policy.json) | 本机工程保存默认值及来源记录 | 执行前，由批次说明解释 |

## 执行入口

先确认 DRP、SDK（比较模式）及输出目录，按任务读取上表文档。查询 Resolve 状态，未启动可通过可用的 MCP 工具启动。

优先使用 `scripts/render_first.py`。Agent 可以使用 MCP、Resolve API 和有证据支持的 UI 诊断处理异常；控制权交接遵守批次说明。缺少比较映射不阻止已明确导出方案的渲染。

## 维护约定

具体规则只在上表对应文档维护；入口保留导航，提示词引用规则，不复制整套限制。修改规则时核对调用代码与相关引用；发现文档和实现不一致须说明并修正，不能把文字声明当作功能已实现。

本批参数写入 manifest；本机保存默认值留在本地配置；实测证据和待验证判断写入报告。不要把某次批次路径、历史失败或临时授权提升为全局规则。实现与宿主验证的边界见 [README](README.md)。
