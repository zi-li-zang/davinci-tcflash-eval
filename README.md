# davinci-tcflash-eval

Agent 入口：[SKILL.md](SKILL.md)。文档职责和引用关系在入口维护；用户输入见 [通用提示词](references/prompt-templates.md)。

## 工具职责

| 工具 | 用途 |
|---|---|
| `scripts/render_first.py` | 串行导入、时间线渲染和可选比较 |
| `scripts/batch_control.py` | 读状态、协作暂停、停止请求与恢复检查 |
| `scripts/run_full_eval_pipeline.py` | 按目录发现项目的便捷入口 |
| MCP / Resolve API / UI | 由 Agent 按批次说明进行宿主检查和诊断恢复 |

## 验证边界

当前实现包含宿主外回归测试。它们支持执行器逻辑、比较和报告行为的验证，不证明真实 Resolve 渲染、UI 恢复、NAS 重链接或某批素材已验收。宿主结果以对应批次的实际证据为准。

运行回归测试：

```sh
"$PYTHON" -m unittest discover -s tests
```
