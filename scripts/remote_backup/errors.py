"""验收断言失败；与连接库解耦以便宿主执行安全检查。"""


class CheckFailure(RuntimeError):
    pass


class EnvironmentBlocked(CheckFailure):
    pass
