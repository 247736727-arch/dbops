"""模型与加密字段的单元测试。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import override_settings
from django.utils import timezone

from apps.core.crypto import SecretDecryptionError, decrypt, encrypt, mask
from apps.inventory.models import Cluster, DBInstance, Department

pytestmark = pytest.mark.django_db

PLAINTEXT = "Init#Passw0rd"


class TestEncryptedField:
    def test_password_is_encrypted_in_database(self, instance):
        """落库必须是密文——直接读原始列来验证，而不是读 ORM 属性。"""
        with connection.cursor() as cursor:
            cursor.execute("SELECT password FROM inventory_dbinstance WHERE id = %s", [instance.pk])
            raw = cursor.fetchone()[0]

        assert raw != PLAINTEXT
        assert raw.startswith("gAAAAA"), "Fernet token 以版本字节 0x80 开头"

        instance.refresh_from_db()
        assert instance.password == PLAINTEXT  # ORM 读出的是明文

    def test_same_plaintext_produces_different_ciphertext(self):
        """Fernet 每次加密带随机 IV，所以不能拿密文做等值查询。"""
        assert encrypt(PLAINTEXT) != encrypt(PLAINTEXT)

    def test_key_rotation_is_backward_compatible(self):
        """新密钥放最前面时，旧密钥加密的历史数据仍然解得开。"""
        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()

        with override_settings(FIELD_ENCRYPTION_KEYS=[old_key]):
            ciphertext = encrypt("legacy-secret")

        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key, old_key]):
            assert decrypt(ciphertext) == "legacy-secret"

        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key]):
            with pytest.raises(SecretDecryptionError):
                decrypt(ciphertext)

    def test_masked_password_never_leaks_plaintext(self, instance):
        masked = instance.masked_password
        assert PLAINTEXT not in masked
        assert masked.startswith("In")
        assert "Init#Passw0rd" not in masked

    def test_mask_helper(self):
        assert mask("") == ""
        assert mask("ab") == "**"
        assert mask("abcdefghij") == "ab******ij"


class TestRelations:
    def test_department_with_clusters_cannot_be_deleted(self, department, cluster):
        with pytest.raises(ProtectedError):
            department.delete()

    def test_cluster_with_instances_cannot_be_deleted(self, cluster, instance):
        with pytest.raises(ProtectedError):
            cluster.delete()

    def test_probe_log_is_cascade_deleted_with_instance(self, instance):
        from apps.inventory.models import ProbeLog

        ProbeLog.objects.create(instance=instance, reachable=True, reason=ProbeLog.Reason.OK)
        instance.delete()
        assert ProbeLog.objects.count() == 0

    def test_instance_host_port_must_be_unique(self, instance, department):
        other_cluster = Cluster.objects.create(name="another", department=department)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                DBInstance.objects.create(
                    cluster=other_cluster, name="dup-endpoint", host=instance.host, port=instance.port
                )

    def test_cluster_name_unique_within_department(self, cluster, department):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Cluster.objects.create(name=cluster.name, department=department)

    def test_same_cluster_name_allowed_in_different_departments(self, cluster):
        other_dept = Department.objects.create(name="风控", code="RISK")
        same_name = Cluster.objects.create(name=cluster.name, department=other_dept)
        assert same_name.pk != cluster.pk


class TestPasswordLifecycle:
    def test_set_password_moves_old_value_to_previous(self, instance):
        instance.set_password("BrandNew#2026")
        instance.save()

        instance.refresh_from_db()
        assert instance.password == "BrandNew#2026"
        assert instance.previous_password == PLAINTEXT
        assert instance.previous_password_expires_at is not None

    def test_is_password_due(self, instance):
        assert instance.is_password_due(12) is False  # 刚设置过

        DBInstance.objects.filter(pk=instance.pk).update(
            password_rotated_at=timezone.now() - timedelta(hours=13)
        )
        instance.refresh_from_db()
        assert instance.is_password_due(12) is True

    def test_never_rotated_instance_is_due(self, instance):
        DBInstance.objects.filter(pk=instance.pk).update(password_rotated_at=None)
        instance.refresh_from_db()
        assert instance.is_password_due(12) is True
