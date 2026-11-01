"""初始化演示数据：`python manage.py seed_demo`

幂等，可重复执行。同时创建一个可用的 API Token，方便 curl 验收。
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from apps.inventory.models import Cluster, DBInstance, Department

DEMO_DATA = {
    "交易": {
        "code": "TRADE",
        "owner": "张伟",
        "clusters": [
            ("trade-order-mysql", "mysql", "prod", ["trade-order-01", "trade-order-02"]),
            ("trade-cache-redis", "redis", "prod", ["trade-redis-01"]),
        ],
    },
    "风控": {
        "code": "RISK",
        "owner": "李娜",
        "clusters": [
            ("risk-mysql", "mysql", "prod", ["risk-db-01"]),
            ("risk-mysql-staging", "mysql", "staging", ["risk-db-stg"]),
        ],
    },
    "数据平台": {
        "code": "DATA",
        "owner": "王强",
        "clusters": [
            ("dw-postgres", "postgresql", "prod", ["dw-master", "dw-replica"]),
            # 这个集群故意不放实例：用于验证统计任务会补 0
            ("dw-mysql-archive", "mysql", "test", []),
        ],
    },
}


class Command(BaseCommand):
    help = "创建演示用的部门 / 集群 / 实例 / 用户 Token"

    @transaction.atomic
    def handle(self, *args, **options):
        created_instances = 0
        port_cursor = 3306

        for dept_name, payload in DEMO_DATA.items():
            department, _ = Department.objects.get_or_create(
                code=payload["code"],
                defaults={"name": dept_name, "owner": payload["owner"], "description": f"{dept_name}部门"},
            )
            for cluster_name, engine, environment, instance_names in payload["clusters"]:
                cluster, _ = Cluster.objects.get_or_create(
                    department=department,
                    name=cluster_name,
                    defaults={"engine": engine, "environment": environment},
                )
                for index, instance_name in enumerate(instance_names):
                    port_cursor += 1
                    instance, created = DBInstance.objects.get_or_create(
                        cluster=cluster,
                        name=instance_name,
                        defaults={
                            "host": "127.0.0.1",
                            "port": port_cursor,
                            "username": f"{payload['code'].lower()}_app",
                            "role": DBInstance.Role.MASTER if index == 0 else DBInstance.Role.SLAVE,
                        },
                    )
                    if created or not instance.password:
                        instance.set_password("Init#Passw0rd", keep_previous=False)
                        instance.save()
                        created_instances += 1

        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            username="demo",
            defaults={"is_staff": True, "email": "demo@example.com"},
        )
        user.set_password("demo123456")
        user.save()
        token, _ = Token.objects.get_or_create(user=user)

        self.stdout.write(self.style.SUCCESS(f"演示数据就绪，新建/初始化实例 {created_instances} 个"))
        self.stdout.write("")
        self.stdout.write("接口调用示例：")
        self.stdout.write(f'  curl -H "Authorization: Token {token.key}" http://127.0.0.1:8000/api/instances/')
        self.stdout.write("")
        self.stdout.write("账号：demo / demo123456 （is_staff=True，可调管理员接口）")
