#!/usr/bin/env python3
"""
resolve_worker.py — DaVinci Resolve 隔离控制子进程

由主监控器 render_first.py 按阶段通过子进程调用并施加硬超时保护。
职责：
  1. 连接本机自带的 DaVinciResolveScript API（零第三方依赖）。
  2. 执行经过严格限定的单步原子动作：
     - preflight: 检查宿主存活、版本、是否正在渲染、当前工程保护状态。
     - import_copy: 将 template.drp 导入为带唯一运行后缀的测试工程副本。
     - render: 仅修改允许的导出字段，新增单个 Render Job 并轮询状态。
  3. 输出结构化 JSON 至标准输出，供父进程记录至 events.jsonl。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from fractions import Fraction
import platform
import sys
import time
from typing import Any, Dict, List, Optional

RESOLVE_SCRIPT_API = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
RESOLVE_SCRIPT_MODULES = os.path.join(RESOLVE_SCRIPT_API, "Modules")


def connect_resolve() -> Optional[Any]:
    """连接本机正在运行的 DaVinci Resolve Studio 实例。"""
    if RESOLVE_SCRIPT_MODULES not in sys.path and os.path.isdir(RESOLVE_SCRIPT_MODULES):
        sys.path.append(RESOLVE_SCRIPT_MODULES)
    try:
        import DaVinciResolveScript as bmd  # type: ignore

        return bmd.scriptapp("Resolve")
    except Exception:
        return None


def action_preflight(payload: Dict[str, Any]) -> Dict[str, Any]:
    """阶段 PREFLIGHT：检查宿主状态并保护用户现有工作。"""
    resolve = connect_resolve()
    if resolve is None:
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "CONTROL_CONNECTION_LOST",
            "reason": "无法通过 DaVinciResolveScript 连接 Resolve（请确认 DaVinci Resolve Studio 已启动且开启外部脚本权限）",
        }

    version_str = ""
    try:
        version_str = str(resolve.GetVersionString() or "")
    except Exception as exc:
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "CONTROL_CONNECTION_LOST",
            "reason": f"读取 Resolve 版本失败: {exc}",
        }

    pm = resolve.GetProjectManager()
    if pm is None:
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "CONTROL_CONNECTION_LOST",
            "reason": "无法获取 ProjectManager",
        }

    current_project = pm.GetCurrentProject()
    current_project_name = current_project.GetName() if current_project else ""

    # 保护规则 4.4：发现已有渲染任务正在运行时，绝不开始测试
    if current_project is not None:
        try:
            render_state = current_project.IsRenderingInProgress()
        except Exception as exc:
            return {"ok":False,"verdict":"BLOCKED","failure_code":"HOST_STATE_UNKNOWN","reason":f"无法确认宿主空闲: {exc}"}
        if render_state is True:
            return {"ok":False,"verdict":"BLOCKED","failure_code":"HOST_BUSY",
                    "reason":f"当前工程 '{current_project_name}' 正在渲染，拒绝抢占宿主"}
        if render_state is not False:
            return {"ok":False,"verdict":"BLOCKED","failure_code":"HOST_STATE_UNKNOWN",
                    "reason":f"当前工程 '{current_project_name}' 的渲染状态未知，拒绝抢占宿主"}

    # 保护所有当前工程；只有明确切换授权或自动保存授权才允许继续。
    allow_switch = bool(payload.get("allow_project_switch", False))
    if (
        current_project_name
        and not allow_switch
        and payload.get("save_current_project") is not True
    ):
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "USER_PROJECT_ACTIVE",
            "reason": (
                f"当前打开了用户工程 '{current_project_name}'。"
                "为防止未保存工作丢失，请先在 Resolve 中保存并关闭/切换至空白工程，或显式确认允许切换。"
            ),
            "data": {
                "resolve_version": version_str,
                "active_project": current_project_name,
            },
        }

    constraints = payload.get("environment_constraints", {})
    def version_parts(value):
        return tuple(int(v) for v in re.findall(r"\d+", str(value))[:3])
    if constraints.get("resolve_version_min") and version_parts(version_str) < version_parts(constraints["resolve_version_min"]):
        return {"ok":False,"verdict":"BLOCKED","failure_code":"ENVIRONMENT_MISMATCH","reason":"Resolve 版本低于契约要求"}
    if constraints.get("platform") == "macOS-arm64" and (platform.system() != "Darwin" or platform.machine() != "arm64"):
        return {"ok":False,"verdict":"BLOCKED","failure_code":"ENVIRONMENT_MISMATCH","reason":"平台不符合 macOS-arm64 契约"}
    save_record = {"status": "NOT_REQUESTED"}
    if payload.get("save_current_project") is True:
        save_record = {"status": "NO_CURRENT_PROJECT"}
        if current_project is not None:
            if current_project_name in ("Untitled Project", "未命名项目") and current_project.GetTimelineCount() == 0:
                save_record = {"status": "SKIPPED_EMPTY_UNTITLED", "project_name": current_project_name}
            else:
                try:
                    before_id = current_project.GetUniqueId()
                    if not isinstance(before_id, str) or not before_id:
                        raise ValueError("无法读取当前工程唯一身份")
                    saved = pm.SaveProject()
                    save_record = {"status": "SAVED" if saved is True else "FAILED",
                                   "project_name": current_project_name, "project_id": before_id,
                                   "api_return": saved is True}
                    if saved is not True:
                        return {"ok": False, "verdict": "BLOCKED", "failure_code": "PROJECT_SAVE_FAILED",
                                "reason": "SaveProject 未返回 True；不切换、不自动命名或覆盖工程",
                                "data": {"save_current_project": save_record}}
                    after = pm.GetCurrentProject()
                    if after is None or after.GetUniqueId() != before_id:
                        return {"ok": False, "verdict": "BLOCKED", "failure_code": "ENVIRONMENT_CHANGED",
                                "reason": "保存后工程身份发生变化，停止切换",
                                "data": {"save_current_project": save_record}}
                    after_render_state = after.IsRenderingInProgress()
                    if after_render_state is True:
                        return {"ok": False, "verdict": "BLOCKED", "failure_code": "HOST_BUSY",
                                "reason": "保存后宿主开始渲染，停止切换",
                                "data": {"save_current_project": save_record}}
                    if after_render_state is not False:
                        return {"ok": False, "verdict": "BLOCKED", "failure_code": "HOST_STATE_UNKNOWN",
                                "reason": "保存后无法确认宿主空闲，停止切换",
                                "data": {"save_current_project": save_record}}
                except Exception as exc:
                    return {"ok": False, "verdict": "BLOCKED", "failure_code": "PROJECT_SAVE_UNCONFIRMED",
                            "reason": f"自动保存或确认失败，未授权继续切换: {exc}",
                            "data": {"save_current_project": save_record}}
    return {
        "ok": True,
        "verdict": "PASS",
        "data": {
            "resolve_version": version_str,
            "active_project": current_project_name,
            "current_page": resolve.GetCurrentPage(),
            "save_current_project": save_record,
        },
    }


def action_import_copy(payload: Dict[str, Any]) -> Dict[str, Any]:
    """阶段 IMPORT_COPY：将 template.drp 导入为独立工程副本，绝不覆盖原模板或同名工程。"""
    resolve = connect_resolve()
    if resolve is None:
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "CONTROL_CONNECTION_LOST",
            "reason": "导入阶段丢失 Resolve 控制连接",
        }

    drp_path = payload["drp_path"]
    copy_name = payload["project_copy_name"]

    if not os.path.isfile(drp_path):
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "MISSING_DRP",
            "reason": f"DRP 模板文件不存在: {drp_path}",
        }

    expected_hash = payload.get("expected_drp_sha256")
    if expected_hash:
        digest = hashlib.sha256()
        with open(drp_path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            return {"ok": False, "failure_code": "DRP_CHANGED", "reason": "DRP 在盘点后变化，未导入"}
    pm = resolve.GetProjectManager()

    # 检查工程名是否冲突，决不删除或覆盖已有工程
    existing_projects = pm.GetProjectListInCurrentFolder() or []
    if copy_name in existing_projects:
        return {
            "ok": False,
            "verdict": "BLOCKED",
            "failure_code": "PROJECT_NAME_CONFLICT",
            "reason": f"目标工程副本名称已存在，拒绝覆盖: {copy_name}",
        }

    imported = pm.ImportProject(drp_path, copy_name)
    if not imported:
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "IMPORT_PROJECT_FAILED",
            "reason": f"ImportProject 调用返回 False (drp={drp_path}, name={copy_name})",
        }

    project = pm.LoadProject(copy_name)
    if project is None:
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "LOAD_PROJECT_FAILED",
            "reason": f"导入后无法加载工程副本: {copy_name}",
        }

    return {
        "ok": True,
        "verdict": "PASS",
        "data": {
            "loaded_project_name": project.GetName(),
            "drp_source": drp_path,
        },
    }


def action_render(payload: Dict[str, Any]) -> Dict[str, Any]:
    """阶段 RENDER：仅设置允许的导出目录与前缀，提交单个渲染任务并轮询完成。"""
    resolve = connect_resolve()
    if resolve is None:
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "CONTROL_CONNECTION_LOST",
            "reason": "渲染提交阶段丢失 Resolve 控制连接",
        }

    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None or project.GetName() != payload["expected_project_name"]:
        return {
            "ok": False,
            "verdict": "INCONCLUSIVE",
            "failure_code": "ENVIRONMENT_CHANGED",
            "reason": "渲染前检测到当前活动工程被切换",
        }

    if project.IsRenderingInProgress() is not False:
        return {
            "ok": False,
            "verdict": "INCONCLUSIVE",
            "failure_code": "ENVIRONMENT_CHANGED",
            "reason": "检测到其它渲染任务正在运行",
        }

    export_rules = payload["export_rules"]
    target_dir = payload["target_dir"]
    custom_name = payload["custom_name"]
    poll_interval = float(payload.get("poll_interval_sec", 1.0))
    max_wait_sec = float(payload.get("timeout_sec", 600.0))

    os.makedirs(target_dir, exist_ok=True)

    # 1. 若契约声明了渲染预设，优先加载渲染预设
    preset_name = export_rules.get("render_preset")
    if preset_name:
        if not project.LoadRenderPreset(str(preset_name)):
            return {
                "ok": False,
                "verdict": "FAIL",
                "failure_code": "LOAD_RENDER_PRESET_FAILED",
                "reason": f"LoadRenderPreset('{preset_name}') 调用失败",
            }

    # 2. 按契约设置 Single clip (1) 或 Individual clips (0)
    render_mode_str = str(export_rules.get("render_mode", "single_clip"))
    render_mode_int = 1 if render_mode_str == "single_clip" else 0
    if not export_rules.get("preserve_current_settings") or "render_mode" in export_rules:
        if not project.SetCurrentRenderMode(render_mode_int):
            return {"ok": False, "failure_code": "SET_RENDER_MODE_FAILED", "reason": "无法设置渲染模式"}

    # 3. 按契约设置导出容器格式与编码 (因为 .drp 默认不持久化 Deliver 临时面板格式)
    fmt = export_rules.get("format")
    codec = export_rules.get("codec")
    if fmt and codec:
        if not project.SetCurrentRenderFormatAndCodec(str(fmt), str(codec)):
            return {
                "ok": False,
                "verdict": "FAIL",
                "failure_code": "SET_FORMAT_CODEC_FAILED",
                "reason": f"SetCurrentRenderFormatAndCodec('{fmt}', '{codec}') 调用失败",
            }

    # Verify that a successful setter selected the requested format/codec.
    if fmt and codec:
        actual = project.GetCurrentRenderFormatAndCodec() or {}
        if actual.get("format") != fmt or actual.get("codec") != codec:
            return {"ok": False, "failure_code": "EXPORT_FORMAT_READBACK_MISMATCH",
                    "reason": f"请求={fmt}/{codec}; 实际={actual}"}

    # 4. 设置输出位置、文件名前缀及契约规定的范围参数
    render_settings: Dict[str, Any] = {
        "TargetDir": target_dir,
        "CustomName": custom_name,
        "ExportVideo": True,
        "ExportAudio": bool(export_rules.get("export_audio", False)),
    }
    if export_rules.get("preserve_current_settings"):
        render_settings.pop("ExportVideo")
        render_settings.pop("ExportAudio")
    if export_rules.get("color_space_tag"):
        render_settings["ColorSpaceTag"] = str(export_rules["color_space_tag"])
    if export_rules.get("gamma_tag"):
        render_settings["GammaTag"] = str(export_rules["gamma_tag"])

    if export_rules.get("mark_in") is not None and export_rules.get("mark_out") is not None:
        render_settings["MarkIn"] = int(export_rules["mark_in"])
        render_settings["MarkOut"] = int(export_rules["mark_out"])
        render_settings["SelectAllFrames"] = False
    else:
        render_settings["SelectAllFrames"] = True

    # Only export representation controls; output paths/range remain executor-owned.
    extra = export_rules.get("image_settings", {})
    allowed = {"FormatWidth", "FormatHeight", "ExportAlpha", "AlphaMode", "VideoQuality"}
    if not isinstance(extra, dict) or set(extra) - allowed:
        return {"ok": False, "failure_code": "INVALID_IMAGE_SETTINGS", "reason": "image_settings 仅支持尺寸、alpha、JPEG质量控制"}
    render_settings.update(extra)
    if not project.SetRenderSettings(render_settings):
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "SET_RENDER_SETTINGS_FAILED",
            "reason": f"SetRenderSettings 调用失败: {render_settings}",
        }

    # 保护规则 4.4：绝不调用 DeleteAllRenderJobs 清空队列，仅记录并启动本次新增的 job_id
    job_id = project.AddRenderJob()
    if not job_id:
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "ADD_RENDER_JOB_FAILED",
            "reason": "AddRenderJob 返回空 ID",
        }

    started = project.StartRendering([job_id], isInteractiveMode=False)
    if not started:
        return {
            "ok": False,
            "verdict": "FAIL",
            "failure_code": "START_RENDERING_FAILED",
            "reason": f"StartRendering([{job_id}]) 返回 False",
        }

    start_ts = time.monotonic()
    last_status: Dict[str, Any] = {}

    while True:
        control_file = payload.get("control_file")
        if control_file and os.path.isfile(control_file):
            with open(control_file, encoding="utf-8") as stream:
                requested = json.load(stream)
            if requested.get("stop_current"):
                project.StopRendering()
                return {"ok": False, "verdict": "FAIL", "failure_code": "STOP_REQUESTED",
                        "reason": "Agent 请求停止当前任务；尚需核验宿主空闲",
                        "data": {"job_id": job_id, "last_status": last_status,
                                 "elapsed_sec": time.monotonic() - start_ts}}
        elapsed = time.monotonic() - start_ts
        if elapsed > max_wait_sec:
            try:
                project.StopRendering()
            except Exception:
                pass
            return {
                "ok": False,
                "verdict": "FAIL",
                "failure_code": "TIMEOUT",
                "reason": f"渲染任务 {job_id} 超过单轮超时上限 ({max_wait_sec}s)",
                "data": {"job_id": job_id, "last_status": last_status, "elapsed_sec": elapsed},
            }

        # 检查当前工程是否被中途篡改
        active_proj = pm.GetCurrentProject()
        if active_proj is None or active_proj.GetName() != payload["expected_project_name"]:
            return {
                "ok": False,
                "verdict": "INCONCLUSIVE",
                "failure_code": "ENVIRONMENT_CHANGED",
                "reason": "渲染期间活动工程被外部切换",
                "data": {"job_id": job_id, "last_status": last_status},
            }

        try:
            last_status = project.GetRenderJobStatus(job_id) or {}
        except Exception as exc:
            return {
                "ok": False,
                "verdict": "FAIL",
                "failure_code": "CONTROL_CONNECTION_LOST",
                "reason": f"查询渲染状态时丢失连接: {exc}",
                "data": {"job_id": job_id},
            }

        if payload.get("status_run_dir"):
            from live_status import write_snapshot
            write_snapshot(payload["status_run_dir"], "RENDER", {
                "phase_state":"running", "job_id":job_id,
                "job_status":last_status,
                "completion_percentage":last_status.get("CompletionPercentage"),
                "stage_elapsed_sec":round(time.monotonic()-start_ts,3)})
        job_state = str(last_status.get("JobStatus", ""))
        if job_state == "Complete":
            return {
                "ok": True,
                "verdict": "PASS",
                "data": {
                    "job_id": job_id,
                    "job_status": last_status,
                    "elapsed_sec": round(time.monotonic() - start_ts, 3),
                    "target_dir": target_dir,
                },
            }
        if job_state in ("Failed", "Cancelled", "Background Render Cancelled", "Remote Render Cancelled"):
            return {
                "ok": False,
                "verdict": "FAIL",
                "failure_code": "RENDER_FAILED",
                "reason": f"Resolve 报告渲染任务失败或取消: {last_status}",
                "data": {
                    "job_id": job_id,
                    "job_status": last_status,
                    "elapsed_sec": round(time.monotonic() - start_ts, 3),
                },
            }

        time.sleep(poll_interval)


def action_inventory(payload):
    resolve = connect_resolve()
    if resolve is None:
        return {"ok": False, "failure_code": "CONTROL_CONNECTION_LOST", "reason": "无法连接 Resolve"}
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None or project.GetName() != payload["expected_project_name"]:
        return {"ok": False, "failure_code": "ENVIRONMENT_CHANGED", "reason": "活动工程变化"}
    if project.IsRenderingInProgress() is not False:
        return {"ok": False, "failure_code": "HOST_STATE_UNKNOWN", "reason": "无法确认工程空闲"}
    return {"ok": True, "data": {
        "timelines": [project.GetTimelineByIndex(i).GetName() for i in range(1, project.GetTimelineCount()+1)],
        "format_codec": project.GetCurrentRenderFormatAndCodec(),
        "render_mode": project.GetCurrentRenderMode(),
        "render_jobs": project.GetRenderJobList(),
        "settings_source": "observed_imported_project_not_certified_template"}}


def action_render_selected(payload):
    inventory = action_inventory(payload)
    if not inventory.get("ok"):
        return inventory
    resolve = connect_resolve()
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetTimelineByIndex(int(payload["timeline_index"]))
    if timeline is None or not project.SetCurrentTimeline(timeline):
        return {"ok": False, "failure_code": "SELECT_TIMELINE_FAILED", "reason": "无法选中指定时间线"}
    from plugin_presence import inspect_timeline
    presence = inspect_timeline(project, timeline)
    if payload.get("status_run_dir"):
        with open(os.path.join(payload["status_run_dir"], "plugin_presence.json"), "w", encoding="utf-8") as stream:
            json.dump(presence, stream, ensure_ascii=False, indent=2)
    if presence["status"] != "PRESENT":
        return {"ok": False, "verdict": "SKIPPED" if presence["status"] == "ABSENT" else "BLOCKED",
                "failure_code": "NO_TRUECUTFLASH_NODE" if presence["status"] == "ABSENT" else "PLUGIN_INSPECTION_UNAVAILABLE",
                "reason": "Color 节点中未发现 TrueCut Flash" if presence["status"] == "ABSENT" else "无法完整读取 Color 节点，未渲染",
                "data": {"plugin_presence": presence}}
    rules = dict(payload["export_rules"])
    # Resolve format/codec from SDK inventory rather than incidental Deliver state.
    from export_policy import sdk_export_rules, resolve_export_rules
    try:
        rules = sdk_export_rules(rules, payload.get("sdk_dir"))
        rules = resolve_export_rules(project, rules)
    except ValueError as exc:
        return {"ok": False, "failure_code": str(exc).split(":", 1)[0], "reason": str(exc)}
    rules.setdefault("preserve_current_settings", rules.get("settings_source") == "imported_current")
    if rules.get("render_preset"):
        if not project.LoadRenderPreset(rules["render_preset"]):
            return {"ok": False, "failure_code": "LOAD_RENDER_PRESET_FAILED", "reason": "指定预设不存在或加载失败"}
        rules.pop("render_preset")
    observed = project.GetCurrentRenderFormatAndCodec() or {}
    if not rules.get("format") and not (observed.get("format") and observed.get("codec")):
        return {"ok": False, "failure_code": "EXPORT_SETTINGS_UNAVAILABLE", "reason": "无法读取可用导出格式和编码"}
    actual_payload = dict(payload, export_rules=rules)
    before = {
        "format_codec": observed, "render_mode": project.GetCurrentRenderMode(),
        "timeline": timeline.GetName(), "timeline_start": timeline.GetStartFrame(),
        "timeline_end_exclusive": timeline.GetEndFrame(), "export_rules": rules}
    result = action_render(actual_payload)
    result.setdefault("data", {})["plugin_presence"] = presence
    result["data"]["observed_before_render"] = before
    if payload.get("status_run_dir"):
        from reporting import write_json
        write_json(os.path.join(payload["status_run_dir"], "render_result.json"), result)
    if not result.get("ok") and project.IsRenderingInProgress() is not False:
        result.update(host_state="UNKNOWN", recovery_reason="渲染失败后无法确认宿主空闲")
    return result


def action_close_project(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Close only the owned test copy, then verify the active project changed."""
    resolve = connect_resolve()
    if resolve is None:
        return {"ok": False, "failure_code": "CONTROL_CONNECTION_LOST", "reason": "无法连接 Resolve"}
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    expected = payload.get("expected_project_name")
    if not expected or not expected.startswith("EVAL_RUN_"):
        return {"ok": False, "failure_code": "USER_PROJECT_ACTIVE", "reason": "未声明本次测试副本，拒绝关闭"}
    if project is None:
        return {"ok": True, "data": {"status": "ALREADY_CLOSED"}}
    if project.GetName() != expected:
        return {"ok": False, "failure_code": "ENVIRONMENT_CHANGED", "reason": "活动工程不是本次测试副本"}
    if project.IsRenderingInProgress() is not False:
        return {"ok": False, "failure_code": "HOST_STATE_UNKNOWN", "reason": "未确认空闲，不关闭工程"}
    returned = pm.CloseProject(project)
    current = pm.GetCurrentProject()
    if not returned or (current and current.GetName() == expected):
        return {"ok": False, "failure_code": "CLOSE_PROJECT_UNCONFIRMED", "reason": "CloseProject 后活动工程未确认改变"}
    return {"ok": True, "verdict": "PASS", "data": {"status": "CLOSED", "verified": True}}


