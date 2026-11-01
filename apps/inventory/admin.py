from django.contrib import admin

from apps.inventory.models import Cluster, DBInstance, Department, PasswordRotationLog, ProbeLog


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "owner", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "owner")


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "department", "engine", "environment", "created_at")
    list_filter = ("engine", "environment", "department")
    search_fields = ("name", "department__name")
    autocomplete_fields = ("department",)


@admin.register(DBInstance)
class DBInstanceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "endpoint",
        "cluster",
        "role",
        "status",
        "masked_password",
        "password_rotated_at",
    )
    list_filter = ("role", "status", "cluster__department", "cluster__engine")
    search_fields = ("name", "host", "username", "cluster__name")
    autocomplete_fields = ("cluster",)
    # 绝不在 admin 里直接展示/编辑明文密码：展示脱敏值，修改走接口或管理命令
    readonly_fields = ("masked_password", "password_rotated_at", "previous_password_expires_at")
    exclude = ("password", "previous_password")
    fieldsets = (
        ("基础信息", {"fields": ("cluster", "name", "role", "host", "port", "username", "status")}),
        ("凭据", {"fields": ("masked_password", "password_rotated_at", "previous_password_expires_at")}),
        ("其它", {"fields": ("remark",)}),
    )


@admin.register(ProbeLog)
class ProbeLogAdmin(admin.ModelAdmin):
    list_display = ("instance", "reachable", "reason", "latency_ms", "checked_at")
    list_filter = ("reachable", "reason")
    search_fields = ("instance__name", "instance__host")
    date_hierarchy = "checked_at"


@admin.register(PasswordRotationLog)
class PasswordRotationLogAdmin(admin.ModelAdmin):
    list_display = ("instance", "success", "trigger", "message", "created_at")
    list_filter = ("success", "trigger")
    search_fields = ("instance__name", "instance__host")
    date_hierarchy = "created_at"
