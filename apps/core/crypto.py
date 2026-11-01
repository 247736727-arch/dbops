"""敏感字段的可逆加密（Fernet = AES-128-CBC + HMAC-SHA256，自带完整性校验）。

为什么不用 `make_password`：口令哈希（pbkdf2/bcrypt/argon2）是单向的，只能验证、
无法取回。实例密码属于「业务密文」，轮换、下发、排障时都要还原成明文去连库，
所以必须可逆。

FIELD_ENCRYPTION_KEYS 支持逗号分隔多把密钥：第一把用于加密，其余仅用于解密。
换密钥时把新的放最前、旧的留后面，历史密文仍可读，等全量重加密后再移除旧密钥。
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
    """DEBUG 下兜底：由 SECRET_KEY 派生一把稳定密钥，让本地开箱即跑。

    生产必须显式配置 FIELD_ENCRYPTION_KEYS —— 换一次 SECRET_KEY，历史密文就全解不开。
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
