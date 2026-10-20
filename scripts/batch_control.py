"""Cooperative control. Requests do not themselves prove host actions completed."""
import argparse
import fcntl
import json
from pathlib import Path
from render_first import LOCK, invoke
from reporting import write_json
from live_status import read_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'pause', 'stop-current', 'recovery-check'])
    parser.add_argument('--batch', required=True)
    args = parser.parse_args()
    root = Path(args.batch).resolve()
    data = json.loads((root / 'report.json').read_text())
    if args.action == 'status':
        # Bounded file reads only, never connect to Resolve beside the controller.
        current = [r for r in data['items'] if r['render_status'] == 'RUNNING']
        for row in current:
            row['live'] = read_snapshot(row['paths']['logs'])
        print(json.dumps({'batch_state': data['batch_state'], 'counts': data['counts'], 'current': current}, ensure_ascii=False))
    elif args.action in ('pause', 'stop-current'):
        # Request file has a single purpose; stop-current also pauses next dispatch.
        write_json(root / 'control.json', {'pause': True, 'stop_current': args.action == 'stop-current'})
        print('请求已写入；请查询 report.json 确认已暂停。正在阻塞的 API 不能保证即时响应。')
    else:
        with open(LOCK, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = invoke('ready_check', {'timeout_sec': 10}, root / 'logs', 15)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') else 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
