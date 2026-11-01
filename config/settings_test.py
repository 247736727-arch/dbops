"""测试专用 settings。

为什么单独拆一个：

Django 的测试运行器会强制 `DEBUG = False`。而本项目刻意要求——
**生产（即非 DEBUG）必须显式配置 FIELD_ENCRYPTION_KEYS**，不允许从 SECRET_KEY 派生
（见过太多因为换 SECRET_KEY 导致历史密文全部解不开的事故）。两条规则叠加的结果是：
跑 pytest 时如果没有单独提供密钥，加密字段会直接抛 ImproperlyConfigured。

因此这里显式注入一把**一次性**的 Fernet 密钥。它只用于测试进程，
不代表任何真实环境，也不会影响 `config.settings` 的行为。

验证方式：把下面这把 key 删掉再跑 pytest，应该立刻失败——
这正说明"缺密钥就无法写敏感字段"的保护是生效的。
"""

from __future__ import annotations

from config.settings import *

# 一次性测试密钥（仅在本进程使用，切勿用于任何真实环境）
FIELD_ENCRYPTION_KEYS = ["Lj6yuxjH6ut5ZNew95LJJe_9GyVjWtMcRUqRl-CnE94="]

# 测试里不应该真的等 3 秒超时，缩短以加快测试
PROBE_TIMEOUT_SECONDS = 1.0

# 密码强度校验在测试数据里没必要引入复杂度
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
