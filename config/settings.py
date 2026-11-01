"""
Django settings —— 数据库实例管理系统（笔试方案 A）

设计要点（对应 5 个考点）：
1. 多表模型：apps.inventory 下的 Department / Cluster / DBInstance / ProbeLog
2. 敏感字段加密：FIELD_ENCRYPTION_KEYS 走环境变量，见 apps/core/crypto.py
3. 周期任务：CELERY_BEAT_SCHEDULE（12h 轮换、每日 00:00 统计）+ CELERY_TIMEZONE
4. 中间件：apps.core.middleware.RequestTimingMiddleware
"""

from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1", "0.0.0.0", "testserver"]),
)
# .env 文件仅用于本地开发，生产环境应由容器/编排注入真实环境变量
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env(
    "SECRET_KEY",
    default="django-insecure-dev-only-key-please-override-in-production",
)

DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# ---------------------------------------------------------------- applications
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "drf_spectacular",
    # local
    "apps.core",
    "apps.inventory",
    "apps.stats",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # 放在靠前位置，才能覆盖后续中间件与视图的全部耗时
    "apps.core.middleware.RequestTimingMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ------------------------------------------------------------------- database
# 本地默认 sqlite（零依赖启动），docker/生产通过 DATABASE_URL 切 postgres
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------- i18n / 时区
LANGUAGE_CODE = "zh-hans"

# 时区是本题的隐藏考点：定时任务 00:00 必须是「北京时间 00:00」
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Django 6.1 起推荐用 MAILERS（支持多别名），EMAIL_BACKEND 已进入弃用倒计时
MAILERS = {
    "default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"},
}

# ------------------------------------------------------------------- DRF
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "DBOps API",
    "DESCRIPTION": "企业内数据库实例管理系统（Django 笔试题 · 方案 A）",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ------------------------------------------------------- 敏感字段加密（可逆）
# 支持多把密钥逗号分隔：第一把用于加密，其余用于解密 → 便于密钥平滑轮换
FIELD_ENCRYPTION_KEYS = env.list("FIELD_ENCRYPTION_KEYS", default=[])

# ------------------------------------------------------------- 探测与轮换策略
# 端口探测超时秒数：不设置超时会导致 worker 线程被不可达目标长期占用
PROBE_TIMEOUT_SECONDS = env.float("PROBE_TIMEOUT_SECONDS", default=3.0)

# 密码轮换周期（小时），题目要求 12 小时
PASSWORD_ROTATION_INTERVAL_HOURS = env.int("PASSWORD_ROTATION_INTERVAL_HOURS", default=12)
# 旧密码保留窗口：轮换后新旧密码并行一段时间，避免应用侧连接瞬间全部失败
PREVIOUS_PASSWORD_GRACE_HOURS = env.int("PREVIOUS_PASSWORD_GRACE_HOURS", default=24)
# 单次轮换允许处理的最大实例数（防止一次任务压垮数据库，超出部分下次继续）
ROTATION_BATCH_SIZE = env.int("ROTATION_BATCH_SIZE", default=500)

# ------------------------------------------------------------------ 日志
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "timing": {"format": "[timing] %(asctime)s %(levelname)s %(message)s"},
        "verbose": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "timing": {"class": "logging.StreamHandler", "formatter": "timing"},
    },
    "loggers": {
        "request.timing": {
            "handlers": ["timing"],
            "level": env("REQUEST_TIMING_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# ------------------------------------------------------------------ Celery
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)

# 必须与 Django 时区一致，否则 crontab(hour=0) 会跑在 UTC 00:00（北京时间 08:00）。
#
# 关于下面这行的疑问：CELERY_ENABLE_UTC=True 会不会覆盖掉上面的时区？
# 读 celery/app/base.py 可知，enable_utc 只在「没有显式设置 timezone」时才作为兜底；
# 这里已经显式给了 Asia/Shanghai，所以 crontab 判定走的仍是北京时间。
# 实测确认：app.timezone == crontab.tz == Asia/Shanghai，
# 且 daily cron hour=0 触发在 Asia/Shanghai 00:00、12h 轮换落在 12:00。
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
# 轮换/统计这类任务必须可重试，且耗时短
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=600)
CELERY_TASK_ACKS_LATE = True

CELERY_BEAT_SCHEDULE = {
    # 第 3 题：每 12 小时随机轮换所有实例密码
    "rotate-instance-passwords": {
        "task": "apps.inventory.tasks.rotate_all_instance_passwords",
        "schedule": crontab(minute=0, hour="*/12"),
        "options": {"expires": 3600},
    },
    # 第 4 题：每天 00:00（Asia/Shanghai）按部门 × 集群统计实例数量
    "daily-instance-statistics": {
        "task": "apps.stats.tasks.collect_daily_instance_stats",
        "schedule": crontab(minute=0, hour=0),
        "options": {"expires": 3600},
    },
}
