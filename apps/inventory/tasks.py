"""第 3 题：每 12 小时随机轮换实例密码。

这道题真正被考察的不是「会不会用 celery」，而是三个线上必须成立的性质：

1. **幂等**：任务可能被重试、被 beat 重复投递、被人工补跑。
   如果只按"到点就换"，同一实例会在几分钟内被换两次密码，
   而应用侧缓存的是上一次的值 → 直接连不上库。
   这里用 `password_rotated_at + 周期` 做窗口判断，重复执行只会跳过。

2. **故障隔离**：批量任务里一个实例失败不能中断整批。
   这里逐实例 try/except，并为每个实例单独开事务、单独写审计记录。

3. **可审计**：谁、什么时候、成功了没有，必须留痕（PasswordRotationLog）。

批次上限（ROTATION_BATCH_SIZE）是为了避免几万个实例一次性打满数据库。
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.inventory.models import DBInstance, PasswordRotationLog

logger = logging.getLogger("apps.inventory.tasks")

# 密码长度：token_urlsafe(24) ≈ 32 字符，约 144 bit 熵
PASSWORD_BYTES = 24


def generate_password(nbytes: int = PASSWORD_BYTES) -> str:
    """生成随机密码。

    必须用 `secrets` 而不是 `random`：random 是梅森旋转伪随机，
    知道部分输出即可预测后续序列，不能用于生成凭据。
    """
    return secrets.token_urlsafe(nbytes)


def rotate_instance_password(instance_id: int, *, force: bool = False, trigger: str = "beat") -> bool:
    """轮换单个实例的密码。返回 True 表示确实执行了轮换。

    幂等保护放在事务内、拿到行锁之后判断：两个 worker 同时处理同一实例时，
    后到的那个会看到已经更新过的 `password_rotated_at` 从而跳过。
    """
    interval_hours = settings.PASSWORD_ROTATION_INTERVAL_HOURS

    with transaction.atomic():
        instance = DBInstance.objects.select_for_update().filter(pk=instance_id).first()
        if instance is None:
            logger.warning("实例 %s 已不存在，跳过密码轮换", instance_id)
            return False

        if not force and not instance.is_password_due(interval_hours):
            logger.info("实例 %s 在 %s 小时内已轮换过，跳过", instance_id, interval_hours)
            return False

        instance.set_password(
            generate_password(),
            keep_previous=True,
            grace_hours=settings.PREVIOUS_PASSWORD_GRACE_HOURS,
        )
        instance.save(
            update_fields=[
                "password",
                "password_rotated_at",
                "previous_password",
                "previous_password_expires_at",
                "updated_at",
            ]
        )
        # 首次设置密码的实例没有"上一版密码"，也就没有过渡期到期时间。
        # 直接对 None 做 :%Y-%m-%d 格式化会抛 TypeError，进而被外层判定为轮换取证失败
        # ——真实后果是：新建但尚未录入密码的实例会让任务在每次调度里都失败。
        expires_at = instance.previous_password_expires_at
        expires_text = expires_at.strftime("%Y-%m-%d %H:%M:%S") if expires_at else "无（首次设置）"

        PasswordRotationLog.objects.create(
            instance=instance,
            success=True,
            trigger=trigger,
            message=f"密码已轮换（有效期 {interval_hours}h，旧密码保留至 {expires_text}）",
        )

    logger.info("实例 %s(%s) 密码轮换完成", instance.name, instance.endpoint)
    return True


@shared_task(name="apps.inventory.tasks.rotate_all_instance_passwords")
def rotate_all_instance_passwords(
    force: bool = False, batch_size: int | None = None, trigger: str = "beat"
) -> dict:
    """批量轮换入口：由 beat 每 12 小时触发，也可手动调用。"""
    interval_hours = settings.PASSWORD_ROTATION_INTERVAL_HOURS
    cutoff = timezone.now() - timedelta(hours=interval_hours)
    limit = batch_size or settings.ROTATION_BATCH_SIZE

    candidates = DBInstance.objects.exclude(status=DBInstance.Status.OFFLINE)
    if not force:
        # 只挑「从未轮换过」或「已超过周期」的实例
        candidates = candidates.filter(
            Q(password_rotated_at__isnull=True) | Q(password_rotated_at__lte=cutoff)
        )
    instance_ids = list(candidates.order_by("password_rotated_at", "id").values_list("id", flat=True)[:limit])

    rotated = skipped = failed = 0
    errors: list[dict] = []

    for instance_id in instance_ids:
        try:
            if rotate_instance_password(instance_id, force=force, trigger=trigger):
                rotated += 1
            else:
                skipped += 1
        except Exception as exc:  # 单实例失败必须隔离，不能中断整批
            failed += 1
            logger.exception("实例 %s 密码轮换失败", instance_id)
            errors.append({"instance_id": instance_id, "error": str(exc)})
            # 失败也留痕，便于告警与人工补偿
            try:
                PasswordRotationLog.objects.create(
                    instance_id=instance_id,
                    success=False,
                    trigger=trigger,
                    message=f"轮换失败：{type(exc).__name__}: {exc}"[:255],
                )
            except Exception:  # 留痕失败不应掩盖原始异常
                logger.exception("写入轮换失败审计记录时出错，instance_id=%s", instance_id)

    summary = {
        "candidates": len(instance_ids),
        "rotated": rotated,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "interval_hours": interval_hours,
    }
    logger.info("密码轮换任务结束：%s", summary)
    return summary
