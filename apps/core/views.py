from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def healthz(request):
    """容器/负载均衡健康检查。刻意不查数据库，避免探活把库压垮。"""
    return JsonResponse({"status": "ok"})
