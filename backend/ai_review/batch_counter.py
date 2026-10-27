"""有界独立计数进程；文件锁在同一持久卷内串行化计数工作。"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import token_budget


def run(operation, payload, config, guard=None, lock_dir=None):
    config = {k: v for k, v in config.items() if k not in ('api_key', 'api_base_url')}
    root = Path(lock_dir or Path(os.environ.get('AI_REVIEW_DB', '/app/review/review.db')).parent)
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + config.get('batch_preprocess_seconds', 120)
    with (root / 'batch-counter.lock').open('a') as lock:
        while True:
            if guard:
                guard()
            if time.monotonic() >= deadline:
                raise token_budget.failure('PREPROCESS_TIMEOUT', '等待计数资源超时，未调用模型')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.1)
        cache_path = None
        if operation == 'count':
            from .common import digest
            profile = payload.get('profile') or token_budget.resolve(config)
            if profile['key'] != 'conservative':
                token_budget.verify(profile)
            cache = root / 'batch-count-cache'
            cache.mkdir(exist_ok=True, mode=0o700)
            cache_path = cache / (digest({'version': 1, 'payload': payload, 'config': config, 'profile': profile}) + '.json')
            if cache_path.exists():
                try:
                    return json.loads(cache_path.read_text())
                except (OSError, ValueError):
                    pass
        env = dict(os.environ, TOKENIZERS_PARALLELISM='false', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        data = json.dumps({'operation': operation, 'payload': payload, 'config': config}, ensure_ascii=False).encode()
        # 大请求可能尚未写完管道就触发 communicate 的短超时；后续无 input 的
        # communicate 不会继续注册 stdin 写事件。匿名文件提供确定 EOF，避免死等。
        source = tempfile.TemporaryFile(prefix='batch-input-', dir=root)
        try:
            source.write(data)
            source.seek(0)
            process = subprocess.Popen([sys.executable, '-m', 'ai_review.batch_counter'], stdin=source,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        except BaseException:
            source.close()
            raise
        try:
            while True:
                if guard:
                    guard()
                if sys.platform.startswith('linux'):
                    try:
                        status = Path(f'/proc/{process.pid}/status').read_text()
                        rss = next(int(line.split()[1])*1024 for line in status.splitlines() if line.startswith('VmRSS:'))
                        if rss > config.get('batch_memory_mib', 512)*1024*1024:
                            raise token_budget.failure('PREPROCESS_RESOURCE', '计数进程超过常驻内存预算，已终止预处理')
                    except (FileNotFoundError, StopIteration):
                        pass
                if time.monotonic() >= deadline:
                    raise token_budget.failure('PREPROCESS_TIMEOUT', '分批预处理超过时间预算，未调用模型')
                try:
                    out, err = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise token_budget.failure('PREPROCESS_RESOURCE', '计数进程异常退出或超过内存预算，未调用模型')
            result = json.loads(out)
            if 'error' in result:
                error = token_budget.failure(result['code'], result['error'])
                from .diagnostics import safe_remote
                error.diagnostic = safe_remote(result.get('diagnostic'))
                raise error
            if cache_path is not None:
                temporary = cache_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(result['value'], ensure_ascii=False))
                temporary.replace(cache_path)
                # 缓存只存计数及摘要，有限保留；不存 PR 正文。
                cached = sorted(cache_path.parent.glob('*.json'), key=lambda p: p.stat().st_mtime)
                for old in cached[:-1024]:
                    old.unlink(missing_ok=True)
            return result['value']
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                process.communicate()
            finally:
                source.close()


def main():
    request = json.load(sys.stdin)
    config = request['config']
    # 父进程监督实际 RSS；Rust 分配器和架构模拟会预留大量虚拟地址，
    # 不能把虚拟地址空间限制误当作实际内存预算。
    try:
        if request['operation'] == 'plan':
            flow_version = request['payload'].get('flow_version', 2)
            if flow_version >= 3:
                from .batch_flow import build
            else:
                from .batch_plan import build
            if flow_version >= 6:
                from .batch_flow import validate_single_size
                validate_single_size(request['payload']['snapshot'])
            options = ({'single_request': flow_version >= 5 and not config.get('batch_splitting_enabled', False),
                        'net_limit': flow_version == 5}
                       if flow_version >= 3 else {})
            value = build(request['payload']['snapshot'], config, request['payload']['history'], **options)
        elif request['operation'] == 'refutations':
            from .batch_flow import refutations
            value = refutations(request['payload']['snapshot'], request['payload']['plan'], config)
        elif request['operation'] == 'benchmark':
            import resource
            sample = 'int value = 123; // 中文代码\n'
            wanted = request['payload']['mib'] * 1024 * 1024
            text = (sample * (wanted // len(sample.encode()) + 1)).encode()[:wanted].decode(errors='ignore')
            messages = [{'role': 'user', 'content': text}]
            durations = []
            for _ in range(6):
                start = time.monotonic()
                tokens = token_budget.count(messages, config)
                durations.append(time.monotonic()-start)
            value = {'首次秒': durations[0], '热计数秒': durations[1:], 'Token': tokens,
                     '峰值内存原始值': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                     '峰值单位': 'KiB' if sys.platform.startswith('linux') else '字节'}
        else:
            value = token_budget.check(request['payload']['messages'], config, request['payload'].get('profile'))
        print(json.dumps({'value': value}, ensure_ascii=False))
    except Exception as exc:
        from .diagnostics import safe_remote
        print(json.dumps({'code': getattr(exc, 'error_code', 'PREPROCESS_RESOURCE'),
                          'diagnostic': safe_remote(getattr(exc, 'diagnostic', {})),
                          'error': str(exc) if not isinstance(exc, MemoryError) else '预处理超过内存预算'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
