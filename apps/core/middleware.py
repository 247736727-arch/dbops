"""第 5 题：统计每个请求的请求耗时。"""

from __future__ import annotations

import logging
from time import perf_counter

logger = logging.getLogger("request.timing")


class RequestTimingMiddleware:
    """请求耗时统计中间件。

    几个刻意的设计选择：

    1. `perf_counter()` 而不是 `time.time()`
       前者是单调递增的高精度计时器，不受系统时钟调整（NTP 校时、夏令时）影响。

    2. `try/finally`
       异常请求（尤其是 500）才是性能与稳定性排查的主角，必须同样被统计到；
       如果只在正常返回的分支里记日志，最需要的数据恰好会缺失。

    3. 只写日志，不写数据库
       每个请求一次 INSERT 会把读流量放大成写流量，高 QPS 下直接成为瓶颈。
       真实落地是把结构化日志丢给 ELK/Loki，再离线聚合 P95/P99。
       （如果确实需要落库，应走异步队列 + 采样，而不是在请求链路里同步写。）

    4. 跳过静态资源与健康检查
       这类请求量大且没有观测价值，白占日志与指标额度。

    ASGI 说明：Django 会把同步中间件放进线程池执行（ASGI 下不会阻塞事件循环，
    但会占用线程池容量）。若项目是纯异步栈，建议再实现一个 `async def __acall__`
    版本，避免线程池被打满。
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
