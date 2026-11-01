from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.inventory.models import Cluster, DBInstance, Department


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="tester", password="tester-pass")


@pytest.fixture
def auth_client(user) -> APIClient:
    # 每个 fixture 必须用**独立**的 client：APIClient.force_authenticate 是挂在实例上的，
    # 共用实例会让前一个 fixture 认证状态被后一个覆盖（曾导致"普通用户竟通过管理员校验"的假绿）
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def admin_client(db) -> APIClient:
    client = APIClient()
    admin = get_user_model().objects.create_superuser(
        username="root", email="root@example.com", password="root-pass"
    )
    client.force_authenticate(user=admin)
    return client


@pytest.fixture
def department(db) -> Department:
    return Department.objects.create(name="交易", code="TRADE", owner="张伟")


@pytest.fixture
def cluster(department) -> Cluster:
    return Cluster.objects.create(
        name="trade-mysql", department=department, engine=Cluster.Engine.MYSQL, environment="prod"
    )


@pytest.fixture
def instance(cluster) -> DBInstance:
    obj = DBInstance(
        cluster=cluster,
        name="trade-db-01",
        host="127.0.0.1",
        port=3306,
        username="trade_app",
    )
    obj.set_password("Init#Passw0rd", keep_previous=False)
    obj.save()
    return obj


@pytest.fixture
def due_instance(instance) -> DBInstance:
    """把最近轮换时间往前拨过周期线，模拟「已到 12h 轮换周期」的实例。

    注意 `instance` fixture 刚创建完，`password_rotated_at` 是"现在"，
    直接跑批量任务会被幂等保护跳过——需要显式把它变成"到期"。
    """
    DBInstance.objects.filter(pk=instance.pk).update(
        password_rotated_at=timezone.now() - timedelta(hours=13)
    )
    instance.refresh_from_db()
    return instance
