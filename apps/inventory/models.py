"""第 1 题：数据库实例管理系统的三层模型。

关系设计（以及为什么这样定）：

    Department 1 ── n Cluster 1 ── n DBInstance
                                    │
                                    └── n ProbeLog / PasswordRotationLog

- **部门-集群 用一对多，不用多对多**
  一个集群在组织结构上只有一个归属部门。若做成 M2M，第 4 题「按部门和集群维度
  统计实例数量」会出现同一实例被多个部门重复计数的问题，口径不再唯一。

- **外键 on_delete 按语义分开设置，不无脑 CASCADE**
  * Cluster.department = PROTECT：部门下还有集群时禁止删除，否则统计维度出现悬空；
  * DBInstance.cluster = PROTECT：实例是真实资产，不能被集群删除连带抹掉；
  * ProbeLog.instance = CASCADE：探测记录是附属数据，随实例一起清理。

- **实例按 host:port 唯一**
  一个数据库实例在网络上的唯一标识就是 host + port，加唯一约束能从源头挡住
  重复录入（这类 CMDB 最常见的脏数据）。
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.crypto import mask
from apps.core.fields import EncryptedTextField


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    """部门：统计维度之一。"""

    name = models.CharField("部门名称", max_length=64, unique=True)
    code = models.CharField("部门编码", max_length=32, unique=True, help_text="用于与 CMDB / HR 系统对账")
    owner = models.CharField("负责人", max_length=64, blank=True)
    description = models.TextField("描述", blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        verbose_name = "部门"
        verbose_name_plural = "部门"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name}({self.code})"


class Cluster(TimeStampedModel):
    """集群：统计维度之二，向下挂实例，向上归属部门。"""

    class Engine(models.TextChoices):
        MYSQL = "mysql", "MySQL"
        POSTGRESQL = "postgresql", "PostgreSQL"
        REDIS = "redis", "Redis"
        MONGODB = "mongodb", "MongoDB"
        OTHER = "other", "其他"

    class Environment(models.TextChoices):
        PROD = "prod", "生产"
        STAGING = "staging", "预发"
        TEST = "test", "测试"
        DEV = "dev", "开发"

    name = models.CharField("集群名称", max_length=64)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,  # 部门下仍有集群时不允许删除
        related_name="clusters",
        verbose_name="所属部门",
    )
    engine = models.CharField("数据库类型", max_length=16, choices=Engine.choices, default=Engine.MYSQL)
    environment = models.CharField(
        "环境", max_length=16, choices=Environment.choices, default=Environment.PROD
    )
    description = models.TextField("描述", blank=True, default="")

    class Meta:
        verbose_name = "集群"
        verbose_name_plural = "集群"
        ordering = ["department__name", "name"]
        constraints = [
            # 同一部门下集群名唯一；不同部门允许重名（如都叫 "order-db"）
            models.UniqueConstraint(fields=["department", "name"], name="uniq_cluster_name_per_department"),
        ]
        indexes = [models.Index(fields=["department", "engine"], name="idx_cluster_dept_engine")]

    def __str__(self) -> str:
        return f"{self.department.name}/{self.name}"


class DBInstance(TimeStampedModel):
    """数据库实例：被管理的核心资产，持有需要加密保存的账号密码。"""

    class Role(models.TextChoices):
        MASTER = "master", "主库"
        SLAVE = "slave", "从库"
        SINGLE = "single", "单实例"

    class Status(models.TextChoices):
        RUNNING = "running", "运行中"
        MAINTENANCE = "maintenance", "维护中"
        OFFLINE = "offline", "已下线"

    cluster = models.ForeignKey(
        Cluster,
        on_delete=models.PROTECT,  # 实例是真实资产，不能被集群删除连带抹掉
        related_name="instances",
        verbose_name="所属集群",
    )
    name = models.CharField("实例名", max_length=64)
    role = models.CharField("角色", max_length=16, choices=Role.choices, default=Role.SINGLE)
    host = models.CharField("主机地址", max_length=255)
    port = models.PositiveIntegerField(
        "端口", validators=[MinValueValidator(1), MaxValueValidator(65535)], default=3306
    )
    username = models.CharField("账号", max_length=64, default="root")
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.RUNNING)
    remark = models.TextField("备注", blank=True, default="")

    # ---- 敏感字段（第 3 题）----
    # 落库即密文；`password` 在 Python 侧永远是明文，加密由 EncryptedTextField 负责
    password = EncryptedTextField("账号密码（加密存储）", blank=True, default="")
    password_rotated_at = models.DateTimeField("密码最近轮换时间", null=True, blank=True, db_index=True)
    # 双密码过渡期：轮换后旧密码仍保留一段时间，避免应用侧连接瞬间全部失败
    previous_password = EncryptedTextField("上一版密码（加密存储）", blank=True, default="")
    previous_password_expires_at = models.DateTimeField("上一版密码失效时间", null=True, blank=True)

    class Meta:
        verbose_name = "数据库实例"
        verbose_name_plural = "数据库实例"
        ordering = ["cluster__department__name", "cluster__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["cluster", "name"], name="uniq_instance_name_per_cluster"),
            # 网络唯一标识：同一 host:port 只能对应一个实例
            models.UniqueConstraint(fields=["host", "port"], name="uniq_instance_host_port"),
        ]
        indexes = [
            models.Index(fields=["cluster", "status"], name="idx_instance_cluster_status"),
            models.Index(fields=["status"], name="idx_instance_status"),
        ]

    def __str__(self) -> str:
        return f"{self.name}@{self.host}:{self.port}"

    # --------------------------------------------------------------- helpers
    @property
    def has_password(self) -> bool:
        return bool(self.password)

    @property
    def masked_password(self) -> str:
        return mask(self.password or "")

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def set_password(
        self, raw_password: str, *, keep_previous: bool = True, grace_hours: int | None = None
    ) -> None:
        """设置新密码，并（可选）把旧密码挪到过渡位。

        注意：这里是**唯一**推荐修改密码的入口，避免业务代码某处直接给
        `password` 赋值却忘了更新 `password_rotated_at`。
        """
        if keep_previous and self.password:
            grace = grace_hours if grace_hours is not None else settings.PREVIOUS_PASSWORD_GRACE_HOURS
            self.previous_password = self.password
            self.previous_password_expires_at = timezone.now() + timedelta(hours=grace)
        self.password = raw_password
        self.password_rotated_at = timezone.now()

    def is_password_due(self, interval_hours: int) -> bool:
        """是否到达轮换周期。供定时任务做幂等判断。"""
        if not self.password_rotated_at:
            return True
        return self.password_rotated_at <= timezone.now() - timedelta(hours=interval_hours)


class ProbeLog(models.Model):
    """第 2 题：每次 TCP 探测的留痕。

    保留历史而不是只更新实例上的一个字段，原因有二：
    - 排障时需要看「什么时间点开始不可达」，单值字段会被覆盖；
    - 后续接入告警/可用性报表时可以直接复用这张表。
    """

    class Reason(models.TextChoices):
        OK = "ok", "可达"
        TIMEOUT = "timeout", "连接超时"
        REFUSED = "refused", "端口拒绝"
        DNS_ERROR = "dns_error", "域名解析失败"
        NETWORK_ERROR = "network_error", "网络不可达"
        UNKNOWN_ERROR = "unknown_error", "未知错误"

    instance = models.ForeignKey(
        DBInstance, on_delete=models.CASCADE, related_name="probe_logs", verbose_name="实例"
    )
    reachable = models.BooleanField("是否可达")
    reason = models.CharField("结果原因", max_length=32, choices=Reason.choices)
    detail = models.TextField("详情", blank=True, default="")
    latency_ms = models.FloatField("耗时(毫秒)", null=True, blank=True)
    checked_at = models.DateTimeField("探测时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "探测记录"
        verbose_name_plural = "探测记录"
        ordering = ["-checked_at", "-id"]
        indexes = [models.Index(fields=["instance", "-checked_at"], name="idx_probelog_instance_time")]

    def __str__(self) -> str:
        flag = "up" if self.reachable else "down"
        return f"{self.instance_id} {flag} @ {self.checked_at:%Y-%m-%d %H:%M:%S}"


class PasswordRotationLog(models.Model):
    """第 3 题：密码轮换审计。敏感操作必须可追溯「谁在什么时候改了什么」。"""

    class Trigger(models.TextChoices):
        BEAT = "beat", "定时任务"
        MANUAL = "manual", "手动触发"
        API = "api", "接口调用"

    instance = models.ForeignKey(
        DBInstance, on_delete=models.CASCADE, related_name="rotation_logs", verbose_name="实例"
    )
    success = models.BooleanField("是否成功", default=True)
    trigger = models.CharField("触发方式", max_length=16, choices=Trigger.choices, default=Trigger.BEAT)
    message = models.CharField("说明", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("轮换时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "密码轮换记录"
        verbose_name_plural = "密码轮换记录"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["instance", "-created_at"], name="idx_rotlog_instance_time")]

    def __str__(self) -> str:
        return f"{self.instance_id} {'ok' if self.success else 'fail'} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
