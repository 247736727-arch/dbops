from rest_framework.routers import DefaultRouter

from apps.stats.views import InstanceStatViewSet

router = DefaultRouter()
router.register("instance-stats", InstanceStatViewSet, basename="instance-stat")

urlpatterns = router.urls
