"""OpenAPI Schema 与 Swagger 文档端点可达性。

改 urls.py / serializers.py 后这两个端点最容易悄悄坏掉（Serializer 的 source
写错会直接让 schema 生成 500），而普通 CRUD 测试覆盖不到，故单独加兜底断言。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


class TestApiDocs:
    def test_schema_endpoint(self, auth_client, instance):
        response = auth_client.get("/api/schema/")
        assert response.status_code == 200
        content = response.content.decode()
        assert "/api/instances/" in content
        assert "/api/instance-stats/" in content

    def test_swagger_ui_endpoint(self, auth_client, instance):
        response = auth_client.get("/api/docs/")
        assert response.status_code == 200
