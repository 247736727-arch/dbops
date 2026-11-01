"""行锁验证——第 3 题幂等保护在两种数据库上并不等价。

`select_for_update()` 在 PostgreSQL 上会真的加行锁，在 SQLite 上被静默忽略。
这个差异决定了「两个 worker 同时轮换同一实例」会不会被幂等保护挡住，
所以把它固定成可执行的断言，而不是只留在 README 的结论里。
"""

from __future__ import annotations

import threading

import pytest
from django.db import connection, connections, transaction
from django.db.utils import OperationalError

from apps.inventory.models import Cluster, DBInstance


def test_select_for_update_sql_matches_backend_capability():
    """同一段 ORM 在两种库上生成的 SQL 并不一样，这是后面所有差异的根源。"""
    sql = str(DBInstance.objects.select_for_update().query)

    if connection.vendor == "postgresql":
        assert "FOR UPDATE" in sql
    else:
        # SQLite 根本没有行锁，Django 静默丢掉 select_for_update，连警告都没有
        assert "FOR UPDATE" not in sql


@pytest.mark.django_db(transaction=True)
def test_second_transaction_is_rejected_on_postgres(department):
    """互斥验证：持有行锁期间，另一个事务必须立刻失败，而不是也拿到锁。

    只在 PostgreSQL 下有意义——SQLite 无行锁，跑出来必然是「拿到了锁」，
    把它写成断言只会掩盖事实。本地默认 SQLite 时这条 skip，CI 的 postgres 格会真跑。
    """
    if connection.vendor != "postgresql":
        pytest.skip("SQLite 不实现行级锁，此断言无意义（见 README 第六节）")

    cluster = Cluster.objects.create(name="lock-cluster", department=department)
    instance = DBInstance.objects.create(
        cluster=cluster, name="lock-db", host="127.0.0.1", port=3394, username="app"
    )

    acquired = threading.Event()
    release = threading.Event()
    contender_result: dict = {}

    def hold_lock():
        try:
            with transaction.atomic():
                DBInstance.objects.select_for_update().get(pk=instance.pk)
                acquired.set()
                release.wait(timeout=10)
        finally:
            connections["default"].close()

    def contend():
        try:
            with transaction.atomic():
                DBInstance.objects.select_for_update(nowait=True).get(pk=instance.pk)
            contender_result["outcome"] = "acquired"
        except OperationalError as exc:
            contender_result["outcome"] = "rejected"
            contender_result["error"] = str(exc)
        except Exception as exc:  # 其它异常也得收进来，否则线程里会静默失败
            contender_result["outcome"] = f"{type(exc).__name__}: {exc}"
        finally:
            connections["default"].close()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=10), "持锁线程没能进入事务"

    contender = threading.Thread(target=contend)
    contender.start()
    contender.join(timeout=10)

    release.set()
    holder.join(timeout=10)

    # 只断言"被拒绝"，不绑死具体错误文案：不同 PG 大版本措辞会变
    assert contender_result["outcome"] == "rejected", contender_result
