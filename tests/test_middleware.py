"""请求耗时中间件测试——重点验证异常路径同样被统计、噪声路径被跳过。"""

from __future__ import annotations

import logging

import pytest
from django.test import RequestFactory

from apps.core.middleware import RequestTimingMiddleware

TIMING_LOGGER = logging.getLogger("request.timing")


@pytest.fixture
def logged_timings(monkeypatch):
    """接管 request.timing 的 info 调用，代替真实日志作为断言对象。

    这里替换 logger 方法而不用 caplog——该 logger 配了 propagate=False，caplog 抓不到。
    """
    records: list[dict] = []

    monkeypatch.setattr(
        TIMING_LOGGER,
        "info",
        lambda msg, *a, **kw: records.append({"args": a, "extra": kw.get("extra", {})}),
    )
    return records


@pytest.mark.django_db
class TestRequestTiming:
    def test_adds_header_and_logs(self, auth_client, instance, logged_timings):
        response = auth_client.get("/api/instances/")

        assert response.status_code == 200
        assert response["X-Request-Time"].endswith("ms")

        extra = logged_timings[-1]["extra"]
        assert extra["method"] == "GET"
        assert extra["path"] == "/api/instances/"
        assert extra["status_code"] == 200
        assert extra["duration_ms"] >= 0

    def test_exception_is_still_measured(self, logged_timings):
        """500 请求恰恰是最需要被统计的，必须走到 finally。"""

        def boom(request):
            raise RuntimeError("视图炸了")

        middleware = RequestTimingMiddleware(boom)
        request = RequestFactory().get("/api/instances/")

        with pytest.raises(RuntimeError):
            middleware(request)

        extra = logged_timings[-1]["extra"]
        assert extra["status_code"] == 500  # 拿不到响应时按 500 记，而不是把这条数据丢掉

    def test_duration_is_attached_to_request(self, logged_timings):
        captured = {}

        def view(request):
            captured["request"] = request
            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = RequestTimingMiddleware(view)
        request = RequestFactory().get("/api/instances/")
        middleware(request)

        assert captured["request"].duration_ms >= 0

    def test_health_check_is_skipped(self, api_client, logged_timings):
        response = api_client.get("/healthz/")

        assert response.status_code == 200
        assert "X-Request-Time" not in response
        assert logged_timings == []

    def test_static_prefix_is_skipped(self, logged_timings):
        called = {"count": 0}

        def view(request):
            called["count"] += 1
            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = RequestTimingMiddleware(view)
        middleware(RequestFactory().get("/static/app.css"))

        assert called["count"] == 1
        assert logged_timings == []
