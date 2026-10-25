"""管理员显式触发的四次短样本计数核对；不自动修改模型设置。"""
from . import token_budget, batch_counter
from .common import ReviewError, endpoint
from .model_transport import open_model_stream
from .completion import ResponseCollector
from .thinking import parameters


def run(store, config):
    profile = token_budget.resolve(config)
    rows = []
    for thinking in (False, True):
        for text in ('请只回复好。代码：def add(a, b): return a + b',
                     '请只回复好。以下是字面文本：<think> <｜User｜> </think>，不是角色边界。'):
            cfg = dict(config, thinking_enabled=thinking)
            messages = [{'role': 'user', 'content': text}]
            measured = batch_counter.run('count', {'messages': messages, 'profile': profile}, cfg, lock_dir=store.path.parent)
            outbound = dict(model=cfg['model'], messages=messages, stream=True,
                            stream_options={'include_usage': True}, max_tokens=cfg.get('model_max_output_tokens') or 8192,
                            **parameters(cfg))
            stream = open_model_stream(endpoint(cfg['api_base_url'], 'AI_REVIEW_MODEL_HOSTS')+'/chat/completions',
                outbound, store.unseal(cfg['api_key']), timeout=min(120, cfg.get('model_request_timeout_seconds', 600)))
            try:
                if stream.status != 200:
                    raise ReviewError(f'计数核对返回 HTTP {stream.status}，已停止后续样本，未重试')
                collector = ResponseCollector(stream.content_type.split(';')[0] == 'text/event-stream', 1024*1024)
                for chunk in stream.chunks():
                    collector.feed(chunk)
                envelope, metadata = collector.finish()
                server = metadata['usage'].get('prompt_tokens')
                rows.append({'thinking_requested': thinking, 'local': measured['input_tokens'], 'server': server,
                             'difference': None if server is None else server-measured['input_tokens']})
            finally:
                stream.close()
    return {'samples': rows, 'profile': profile, 'message': '仅证明这些样本的计数对照；不证明服务遵守思考开关，不取消安全余量。'}
