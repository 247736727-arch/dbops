"""密码轮换任务测试——重点验证幂等与故障隔离。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inventory.models import DBInstance, PasswordRotationLog
from apps.inventory.tasks import (
    generate_password,
    rotate_all_instance_passwords,
    rotate_instance_password,
)

pytestmark = pytest.mark.django_db

PLAINTEXT = "Init#Passw0rd"


def _make_instance(department, *, name: str, port: int, rotated_hours_ago: int) -> DBInstance:
    """创建一个「指定多久没轮换过」的实例。

    批量任务按 `password_rotated_at` 升序处理（最久没轮换的排最前，
    其中 NULL 视为最旧）。想让处理顺序稳定可预期，就必须显式给出轮换时间，
    不能依赖 create 时的默认值。
    """
    from apps.inventory.models import Cluster

    cluster = Cluster.objects.create(name=f"cluster-{name}", department=department)
    instance = DBInstance.objects.create(
        cluster=cluster,
        name=name,
        host="127.0.0.1",
        port=port,
        username="app",
    )
    DBInstance.objects.filter(pk=instance.pk).update(
        password_rotated_at=timezone.now() - timedelta(hours=rotated_hours_ago)
    )
    instance.refresh_from_db()
    return instance


class TestSingleRotation:
    def test_rotate_changes_password_and_keeps_previous(self, instance):
        old_password = instance.password

        assert rotate_instance_password(instance.pk, force=True) is True

        instance.refresh_from_db()
        assert instance.password != old_password
        assert len(instance.password) >= 32
        # 双密码过渡期：旧密码必须还在，否则应用侧会瞬间连不上库
        assert instance.previous_password == old_password
        assert instance.previous_password_expires_at is not None

        log = PasswordRotationLog.objects.get()
        assert log.success is True
        assert log.trigger == PasswordRotationLog.Trigger.BEAT

    def test_first_rotation_without_previous_password(self, department):
        """回归测试：新建、尚未录入密码的实例不能让轮换任务报错。

        曾有缺陷：`previous_password_expires_at` 为空时对 None 做格式化 → TypeError，
        导致这类实例在每次调度里都被判为"轮换失败"。
        """
        instance = _make_instance(department, name="fresh-db", port=3399, rotated_hours_ago=13)

        assert rotate_instance_password(instance.pk, force=True) is True

        instance.refresh_from_db()
        assert instance.password
        assert instance.previous_password == ""
        assert PasswordRotationLog.objects.get().message.endswith("无（首次设置））")

    def test_rotate_missing_instance_returns_false(self, db):
        assert rotate_instance_password(999_999, force=True) is False

    def test_generated_password_is_random(self):
        first, second = generate_password(), generate_password()
        assert first != second
        assert len(first) >= 32  # token_urlsafe(24) ≈ 32 字符，约 144bit 熵

    def test_rotation_records_previous_and_new_ciphertext_separately(self, instance):
        rotate_instance_password(instance.pk, force=True)
        instance.refresh_from_db()

        newest, previous = instance.password, instance.previous_password
        assert newest != PLAINTEXT
        assert previous == PLAINTEXT

        # 再次轮换应把上一版继续往后挪，而不是丢掉
        rotate_instance_password(instance.pk, force=True)
        instance.refresh_from_db()
        assert instance.previous_password == newest


class TestBatchIdempotency:
    def test_second_run_within_interval_is_skipped(self, due_instance):
        """核心性质：任务被重试/重复投递时，密码不能被换两次。"""
        instance = due_instance
        first = rotate_all_instance_passwords()
        instance.refresh_from_db()
        password_after_first_run = instance.password

        assert first["candidates"] == 1
        assert first["rotated"] == 1

        second = rotate_all_instance_passwords()
        instance.refresh_from_db()

        assert second["candidates"] == 0, "12 小时内已轮换的实例不应再进入候选"
        assert second["rotated"] == 0
        assert instance.password == password_after_first_run

    def test_force_overrides_interval(self, due_instance):
        instance = due_instance
        rotate_all_instance_passwords()
        instance.refresh_from_db()
        password = instance.password

        rotate_all_instance_passwords(force=True)
        instance.refresh_from_db()

        assert instance.password != password
        assert instance.previous_password == password

    def test_due_instances_are_picked_up_again(self, instance):
        rotate_all_instance_passwords()  # 此时未到期，什么都不做
        assert instance.previous_password == ""

        DBInstance.objects.filter(pk=instance.pk).update(
            password_rotated_at=timezone.now() - timedelta(hours=13)
        )
        assert rotate_all_instance_passwords()["rotated"] == 1

    def test_offline_instances_are_excluded(self, due_instance):
        DBInstance.objects.filter(pk=due_instance.pk).update(status=DBInstance.Status.OFFLINE)
        assert rotate_all_instance_passwords()["candidates"] == 0

    def test_batch_size_limits_one_run(self, due_instance, department):
        """批次上限生效：超限的实例留到下一次，而不是一次性打满数据库。"""
        oldest = _make_instance(department, name="db-b", port=3390, rotated_hours_ago=20)

        summary = rotate_all_instance_passwords(batch_size=1)

        assert summary["candidates"] == 1
        assert summary["rotated"] == 1
        # 任务按「最久没轮换」排序，被处理的应该是最旧的那个
        assert PasswordRotationLog.objects.get().instance_id == oldest.pk
        due_instance.refresh_from_db()
        assert due_instance.password_rotated_at is not None  # 本次未处理，保持原样

    def test_never_rotated_instance_is_processed_first(self, department):
        """排序回归：NULL（从未轮换过）必须排在最前，不能依赖各库的默认行为。

        PostgreSQL 默认把 NULL 排在最后、SQLite 排在最前。不显式声明 nulls_first
        的话，同一份代码在两种库上会挑出不同的实例——只在批次被截断时才会暴露。
        """
        never = _make_instance(department, name="db-never", port=3392, rotated_hours_ago=13)
        DBInstance.objects.filter(pk=never.pk).update(password_rotated_at=None)
        _make_instance(department, name="db-old", port=3393, rotated_hours_ago=500)

        summary = rotate_all_instance_passwords(force=True, batch_size=1)

        assert summary["candidates"] == 1
        assert PasswordRotationLog.objects.get().instance_id == never.pk


class TestFaultIsolation:
    def test_single_failure_does_not_stop_the_batch(
        self, due_instance, department, monkeypatch
    ):
        """一个实例失败不能中断整批，且失败必须留痕。"""
        instance = due_instance  # 第 2 个被处理 → 成功
        failing = _make_instance(department, name="db-b", port=3391, rotated_hours_ago=20)

        state = {"calls": 0}

        def flaky_generator(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("mock: 数据库修改密码失败")
            return "NewPassw0rd-Fallback"

        monkeypatch.setattr("apps.inventory.tasks.generate_password", flaky_generator)

        summary = rotate_all_instance_passwords()

        assert summary["rotated"] == 1
        assert summary["failed"] == 1
        assert summary["errors"][0]["instance_id"] == failing.pk
        assert summary["errors"][0]["error"].startswith("mock: 数据库修改密码失败")

        # 成功的那个确实换成了新密码
        instance.refresh_from_db()
        assert instance.password == "NewPassw0rd-Fallback"

        failed_logs = PasswordRotationLog.objects.filter(success=False)
        assert failed_logs.count() == 1
        assert failed_logs.get().instance_id == failing.pk
        assert failed_logs.get().trigger == PasswordRotationLog.Trigger.BEAT

    def test_transaction_rolled_back_on_failure(self, due_instance, monkeypatch):
        """失败时密码不能被写成半成品：要么全成功，要么完全没动。"""
        instance = due_instance
        before = instance.password
        old_password = instance.previous_password

        def always_fail(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("apps.inventory.tasks.generate_password", always_fail)
        rotate_all_instance_passwords()

        instance.refresh_from_db()
        assert instance.password == before
        assert instance.previous_password == old_password
        assert instance.password_rotated_at == due_instance.password_rotated_at


class TestRotationApi:
    def test_manual_rotate_endpoint(self, auth_client, instance):
        before = instance.password
        response = auth_client.post(f"/api/instances/{instance.pk}/rotate-password/")

        assert response.status_code == 200
        assert response.json()["rotated"] is True

        instance.refresh_from_db()
        assert instance.password != before
        assert PasswordRotationLog.objects.get().trigger == PasswordRotationLog.Trigger.API

    def test_rotation_logs_endpoint(self, auth_client, instance):
        rotate_instance_password(instance.pk, force=True)
        body = auth_client.get(f"/api/instances/{instance.pk}/rotation-logs/").json()
        assert body["count"] == 1
        assert body["results"][0]["trigger"] == "beat"
