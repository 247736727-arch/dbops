"""第 1/2 题：三层模型的 CRUD + 主动探测接口。"""

from __future__ import annotations

import logging

from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from apps.inventory.models import Cluster, DBInstance, Department
from apps.inventory.serializers import (
    ClusterSerializer,
    DBInstanceSerializer,
    DepartmentSerializer,
    PasswordRotationLogSerializer,
    ProbeLogSerializer,
)
from apps.inventory.services import InvalidProbeTarget, probe_instance
from apps.inventory.tasks import rotate_instance_password

logger = logging.getLogger("apps.inventory.views")


class DepartmentViewSet(viewsets.ModelViewSet):
    """部门 CRUD。"""

    serializer_class = DepartmentSerializer
    filterset_fields = ["is_active"]
    search_fields = ["name", "code", "owner"]
    ordering_fields = ["name", "created_at", "cluster_count"]

    def get_queryset(self):
        # annotate 预计算集群数量，避免序列化时每行一次 count() 查询
        # annotate 会让 Meta.ordering 失效 → 必须显式排序，否则分页结果不稳定
        return (
            Department.objects.annotate(cluster_count=Count("clusters", distinct=True))
            .order_by("name")
        )


class ClusterViewSet(viewsets.ModelViewSet):
    """集群 CRUD。"""

    serializer_class = ClusterSerializer
    filterset_fields = ["department", "engine", "environment"]
    search_fields = ["name", "department__name"]
    ordering_fields = ["name", "created_at", "instance_count"]

    def get_queryset(self):
        # select_related 防 N+1；显式排序避免分页结果不稳定
        return (
            Cluster.objects.select_related("department")
            .annotate(instance_count=Count("instances", distinct=True))
            .order_by("department__name", "name")
        )


class DBInstanceViewSet(viewsets.ModelViewSet):
    """数据库实例 CRUD，以及探测 / 凭据 / 轮换等动作接口。"""

    serializer_class = DBInstanceSerializer
    filterset_fields = ["cluster", "cluster__department", "role", "status", "username"]
    search_fields = ["name", "host", "username", "cluster__name"]
    ordering_fields = ["name", "created_at", "password_rotated_at"]

    def get_queryset(self):
        # 列表页会用到 cluster.name / department.name，必须 select_related 防 N+1
        return DBInstance.objects.select_related("cluster", "cluster__department")

    # ------------------------------------------------------------ 第 2 题
    @action(detail=True, methods=["post"], url_path="probe")
    def probe(self, request, pk=None):
        """探测该实例的 TCP 端口是否可达，并把结果写入探测记录表。"""
        instance = self.get_object()
        try:
            log = probe_instance(instance)
        except InvalidProbeTarget as exc:
            return Response({"detail": str(exc)}, status=400)

        data = ProbeLogSerializer(log).data
        data["endpoint"] = instance.endpoint
        return Response(data, status=200)

    @action(detail=True, methods=["get"], url_path="probe-logs")
    def probe_logs(self, request, pk=None):
        """该实例的历史探测记录（倒序分页）。"""
        instance = self.get_object()
        queryset = instance.probe_logs.all()
        page = self.paginate_queryset(queryset)
        serializer = ProbeLogSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    # ------------------------------------------------------------ 第 3 题
    @action(detail=True, methods=["post"], url_path="rotate-password")
    def rotate_password(self, request, pk=None):
        """手动触发单个实例的密码轮换（演示/应急用，默认强制）。"""
        instance = self.get_object()
        force = request.data.get("force", True)
        changed = rotate_instance_password(instance.pk, force=bool(force), trigger="api")
        instance.refresh_from_db()
        return Response(
            {
                "instance": instance.pk,
                "rotated": changed,
                "password_rotated_at": instance.password_rotated_at,
                "previous_password_expires_at": instance.previous_password_expires_at,
            }
        )

    @action(detail=True, methods=["get"], url_path="rotation-logs")
    def rotation_logs(self, request, pk=None):
        instance = self.get_object()
        queryset = instance.rotation_logs.all()
        page = self.paginate_queryset(queryset)
        serializer = PasswordRotationLogSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="credential", permission_classes=[IsAdminUser])
    def credential(self, request, pk=None):
        """取用明文凭据：仅管理员可用。

        真实系统里这一步应该再加审批流 + 操作审计 + 短期有效令牌，
        这里仅做权限收敛，并把访问行为写入日志，避免"谁都能拿到生产库密码"。
        """
        instance = self.get_object()
        logger.warning(
            "凭据被读取 instance=%s user=%s ip=%s",
            instance.pk,
            getattr(request.user, "username", None),
            request.META.get("REMOTE_ADDR"),
        )
        return Response(
            {
                "instance": instance.pk,
                "endpoint": instance.endpoint,
                "username": instance.username,
                "password": instance.password,
                "password_rotated_at": instance.password_rotated_at,
            }
        )
