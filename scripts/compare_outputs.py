#!/usr/bin/env python3
"""
compare_outputs.py — A 类 SDK 一致性确定性比较器

严格执行《SDK 一致性回归测试规范》：
  1. 先验证“可比较”前置条件（帧映射完整性、文件存在性、分辨率、通道与位深一致性）。
  2. 按批准的帧映射表（frame_mapping）逐帧比对，严禁盲目假设“偶数文件名=插值帧”。
  3. 计算核心指标：
     - max_abs_error (最大绝对误差)
     - mean_abs_error (平均绝对误差)
     - outlier_pixel_ratio (超阈值像素比例)
     - anchor / interpolated / boundary_hold 分组统计
     - 区分 EXACT_MATCH 与 WITHIN_TOLERANCE
  4. 发生大面积超差时，仅生成“疑似偏移一帧”只读诊断，绝不自动平移基准判 PASS。
  5. 按固定规则（误差最高 Top-N 帧 + 必选边界帧）输出可视化对照图、带放大倍率标注的差异图、最大误差局部放大图与三帧时序图。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 启用 OpenCV OpenEXR 16-bit half / 32-bit float 读取支持
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")


def _load_image_normalized(path: str) -> Tuple[Any, Tuple[int, int, int], int]:
    """
    加载无损图像并归一化至浮点数组 (8/16-bit 整数归一化至 [0.0, 1.0]，float EXR 保持原始浮点值)。
    返回: (numpy_array_HWC_float64, (height, width, channels), bit_depth)
    """
    try:
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"缺少 numpy 比较依赖，无法执行像素级比较: {exc}") from exc

    # 优先尝试 OpenCV (支持 16-bit PNG/TIFF/EXR 与 32-bit float EXR 原生读取)
    try:
        import cv2  # type: ignore

        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is not None:
            if img.ndim == 2:
                img = img[:, :, None]
            elif img.shape[2] >= 3:
                # BGR -> RGB
                img = img[:, :, [2, 1, 0] + list(range(3, img.shape[2]))]
            h, w, c = img.shape
            if img.dtype == np.uint8:
                return img.astype(np.float64) / 255.0, (h, w, c), 8
            if img.dtype == np.uint16:
                return img.astype(np.float64) / 65535.0, (h, w, c), 16
            if img.dtype == np.float16:
                return img.astype(np.float64), (h, w, c), 16
            if img.dtype in (np.float32, np.float64):
                return img.astype(np.float64), (h, w, c), 32
    except ImportError:
        pass

    # 备选：Pillow
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as pil_img:
            arr = np.asarray(pil_img)
            if arr.ndim == 2:
                arr = arr[:, :, None]
            h, w, c = arr.shape
            if arr.dtype == np.uint8:
                return arr.astype(np.float64) / 255.0, (h, w, c), 8
            if arr.dtype in (np.uint16, np.int32):
                return arr.astype(np.float64) / 65535.0, (h, w, c), 16
            return arr.astype(np.float64), (h, w, c), 32
    except ImportError as exc:
        raise RuntimeError(f"缺少图像解码库 (cv2 或 PIL)，无法解码 {path}: {exc}") from exc


def _save_image_uint8(path: str, arr_norm: Any) -> None:
    """将浮点数组裁剪至 [0.0, 1.0] 并保存为 8-bit PNG 证据图。"""
    import numpy as np  # type: ignore

    clipped = np.clip(arr_norm * 255.0 + 0.5, 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        import cv2  # type: ignore

        if clipped.ndim == 3 and clipped.shape[2] == 3:
            clipped_bgr = clipped[:, :, ::-1]
        else:
            clipped_bgr = clipped
        if cv2.imwrite(path, clipped_bgr):
            return
    except ImportError:
        pass

    from PIL import Image  # type: ignore

    Image.fromarray(clipped).save(path)


def build_frame_mapping(comparison_rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    根据契约中的 frame_mapping_rule 生成确定性帧映射表。
    严禁未经声明直接假设偶数文件名即插值帧。
    """
    rule = comparison_rules.get("frame_mapping_rule", {})
    if "entries" in rule and isinstance(rule["entries"], list) and len(rule["entries"]) > 0:
        return _validate_mapping([dict(entry) for entry in rule["entries"]])

    rule_type = rule.get("type", "explicit_or_2x_standard")
    frame_count = int(rule.get("frame_count", 0))
    start_idx = int(rule.get("output_start_index", 0))
    sdk_start_idx = int(rule.get("sdk_start_index", start_idx))
    input_start_idx = int(rule.get("input_start_index", 0))
    if frame_count <= 0:
        raise ValueError("frame_mapping_rule 必须提供非空 entries 或正整数 frame_count")

    if rule.get("anchor_phase") != 0:
        raise ValueError("标准 2x 映射须明确 anchor_phase=0；相位未知用逐帧文件映射和 other")
    timestep = float(rule["interpolation_timestep"]) if "interpolation_timestep" in rule else None
    if timestep is None or not math.isfinite(timestep) or not 0 < timestep < 1:
        raise ValueError("标准 2x 映射必须声明 interpolation_timestep (0 < t < 1)，不得推断 JA")
    if frame_count % 2 == 0 and "tail_policy" not in rule:
        raise ValueError("偶数长度映射必须声明 tail_policy: interpolate 或 boundary_hold")
    if rule.get("tail_policy", "interpolate") not in ("interpolate", "boundary_hold"):
        raise ValueError("不支持的 tail_policy")
    overrides_by_idx: Dict[int, Dict[str, Any]] = {}
    for item in rule.get("explicit_overrides", []):
        idx = int(item["output_frame_index"])
        if idx in overrides_by_idx or not start_idx <= idx < start_idx + frame_count:
            raise ValueError("explicit_overrides 重复或越界")
        overrides_by_idx[idx] = item

    mapping: List[Dict[str, Any]] = []
    for i in range(frame_count):
        out_idx = start_idx + i
        sdk_idx = sdk_start_idx + i
        if out_idx in overrides_by_idx:
            mapping.append(overrides_by_idx[out_idx])
            continue

        if rule_type == "explicit_or_2x_standard":
            src_a = input_start_idx + (i // 2)
            if i == frame_count - 1 and i % 2 == 1 and rule.get("tail_policy") == "boundary_hold":
                f_type = "boundary_hold"
                src_b = src_a
                ts = 0.0
            elif i % 2 == 0:
                f_type = "anchor"
                src_b = src_a
                ts = 0.0
            else:
                f_type = "interpolated"
                src_b = src_a + 1
                ts = timestep
            mapping.append(
                {
                    "output_frame_index": out_idx,
                    "sdk_frame_index": sdk_idx,
                    "frame_type": f_type,
                    "input_frame_a": src_a,
                    "input_frame_b": src_b,
                    "expected_timestep": ts,
                }
            )
        else:
            raise ValueError(f"不支持的帧映射规则类型: {rule_type}")

    return _validate_mapping(mapping)


def _compute_psnr(mse: float) -> float:
    if mse <= 1e-12:
        return 99.0
    return round(10.0 * math.log10(1.0 / mse), 4)


def _compute_ssim(img1: Any, img2: Any) -> float:
    """
    计算多通道平均 SSIM（支持 2D/3D 图像与 HDR 浮点图像动态范围自适应）。
    若计算失败或维度不匹配返回 0.0，杜绝静默假阳性。
    """
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore

        if img1.shape != img2.shape:
            return 0.0

        num_channels = img1.shape[2] if img1.ndim == 3 else 1
        vals = []
        for c in range(min(num_channels, 3)):
            i1 = img1[:, :, c].astype(np.float64) if img1.ndim == 3 else img1.astype(np.float64)
            i2 = img2[:, :, c].astype(np.float64) if img2.ndim == 3 else img2.astype(np.float64)

            # 自适应动态范围 L（默认归一化图像为 1.0，浮点 HDR 取实际最大像素值）
            data_range = max(1.0, float(np.max(i1)), float(np.max(i2)))
            c1 = (0.01 * data_range) ** 2
            c2 = (0.03 * data_range) ** 2

            mu1 = cv2.GaussianBlur(i1, (11, 11), 1.5)
            mu2 = cv2.GaussianBlur(i2, (11, 11), 1.5)
            mu1_sq = mu1 ** 2
            mu2_sq = mu2 ** 2
            mu1_mu2 = mu1 * mu2
            sigma1_sq = cv2.GaussianBlur(i1 ** 2, (11, 11), 1.5) - mu1_sq
            sigma2_sq = cv2.GaussianBlur(i2 ** 2, (11, 11), 1.5) - mu2_sq
            sigma12 = cv2.GaussianBlur(i1 * i2, (11, 11), 1.5) - mu1_mu2

            num = (2 * mu1_mu2 + c1) * (2 * sigma12 + c2)
            den = (mu1_sq + mu2_sq + c1) * (sigma1_sq + sigma2_sq + c2)
            ssim_map = np.where(den > 0, num / den, 1.0)
            vals.append(float(np.mean(ssim_map)))

        return round(float(np.mean(vals)), 6) if vals else 1.0
    except Exception:
        return 0.0


def _generate_visual_artifacts(
    selected_indices: List[int],
    frame_records_by_idx: Dict[int, Dict[str, Any]],
    resolve_paths_by_idx: Dict[int, str],
    sdk_paths_by_idx: Dict[int, str],
    comparison_dir: str,
    diff_amplification: int,
    roi_size: Tuple[int, int],
) -> List[Dict[str, Any]]:
    """为选定的关键帧生成对照图、放大差异图、最大误差 ROI 裁剪图与三帧时序图。"""
    import numpy as np  # type: ignore

    artifacts: List[Dict[str, Any]] = []
    vis_dir = os.path.join(comparison_dir, "visuals")
    os.makedirs(vis_dir, exist_ok=True)

    for idx in selected_indices:
        if idx not in resolve_paths_by_idx or idx not in sdk_paths_by_idx:
            continue
        res_img, (h, w, _), _ = _load_image_normalized(resolve_paths_by_idx[idx])
        sdk_img, _, _ = _load_image_normalized(sdk_paths_by_idx[idx])
        all_diff = np.abs(res_img - sdk_img)
        alpha_path = None
        if res_img.shape[2] == 4:
            alpha_path = os.path.join(vis_dir, f"frame_{idx:04d}_alpha_res_sdk_diff.png")
            alpha_strip = np.concatenate([res_img[:, :, 3], sdk_img[:, :, 3],
                                          all_diff[:, :, 3] * float(diff_amplification)], axis=1)
            _save_image_uint8(alpha_path, alpha_strip)
        # RGB evidence is not composited or unpremultiplied; alpha has a separate strip.
        res_img, sdk_img = res_img[:, :, :3], sdk_img[:, :, :3]
        abs_diff = all_diff[:, :, :3]

        # 1. 并排对照图 (Resolve | SDK)
        sep = np.ones((h, 4, 3), dtype=np.float64)
        side_by_side = np.concatenate([res_img, sep, sdk_img], axis=1)
        sbs_path = os.path.join(vis_dir, f"frame_{idx:04d}_side_by_side.png")
        _save_image_uint8(sbs_path, side_by_side)

        # 2. 放大差异图 (标注放大倍率)
        amp_diff = np.clip(abs_diff * float(diff_amplification), 0.0, 1.0)
        diff_path = os.path.join(
            vis_dir, f"frame_{idx:04d}_diff_x{diff_amplification}.png"
        )
        _save_image_uint8(diff_path, amp_diff)

        # 3. 最大误差区域局部放大图 (ROI Crop: Resolve | SDK | Diff xN)
        pixel_max_err = np.max(all_diff, axis=2)
        max_y, max_x = np.unravel_index(np.argmax(pixel_max_err), pixel_max_err.shape)
        crop_w, crop_h = roi_size
        y0 = max(0, min(h - crop_h, int(max_y) - crop_h // 2))
        x0 = max(0, min(w - crop_w, int(max_x) - crop_w // 2))
        y1 = min(h, y0 + crop_h)
        x1 = min(w, x0 + crop_w)

        roi_res = res_img[y0:y1, x0:x1]
        roi_sdk = sdk_img[y0:y1, x0:x1]
        roi_diff = amp_diff[y0:y1, x0:x1]
        roi_sep = np.ones((y1 - y0, 4, 3), dtype=np.float64)
        roi_combined = np.concatenate([roi_res, roi_sep, roi_sdk, roi_sep, roi_diff], axis=1)
        roi_path = os.path.join(
            vis_dir, f"frame_{idx:04d}_roi_{x0}_{y0}_x{diff_amplification}.png"
        )
        _save_image_uint8(roi_path, roi_combined)

        # 4. 前后三帧时序对照 (prev -> curr -> next)
        temporal_path = None
        if (idx - 1) in resolve_paths_by_idx and (idx + 1) in resolve_paths_by_idx:
            prev_img, _, _ = _load_image_normalized(resolve_paths_by_idx[idx - 1])
            next_img, _, _ = _load_image_normalized(resolve_paths_by_idx[idx + 1])
            temporal_strip = np.concatenate([prev_img[:, :, :3], sep, res_img, sep, next_img[:, :, :3]], axis=1)
            temporal_path = os.path.join(vis_dir, f"frame_{idx:04d}_temporal_strip.png")
            _save_image_uint8(temporal_path, temporal_strip)

        rec = frame_records_by_idx[idx]
        artifacts.append(
            {
                "output_frame_index": idx,
                "frame_type": rec["frame_type"],
                "max_abs_error": rec["max_abs_error"],
                "mean_abs_error": rec["mean_abs_error"],
                "max_error_coord": [int(max_x), int(max_y)],
                "roi_box": [x0, y0, x1, y1],
                "diff_amplification": diff_amplification,
                "preview_domain": "RGB stored values clipped to [0,1]; alpha strip separate; no alpha conversion",
                "alpha_side_by_side_diff_image": alpha_path,
                "side_by_side_image": sbs_path,
                "diff_image": diff_path,
                "roi_crop_image": roi_path,
                "temporal_strip_image": temporal_path,
            }
        )

    return artifacts


def _validate_mapping(mapping):
    indices = [int(entry["output_frame_index"]) for entry in mapping]
    if not indices or len(set(indices)) != len(indices) or sorted(indices) != list(range(len(indices))):
        raise ValueError("output_frame_index 必须唯一并完整覆盖零基输出范围")
    for entry in mapping:
        int(entry["sdk_frame_index"])
        kind = entry.get("frame_type", "other")
        entry["frame_type"] = kind
        if kind not in ("anchor", "interpolated", "boundary_hold", "other"):
            raise ValueError("无效 frame_type；相位未知用 other，不伪造输入帧")
        if kind != "other":
            if "input_frame_a" not in entry or "input_frame_b" not in entry:
                raise ValueError("标注帧类型时必须包含 input_frame_a / input_frame_b")
            int(entry["input_frame_a"]); int(entry["input_frame_b"])
            ts = float(entry["expected_timestep"])
            if not math.isfinite(ts) or not 0 <= ts <= 1:
                raise ValueError("expected_timestep 必须是有限的 [0,1] 值")
            if kind == "interpolated" and not 0 < ts < 1:
                raise ValueError("插值帧 timestep 必须在 (0,1)")
        for key in ("resolve_filename", "sdk_filename"):
            name = entry.get(key)
            if name:
                p_name = os.path.normpath(name)
                if os.path.isabs(p_name) or p_name.startswith("..") or p_name in (".", ".."):
                    raise ValueError("帧文件名不能包含绝对路径或跨级上级路径")
    return sorted(mapping, key=lambda e: int(e["output_frame_index"]))


def validate_comparison_rules(case_data):
    rules = case_data.get("comparison_rules", {})
    if rules.get("allow_sorted_sequence_fallback", False) or rules.get("resolve_output_pattern") == "@sorted_sequence":
        raise ValueError("禁止排序兜底；请提供明确命名规则或逐帧文件名")
    tolerances = rules.get("tolerances", {})
    for key in ("max_abs_error_limit", "mean_abs_error_limit", "outlier_pixel_threshold", "max_outlier_pixel_ratio"):
        value = float(tolerances[key])
        if not math.isfinite(value) or value < 0 or (key == "max_outlier_pixel_ratio" and value > 1):
            raise ValueError(f"无效容差: {key}")
    domain = rules.get("pixel_domain", {})
    if domain.get("channels") not in ("RGB", "RGBA"):
        raise ValueError("只支持显式 RGB / RGBA；不静默丢弃其他通道")
    if domain.get("channels") == "RGBA" and domain.get("alpha_representation") not in ("straight", "premultiplied"):
        raise ValueError("RGBA 必须声明双方一致的 alpha_representation: straight / premultiplied；不自动转换")
    mapping = build_frame_mapping(rules)
    expected = int(case_data["export_rules"]["expected_frame_count"])
    if len(mapping) != expected:
        raise ValueError("帧映射必须覆盖全部 expected_frame_count")
    return mapping


def safe_frame_path(root, name):
    """Allow declared relative subdirectories, never traversal or symlink escape."""
    root = Path(root).resolve()
    rel = Path(name)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts or name == '.':
        raise ValueError("帧路径必须是基准/输出目录内的相对路径")
    full = (root / rel).resolve()
    if root not in full.parents:
        raise ValueError("帧路径越出声明目录（含符号链接）")
    return str(full)


def image_inventory(root):
    root = Path(root).resolve()
    extensions = {'.exr', '.tif', '.tiff', '.png', '.jpg', '.jpeg', '.dpx', '.bmp'}
    return {safe_frame_path(root, str(p.relative_to(root))) for p in root.rglob('*')
            if p.is_file() and p.suffix.lower() in extensions
            and not any(part.startswith('.') for part in p.relative_to(root).parts)}


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run_comparison(case_data, resolve_output_dir, baseline_dir, comparison_out_dir):
    os.makedirs(comparison_out_dir, exist_ok=True)
    try:
        validate_comparison_rules(case_data)
    except (KeyError, TypeError, ValueError) as exc:
        result = {"ok": False, "verdict": "BLOCKED", "failure_code": "INVALID_COMPARISON_CONTRACT", "reason": str(exc)}
    else:
        try:
            result = _run_comparison(case_data, resolve_output_dir, baseline_dir, comparison_out_dir)
        except ImportError as exc:
            result = {"ok": False, "verdict": "BLOCKED", "failure_code": "COMPARISON_DEPENDENCY_MISSING", "reason": str(exc)}
        except Exception as exc:
            result = {"ok": False, "verdict": "INCONCLUSIVE", "failure_code": "COMPARISON_ERROR", "reason": str(exc)}
    coverage_path = Path(comparison_out_dir) / "coverage.json"
    if coverage_path.is_file(): result.setdefault("coverage", json.loads(coverage_path.read_text()))
    with open(os.path.join(comparison_out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
    return result


def _run_comparison(
    case_data: Dict[str, Any],
    resolve_output_dir: str,
    baseline_dir: str,
    comparison_out_dir: str,
) -> Dict[str, Any]:
    """执行完整 A 类比较并写出 summary.json。"""
    import numpy as np  # type: ignore

    os.makedirs(comparison_out_dir, exist_ok=True)
    comp_rules = case_data.get("comparison_rules", {})
    export_rules = case_data.get("export_rules", {})
    tolerances = comp_rules.get("tolerances", {})

    max_abs_limit = float(tolerances.get("max_abs_error_limit", 0.004))
    mean_abs_limit = float(tolerances.get("mean_abs_error_limit", 0.0005))
    outlier_thresh = float(tolerances.get("outlier_pixel_threshold", 0.002))
    max_outlier_ratio = float(tolerances.get("max_outlier_pixel_ratio", 0.001))

    try:
        mapping = build_frame_mapping(comp_rules)
    except Exception as exc:
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "INVALID_FRAME_MAPPING",
            "reason": f"生成帧映射表失败: {exc}",
        }

    sdk_pattern = comp_rules.get("file_pattern", "sdk_frame_{index:04d}.png")
    res_pattern = comp_rules.get("resolve_output_pattern", "resolve_out_{timeline_frame:08d}.png")
    timeline_start = int(export_rules.get("mark_in", 0))
    expected_res = list(case_data.get("locked_settings", {}).get("resolution", [0, 0]))

    resolve_paths_by_idx: Dict[int, str] = {}
    sdk_paths_by_idx: Dict[int, str] = {}

    sorted_res_files: List[str] = []
    if os.path.isdir(resolve_output_dir):
        sorted_res_files = sorted(
            f
            for f in os.listdir(resolve_output_dir)
            if not f.startswith(".") and os.path.isfile(os.path.join(resolve_output_dir, f))
        )

    # 门禁 1：验证所有映射帧的文件存在性
    missing_files: List[str] = []
    for seq_idx, entry in enumerate(mapping):
        out_idx = int(entry["output_frame_index"])
        sdk_idx = int(entry["sdk_frame_index"])
        tl_frame = timeline_start + out_idx

        sdk_file = entry.get("sdk_filename") or sdk_pattern.format(index=sdk_idx)
        if entry.get("resolve_filename"):
            res_file = entry["resolve_filename"]
        else:
            res_file = res_pattern.format(index=out_idx, timeline_frame=tl_frame, sdk_index=sdk_idx)
        sdk_full = safe_frame_path(baseline_dir, sdk_file)
        res_full = safe_frame_path(resolve_output_dir, res_file)

        if not os.path.isfile(sdk_full):
            missing_files.append(f"SDK 基准帧缺失: {sdk_full}")
        if not os.path.isfile(res_full):
            missing_files.append(f"Resolve 输出帧缺失: {res_full}")

        sdk_paths_by_idx[out_idx] = sdk_full
        resolve_paths_by_idx[out_idx] = res_full

    actual_res = image_inventory(resolve_output_dir)
    actual_sdk = image_inventory(baseline_dir)
    mapped_res, mapped_sdk = set(resolve_paths_by_idx.values()), set(sdk_paths_by_idx.values())
    coverage = {"mapped_frame_count": len(mapping), "resolve_file_count": len(actual_res),
                "sdk_file_count": len(actual_sdk), "missing_files": missing_files,
                "extra_resolve_files": sorted(actual_res - mapped_res),
                "extra_sdk_files": sorted(actual_sdk - mapped_sdk)}
    with open(os.path.join(comparison_out_dir, "coverage.json"), "w", encoding="utf-8") as stream:
        json.dump(coverage, stream, ensure_ascii=False, indent=2)
    if missing_files or actual_res != mapped_res or actual_sdk != mapped_sdk:
        return {"ok": False, "verdict": "BLOCKED", "failure_code": "FRAME_COVERAGE_MISMATCH",
                "reason": "帧映射未完整覆盖声明目录：缺失/额外帧见 coverage.json；未截断比较", "coverage": coverage}

    if len(set(resolve_paths_by_idx.values())) != len(mapping) or len(set(sdk_paths_by_idx.values())) != len(mapping):
        return {"ok": False, "verdict": "BLOCKED", "failure_code": "INVALID_FRAME_MAPPING", "reason": "帧映射不能重复使用同一个输出/基准文件"}
    actual_mapping = [dict(entry, resolve_path=resolve_paths_by_idx[int(entry["output_frame_index"])],
                           sdk_path=sdk_paths_by_idx[int(entry["output_frame_index"])] ) for entry in mapping]
    for item in actual_mapping:
        item["resolve_sha256"] = _file_hash(item["resolve_path"])
        item["sdk_sha256"] = _file_hash(item["sdk_path"])
    with open(os.path.join(comparison_out_dir, "frame_mapping.json"), "w", encoding="utf-8") as f:
        json.dump(actual_mapping, f, ensure_ascii=False, indent=2)

    # 逐帧比较
    frame_results: List[Dict[str, Any]] = []
    frame_records_by_idx: Dict[int, Dict[str, Any]] = {}
    failed_frames: List[Dict[str, Any]] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {
        "anchor": [],
        "interpolated": [],
        "boundary_hold": [],
        "other": [],
    }

    for entry in mapping:
        out_idx = int(entry["output_frame_index"])
        f_type = str(entry.get("frame_type", "other"))
        if f_type not in grouped:
            f_type = "other"

        res_arr, (rh, rw, rc), r_bits = _load_image_normalized(resolve_paths_by_idx[out_idx])
        sdk_arr, (sh, sw, sc), s_bits = _load_image_normalized(sdk_paths_by_idx[out_idx])

        # 门禁 2：验证分辨率与通道数可比较
        if (rh, rw, rc) != (sh, sw, sc):
            return {
                "ok": False,
                "verdict": "BLOCKED",
                "failure_code": "COMPARABILITY_GATE_FAILED",
                "reason": (
                    f"帧 {out_idx} 规格不一致，不可直接比较: "
                    f"Resolve={(rw, rh, rc)}, SDK={(sw, sh, sc)}"
                ),
            }
        if r_bits != s_bits:
            return {"ok": False, "verdict": "BLOCKED", "failure_code": "COMPARABILITY_GATE_FAILED",
                    "reason": f"帧 {out_idx} 解码位深不一致: Resolve={r_bits}, SDK={s_bits}"}
        if expected_res != [0, 0] and [rw, rh] != expected_res:
            return {
                "ok": False,
                "verdict": "FAIL",
                "failure_code": "OUTPUT_INVALID",
                "reason": f"帧 {out_idx} 分辨率 {[rw, rh]} 与契约锁定分辨率 {expected_res} 不符",
            }

        if not np.isfinite(res_arr).all() or not np.isfinite(sdk_arr).all():
            return {"ok": False, "verdict": "FAIL", "failure_code": "NONFINITE_PIXELS", "reason": f"帧 {out_idx} 含 NaN/Inf"}
        channels = comp_rules.get("pixel_domain", {}).get("channels", "RGB")
        if channels not in ("RGB", "RGBA", "Y") or rc != {"RGB": 3, "RGBA": 4, "Y": 1}[channels]:
            return {"ok": False, "verdict": "BLOCKED", "failure_code": "COMPARABILITY_GATE_FAILED", "reason": "实际通道与声明不符；不允许静默丢弃 alpha"}
        diff = np.abs(res_arr - sdk_arr)
        max_abs = float(np.max(diff))
        mean_abs = float(np.mean(diff))
        mse = float(np.mean(diff ** 2))
        outlier_ratio = float(np.mean(np.any(diff > outlier_thresh, axis=2)))
        psnr = _compute_psnr(mse)

        frame_passed = (
            max_abs <= max_abs_limit
            and mean_abs <= mean_abs_limit
            and outlier_ratio <= max_outlier_ratio
        )
        exact = max_abs == 0.0

        def channel_metrics(values):
            return {"max_abs_error": float(np.max(values)),
                    "mean_abs_error": float(np.mean(values)),
                    "outlier_pixel_ratio": float(np.mean(np.any(values > outlier_thresh, axis=2)))}

        rec = {
            "channel_metrics": {"rgb": channel_metrics(diff[:, :, :3]),
                                "alpha": channel_metrics(diff[:, :, 3:4]) if channels == "RGBA" else None},
            "output_frame_index": out_idx,
            "sdk_frame_index": int(entry["sdk_frame_index"]),
            "frame_type": f_type,
            "input_frame_a": entry.get("input_frame_a"),
            "input_frame_b": entry.get("input_frame_b"),
            "expected_timestep": entry.get("expected_timestep"),
            "resolve_decoded_bit_depth": r_bits,
            "sdk_decoded_bit_depth": s_bits,
            "max_abs_error": max_abs,
            "mean_abs_error": mean_abs,
            "outlier_pixel_ratio": outlier_ratio,
            "psnr_db": psnr,
            "ssim": _compute_ssim(res_arr, sdk_arr),
            "exact_match": exact,
            "passed": frame_passed,
            "failed_rules": [name for name, failed in (
                ("max_abs_error_limit", max_abs > max_abs_limit),
                ("mean_abs_error_limit", mean_abs > mean_abs_limit),
                ("max_outlier_pixel_ratio", outlier_ratio > max_outlier_ratio)) if failed],
        }
        frame_results.append(rec)
        frame_records_by_idx[out_idx] = rec
        grouped[f_type].append(rec)
        if not frame_passed:
            failed_frames.append(rec)

    # 汇总全局统计
    all_max_abs = max((r["max_abs_error"] for r in frame_results), default=0.0)
    all_mean_abs = (
        sum(r["mean_abs_error"] for r in frame_results) / len(frame_results)
        if frame_results
        else 0.0
    )
    all_max_outlier = max((r["outlier_pixel_ratio"] for r in frame_results), default=0.0)
    all_exact = all(r["exact_match"] for r in frame_results)
    overall_passed = len(failed_frames) == 0

    def _summarize_group(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            return {
                "count": 0,
                "max_abs_error": 0.0,
                "mean_abs_error": 0.0,
                "failed_count": 0,
                "status": "N/A",
            }
        g_failed = sum(1 for x in items if not x["passed"])
        return {
            "count": len(items),
            "max_abs_error": round(max(x["max_abs_error"] for x in items), 6),
            "mean_abs_error": round(
                sum(x["mean_abs_error"] for x in items) / len(items), 6
            ),
            "failed_count": g_failed,
            "status": "PASS" if g_failed == 0 else "FAIL",
        }

    grouped_summary = {k: _summarize_group(v) for k, v in grouped.items()}

    # 5.2 节规定：发生无法对齐/超差时，可生成“疑似偏移一帧”只读诊断，但绝不以此改判 PASS
    offset_diagnostic: Optional[Dict[str, Any]] = None
    if not overall_passed and len(frame_results) >= 3:
        first_fail_idx = failed_frames[0]["output_frame_index"]
        res_cur, _, _ = _load_image_normalized(resolve_paths_by_idx[first_fail_idx])
        shift_candidates = {}
        for delta in (-1, 1):
            neighbor_idx = first_fail_idx + delta
            if neighbor_idx in sdk_paths_by_idx:
                sdk_nei, _, _ = _load_image_normalized(sdk_paths_by_idx[neighbor_idx])
                shift_candidates[str(delta)] = round(
                    float(np.mean(np.abs(res_cur - sdk_nei))), 6
                )
        cur_mean = failed_frames[0]["mean_abs_error"]
        for delta_str, shifted_mean in shift_candidates.items():
            if shifted_mean < cur_mean * 0.25 and shifted_mean <= mean_abs_limit:
                offset_diagnostic = {
                    "warning": (
                        f"诊断提示：帧 {first_fail_idx} 与偏移 {delta_str} 帧的 SDK 基准误差显著降低 "
                        f"({shifted_mean} vs {cur_mean})，疑似存在序列帧偏移。"
                        "按规范严禁自动平移通过，维持 FAIL 结论。"
                    ),
                    "suspected_frame_offset": int(delta_str),
                }
                break

    # 5.4 节规定：按固定规则抽取误差最高的前 N 帧 + 指定边界帧生成视觉证据
    vis_cfg = comp_rules.get("visual_evidence", {})
    top_n = int(vis_cfg.get("top_n_worst_frames", 3))
    boundary_indices = [int(x) for x in vis_cfg.get("mandatory_boundary_frames", [0, 1])]
    diff_amp = int(vis_cfg.get("diff_amplification", 16))
    roi_size_list = vis_cfg.get("roi_crop_size", [256, 256])
    roi_size = (int(roi_size_list[0]), int(roi_size_list[1]))

    sorted_by_err = sorted(
        frame_results, key=lambda x: x["max_abs_error"], reverse=True
    )
    top_err_indices = [x["output_frame_index"] for x in sorted_by_err[:top_n]]
    selected_vis_indices = []
    for idx in top_err_indices + boundary_indices:
        if idx in frame_records_by_idx and idx not in selected_vis_indices:
            selected_vis_indices.append(idx)

    evidence_errors = []
    visual_artifacts = []
    try:
        visual_artifacts = _generate_visual_artifacts(
            selected_indices=selected_vis_indices,
            frame_records_by_idx=frame_records_by_idx,
            resolve_paths_by_idx=resolve_paths_by_idx,
            sdk_paths_by_idx=sdk_paths_by_idx,
            comparison_dir=comparison_out_dir,
            diff_amplification=diff_amp,
            roi_size=roi_size,
        )
    except Exception as exc:
        evidence_errors.append(f"视觉证据生成失败: {exc}")
    for item in actual_mapping:
        if _file_hash(item["sdk_path"]) != item["sdk_sha256"] or _file_hash(item["resolve_path"]) != item["resolve_sha256"]:
            return {"ok": False, "verdict": "INCONCLUSIVE", "failure_code": "INPUT_CHANGED", "reason": "比较期间输入文件发生变化"}

    match_grade = (
        "EXACT_MATCH"
        if all_exact
        else ("WITHIN_TOLERANCE" if overall_passed else "OUT_OF_TOLERANCE")
    )

    summary = {
        "ok": overall_passed and not evidence_errors,
        "verdict": ("INCONCLUSIVE" if evidence_errors else "PASS") if overall_passed else "FAIL",
        "evidence_errors": evidence_errors,
        "failure_code": ("VISUAL_EVIDENCE_ERROR" if evidence_errors else None) if overall_passed else "PIXEL_OUT_OF_TOLERANCE",
        "reason": ("像素满足容差，但视觉证据生成失败" if evidence_errors else "全部规定帧满足 SDK 一致性容差") if overall_passed else f"存在 {len(failed_frames)} 帧超出一致性容差上限",
        "coverage": coverage,
        "metric_version": "3-pixel-any-channel-rgba-breakdown",
        "pixel_domain": comp_rules.get("pixel_domain", {}),
        "comparison_scope": "decoded pixels; declared color domain is not independently verified",
        "psnr_peak": 1.0,
        "match_classification": match_grade,
        "evaluated_frame_count": len(frame_results),
        "failed_frame_count": len(failed_frames),
        "metrics": {
            **{name: ({"max_abs_error": max(r["channel_metrics"][name]["max_abs_error"] for r in frame_results),
                       "mean_abs_error": sum(r["channel_metrics"][name]["mean_abs_error"] for r in frame_results) / len(frame_results)}
                      if frame_results and frame_results[0]["channel_metrics"][name] is not None else None)
               for name in ("rgb", "alpha")},
            "max_abs_error": all_max_abs,
            "mean_abs_error": all_mean_abs,
            "max_outlier_pixel_ratio": all_max_outlier,
            "mean_ssim": round(sum(r["ssim"] for r in frame_results) / len(frame_results), 6) if frame_results else 1.0,
            "min_ssim": round(min(r["ssim"] for r in frame_results), 6) if frame_results else 1.0,
        },
        "tolerances": {
            "max_abs_error_limit": max_abs_limit,
            "mean_abs_error_limit": mean_abs_limit,
            "outlier_pixel_threshold": outlier_thresh,
            "max_outlier_pixel_ratio": max_outlier_ratio,
        },
        "grouped_statistics": grouped_summary,
        "failed_frames": failed_frames,
        "offset_diagnostic": offset_diagnostic,
        "visual_artifacts": visual_artifacts,
        "per_frame": frame_results,
    }

    summary_path = os.path.join(comparison_out_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic SDK Equivalence Comparator")
    parser.add_argument("--case-json", required=True)
    parser.add_argument("--resolve-output-dir", required=True)
    parser.add_argument("--baseline-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    with open(args.case_json, "r", encoding="utf-8") as f:
        case_data = json.load(f)

    summary = run_comparison(
        case_data=case_data,
        resolve_output_dir=args.resolve_output_dir,
        baseline_dir=args.baseline_dir,
        comparison_out_dir=args.out_dir,
    )
    print(json.dumps({"ok": summary["ok"], "verdict": summary["verdict"]}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
