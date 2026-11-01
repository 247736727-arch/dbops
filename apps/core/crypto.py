"""敏感字段的可逆加密工具。

为什么不用 `django.contrib.auth.hashers.make_password`？
    口令哈希（pbkdf2 / bcrypt / argon2）是**单向**的，只能验证、无法取回。
    而数据库实例密码属于「业务密文」——系统在轮换、下发、排障时需要把它还原成
    明文再去连库，因此必须使用**可逆**的对称加密（这里用 Fernet：
    AES-128-CBC + HMAC-SHA256，带完整性校验，能发现密文被篡改）。

密钥管理：
    FIELD_ENCRYPTION_KEYS 支持逗号分隔的多把密钥，第一把用于加密，其余仅用于解密。
    轮换密钥时把新密钥放最前面、旧的留在后面，历史数据仍可读出，等全量重新加密后
    再移除旧密钥即可。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class SecretDecryptionError(RuntimeError):
    """密文无法解密：通常是密钥被替换或数据被篡改。"""


def _derive_key_from_secret_key(secret: str) -> str:
    """开发环境兜底：从 SECRET_KEY 派生一把稳定的 Fernet 密钥。

    仅用于开箱即跑（DEBUG=True）。生产必须显式配置 FIELD_ENCRYPTION_KEYS，
    否则一旦 SECRET_KEY 变更，所有历史密文都会解不开。
    """
    digest = hashlib.sha256(f"fernet:{secret}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


@lru_cache(maxsize=8)
def _build_multifernet(keys: tuple[str, ...]) -> MultiFernet:
    fernets = []
    for raw in keys:
        try:
            fernets.append(Fernet(raw.encode() if isinstance(raw, str) else raw))
        except (TypeError, ValueError) as exc:  # 长度不对 / 不是 urlsafe base64
            raise ImproperlyConfigured(
                f"FIELD_ENCRYPTION_KEYS 中存在非法密钥（需 32 字节 urlsafe-base64）: {exc}"
            ) from exc
    return MultiFernet(fernets)


def get_fernet() -> MultiFernet:
    keys = tuple(k.strip() for k in (getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or []) if k.strip())
    if not keys:
        if not settings.DEBUG:
            raise ImproperlyConfigured(
                "生产环境必须配置 FIELD_ENCRYPTION_KEYS，不能依赖 SECRET_KEY 派生。"
            )
        logger.warning(
            "FIELD_ENCRYPTION_KEYS 未配置，已临时从 SECRET_KEY 派生密钥（仅限本地开发）。"
        )
        keys = (_derive_key_from_secret_key(settings.SECRET_KEY),)
    return _build_multifernet(keys)


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        raise ValueError("待加密内容不能为 None")
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if ciphertext in (None, ""):
        return ""
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError(
            "密文解密失败：密钥不匹配或数据已被篡改。"
        ) from exc


def mask(secret: str, keep: int = 2) -> str:
    """脱敏展示，用于接口与 admin，避免明文外泄。"""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * 6}{secret[-keep:]}"
