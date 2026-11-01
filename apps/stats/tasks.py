"""第 4 题：每天 00:00 按 部门 × 集群 统计实例数量并入库。

三个要点：

1. **聚合在数据库侧完成**：`values(...).annotate(Count("id"))` 一条 SQL 出结果，
   不要「查全部实例再在 Python 里 for 循环分组」——实例上万时内存和耗时都会失控。

2. **用 LEFT 语义补零**：只用 Instance 聚合会漏掉「一个实例都没有的集群」。
   每日快照应该完整覆盖所有集群，否则报表上看不出"这个集群空了"。
   这里以 Cluster 为基准表，用聚合结果做字典补零。

3. **幂等写入**：唯一约束 + `bulk_create(update_conflicts=True)`，
   重复执行同一天只会更新计数，不会产生重复行。
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.inventory.models import Cluster, DBInstance
from apps.stats.models import InstanceStat

logger = logging.getLogger("apps.stats.tasks")


@shared_task(name="apps.stats.tasks.collect_daily_instance_stats")
def collect_daily_instance_stats(stat_date: str | dt.date | None = None) -> dict:
    """统计指定日期的实例数量。`stat_date` 缺省为服务器本地日期（Asia/Shanghai）。"""
    if stat_date is None:
        target_date = timezone.localdate()
    elif isinstance(stat_date, str):
        target_date = dt.date.fromisoformat(stat_date)
    else:
        target_date = stat_date

    # 1) 一次聚合拿到「部门 × 集群」的实例数
    aggregated = (
        DBInstance.objects.values("cluster__department_id", "cluster_id")
        .annotate(total=Count("id"))
        .order_by()
    )
    counts = {(row["cluster__department_id"], row["cluster_id"]): row["total"] for row in aggregated}

    # 2) 以集群为基准补零，保证快照完整
    cluster_rows = Cluster.objects.values_list("id", "department_id")
    now = timezone.now()
    objects = [
        InstanceStat(
            stat_date=target_date,
            department_id=department_id,
            cluster_id=cluster_id,
            instance_count=counts.get((department_id, cluster_id), 0),
            updated_at=now,
        )
        for cluster_id, department_id in cluster_rows
    ]

    if not objects:
        logger.warning("没有任何集群，跳过 %s 的统计", target_date)
        return {"stat_date": str(target_date), "rows": 0}

    with transaction.atomic():
        InstanceStat.objects.bulk_create(
            objects,
            batch_size=500,
            update_conflicts=True,  # 命中唯一约束则更新，保证任务可重跑
            unique_fields=["stat_date", "department", "cluster"],
            update_fields=["instance_count", "updated_at"],
        )

    total_instances = sum(obj.instance_count for obj in objects)
    logger.info(
        "统计完成 date=%s rows=%s total_instances=%s", target_date, len(objects), total_instances
    )
    return {
        "stat_date": str(target_date),
        "rows": len(objects),
        "total_instances": total_instances,
    }
