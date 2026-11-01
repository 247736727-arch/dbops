from django.contrib import admin

from apps.stats.models import InstanceStat


@admin.register(InstanceStat)
class InstanceStatAdmin(admin.ModelAdmin):
    list_display = ("stat_date", "department", "cluster", "instance_count", "updated_at")
    list_filter = ("stat_date", "department")
    search_fields = ("department__name", "cluster__name")
    date_hierarchy = "stat_date"
    readonly_fields = ("created_at", "updated_at")
