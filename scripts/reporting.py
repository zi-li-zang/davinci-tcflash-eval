"""Unified report projections. Machine records are authoritative; no fabricated measurements."""
import datetime
import json
import sys
import platform
from pathlib import Path


def write_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def table(headers, records):
    def cell(value):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        return str(value if value is not None else '未测量/未知').replace('|', '\\|').replace('\n', ' ')
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(cell(v) for v in row) + ' |' for row in records])


def fields(data):
    return table(['字段', '记录'], data.items())


def link(path, label=None):
    return f'[{label or Path(path).name}](<{path}>)'


def item_report(row):
    ev = row.get('evidence', {})
    render = ev.get('render', {})
    observed = render.get('data', {}).get('observed_before_render', {})
    presence = render.get('data', {}).get('plugin_presence', {})
    comp = ev.get('comparison', {})
    detail = comp.get('data', comp)
    attempts = row.get('attempts', [])
    paths = row.get('paths', {})
    observations_file = Path(row.get('run_dir', '.')) / 'observations.json'
    try:
        observations = json.loads(observations_file.read_text()) if observations_file.is_file() else {}
    except (ValueError, OSError):
        observations = {'read_error': '补充观察文件不可读，请检查 observations.json'}
    worst = sorted(detail.get('per_frame', []), key=lambda r: r.get('max_abs_error', 0), reverse=True)[:3]
    visuals = []
    for artifact in detail.get('visual_artifacts', [])[:3]:
        visuals.append(f"帧 {artifact['output_frame_index']}；差异倍率 {artifact.get('diff_amplification')}；ROI {artifact.get('roi_box')}")
        for key in ('side_by_side_image', 'diff_image', 'roi_crop_image', 'alpha_side_by_side_diff_image', 'temporal_strip_image'):
            image = artifact.get(key)
            if image and Path(image).is_file(): visuals.append(f'![{key}]({image})')
    coverage = detail.get('coverage', {})
    coverage_view = {k: v for k, v in coverage.items() if not isinstance(v, list)}
    for key in ('missing_files', 'extra_resolve_files', 'extra_sdk_files'):
        if key in coverage:
            coverage_view[key + '_count'] = len(coverage[key])
            if coverage[key]: coverage_view[key + '_first_5'] = coverage[key][:5]
    declared = row.get('declared', {})
    config = {'原始 DRP': row['drp_path'], 'DRP SHA-256': row.get('drp_sha256'),
              '实测时间线': observed.get('timeline'), '配置前观察导出格式/编码': observed.get('format_codec'),
              '配置前观察导出模式': observed.get('render_mode'),
              '本次解析后导出请求': observed.get('export_rules'), '实测时间线起点': observed.get('timeline_start'),
              '实测时间线终点（排他）': observed.get('timeline_end_exclusive'),
              '实测插件探测状态': presence.get('status'), '实测插件匹配节点数': len(presence.get('matches', [])),
              '声明导出策略': declared.get('export'), '声明 SDK 目录': declared.get('sdk_dir')}
    metrics = detail.get('metrics', {})
    lines = ['# 当前项详细报告', '', '## 1. 结果概要', '',
             table(['项目 / 时间线', '渲染', '比较', '结论', '原因'], [[row['project_id'] + '/' + row.get('timeline', ''),
                 row['render_status'], row['compare_status'], row['status'], row.get('reason', '')]]), '',
             '结论针对 Resolve 导出后的解码像素，不直接证明插件内部 SDK tensor 一致。', '',
             '## 2. 输入、声明与实测配置', '', fields(config), '',
             '完整插件位置与逐节点证据见 logs/ 当前尝试的 plugin_presence.json；未读取的 JA、算法、timestep、色彩域及设备参数均未知。', '',
             '## 3. 渲染与产物完整性', '',
             fields(row.get('integrity', {'state': '未盘点'})), '',
             table(['渲染引擎耗时(s)', '渲染轮询墙钟(s)', '本次执行墙钟(s)'], [[row.get('render_elapsed_sec'),
                 row.get('render_poll_wall_sec'), row.get('executor_total_sec')]]), '',
             '文件盘点不等于逐帧解码验证；轮询耗时不代表 SDK 推理耗时。', '',
             '## 4. 可比性与映射覆盖', '',
             fields(coverage_view or {'state': row['compare_status'], 'reason': row.get('reason', '')}), '',
             '完整缺失/额外文件列表见 coverage.json，正式文件对应见 frame_mapping.json。未验证相位时不标注真实帧/插值帧。', '',
             '## 5. 严格像素指标', '',
             fields({'一致性分类': detail.get('match_classification'), '实测比较帧数': detail.get('evaluated_frame_count'),
                     '超差帧数': detail.get('failed_frame_count'), **metrics}) if metrics else '未获得可比较指标。', '']
    if worst:
        lines.extend([table(['输出索引', '帧类型', '最大误差', '均值误差'], [[r['output_frame_index'], r['frame_type'], r['max_abs_error'], r['mean_abs_error']] for r in worst]), ''])
    lines.extend(['完整逐帧、RGB/alpha 及辅助 SSIM/PSNR 数据见 comparison/ 对应尝试的 summary.json。', '',
             '## 6. 图像证据与视觉检查', '', '\n\n'.join(visuals) or '未取得图像证据。', '',
             '自动生成图像不代表 Agent 已视觉检查；未记录视觉检查时不自动添加主观评分。',
             fields({'视觉检查记录': observations.get('visual_review', '未记录')}), '',
             '## 7. 异常、恢复与尝试历史', '',
             table(['尝试', '状态', '原始错误码', '原因', '日志'], [[a['number'], a.get('status'),
                 a.get('result', {}).get('failure_code', ''), a.get('reason', ''), link(a['logs'])] for a in attempts]) if attempts else '未发起渲染尝试。', '',
             '## 8. 已证实事实、待验证事项与证据索引', '',
             '- 已记录事实：以上执行器返回结果及实际文件盘点。',
             '- 待验证：未完成的映射、配置或宿主检查；不得将文件名推测写成故障根因。', '',
             fields({k: link(v) for k, v in paths.items()}) if paths else '本项无渲染产物；准备证据见批次 logs/。', '',
             '补充诊断与观察（由 Agent 记录，需含证据路径）：', '', fields(observations) if observations else '未记录补充观察。'])
    return '\n'.join(lines) + '\n'