def action_ready_check(payload: Dict[str, Any]) -> Dict[str, Any]:
    """就绪态等待（Ready / Idle Check）：冷却和状态轮询，确认主线程正常响应。"""
    cooldown = float(payload.get("cooldown_sec", 0.0))
    if cooldown > 0:
        time.sleep(cooldown)

    timeout_sec = float(payload.get("timeout_sec", 10.0))
    poll_interval = float(payload.get("poll_interval_sec", 1.0))
    deadline = time.monotonic() + timeout_sec

    last_error = "未轮询"
    while time.monotonic() < deadline:
        resolve = connect_resolve()
        if resolve is None:
            last_error = "无法连接 Resolve"
            time.sleep(poll_interval)
            continue

        try:
            pm = resolve.GetProjectManager()
            if pm is None:
                last_error = "无法获取 ProjectManager"
                time.sleep(poll_interval)
                continue

            db = pm.GetCurrentDatabase()
            proj_list = pm.GetProjectListInCurrentFolder()

            curr_proj = pm.GetCurrentProject()
            if curr_proj and curr_proj.IsRenderingInProgress() is not False:
                last_error = "宿主仍处于渲染进行中"
                time.sleep(poll_interval)
                continue

            if proj_list is not None:
                return {
                    "ok": True,
                    "verdict": "PASS",
                    "data": {
                        "database": db,
                        "project_count": len(proj_list),
                        "current_page": resolve.GetCurrentPage(),
                    }
                }
        except Exception as exc:
            last_error = str(exc)

        time.sleep(poll_interval)

    return {"ok": False, "verdict": "FAIL", "failure_code": "HOST_NOT_READY", "reason": f"等待宿主就绪超时: {last_error}"}


ACTION_MAP = {
    "inventory": action_inventory,
    "render_selected": action_render_selected,
    "preflight": action_preflight,
    "import_copy": action_import_copy,
    "render": action_render,
    "close_project": action_close_project,
    "ready_check": action_ready_check,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic DaVinci Resolve Worker")
    parser.add_argument("--action", required=True, choices=list(ACTION_MAP.keys()))
    parser.add_argument("--payload-file", required=True, help="Path to JSON input payload")
    args = parser.parse_args()

    with open(args.payload_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    handler = ACTION_MAP[args.action]
    result = handler(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
