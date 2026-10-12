"""访问控制的可公开错误，禁止包含密码和目录原始响应。"""


class AccessError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
