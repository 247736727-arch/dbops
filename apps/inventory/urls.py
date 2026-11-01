from rest_framework.routers import DefaultRouter

from apps.inventory.views import ClusterViewSet, DBInstanceViewSet, DepartmentViewSet

router = DefaultRouter()
router.register("departments", DepartmentViewSet, basename="department")
router.register("clusters", ClusterViewSet, basename="cluster")
router.register("instances", DBInstanceViewSet, basename="instance")

urlpatterns = router.urls
