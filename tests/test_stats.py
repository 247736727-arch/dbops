"""第 4 题：每日 00:00 按 部门 × 集群 统计实例数量入库——重点验证补零与幂等重跑。"""

from __future__ import annotations

import pytest

from apps.inventory.models import Cluster, DBInstance, Department
from apps.stats.models import InstanceStat
from apps.stats.tasks import collect_daily_instance_stats

pytestmark = pytest.mark.django_db

STAT_DATE = "2026-10-28"


def _counts(stat_date: str) -> dict[int, int]:
    return {
        row.cluster_id: row.instance_count
        for row in InstanceStat.objects.filter(stat_date=stat_date)
    }


class TestDailyStats:
    def test_counts_per_cluster_and_fills_zero(self, department, cluster, instance):
        # 多一个实例 → 该集群应统计到 2
        DBInstance.objects.create(cluster=cluster, name="slave-02", host="127.0.0.1", port=3380)
        # 没有任何实例的集群也必须出现在快照里（否则报表上看不出"这个集群空了"）
        empty_cluster = Cluster.objects.create(name="empty-cluster", department=department)

        result = collect_daily_instance_stats(STAT_DATE)

        assert result["stat_date"] == STAT_DATE
        assert result["rows"] == 2
        assert _counts(STAT_DATE) == {cluster.id: 2, empty_cluster.id: 0}

    def test_dimension_is_department_x_cluster(self, department, cluster, instance):
        """重名集群在不同部门下必须各算一行，不能因为名字相同被归并。"""
        other_department = Department.objects.create(name="数据平台", code="DATA")
        same_name_cluster = Cluster.objects.create(name=cluster.name, department=other_department)
        DBInstance.objects.create(
            cluster=same_name_cluster, name="dep-db", host="127.0.0.1", port=3382
        )

        result = collect_daily_instance_stats(STAT_DATE)

        assert result["rows"] == 2
        assert InstanceStat.objects.filter(
            stat_date=STAT_DATE, department_id=department.id, cluster=cluster, instance_count=1
        ).exists()
        assert InstanceStat.objects.filter(
            stat_date=STAT_DATE,
            department_id=other_department.id,
            cluster=same_name_cluster,
            instance_count=1,
        ).exists()

    def test_rerun_is_idempotent(self, department, cluster, instance):
        """最常见事故：任务重试一次，当天数据翻倍。"""
        cluster_b = Cluster.objects.create(name="cluster-b", department=department)

        first = collect_daily_instance_stats(STAT_DATE)
        second = collect_daily_instance_stats(STAT_DATE)
        third = collect_daily_instance_stats(STAT_DATE)

        assert first["rows"] == second["rows"] == third["rows"] == 2
        # 无论如何重跑，(date, department, cluster) 只能有一行
        assert InstanceStat.objects.filter(stat_date=STAT_DATE).count() == 2
        assert _counts(STAT_DATE) == {cluster.id: 1, cluster_b.id: 0}

    def test_rerun_updates_the_number(self, cluster, instance):
        collect_daily_instance_stats(STAT_DATE)
        assert _counts(STAT_DATE)[cluster.id] == 1

        DBInstance.objects.create(cluster=cluster, name="new-02", host="127.0.0.1", port=3381)
        collect_daily_instance_stats(STAT_DATE)
        assert _counts(STAT_DATE)[cluster.id] == 2

        DBInstance.objects.filter(name="new-02").delete()
        collect_daily_instance_stats(STAT_DATE)
        assert _counts(STAT_DATE)[cluster.id] == 1

    def test_defaults_to_localdate(self, cluster, instance):
        result = collect_daily_instance_stats()
        assert InstanceStat.objects.filter(stat_date=result["stat_date"]).exists()

    def test_unique_constraint_blocks_manual_duplicate(self, cluster, instance):
        from django.db import IntegrityError, transaction

        collect_daily_instance_stats(STAT_DATE)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                InstanceStat.objects.create(
                    stat_date=STAT_DATE, department=cluster.department, cluster=cluster, instance_count=9
                )

    def test_no_clusters_is_a_no_op(self, db):
        assert collect_daily_instance_stats(STAT_DATE)["rows"] == 0
        assert InstanceStat.objects.count() == 0


class TestStatsApi:
    def test_query_requires_auth(self, api_client, cluster, instance):
        collect_daily_instance_stats(STAT_DATE)
        assert api_client.get("/api/instance-stats/").status_code == 401

    def test_query_and_filter_by_date(self, auth_client, cluster, instance):
        collect_daily_instance_stats(STAT_DATE)

        response = auth_client.get("/api/instance-stats/", {"stat_date": STAT_DATE})
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        row = payload["results"][0]
        assert row["instance_count"] == 1
        assert row["department_name"] == cluster.department.name
        assert row["cluster_name"] == cluster.name

        other_day = auth_client.get("/api/instance-stats/", {"stat_date": "2020-01-01"})
        assert other_day.json()["count"] == 0

    def test_stats_list_is_read_only(self, auth_client, cluster, instance):
        response = auth_client.post("/api/instance-stats/", {"instance_count": 1}, format="json")
        assert response.status_code in (403, 405)

    def test_manual_collect_requires_admin(self, auth_client, admin_client, cluster, instance):
        assert auth_client.post("/api/instance-stats/collect/").status_code == 403

        response = admin_client.post(
            "/api/instance-stats/collect/", {"stat_date": STAT_DATE}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["rows"] == 1