def summarize(root, rows, state='RUNNING', reason=''):
    root = Path(root)
    parents = {}
    for row in rows:
        parents.setdefault(row['project_id'], []).append(row['status'])
    parents = {k: v[0] if len(set(v)) == 1 else 'PARTIAL' for k, v in parents.items()}
    previous = json.loads((root / 'report.json').read_text()) if (root / 'report.json').exists() else {}
    data = {'schema_version': 2, 'created_at': previous.get('created_at', datetime.datetime.now().astimezone().isoformat()),
            'updated_at': datetime.datetime.now().astimezone().isoformat(), 'batch_state': state,
            'runtime': previous.get('runtime', {'python': sys.executable, 'python_version': platform.python_version(),
                                               'platform': platform.platform(), 'gpu': None}),
            'reason': reason, 'projects': parents, 'items': rows,
            'counts': {s: sum(r['status'] == s for r in rows) for s in
                       ('PASS', 'FAIL', 'SKIPPED', 'ERROR', 'NOT_RUN', 'RENDERED_NOT_COMPARED')}}
    for row in rows:
        if not row.get('run_dir') and row['status'] != 'NOT_RUN':
            row['run_dir'] = str(root / 'items' / row['item_id'])
        if row.get('run_dir'):
            folder = Path(row['run_dir']); folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / 'result.json', row)
            (folder / 'report.md').write_text(item_report(row), encoding='utf-8')
    write_json(root / 'report.json', data)
    failures = [r for r in rows if r['status'] in ('FAIL', 'ERROR', 'SKIPPED') or len(r.get('attempts', [])) > 1]
    lines = ['# TrueCut Flash 批次报告', '', '## 1. 批次概要', '',
             fields({'状态': state, '当前原因': reason, '批次目录': str(root), '创建时间': data['created_at'],
                     '更新时间': data['updated_at'], **data['runtime']}), '',
             '输入及执行策略见 manifest.json；SDK 基准只读。', '',
             '## 2. 进度与计数', '', f'项目数：{len(parents)}；已发现项数：{len(rows)}。未导入项目的时间线数量未知。', '',
             table(list(data['counts']), [list(data['counts'].values())]), '', '## 3. 项目与当前项', '',
             table(['项目 / 时间线', '渲染', '比较', '结论', '尝试数', '轮询(s)', '总墙钟(s)', '原因 / 详细报告'], [
                 [r['project_id'] + '/' + r.get('timeline', '尚未盘点'), r['render_status'], r['compare_status'],
                  r['status'], len(r.get('attempts', [])), r.get('render_poll_wall_sec'), r.get('executor_total_sec'),
                  r.get('reason', '') + (' ' + link(str(Path(r['run_dir']) / 'report.md'), '详细报告') if r.get('run_dir') else '')]
                 for r in rows]), '', '父项目状态：', '', fields(parents), '', '## 4. 失败与恢复', '',
             table(['当前项', '结论', '原因', '尝试数'], [[r['item_id'], r['status'], r.get('reason', ''), len(r.get('attempts', []))] for r in failures]) if failures else '暂未记录失败或恢复尝试。', '',
             '首次失败保留在每项 attempts 中；恢复操作见 logs/ 下的 events.jsonl 与 operations/。PAUSED 表示调度暂停，不能视为宿主已恢复。', '',
             '## 5. 结论与待处理事项', '',
             '仅 PASS 项完成了声明范围内的比较；RENDERED_NOT_COMPARED 只说明已导出。存在其他状态时不能宣称整批通过。',
             '未测量的时间为 null；日志心跳不是宿主健康证明；图像/配置未读取时不作已验证声明。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return data
