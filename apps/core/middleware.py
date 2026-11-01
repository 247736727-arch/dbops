"""请求耗时统计中间件。"""

from __future__ import annotations

import logging
from time import perf_counter

logger = logging.getLogger("request.timing")


class RequestTimingMiddleware:
    """请求耗时统计中间件。

    - `perf_counter()` 而非 `time.time()`：单调递增，不受 NTP 校时影响。
    - `try/finally`：500 请求才是排查的主角，必须同样被统计到。
      只在正常分支记日志，最需要的数据恰好会缺失。
    - 只写日志不写库：每次请求一次 INSERT 会把读流量放大成写流量，高 QPS 下直接
      成为瓶颈；确实要落库应走异步队列 + 采样，而不是在请求链路里同步写。
    - 跳过静态资源与健康检查：量大且没有观测价值。

    ASGI 下 Django 会把同步中间件丢进线程池（不阻塞事件循环，但占线程容量）；
    纯异步栈应再实现 `async def __acall__`。
    """

    # 这些前缀的请求不统计
    SKIP_PATH_PREFIXES = ("/static/", "/media/", "/favicon.ico")
    SKIP_PATHS = ("/healthz/", "/healthz")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_skip(request.path_info):
            return self.get_response(request)

        started_at = perf_counter()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            duration_ms = (perf_counter() - started_at) * 1000
            status_code = getattr(response, "status_code", 500)
            # 挂到 request 上，方便同一请求链路内的其他代码（日志、埋点）复用
            request.duration_ms = duration_ms
            logger.info(
                "%s %s -> %s %.2fms",
                request.method,
                request.get_full_path(),
                status_code,
                duration_ms,
                extra={
                    "method": request.method,
                    "path": request.path,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "user_id": getattr(getattr(request, "user", None), "id", None),
                    # 流式响应只能统计到「响应头生成」的耗时，不含 body 传输，
                    # 这里显式标注一下，避免误读指标
                    "streaming": getattr(response, "streaming", False),
                },
            )
            if response is not None:
                response["X-Request-Time"] = f"{duration_ms:.1f}ms"

    @classmethod
    def _should_skip(cls, path: str) -> bool:
        if path in cls.SKIP_PATHS:
            return True
        return path.startswith(cls.SKIP_PATH_PREFIXES)
