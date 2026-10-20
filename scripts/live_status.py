"""Read-only consumers; atomic snapshots, not proof of host health."""
import datetime
import json
import os
import tempfile
import time
from pathlib import Path


def write_snapshot(run_dir, stage, details=None):
    data = dict(details or {})
    data.update(stage=stage, updated_at=datetime.datetime.now().astimezone().isoformat(),
                updated_epoch=time.time(), writer_pid=os.getpid())
    directory = Path(run_dir)
    fd, tmp = tempfile.mkstemp(prefix='.status-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, directory/'run_status.json')
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def read_snapshot(run_dir, stale_after=10):
    path=Path(run_dir)/'run_status.json'
    if not path.exists(): return {'available':False}
    try:
        data=json.loads(path.read_text())
        age=max(0, time.time()-data['updated_epoch'])
        data.update(available=True, age_sec=round(age,3), stale=age>stale_after)
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'available':False, 'read_error':str(exc)}


from contextlib import contextmanager
import threading


@contextmanager
def phase_heartbeat(run_dir, stage):
    """Executor liveness only; does not assert progress or host responsiveness."""
    stop=threading.Event()
    start=time.monotonic()
    def emit():
        write_snapshot(run_dir,stage,{'phase_state':'running',
            'heartbeat_scope':'executor_wait_loop',
            'stage_elapsed_sec':round(time.monotonic()-start,3)})
    def loop():
        while not stop.wait(2):
            try: emit()
            except OSError: return
    emit()
    thread=threading.Thread(target=loop,daemon=True)
    thread.start()
    try: yield
    finally:
        stop.set()
        thread.join()
