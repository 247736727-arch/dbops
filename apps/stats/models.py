"""每日统计结果的存储模型。

- **唯一约束 (stat_date, department, cluster)**：定时任务一定会遇到重试、补跑、
  人工触发。没有唯一键，同一天会被写多份、报表直接翻倍；有了唯一键，配合
  `bulk_create(update_conflicts=True)` 就天然幂等。
- **外键用 PROTECT 而非 CASCADE**：统计数据是历史快照，不能因为删了集群就消失；
  删集群前必须先归档统计。
"""

from __future__ import annotations

from django.db import models

from apps.inventory.models import Cluster, Department


class InstanceStat(models.Model):
    """按 部门 × 集群 维度的每日实例数量快照。"""

    stat_date = models.DateField("统计日期", db_index=True)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="instance_stats", verbose_name="部门"
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.PROTECT, related_name="instance_stats", verbose_name="集群"
    )
    instance_count = models.PositiveIntegerField("实例数量", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "实例数量统计"
        verbose_name_plural = "实例数量统计"
        ordering = ["-stat_date", "department__name", "cluster__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["stat_date", "department", "cluster"],
                name="uniq_instance_stat_per_day",
            )
        ]
        indexes = [models.Index(fields=["stat_date", "department"], name="idx_stat_date_dept")]

    def __str__(self) -> str:
        return f"{self.stat_date} {self.department_id}/{self.cluster_id} = {self.instance_count}"
