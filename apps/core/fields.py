"""加密字段：读写自动加解密，业务代码拿到的永远是明文。"""

from __future__ import annotations

from django.db import models

from apps.core.crypto import decrypt, encrypt


class EncryptedTextField(models.TextField):
    """可逆加密的文本字段。

    - 落库前加密（get_prep_value），读出后解密（from_db_value）
    - 用 TextField 而不是 CharField：Fernet 密文长度约为明文的 1.4 倍 + 定长开销，
      CharField 的 max_length 很容易不够而导致截断/报错
    - 注意：**不能用该字段做过滤/排序**，因为 Fernet 每次加密都带随机 IV，
      同一明文两次加密结果不同；按密文查询永远匹配不到。

    用法：
        password = EncryptedTextField(blank=True, default="")
    """

    description = "Fernet 加密存储的文本"

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        return encrypt(str(value))

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        return decrypt(value)
