"""CRUD 接口 + TCP 探测接口测试（网络层用 mock，不依赖真实网络）。"""

from __future__ import annotations

import socket

import pytest
from django.conf import settings

from apps.inventory.models import DBInstance, ProbeLog

pytestmark = pytest.mark.django_db


def _patch_socket(monkeypatch, *, error: Exception | None = None, captured: dict | None = None):
    """替换 socket.create_connection，并记录调用参数（用于断言超时确实被传入）。"""

    class _FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_create_connection(address, timeout=None, *args, **kwargs):
        if captured is not None:
            captured["address"] = address
            captured["timeout"] = timeout
        if error is not None:
            raise error
        return _FakeSocket()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)


class TestProbeApi:
    def test_probe_reachable(self, auth_client, instance, monkeypatch):
        captured: dict = {}
        _patch_socket(monkeypatch, captured=captured)

        response = auth_client.post(f"/api/instances/{instance.pk}/probe/")

        assert response.status_code == 200
        body = response.json()
        assert body["reachable"] is True
        assert body["reason"] == "ok"
        assert body["latency_ms"] is not None
        assert body["endpoint"] == f"{instance.host}:{instance.port}"
        # 超时必须显式传入，否则不可达目标会长期占用 worker
        assert captured["timeout"] == settings.PROBE_TIMEOUT_SECONDS
        assert captured["address"] == (instance.host, instance.port)

        log = ProbeLog.objects.get()
        assert log.reachable is True and log.instance_id == instance.pk

    def test_probe_timeout(self, auth_client, instance, monkeypatch):
        _patch_socket(monkeypatch, error=socket.timeout("timed out"))
        body = auth_client.post(f"/api/instances/{instance.pk}/probe/").json()
        assert body["reachable"] is False
        assert body["reason"] == ProbeLog.Reason.TIMEOUT

    def test_probe_connection_refused(self, auth_client, instance, monkeypatch):
        _patch_socket(monkeypatch, error=ConnectionRefusedError(10061, "refused"))
        body = auth_client.post(f"/api/instances/{instance.pk}/probe/").json()
        assert body["reachable"] is False
        assert body["reason"] == ProbeLog.Reason.REFUSED

    def test_probe_dns_error(self, auth_client, instance, monkeypatch):
        _patch_socket(monkeypatch, error=socket.gaierror(11001, "getaddrinfo failed"))
        body = auth_client.post(f"/api/instances/{instance.pk}/probe/").json()
        assert body["reachable"] is False
        assert body["reason"] == ProbeLog.Reason.DNS_ERROR

    def test_probe_rejects_invalid_target(self, auth_client, instance, monkeypatch):
        DBInstance.objects.filter(pk=instance.pk).update(host="0.0.0.0")
        response = auth_client.post(f"/api/instances/{instance.pk}/probe/")
        assert response.status_code == 400

    def test_probe_logs_endpoint(self, auth_client, instance, monkeypatch):
        _patch_socket(monkeypatch)
        auth_client.post(f"/api/instances/{instance.pk}/probe/")
        response = auth_client.get(f"/api/instances/{instance.pk}/probe-logs/")
        assert response.status_code == 200
        assert response.json()["count"] == 1


class TestInstanceCrud:
    def test_requires_authentication(self, api_client):
        assert api_client.get("/api/instances/").status_code == 401

    def test_list_instances(self, auth_client, instance):
        response = auth_client.get("/api/instances/")
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        row = payload["results"][0]
        assert row["cluster_name"] == instance.cluster.name
        assert row["department_name"] == instance.cluster.department.name

    def test_create_instance_encrypts_password_and_never_echoes_it(self, auth_client, cluster):
        response = auth_client.post(
            "/api/instances/",
            {
                "cluster": cluster.pk,
                "name": "new-db-01",
                "host": "127.0.0.1",
                "port": 3310,
                "username": "app",
                "password": "Plain#Secret123",
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert "password" not in body
        assert body["has_password"] is True
        assert "Plain#Secret123" not in response.content.decode()

        created = DBInstance.objects.get(pk=body["id"])
        assert created.password == "Plain#Secret123"
        assert created.password_rotated_at is not None

    def test_duplicate_endpoint_rejected(self, auth_client, instance, department):
        from apps.inventory.models import Cluster

        other_cluster = Cluster.objects.create(name="c2", department=department)
        response = auth_client.post(
            "/api/instances/",
            {
                "cluster": other_cluster.pk,
                "name": "dup",
                "host": instance.host,
                "port": instance.port,
            },
            format="json",
        )
        assert response.status_code == 400
        assert "host" in response.json()

    def test_duplicate_name_in_same_cluster_rejected(self, auth_client, instance):
        response = auth_client.post(
            "/api/instances/",
            {
                "cluster": instance.cluster_id,
                "name": instance.name,
                "host": "127.0.0.1",
                "port": 3399,
            },
            format="json",
        )
        assert response.status_code == 400

    def test_update_and_delete(self, auth_client, instance):
        response = auth_client.patch(
            f"/api/instances/{instance.pk}/", {"remark": "扩容中"}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["remark"] == "扩容中"

        assert auth_client.delete(f"/api/instances/{instance.pk}/").status_code == 204
        assert not DBInstance.objects.filter(pk=instance.pk).exists()

    def test_department_cluster_list_counts(self, auth_client, instance):
        departments = auth_client.get("/api/departments/").json()["results"]
        assert departments[0]["cluster_count"] == 1
        clusters = auth_client.get("/api/clusters/").json()["results"]
        assert clusters[0]["instance_count"] == 1


class TestCredentialPermission:
    def test_normal_user_forbidden(self, auth_client, instance):
        response = auth_client.get(f"/api/instances/{instance.pk}/credential/")
        assert response.status_code == 403

    def test_admin_can_read_plaintext(self, admin_client, instance):
        response = admin_client.get(f"/api/instances/{instance.pk}/credential/")
        assert response.status_code == 200
        assert response.json()["password"] == "Init#Passw0rd"
