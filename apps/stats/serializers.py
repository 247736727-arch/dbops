from rest_framework import serializers

from apps.stats.models import InstanceStat


class InstanceStatSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True)
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    engine = serializers.CharField(source="cluster.engine", read_only=True)

    class Meta:
        model = InstanceStat
        fields = [
            "id",
            "stat_date",
            "department",
            "department_name",
            "department_code",
            "cluster",
            "cluster_name",
            "engine",
            "instance_count",
            "updated_at",
        ]
        read_only_fields = fields
