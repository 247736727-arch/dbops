"""Celery 应用入口：worker 与 beat 都通过 `-A config` 找到这个 app。"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("dbops")

# namespace="CELERY" → 所有配置都从 Django settings 的 CELERY_* 读取
app.config_from_object("django.conf:settings", namespace="CELERY")

# 自动发现各个 app 下的 tasks.py
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
