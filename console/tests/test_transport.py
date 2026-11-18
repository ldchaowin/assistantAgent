"""Bounded, cached name resolution for the hand's mDNS endpoint."""
import socket
import time

import pytest

from phone_console import transport
from phone_console.transport import (
    TcpTransport,
    TransportError,
    clear_dns_cache,
    invalidate_host,
    resolve_host,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_dns_cache()
    yield
    clear_dns_cache()


def test_resolve_host_passes_ip_literals_through(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("no lookup expected for an IP literal")

    monkeypatch.setattr(transport.socket, "gethostbyname", boom)
    assert resolve_host("127.0.0.1", 9000) == "127.0.0.1"
    assert resolve_host("::1", 9000) == "::1"


def test_resolve_host_caches_the_first_answer(monkeypatch) -> None:
    calls = []

    def fake_lookup(host):
        calls.append(host)
        return "192.168.1.20"

    monkeypatch.setattr(transport.socket, "gethostbyname", fake_lookup)
    assert resolve_host("hand.local", 9000) == "192.168.1.20"
    assert resolve_host("hand.local", 9000) == "192.168.1.20"
    # A second reconnect must not pay the mDNS lookup again.
    assert calls == ["hand.local"]


def test_resolve_host_bounds_a_silent_mdns_lookup(monkeypatch) -> None:
    def slow_lookup(host):
        time.sleep(1.0)
        return "192.168.1.20"

    monkeypatch.setattr(transport.socket, "gethostbyname", slow_lookup)
    started = time.monotonic()
    with pytest.raises(TransportError):
        resolve_host("hand.local", 9000, timeout=0.05)
    # getaddrinfo cannot be interrupted, so the lookup thread keeps running; the
    # caller must not wait for it.
    assert time.monotonic() - started < 0.8


def test_resolve_host_falls_back_to_the_last_good_address(monkeypatch) -> None:
    transport._dns_cache[("hand.local", 9000)] = (0.0, "192.168.1.20")

    def fail(host):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(transport.socket, "gethostbyname", fail)
    # The cached entry is expired, the name no longer resolves: retrying the
    # last known address beats a multi-second mDNS timeout on every reconnect.
    assert resolve_host("hand.local", 9000, timeout=0.05) == "192.168.1.20"


def test_invalidate_host_forces_a_fresh_lookup(monkeypatch) -> None:
    transport._dns_cache[("hand.local", 9000)] = (time.monotonic(), "192.168.1.20")
    invalidate_host("hand.local", 9000)
    monkeypatch.setattr(transport.socket, "gethostbyname", lambda host: "192.168.1.8")
    assert resolve_host("hand.local", 9000) == "192.168.1.8"


def test_tcp_transport_connects_to_the_resolved_address(monkeypatch) -> None:
    monkeypatch.setattr(transport.socket, "gethostbyname", lambda host: "192.168.1.20")
    seen = []

    class FakeSock:
        def setsockopt(self, *_args) -> None:
            return None

        def settimeout(self, _value) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_connect(address, timeout):
        seen.append((address, timeout))
        return FakeSock()

    monkeypatch.setattr(transport.socket, "create_connection", fake_connect)
    channel = TcpTransport("hand.local", 9000, connect_timeout=1.5)
    try:
        channel.reconnect()
    finally:
        channel.close()
    assert seen == [(("192.168.1.20", 9000), 1.5)]


def test_failed_connect_drops_the_cached_address(monkeypatch) -> None:
    transport._dns_cache[("hand.local", 9000)] = (time.monotonic(), "192.168.1.20")

    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(transport.socket, "create_connection", refuse)
    channel = TcpTransport("hand.local", 9000)
    with pytest.raises(TransportError):
        channel.reconnect()
    assert ("hand.local", 9000) not in transport._dns_cache
