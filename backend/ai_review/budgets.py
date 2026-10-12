"""同一任务使用创建时预算；外围时限仅为传输及回收留出余量。"""

TIME_FIELDS = frozenset({'task_timeout_seconds', 'review_timeout_seconds', 'model_request_timeout_seconds', 'model_max_output_tokens', 'model_response_max_mib', 'thinking_protocol', 'thinking_enabled', 'thinking_token_budget', 'context_retrieval_budget', 'model_context_tokens', 'compaction_threshold_tokens'})
CONTEXT_DEFAULTS = {'model_context_tokens': 262144, 'compaction_threshold_tokens': 210000}
MODEL_TIMEOUT_DEFAULT = 600
MODEL_TIMEOUT_MAX = 3600
REVIEW_TIMEOUT_DEFAULT = 600
FINISH_MARGIN = 40


def validate_context(config):
    from .common import ReviewError
    for key in CONTEXT_DEFAULTS:
        if type(config.get(key)) is not int or not 4096 <= config[key] <= 1048576:
            raise ReviewError('模型窗口和压缩阈值必须为 4096 至 1048576 的整数 Token')
    reserve = config.get('model_max_output_tokens') or 8192
    if config['compaction_threshold_tokens'] + reserve > config['model_context_tokens']:
        raise ReviewError('压缩阈值加输出预留不能超过模型窗口；输出额度为 0 时至少预留 8192 Token')


def execution_revision(config):
    # 旧历史记录的编辑版本同时也是执行版本，不能用当前配置回填。
    return config.get('execution_revision', config['revision'])


def model_timeout(config):
    return min(config.get('model_request_timeout_seconds', MODEL_TIMEOUT_DEFAULT),
               config['task_timeout_seconds'] - FINISH_MARGIN)


def review_timeout(config):
    """旧配置没有原生会话时限时继承原任务时限；新任务仍从历史配置读取冻结值。"""
    return config.get('review_timeout_seconds', config.get('task_timeout_seconds', REVIEW_TIMEOUT_DEFAULT))


def call_reservation(config):
    return model_timeout(config) + FINISH_MARGIN


# 完整单次消息的资源保护；字节限制不冒称未知服务的精确 Token 窗口。
MAX_MODEL_INPUT_BYTES = 64 * 1024


def check_input(messages):
    from .common import ReviewError, encode
    size=len(encode(messages).encode('utf-8'))
    if size>MAX_MODEL_INPUT_BYTES:
        error=ReviewError(f'本单元完整模型输入为 {size} 字节，超过 {MAX_MODEL_INPUT_BYTES} 字节预算；未发送上游，已保留变更原文')
        error.error_code='MODEL_INPUT_BUDGET'
        error.input_bytes=size
        raise error
    return size
