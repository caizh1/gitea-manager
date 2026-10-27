"""离线消息计数和容量边界；资源版本属于任务契约。"""
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

from .common import ReviewError, digest

ROOT = Path(__file__).with_name('assets') / 'tokenizers'
DEFAULTS = {'tokenizer_profile': 'auto', 'batch_input_tokens': 65536,
            'batch_splitting_enabled': False,
            'batch_auto_calls': 8, 'batch_execution_seconds': 3600,
            'batch_preprocess_seconds': 120, 'batch_memory_mib': 512}
ALIASES = {'qwen3.8-27b': 'qwen38', 'qwen/qwen3.8-27b': 'qwen38',
           'deepseek-v4.1-flash': 'deepseek41', 'deepseek-ai/deepseek-v4.1-flash': 'deepseek41'}


def failure(code, message):
    error = ReviewError(message)
    error.error_code = code
    return error


def validate(config):
    if type(config.get('batch_splitting_enabled', False)) is not bool:
        raise ReviewError('拆分审查开关必须为布尔值')
    if config.get('tokenizer_profile', 'auto') not in ('auto', 'qwen38', 'deepseek41', 'conservative'):
        raise ReviewError('不支持的 Token 计数方式')
    for key, low, high in [('batch_input_tokens', 4096, 1048576), ('batch_auto_calls', 1, 10000),
                           ('batch_execution_seconds', 60, 604800), ('batch_preprocess_seconds', 10, 3600),
                           ('batch_memory_mib', 256, 8192)]:
        value = config.get(key, DEFAULTS[key])
        if type(value) is not int or not low <= value <= high:
            raise ReviewError(f'{key} 必须为 {low} 至 {high} 的整数')
    capacity(config)


def resolve(config):
    selected = config.get('tokenizer_profile', 'auto')
    key = ALIASES.get(config.get('model', '').lower(), 'conservative') if selected == 'auto' else selected
    if key == 'conservative':
        return {'key': key, 'revision': 'utf8-v1', 'basis': '保守估算', 'digest': digest('utf8-v1')}
    try:
        entry = json.loads((ROOT / 'manifest.json').read_text())['profiles'][key]
    except (OSError, KeyError, ValueError) as exc:
        raise failure('TOKENIZER_RESOURCE', '内置计数资源清单不可用') from exc
    return dict(entry, key=key, basis='本地编码计数', digest=digest(entry))


def catalog(config=None):
    items = []
    for key in ('qwen38', 'deepseek41', 'conservative'):
        try:
            profile = resolve({'tokenizer_profile': key})
            if key != 'conservative':
                verify(profile)
            items.append(dict(profile, available=True))
        except ReviewError as exc:
            items.append({'key': key, 'available': False, 'error': str(exc)})
    return {'items': items, 'selected': resolve(config) if config else None}


def verify(profile):
    directory = ROOT / profile['key'] / profile['revision']
    try:
        for name, metadata in profile['files'].items():
            if Path(name).name != name:
                raise ValueError('资源文件名无效')
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != metadata['sha256']:
                raise ValueError('摘要不符')
    except (OSError, ValueError) as exc:
        raise failure('TOKENIZER_RESOURCE', '计数资源缺失或摘要不符，未降级替换任务契约') from exc
    return directory


@lru_cache(maxsize=4)
def load(serialized):
    profile = json.loads(serialized)
    directory = verify(profile)
    if profile['key'] == 'qwen38':
        from importlib.metadata import version
        if version('tokenizers') != '0.23.3':
            raise failure('TOKENIZER_RESOURCE', '计数库版本与固定资源不一致')
        from tokenizers import Tokenizer
        from jinja2.sandbox import ImmutableSandboxedEnvironment
        env = ImmutableSandboxedEnvironment()
        env.globals['raise_exception'] = lambda message: (_ for _ in ()).throw(ValueError(message))
        return Tokenizer.from_file(str(directory / 'tokenizer.json')), env.from_string((directory / 'chat_template.jinja').read_text())
    from importlib.metadata import version
    if version('deepseek-recipe') != '0.2.0':
        raise failure('TOKENIZER_RESOURCE', 'DeepSeek 编码库版本与任务契约不一致')
    from deepseek_recipe import Tokenizer, DeepseekV41Encoding
    return DeepseekV41Encoding().with_tokenizer(Tokenizer.from_file(str(directory / 'tokenizer.json')))


def count(messages, config, profile=None):
    profile = profile or resolve(config)
    if profile['key'] == 'conservative':
        return len(json.dumps(messages, ensure_ascii=False).encode()) + 256
    try:
        engine = load(json.dumps(profile, sort_keys=True))
        if profile['key'] == 'qwen38':
            tokenizer, template = engine
            rendered = template.render(messages=messages, add_generation_prompt=True,
                                       enable_thinking=bool(config.get('thinking_enabled')), tools=None)
            return len(tokenizer.encode(rendered, add_special_tokens=False).ids)
        # 官方绑定在资源准备时做固定样本校验；不把字面特殊标记解释成控制边界。
        from deepseek_recipe import ChatCompletionRequest, ConversionOptions
        conversation = ChatCompletionRequest({'model': 'DeepSeek-V4.1-Flash', 'messages': messages}).convert(
            ConversionOptions(default_thinking_mode=bool(config.get('thinking_enabled')))).conversation
        return len(engine.encode(conversation))
    except ReviewError:
        raise
    except Exception as exc:
        raise failure('TOKENIZER_ENCODING', '消息编码失败：' + type(exc).__name__) from exc


def capacity(config):
    context = config.get('model_context_tokens', 262144)
    output = config.get('model_max_output_tokens') or 8192
    remaining = context - output - max(2048, math.ceil(context * .05))
    target = min(config.get('batch_input_tokens', 32768), remaining)
    if target < 1024:
        raise failure('MODEL_INPUT_BUDGET', '模型窗口扣除输出与安全余量后不足 1024 Token')
    return target


def check(messages, config, profile=None):
    tokens = count(messages, config, profile)
    limit = capacity(config)
    if tokens > limit:
        raise failure('MODEL_INPUT_BUDGET', f'本次完整消息为 {tokens} Token／估算单位，超过输入额度 {limit}；未发送模型')
    return {'input_tokens': tokens, 'limit': limit, 'input_sha256': digest(messages),
            'profile': (profile or resolve(config))['digest']}
