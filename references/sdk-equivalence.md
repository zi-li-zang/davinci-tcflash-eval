# 已有外部 SDK 与插件导出像素一致性

## 先渲染，再确定能否比较

本文件维护比较接入与判定规则。导出选型见 [批次执行说明](batch-execution.md)，结论呈现与证据标记见 [报告规范](output-format.md)。

比较点为 Resolve 导出后解码像素，不是插件内部 tensor。节点存在与渲染成功都不能代替一致性结果；差异也不能直接证明 SDK 算法错误。

渲染之前不要求完整帧映射。渲染后可以依据 SDK 生成说明、模板声明、实际文件编号及时间线范围建立明确对应。单纯像素比较只需双方文件对应和明确像素域；不需要伪造 JA、输入帧和 timestep。

单时间线在项目任务的 `comparison` 中提供契约；多时间线在 `timeline_comparisons[真实时间线名]` 中分别填写 `comparison` 和可选的 `sdk_dir`。没有比较契约时保留渲染结果，比较标记未就绪。

当前比较器只解码图像序列；视频容器需要另有明确、保留精度的解码方案。

单项 `comparison` 最小示例（用实际映射替换）：

```json
{
  "export_rules": {"expected_frame_count": 1},
  "comparison_rules": {
    "pixel_domain": {"channels": "RGB"},
    "frame_mapping_rule": {
      "entries": [
        {"output_frame_index": 0, "sdk_frame_index": 0,
         "resolve_filename": "output00000000.exr", "sdk_filename": "LE/frame0000.exr", "frame_type": "other"}
      ]
    },
    "tolerances": {"max_abs_error_limit": 0, "mean_abs_error_limit": 0,
      "outlier_pixel_threshold": 0, "max_outlier_pixel_ratio": 0}
  }
}
```

`other` 表示帧类型未确认，允许缺省 input_frame_a/b 和 expected_timestep。标注 anchor/interpolated/boundary_hold 必须有已确认的输入帧和 timestep。标准 2x 生成规则必须明确 `anchor_phase: 0`、interpolation_timestep、frame_count 和必要的 tail_policy；仅支持首项为 anchor 的这一声明规则。其他相位用明确逐帧映射，不默认奇偶位。

## 路径与覆盖

显式文件名或 pattern 可以生成 `LE/...`、`RE/...` 等相对路径；不能是绝对路径、包含 `..` 或通过符号链接跳出根目录。不去掉眼别子目录，不按相似名称配对。

完整映射必须覆盖 expected_frame_count 和连续零基 output_frame_index，双方文件不重复。比较器盘点整个声明目录下支持的图像文件，排除隐藏/AppleDouble 元数据。缺失或额外帧返回 FRAME_COVERAGE_MISMATCH，并写 coverage.json；不按 min(len(...)) 截断，不做排序兜底。若项目级基准含其他时间线/眼别/JA，应将该独立用例的 sdk_dir 明确指向对应序列根目录，而不是把整棵混合目录当成一条序列。

实际映射与文件 SHA-256 写 frame_mapping.json。比较前后核对文件变化；检测到变化不判正常通过。

## 数值和可比性

- 批次严格零容差；缺省容差可补为零，已有非零契约视为冲突，不静默改写。独立比较器支持明确的非零阈值，但不能将其 WITHIN_TOLERANCE 宣称严格一致。
- 当前解码表示：8/16 位整数除以各自整数最大值转为 float64，浮点 EXR 保留存储解码值，包括负值与 >1。该固定解码表示不做色彩空间、曝光或 alpha 转换；报告应明确表示与色彩链路是否独立核验。
- 尺寸与 RGB/RGBA 通道必须一致。RGBA 声明双方同为 straight 或 premultiplied，alpha 参与比较；不丢 alpha、不自动反预乘。文件名 premult 不能代替语义说明。
- decoded_bit_depth 是解码数组类型，不证明 EXR 文件实际存 half/float。不同解码位深不能直接判定同一表示，须报告规格问题。
- NaN/Inf 返回 NONFINITE_PIXELS 并 FAIL；尺寸/通道/语义不可比时 BLOCKED，保留已渲染结果。缺文件、坏文件不得通过。
- 不自动移帧、缩放、补帧、改色彩或裁剪来通过。没有匹配条件时列明缺失材料，按批次说明继续处理其他项。

指标：max_abs_error、mean_abs_error、任一通道超阈值的像素占比；RGB 与 alpha 分组记录。判定前不舍入，EXACT_MATCH 要求真实数值误差为零。PSNR/SSIM 仅辅助；PSNR 参考峰值 1，显示值封顶 99，不代表 EXR HDR 主观评分。

## 报告与证据

先列可比性和 coverage，再列整体指标、最差帧和分组结果。像素超差保留 PIXEL_OUT_OF_TOLERANCE；视觉证据生成失败不能抹去已确认的 FAIL。像素通过但所需视觉证据失败则 INCONCLUSIVE。

可用时生成 Top-N 最大差异帧、指定边界帧、并排图、带倍率的差异图、ROI、时序图。PNG 预览裁剪至 [0,1]，不是原始浮点证据；报告实际是否目视，不自动给主观质量分。

总误差和 ROI 不能证明差异只在高光、或正常区域全部一致；这需要另有分区测量。疑似偏移诊断不改正式映射。事实分类遵守报告规范。
