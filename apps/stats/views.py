"""统计结果的只读查询接口 + 手动触发入口。"""

from __future__ import annotations

import django_filters
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from apps.stats.models import InstanceStat
from apps.stats.serializers import InstanceStatSerializer
from apps.stats.tasks import collect_daily_instance_stats


class InstanceStatFilter(django_filters.FilterSet):
    stat_date = django_filters.DateFilter(field_name="stat_date")
    date_from = django_filters.DateFilter(field_name="stat_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="stat_date", lookup_expr="lte")
    department = django_filters.NumberFilter(field_name="department_id")
    cluster = django_filters.NumberFilter(field_name="cluster_id")

    class Meta:
        model = InstanceStat
        fields = ["stat_date", "date_from", "date_to", "department", "cluster"]


class InstanceStatViewSet(viewsets.ReadOnlyModelViewSet):
    """统计结果只读查询：/api/instance-stats/?stat_date=2026-10-28"""

    serializer_class = InstanceStatSerializer
    filterset_class = InstanceStatFilter
    search_fields = ["department__name", "cluster__name"]
    ordering_fields = ["stat_date", "instance_count"]

    def get_queryset(self):
        return InstanceStat.objects.select_related("department", "cluster")

    @action(detail=False, methods=["post"], url_path="collect", permission_classes=[IsAdminUser])
    def collect(self, request):
        """手动触发一次统计（等价于 beat 在 00:00 做的事，方便验收/补跑）。

        同步调用而非 .delay()：调用方要立刻拿到结果确认，且统计是毫秒级操作；
        规模变大后应改为投递队列并返回 task_id。
        """
        stat_date = request.data.get("stat_date")
        result = collect_daily_instance_stats(stat_date)
        return Response(result)
