"""DRP-first serial runner. Comparison readiness never gates rendering."""
import argparse
import copy
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
LOCK = '/tmp/davinci_tcflash_eval_executor.lock'


def select_timelines(names, policy='all'):
    if not names:
        raise ValueError('NO_TIMELINE: 工程无时间线')
    if policy == 'all':
        return list(range(1, len(names) + 1))
    if policy == 'single':
        if len(names) != 1:
            raise ValueError('TIMELINE_POLICY_REQUIRED: 多时间线请指定 all 或名称列表')
        return [1]
    if not isinstance(policy, list) or not policy or len(set(policy)) != len(policy):
        raise ValueError('无效 timeline_policy')
    if any(names.count(n) != 1 for n in policy):
        raise ValueError('时间线名称不存在或不唯一；请核对 inventory.json')
    return [names.index(n) + 1 for n in policy]


def render_requirements(rules):
    errors = []
    if not isinstance(rules, dict):
        return ['export 必须为对象']
    if rules.get('render_mode', 'single_clip') not in ('single_clip', 'individual_clips'):
        errors.append('不支持的 render_mode')
    if not (rules.get('render_preset') or (rules.get('format') and rules.get('codec'))
            or rules.get('settings_source') in ('imported_current', 'sdk_aligned')):
        errors.append('EXPORT_POLICY_REQUIRED: 声明 sdk_aligned、显式预设或 format/codec（压力测试可显式 imported_current）')
    if (rules.get('mark_in') is None) != (rules.get('mark_out') is None):
        errors.append('mark_in / mark_out 必须同时提供')
    if rules.get('mark_in') is not None and int(rules['mark_out']) < int(rules['mark_in']):
        errors.append('导出范围颠倒')
    return errors


def comparison_case(task):
    case = copy.deepcopy(task.get('comparison'))
    if case is None:
        return None
    rules = case.setdefault('comparison_rules', {})
    if rules.get('pixel_domain', {}).get('channels') not in ('RGB', 'RGBA'):
        raise ValueError('比较须明确 RGB/RGBA 像素域')
    if any(float(v) != 0 for v in rules.get('tolerances', {}).values()):
        raise ValueError('严格一致性要求零容差；现有契约冲突，未修改')
    rules['tolerances'] = {
        key: 0 for key in ('max_abs_error_limit', 'mean_abs_error_limit',
                          'outlier_pixel_threshold', 'max_outlier_pixel_ratio')}
    return case


from reporting import write_json, summarize


def invoke(action, payload, directory, timeout=120, evidence=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    operations = directory / 'operations'
    operations.mkdir(exist_ok=True)
    token = f'{len(list(operations.glob("*.input.json"))) + 1:03d}_{action}'
    payload_path = operations / (token + '.input.json')
    write_json(payload_path, payload)
    if evidence:
        cmd = [sys.executable, str(HERE / 'evidence_worker.py'), action]
    else:
        cmd = [sys.executable, str(HERE / 'resolve_worker.py'), '--action', action,
               '--payload-file', str(payload_path)]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=json.dumps(payload) if evidence else None,
                              text=True, capture_output=True, timeout=timeout)
        (operations / (token + '.log')).write_text(proc.stdout + '\nSTDERR:\n' + proc.stderr)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        if proc.returncode != (0 if result.get('ok') else 1):
            raise ValueError('退出码与结果矛盾')
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError, TypeError, AttributeError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            def text(value):
                return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
            (operations / (token + '.log')).write_text(text(exc.stdout) + '\nSTDERR:\n' + text(exc.stderr))
        result = {'ok': False, 'verdict': 'ERROR', 'failure_code': 'WORKER_STATE_UNKNOWN', 'reason': str(exc)}
        checkpoint = directory / 'render_result.json'
        if action == 'render_selected' and checkpoint.is_file():
            try:
                original = json.loads(checkpoint.read_text())
                if isinstance(original, dict) and original.get('ok') is False:
                    result = dict(original, host_state='UNKNOWN', worker_failure_code='WORKER_STATE_UNKNOWN',
                                  recovery_reason=str(exc))
            except (ValueError, OSError): pass
    result['worker_wall_sec'] = round(time.monotonic() - started, 3)
    write_json(operations / (token + '.result.json'), result)
    with (directory / 'worker.log').open('a') as stream:
        stream.write(action + ': ' + json.dumps(result, ensure_ascii=False) + '\n')
    with (directory / 'events.jsonl').open('a') as stream:
        stream.write(json.dumps({'time': datetime.datetime.now().astimezone().isoformat(),
                                 'action': action, 'result': result}, ensure_ascii=False) + '\n')
    from live_status import write_snapshot
    write_snapshot(directory, action, {'phase_state': 'finished', 'result': result})
    return result


