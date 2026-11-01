"""测试专用 settings。

Django 测试运行器会强制 DEBUG=False，而「非 DEBUG 必须显式配置
FIELD_ENCRYPTION_KEYS」，两者叠加会让 pytest 直接抛 ImproperlyConfigured。
所以这里注入一把一次性 Fernet 密钥；把它删掉再跑 pytest 会立刻失败，
正好验证「缺密钥就不允许写敏感字段」的保护是生效的。
"""

from __future__ import annotations

from config.settings import *

# 一次性测试密钥（仅在本进程使用，切勿用于任何真实环境）
FIELD_ENCRYPTION_KEYS = ["Lj6yuxjH6ut5ZNew95LJJe_9GyVjWtMcRUqRl-CnE94="]

# 测试里不应该真的等 3 秒超时，缩短以加快测试
PROBE_TIMEOUT_SECONDS = 1.0

# 密码强度校验在测试数据里没必要引入复杂度
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
