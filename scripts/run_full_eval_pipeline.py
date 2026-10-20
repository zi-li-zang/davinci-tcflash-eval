#!/usr/bin/env python3
"""Convenience discovery wrapper; SDK-aligned export; no inferred frame correspondence or phase."""
import argparse
import datetime
import fcntl
import json
from pathlib import Path
import re
import sys
from render_first import LOCK, run, compare_item
from reporting import summarize


def natural_sort_key(value):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(value))]


def build_manifest(drp_root, sdk_root, project_ids=None):
    files = sorted([p for p in Path(drp_root).glob('*.drp') if not p.name.startswith('.')], key=lambda p: natural_sort_key(p.name))
    if project_ids: files = [p for p in files if p.stem in project_ids]
    if not files: raise ValueError('没有匹配的 DRP')
    # Same-name directory is a project-level lead only, not a verified frame association.
    return {'timeline_policy': 'all', 'export': {'settings_source': 'sdk_aligned'},
            'projects': [{'id': p.stem, 'drp_path': str(p.resolve()),
                          'sdk_dir': str((Path(sdk_root) / p.stem).resolve()),
                          'export': {'settings_source': 'sdk_aligned'}} for p in files]}


def find_sdk_files_for_timeline(sdk_dir, timeline_name='Timeline 1'):
    """Inventory only; the order conveys no frame/eye/JA association."""
    from compare_outputs import image_inventory
    return sorted([Path(p) for p in image_inventory(sdk_dir)], key=natural_sort_key)


def run_post_comparison(output_dir, manifest):
    """Use explicit contracts only. Never construct mappings by sorting."""
    path = Path(output_dir) / 'report.json'
    data = json.loads(path.read_text())
    tasks = {t['id']: t for t in manifest['projects']}
    for row in data['items']:
        if row['render_status'] != 'COMPLETE' or row.get('compare_status') in ('PASS', 'FAIL'): continue
        task = tasks[row['project_id']]
        # Always prefer named association; project-level contract only for one-timeline projects.
        siblings = [r for r in data['items'] if r['project_id'] == row['project_id']]
        contract = task.get('timeline_comparisons', {}).get(row.get('timeline'), {}) if len(siblings) > 1 else task
        compare_item(row, contract, task)
        if row.get('attempts'):
            row['attempts'][-1].update(status=row['status'], comparison=row.get('evidence', {}).get('comparison'))
    summarize(Path(output_dir), data['items'], data['batch_state'], data.get('reason', ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drp-dir', required=True)
    parser.add_argument('--sdk-dir', required=True)
    parser.add_argument('--output-base', required=True)
    parser.add_argument('--output-dir')
    parser.add_argument('--projects', default='')
    parser.add_argument('--skip-render', action='store_true')
    parser.add_argument('--skip-compare', action='store_true')
    args = parser.parse_args()
    if args.skip_render:
        parser.error('单独补比对请读取本批 manifest 调用 run_post_comparison；不从文件排序创建映射')
    manifest = build_manifest(Path(args.drp_dir), Path(args.sdk_dir), args.projects.split(',') if args.projects else None)
    root = Path(args.output_dir or str(Path(args.output_base) / datetime.datetime.now().strftime('batch_%Y%m%d_%H%M%S')))
    with open(LOCK, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = run(manifest, root)
    print(root / 'report.md')
    return 0 if all(r['status'] == 'PASS' for r in rows) else 1


if __name__ == '__main__': sys.exit(main())