def unsafe(result):
    return result.get('host_state') == 'UNKNOWN' or result.get('failure_code') in {
        'WORKER_STATE_UNKNOWN', 'CONTROL_CONNECTION_LOST', 'HOST_STATE_UNKNOWN',
        'HOST_BUSY', 'ENVIRONMENT_CHANGED', 'TIMEOUT', 'SAVE_PROJECT_FAILED',
        'USER_PROJECT_ACTIVE', 'PROJECT_SAVE_FAILED'} or result.get('verdict') == 'ERROR'


def _hash(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def slug(value):
    import re
    return re.sub(r'[^\w.-]+', '_', value).strip('._')[:96] or 'project'


def control(root):
    path = root / 'control.json'
    return json.loads(path.read_text()) if path.exists() else {}


def run(manifest, root, call=invoke, resume=False, retry_items=()):
    root = Path(root).resolve()
    tasks = manifest['projects']
    if not tasks or len({t['id'] for t in tasks}) != len(tasks):
        raise ValueError('projects 不能为空，id 必须唯一')
    for task in tasks:
        inputs = [(k, task.get(k)) for k in ('drp_path', 'sdk_dir')]
        inputs.extend(('sdk_dir', v.get('sdk_dir')) for v in task.get('timeline_comparisons', {}).values())
        for key, value in inputs:
            if value:
                if not Path(value).is_absolute(): raise ValueError('输入路径必须为绝对路径')
                source = Path(value).resolve()
                protected = source.parent if key == 'drp_path' else source
                if root == protected or protected in root.parents or root in protected.parents:
                    raise ValueError('输出目录与输入区域重叠')
    if resume:
        if json.loads((root / 'manifest.json').read_text()) != manifest:
            raise ValueError('恢复必须使用本批原始 manifest；新测试使用新批次')
        rows = json.loads((root / 'report.json').read_text())['items']
        for row in rows:
            if row.get('render_status') == 'RUNNING':
                row.update(status='ERROR', render_status='INTERRUPTED', reason='前控制器退出，尝试结果未确认')
                if row.get('attempts'):
                    row['attempts'][-1].update(status='ERROR', result={'failure_code': 'CONTROLLER_INTERRUPTED'}, reason=row['reason'])
        if set(retry_items) - {r['item_id'] for r in rows}:
            raise ValueError('retry item 不存在')
        # Check before autosave/import. Do not touch an unknown host merely to resume.
        ready = call('ready_check', {'timeout_sec': 10}, root / 'logs', 15)
        if not ready.get('ok'):
            summarize(root, rows, 'PAUSED', '恢复前无法确认宿主空闲')
            return rows
        write_json(root / 'control.json', {})
    else:
        root.mkdir(parents=True, exist_ok=False)
        (root / 'logs').mkdir()
        (root / 'items').mkdir()
        write_json(root / 'manifest.json', manifest)
        rows = [{'project_id': t['id'], 'item_id': f'{i+1:03d}_{slug(t["id"])}',
                 'drp_path': str(Path(t['drp_path']).resolve()), 'status': 'NOT_RUN',
                 'render_status': 'NOT_STARTED', 'compare_status': 'NOT_STARTED',
                 'render_elapsed_sec': None, 'render_poll_wall_sec': None, 'executor_total_sec': None,
                 'attempts': []} for i, t in enumerate(tasks)]
    summarize(root, rows)
    host_ready = False
    paused = False
    pause_reason = ''

    def check_pause():
        return bool(control(root).get('pause'))

    for number, task in enumerate(tasks):
        group = [r for r in rows if r['project_id'] == task['id']]
        pending = [r for r in group if r['status'] == 'NOT_RUN' or r['item_id'] in retry_items]
        if not pending: continue
        if check_pause():
            paused, pause_reason = True, '收到调度暂停请求'; break
        folder = root / 'logs' / f'{number+1:03d}_{slug(task["id"])}'
        folder.mkdir(parents=True, exist_ok=True)
        source = Path(task['drp_path']).resolve()
        try:
            with source.open('rb') as stream: digest = _hash(stream)
            if any(r.get('drp_sha256', digest) != digest for r in group):
                raise ValueError('DRP_CHANGED: 恢复时原始 DRP 已变化')
            rules = task.get('export', manifest.get('export', {'settings_source': 'sdk_aligned' if task.get('sdk_dir') else 'imported_current'}))
            errors = render_requirements(rules)
            for key in ('render_timeout_sec', 'compare_timeout_sec'):
                value = float(task.get(key, 3600))
                if not math.isfinite(value) or value <= 0: errors.append(key + ' 必须为有限正数')
            if errors: raise ValueError('; '.join(errors))
        except (OSError, ValueError, TypeError) as exc:
            for row in pending: row.update(status='SKIPPED', reason=str(exc), failure_code='INPUT_INVALID')
            summarize(root, rows); continue
        for row in group: row['drp_sha256'] = digest
        if not host_ready:
            local = json.loads((HERE.parent / 'references/local-execution-policy.json').read_text())
            policy = manifest.get('project_policy', local['project_policy'])
            ready = call('preflight', {'save_current_project': policy == 'save_then_switch'}, folder)
        else:
            ready = call('ready_check', {'timeout_sec': 10}, folder, 15)
        if not ready.get('ok'):
            paused, pause_reason = True, ready.get('reason', '无法确认宿主就绪')
            pending[0].update(status='ERROR', reason=pause_reason, preflight=ready)
            break
        host_ready = True
        name = 'EVAL_RUN_' + uuid.uuid4().hex
        imported = call('import_copy', {'drp_path': str(source), 'project_copy_name': name,
                                      'expected_drp_sha256': digest}, folder)
        if not imported.get('ok'):
            for row in pending: row.update(status='ERROR', reason=imported.get('reason', '导入失败'),
                                            failure_code=imported.get('failure_code'), import_result=imported)
            # A failed import may leave a partially loaded project. Verify before next dispatch.
            ready = call('ready_check', {'timeout_sec': 10}, folder, 15)
            if not ready.get('ok'):
                paused, pause_reason = True, '导入失败且宿主未确认恢复'; break
            summarize(root, rows); continue
        inventory = call('inventory', {'expected_project_name': name}, folder)
        write_json(folder / 'inventory.json', inventory)
        if not inventory.get('ok'):
            pending[0].update(status='ERROR', reason=inventory.get('reason', '盘点失败'), evidence={'inventory': inventory})
            paused, pause_reason = True, '工程盘点失败，需诊断'; break
        names = inventory['data']['timelines']
        try:
            selected = select_timelines(names, task.get('timeline_policy', manifest.get('timeline_policy', 'all')))
        except ValueError as exc:
            for row in pending: row.update(status='SKIPPED', reason=str(exc))
            selected = []
        if selected and 'timeline_index' not in group[0]:
            parent = group[0]; position = rows.index(parent); rows.remove(parent); group = []
            for idx in selected:
                row = copy.deepcopy(parent)
                row.update(item_id=f'{number+1:03d}_{slug(task["id"])}__t{idx:02d}',
                           timeline=names[idx-1], timeline_index=idx)
                row['run_dir'] = str(root / 'items' / row['item_id'])
                rows.insert(position + len(group), row); group.append(row)
        for row in group:
            if row['status'] != 'NOT_RUN' and row['item_id'] not in retry_items: continue
            if row.get('timeline_index') not in selected: continue
            if names[row['timeline_index']-1] != row['timeline']:
                paused, pause_reason = True, '恢复时实测时间线清单变化'; break
            if check_pause():
                paused, pause_reason = True, '收到调度暂停请求'; break
            out = root / 'items' / row['item_id']; out.mkdir(parents=True, exist_ok=True)
            row['run_dir'] = str(out)
            attempt_no = len(row['attempts']) + 1
            if attempt_no > int(manifest.get('max_attempts', 2)):
                row['reason'] = '达到单项尝试上限；需明确调整预算后另建批次'; continue
            attempt_name = f'attempt_{attempt_no:02d}'
            logs = out / 'logs' / attempt_name; logs.mkdir(parents=True, exist_ok=False)
            render_dir = out / 'render' / attempt_name
            render_dir.parent.mkdir(exist_ok=True)
            compare_dir = out / 'comparison' / attempt_name
            row.update(paths={'render': str(render_dir), 'comparison': str(compare_dir), 'logs': str(logs)},
                       declared={'export': rules, 'sdk_dir': task.get('sdk_dir'),
                                 'timeline_comparison': task.get('timeline_comparisons', {}).get(row['timeline'])},
                       status='NOT_RUN', render_status='RUNNING', compare_status='NOT_STARTED', evidence={})
            attempt = {'number': attempt_no, 'status': 'RUNNING', 'logs': str(logs), 'project_copy_name': name,
                       'started_at': datetime.datetime.now().astimezone().isoformat()}
            row['attempts'].append(attempt)
            started = time.monotonic(); summarize(root, rows)
            timeout = float(task.get('render_timeout_sec', 3600))
            rendered = call('render_selected', {'expected_project_name': name, 'timeline_index': row['timeline_index'],
                       'export_rules': rules, 'sdk_dir': task.get('timeline_comparisons', {}).get(row['timeline'], {}).get('sdk_dir', task.get('sdk_dir')), 'target_dir': str(render_dir), 'custom_name': 'output',
                       'status_run_dir': str(logs), 'control_file': str(root / 'control.json'), 'timeout_sec': timeout},
                       logs, timeout + 30)
            row['evidence']['render'] = rendered
            row['render_poll_wall_sec'] = rendered.get('data', {}).get('elapsed_sec')
            row['render_elapsed_sec'] = None
            row['failure_code'] = rendered.get('failure_code')
            if not rendered.get('ok'):
                skipped = rendered.get('failure_code') == 'NO_TRUECUTFLASH_NODE'
                inspection = rendered.get('failure_code') == 'PLUGIN_INSPECTION_UNAVAILABLE'
                row.update(status='SKIPPED' if skipped else 'ERROR' if unsafe(rendered) or inspection else 'FAIL',
                           render_status='SKIPPED' if skipped else 'FAILED', reason=rendered.get('reason', '渲染失败'))
                if unsafe(rendered) or inspection:
                    paused, pause_reason = True, '宿主或插件检查需要 Agent 诊断；原始错误已保留'
            else:
                files = [{'name': str(p.relative_to(render_dir)), 'bytes': p.stat().st_size}
                         for p in render_dir.rglob('*') if p.is_file() and not any(x.startswith('.') for x in p.relative_to(render_dir).parts)]
                row['integrity'] = {'files_count': len(files), 'zero_byte_files': [f['name'] for f in files if not f['bytes']],
                                    'decoded_frame_count': None, 'expected_frame_count': None}
                write_json(logs / 'output_inventory.json', files)
                if not files or any(f['bytes'] == 0 for f in files):
                    row.update(status='ERROR', render_status='OUTPUT_INVALID', reason='任务完成但产物缺失或为空')
                else:
                    row.update(status='RENDERED_NOT_COMPARED', render_status='COMPLETE', compare_status='NOT_READY',
                               reason='缺少明确比较配置；渲染结果已保留')
                    comparison_task = task.get('timeline_comparisons', {}).get(row['timeline'], {}) if len(names) > 1 else task
                    compare_item(row, comparison_task, task, call)
            row['executor_total_sec'] = round(time.monotonic() - started, 3)
            attempt.update(status=row['status'], reason=row.get('reason'), result=copy.deepcopy(rendered),
                           comparison=copy.deepcopy(row['evidence'].get('comparison')),
                           paths=copy.deepcopy(row['paths']), executor_total_sec=row['executor_total_sec'],
                           finished_at=datetime.datetime.now().astimezone().isoformat())
            row['selected_attempt'] = attempt_no
            if control(root).get('stop_current'):
                paused, pause_reason = True, '当前渲染停止请求已处理；等待 Agent 核验'
            summarize(root, rows, 'PAUSED' if paused else 'RUNNING', pause_reason)
            if paused: break
        if paused: break  # never close/switch an unconfirmed host
        closed = call('close_project', {'expected_project_name': name}, folder)
        if not closed.get('ok'):
            paused, pause_reason = True, closed.get('reason', '无法确认测试工程已关闭'); break
    summarize(root, rows, 'PAUSED' if paused else 'COMPLETE', pause_reason)
    return rows


def compare_item(row, comparison_task, task, call=invoke):
    try:
        case = comparison_case(comparison_task)
        if case is None: return
        baseline = comparison_task.get('sdk_dir', task.get('sdk_dir', ''))
        if not baseline or not Path(baseline).is_dir(): raise ValueError('SDK 基准目录缺失')
        directory = Path(row['paths']['comparison'])
        if directory.exists():
            index = 1
            while (directory.parent / f'attempt_{index:02d}').exists(): index += 1
            row['paths']['comparison'] = str(directory.parent / f'attempt_{index:02d}')
        result = call('compare', {'case_data': case, 'resolve_output_dir': row['paths']['render'],
                      'baseline_dir': baseline, 'comparison_out_dir': row['paths']['comparison']},
                      Path(row['paths']['logs']), float(task.get('compare_timeout_sec', 3600)), evidence=True)
        row['evidence']['comparison'] = result
        row['compare_failure_code'] = result.get('failure_code')
        row.setdefault('comparison_attempts', []).append({'directory': row['paths']['comparison'],
                                                        'result': copy.deepcopy(result)})
        verdict = result.get('verdict')
        row['compare_status'] = verdict or 'ERROR'
        row['status'] = verdict if verdict in ('PASS', 'FAIL') else 'RENDERED_NOT_COMPARED'
        row['reason'] = result.get('reason', '')
    except (ValueError, TypeError, KeyError) as exc:
        row['reason'] = '比较配置待补充：' + str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--resume', action='store_true', help='核验空闲后继续本批未执行项')
    parser.add_argument('--retry-item', action='append', default=[], help='显式重试某项，保留首次失败；最多 max_attempts')
    args = parser.parse_args()
    if args.retry_item and not args.resume: parser.error('--retry-item 需要 --resume')
    manifest = json.loads(Path(args.manifest).read_text())
    with open(LOCK, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = run(manifest, Path(args.output), resume=args.resume, retry_items=args.retry_item)
    return 0 if all(r['status'] == 'PASS' for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
