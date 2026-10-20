"""Isolated image work: no Resolve connection. Parent enforces absolute deadline."""
import json
import sys


def execute(action, payload):
    if action == 'dependencies':
        import numpy
        import cv2
        from compare_outputs import _load_image_normalized
        for path in payload.get('sample_paths', []):
            _load_image_normalized(path)
        return {'ok':True,'verdict':'PASS','data':{'numpy':numpy.__version__,'opencv':cv2.__version__, 'decoded_samples':payload.get('sample_paths',[])}}
    if action == 'compare':
        from compare_outputs import run_comparison
        return run_comparison(**payload)
    raise ValueError('Unknown evidence action')


if __name__ == '__main__':
    try:
        result = execute(sys.argv[1], json.load(sys.stdin))
    except ImportError as exc:
        result = {'ok':False,'verdict':'BLOCKED','failure_code':'IMAGE_DEPENDENCY_MISSING','reason':str(exc)}
    except Exception as exc:
        result = {'ok':False,'verdict':'INCONCLUSIVE','failure_code':'EVIDENCE_WORKER_ERROR','reason':str(exc)}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    sys.exit(0 if result.get('ok') else 1)
