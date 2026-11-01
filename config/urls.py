from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.views import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    # OpenAPI 文档：/api/docs/
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    # 第 1/2 题：部门 / 集群 / 实例 CRUD + 端口探测
    path("api/", include("apps.inventory.urls")),
    # 第 4 题：统计结果查询
    path("api/", include("apps.stats.urls")),
    # 容器健康检查
    path("healthz/", healthz, name="healthz"),
]
