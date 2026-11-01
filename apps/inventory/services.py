"""第 2 题：TCP 端口可达性探测。

这段代码是整份笔试里最容易「写得能跑但被扣分」的地方，关键在两点：

1. **必须显式设置超时**
   `socket.create_connection()` 不传 timeout 时会使用系统默认（可能长达数分钟）。
   探测目标一旦是不可达的 IP，worker/gunicorn 线程就会被长期占用，
   几十个这样的请求就足以拖垮整个服务——这是最典型的「主动探测型接口」事故。

2. **异常必须分类，不能只返回 True/False**
   "连不上"至少有四种完全不同的原因（超时 / 端口拒绝 / DNS 解析失败 /
   网络不可达），运维处置动作完全不同。只返回布尔值等于把排障成本转嫁给使用者。
"""

from __future__ import annotations

import errno
import ipaddress
import socket
import time
from dataclasses import dataclass

from django.conf import settings

from apps.inventory.models import DBInstance, ProbeLog

# 0.0.0.0 / 广播地址不是合法探测目标，容易误伤或产生无意义结果
_INVALID_HOSTS = {"0.0.0.0", "::", "255.255.255.255", "localhost."}


class InvalidProbeTarget(ValueError):
    """探测目标不合法。"""


@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    reason: str
    detail: str = ""
    latency_ms: float | None = None


def validate_target(host: str, port: int) -> None:
    """在发起连接前拦住明显不合法的目标。

    安全提示：探测接口本质是「让服务端代替调用方发起网络连接」，天然具备 SSRF
    特征。生产环境还应叠加内网网段白名单/黑名单与并发上限，避免被用来扫描内网。
    """
    host = (host or "").strip()
    if not host:
        raise InvalidProbeTarget("host 不能为空")
    if host.lower() in _INVALID_HOSTS:
        raise InvalidProbeTarget(f"host={host} 不是合法的探测目标")
    if not (1 <= int(port) <= 65535):
        raise InvalidProbeTarget(f"port={port} 超出 1-65535 范围")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名，交给 DNS 解析阶段报错
    if ip.is_multicast or ip.is_unspecified:
        raise InvalidProbeTarget(f"host={host} 不是合法的探测目标")


def tcp_probe(host: str, port: int, timeout: float | None = None) -> ProbeResult:
    """对 host:port 做一次 TCP 连接探测。不抛异常，所有失败都转成结构化结果。"""
    timeout = settings.PROBE_TIMEOUT_SECONDS if timeout is None else timeout
    started_at = time.perf_counter()

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            latency_ms = (time.perf_counter() - started_at) * 1000
            return ProbeResult(
                reachable=True,
                reason=ProbeLog.Reason.OK,
                detail="TCP 三次握手成功",
                latency_ms=round(latency_ms, 2),
            )
    except socket.timeout:
        # 目标在网络上不可达或丢包：最常见的「防火墙静默丢包」
        return ProbeResult(
            reachable=False,
            reason=ProbeLog.Reason.TIMEOUT,
            detail=f"{timeout}s 内未完成 TCP 握手（网络不通或被防火墙丢包）",
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
    except ConnectionRefusedError:
        # 主机可达但端口没有监听：通常是服务没起来
        return ProbeResult(
            reachable=False,
            reason=ProbeLog.Reason.REFUSED,
            detail="连接被拒绝：主机可达但端口未监听",
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
    except socket.gaierror as exc:
        return ProbeResult(
            reachable=False,
            reason=ProbeLog.Reason.DNS_ERROR,
            detail=f"域名解析失败：{exc}",
        )
    except OSError as exc:
        reason = (
            ProbeLog.Reason.NETWORK_ERROR
            if exc.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}
            else ProbeLog.Reason.UNKNOWN_ERROR
        )
        return ProbeResult(reachable=False, reason=reason, detail=f"{type(exc).__name__}: {exc}")


def probe_instance(instance: DBInstance, timeout: float | None = None) -> ProbeLog:
    """探测指定实例并落库留痕，返回探测记录。"""
    validate_target(instance.host, instance.port)
    result = tcp_probe(instance.host, instance.port, timeout=timeout)
    return ProbeLog.objects.create(
        instance=instance,
        reachable=result.reachable,
        reason=result.reason,
        detail=result.detail,
        latency_ms=result.latency_ms,
    )
