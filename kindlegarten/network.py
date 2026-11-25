"""Shared policy for browser and API connections."""

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit
from urllib.request import getproxies

PROXY = ContextVar("proxy", default=None)


def proxy_address():
    configured = PROXY.get()
    if configured is None:
        configured = os.environ.get("KINDLEGARTEN_PROXY", "system")
    if configured == "direct":
        return None
    if configured == "system":
        values = getproxies()
        return values.get("https") or values.get("all") or values.get("http")
    parts = urlsplit(configured)
    if parts.scheme not in {"http", "https", "socks5"} or not parts.hostname or not parts.port:
        raise ValueError("Invalid proxy address")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("Invalid proxy address")
    if parts.scheme == "socks5" and (parts.username or parts.password):
        raise ValueError("SOCKS5 authentication is not supported")
    return configured


@contextmanager
def using_proxy(value):
    token = PROXY.set(value)
    try:
        proxy_address()
        yield
    finally:
        PROXY.reset(token)


def worker_environment():
    env = dict(os.environ)
    if PROXY.get() is not None:
        env["KINDLEGARTEN_PROXY"] = PROXY.get()
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


def browser_proxy():
    from urllib.parse import unquote

    if not (address := proxy_address()):
        return {}
    parts = urlsplit(address)
    config = {"server": f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"}
    if parts.username is not None:
        config.update(username=unquote(parts.username), password=unquote(parts.password or ""))
    return {"proxy": config}


def browser_options():
    if (PROXY.get() or os.environ.get("KINDLEGARTEN_PROXY")) == "direct":
        return {"args": ["--no-proxy-server"]}
    return browser_proxy()


def http_client(*, redirects=False):
    import httpx

    address = proxy_address()
    transport = httpx.HTTPTransport(proxy=address, retries=2, trust_env=False)
    return httpx.Client(transport=transport, timeout=60, follow_redirects=redirects, trust_env=False,
                        headers={"User-Agent": "Mozilla/5.0"})


@contextmanager
def amazon_client(*args, **kwargs):
    from kindl.client import AmazonDeviceClient
    from kindlegarten.marketplaces import extend_kindl_regions

    extend_kindl_regions()
    with AmazonDeviceClient(*args, **kwargs) as client:
        client.http.close()
        client.http = http_client(redirects=True)
        yield client
