"""第 2 题补充：OpenAPI Schema 与 Swagger 文档端点可达性。

这两个端点最容易在改造 urls.py / serializers.py 之后悄悄坏掉
（比如某个 Serializer 的 source 写错会直接导致 schema 生成 500），
但普通 CRUD 测试覆盖不到，所以单独加一条兜底断言。
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
