"""由任务冻结配置生成模型控制参数；不接受模型输出扩大预算。"""
from .common import ReviewError

DEFAULTS = {'thinking_protocol': 'provider_default', 'thinking_enabled': False,
            'thinking_token_budget': 4096, 'context_retrieval_budget': 20}
PROTOCOLS = ('provider_default', 'qwen_vllm', 'deepseek', 'reasoning_effort')


def validate(config):
    if config.get('thinking_protocol') not in PROTOCOLS:
        raise ReviewError('请选择受支持的思考控制协议')
    if type(config.get('thinking_enabled')) is not bool:
        raise ReviewError('深度思考开关必须为布尔值')
    for key, low, high in [('thinking_token_budget', 128, 32768), ('context_retrieval_budget', 1, 200)]:
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ReviewError(f'{key} 超出允许范围 {low} 至 {high}')
    if config['thinking_protocol'] == 'provider_default' and config['thinking_enabled']:
        raise ReviewError('开启思考前请选择模型服务的控制协议')
    output = config.get('model_max_output_tokens', 0)
    if config['thinking_protocol'] == 'qwen_vllm' and config['thinking_enabled'] and output and output <= config['thinking_token_budget']:
        raise ReviewError('总输出额度必须大于思考额度，为审查结论保留空间')


def parameters(config):
    protocol = config.get('thinking_protocol')
    # 历史任务保持原行为；升级后管理员可明确选择协议，不推测私有模型名称。
    if protocol is None:
        return {'thinking': {'type': 'disabled'}} if config.get('model', '').lower().startswith('deepseek') else {}
    enabled = config.get('thinking_enabled', False)
    if protocol == 'qwen_vllm':
        result = {'chat_template_kwargs': {'enable_thinking': enabled}}
        if enabled:
            result['thinking_token_budget'] = config.get('thinking_token_budget', 4096)
        return result
    if protocol == 'deepseek':
        return {'thinking': {'type': 'enabled' if enabled else 'disabled'}}
    if protocol == 'reasoning_effort':
        return {'reasoning_effort': 'low' if enabled else 'none'}
    return {}


def description(config):
    protocol = config.get('thinking_protocol', 'legacy')
    if protocol == 'provider_default':
        return '未发送思考控制参数；服务默认行为未验证，请选择实际协议后检查连接'
    return '已发送思考控制参数；请求兼容不代表服务端已执行，需结合服务配置与真实审查验证'
