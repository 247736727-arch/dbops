"""序列化层。

密码只进不出：`password` 是 write_only，创建/更新时可传，任何响应里都不会出现；
列表只回 `has_password` 与 `masked_password`（脱敏），要拿明文必须走 `credential`
接口（管理员权限 + 审计日志）。
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator

from apps.inventory.models import Cluster, DBInstance, Department, PasswordRotationLog, ProbeLog
from apps.inventory.services import InvalidProbeTarget, validate_target


class DepartmentSerializer(serializers.ModelSerializer):
    cluster_count = serializers.SerializerMethodField(help_text="该部门下的集群数量")

    class Meta:
        model = Department
        fields = [
            "id",
            "name",
            "code",
            "owner",
            "description",
            "is_active",
            "cluster_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_cluster_count(self, obj) -> int:
        # 列表接口由 ViewSet 用 annotate 预计算，避免 N+1；单条时才回落到 count()
        value = getattr(obj, "cluster_count", None)
        return value if value is not None else obj.clusters.count()


class ClusterSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    instance_count = serializers.SerializerMethodField(help_text="该集群下的实例数量")
    engine_display = serializers.CharField(source="get_engine_display", read_only=True)
    environment_display = serializers.CharField(source="get_environment_display", read_only=True)

    class Meta:
        model = Cluster
        fields = [
            "id",
            "name",
            "department",
            "department_name",
            "engine",
            "engine_display",
            "environment",
            "environment_display",
            "description",
            "instance_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = [
            UniqueTogetherValidator(
                queryset=Cluster.objects.all(),
                fields=["department", "name"],
                message="同一部门下已存在同名集群",
            )
        ]

    def get_instance_count(self, obj) -> int:
        value = getattr(obj, "instance_count", None)
        return value if value is not None else obj.instances.count()


class DBInstanceSerializer(serializers.ModelSerializer):
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    department_id = serializers.IntegerField(source="cluster.department_id", read_only=True)
    department_name = serializers.CharField(source="cluster.department.name", read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    endpoint = serializers.CharField(read_only=True)
    has_password = serializers.BooleanField(read_only=True)
    masked_password = serializers.CharField(read_only=True)
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,  # 密码不能因为首尾空格被悄悄改写
        help_text="明文密码，服务端加密后落库；任何接口都不会回显",
    )

    class Meta:
        model = DBInstance
        fields = [
            "id",
            "cluster",
            "cluster_name",
            "department_id",
            "department_name",
            "name",
            "role",
            "role_display",
            "host",
            "port",
            "username",
            "password",
            "has_password",
            "masked_password",
            "status",
            "status_display",
            "remark",
            "password_rotated_at",
            "previous_password_expires_at",
            "endpoint",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "password_rotated_at",
            "previous_password_expires_at",
            "created_at",
            "updated_at",
        ]
        validators = [
            UniqueTogetherValidator(
                queryset=DBInstance.objects.all(),
                fields=["cluster", "name"],
                message="同一集群下已存在同名实例",
            )
        ]

    def validate(self, attrs):
        # 部分更新时字段可能缺失，用实例上的现值补齐再做校验
        host = attrs.get("host", getattr(self.instance, "host", None))
        port = attrs.get("port", getattr(self.instance, "port", None))
        if host and port:
            try:
                validate_target(host, port)
            except InvalidProbeTarget as exc:
                raise serializers.ValidationError({"host": str(exc)}) from exc
            duplicated = DBInstance.objects.filter(host=host, port=port)
            if self.instance is not None:
                duplicated = duplicated.exclude(pk=self.instance.pk)
            if duplicated.exists():
                raise serializers.ValidationError({"host": f"{host}:{port} 已存在其它实例"})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password", "")
        instance = DBInstance(**validated_data)
        if password:
            instance.set_password(password, keep_previous=False)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class ProbeLogSerializer(serializers.ModelSerializer):
    reason_display = serializers.CharField(source="get_reason_display", read_only=True)

    class Meta:
        model = ProbeLog
        fields = [
            "id",
            "instance",
            "reachable",
            "reason",
            "reason_display",
            "detail",
            "latency_ms",
            "checked_at",
        ]
        read_only_fields = fields


class PasswordRotationLogSerializer(serializers.ModelSerializer):
    trigger_display = serializers.CharField(source="get_trigger_display", read_only=True)

    class Meta:
        model = PasswordRotationLog
        fields = ["id", "instance", "success", "trigger", "trigger_display", "message", "created_at"]
        read_only_fields = fields
