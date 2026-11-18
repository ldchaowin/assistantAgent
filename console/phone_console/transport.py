"""Serial, TCP and in-memory transports."""
from __future__ import annotations

import ipaddress
import select
import socket
import threading
import time
from typing import Protocol

import serial
from serial.tools import list_ports

KNOWN_VIDS = {
    0x1A86: "CH340/CH341",
    0x10C4: "CP210x",
    0x303A: "Espressif",
}

# mDNS names such as ``hand.local`` can cost 2-5s per lookup when the
# responder is asleep, and a socket timeout cannot interrupt getaddrinfo. Look
# it up in a bounded side thread and remember the last good address.
DEFAULT_DNS_TIMEOUT = 2.0
DNS_CACHE_TTL_S = 300.0
_dns_cache: dict[tuple[str, int], tuple[float, str]] = {}
_dns_lock = threading.Lock()


class TransportError(RuntimeError):
    """The transport is disconnected or could not be opened."""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def clear_dns_cache() -> None:
    """Drop cached hand addresses (tests, or after a known network change)."""
    with _dns_lock:
        _dns_cache.clear()


def resolve_host(host: str, port: int, timeout: float = DEFAULT_DNS_TIMEOUT) -> str:
    """Resolve ``host`` within ``timeout`` seconds, caching the last good address.

    A stale cached address is preferred over failing fast: the hand usually
    keeps its lease, and one failed TCP connect costs far less than repeated
    2-5s mDNS lookups.
    """
    if _is_ip_literal(host):
        return host
    key = (host, int(port))
    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(key)
    if cached is not None and (now - cached[0]) <= DNS_CACHE_TTL_S:
        return cached[1]
    result: dict[str, object] = {}

    def lookup() -> None:
        try:
            result["address"] = socket.gethostbyname(host)
        except OSError as exc:  # pragma: no cover - depends on the network
            result["error"] = exc

    thread = threading.Thread(target=lookup, name=f"resolve-{host}", daemon=True)
    thread.start()
    thread.join(max(0.05, float(timeout)))
    address = result.get("address")
    if isinstance(address, str):
        with _dns_lock:
            _dns_cache[key] = (now, address)
        return address
    if cached is not None:
        return cached[1]
    detail = result.get("error")
    raise TransportError(
        f"cannot resolve {host} within {timeout:.1f}s"
        + (f": {detail}" if detail is not None else " (no mDNS/DNS answer)")
    )


def invalidate_host(host: str, port: int) -> None:
    with _dns_lock:
        _dns_cache.pop((host, int(port)), None)


def parse_tcp_endpoint(value: str, default_port: int = 9000) -> tuple[str, int]:
    """Parse ``host`` or ``host:port`` into a ``(host, port)`` pair."""
    text = str(value).strip()
    if not text:
        raise ValueError("tcp endpoint must be HOST[:PORT]")
    if text.startswith("["):  # [ipv6]:port
        end = text.find("]")
        if end < 0:
            raise ValueError("bad IPv6 endpoint")
        host = text[1:end]
        rest = text[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:] else default_port
        return host, port
    if text.count(":") == 1:
        host, raw_port = text.split(":", 1)
        if raw_port:
            return host, int(raw_port)
        return host, default_port
    return text, default_port


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def readline(self, timeout: float) -> bytes | None: ...

    def close(self) -> None: ...


def list_serial_ports() -> list[dict]:
    ports = []
    for p in list_ports.comports():
        ports.append(
            {
                "device": p.device,
                "description": p.description,
                "vid": p.vid,
                "pid": p.pid,
                "known": KNOWN_VIDS.get(p.vid or -1, ""),
            }
        )
    return ports


def auto_port(preferred: str | None = None) -> str:
    if preferred:
        return preferred
    ports = list_serial_ports()
    if not ports:
        raise RuntimeError("no serial ports found")
    known = [p for p in ports if p["vid"] in KNOWN_VIDS]
    if len(known) == 1:
        return known[0]["device"]
    if len(ports) == 1:
        return ports[0]["device"]
    names = ", ".join(f"{p['device']} ({p['description']})" for p in ports)
    raise RuntimeError(f"multiple serial ports; pass --port. Found: {names}")


class SerialTransport:
    def __init__(self, port: str, baud: int = 460800) -> None:
        self.port = port
        self.baud = baud
        self._ser = serial.Serial(port, baud, timeout=0)
        time.sleep(0.05)
        self._ser.reset_input_buffer()

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def readline(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + max(0.0, timeout)
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._ser.read(1)
            if chunk:
                if chunk == b"\n":
                    return bytes(buf) + b"\n"
                buf.extend(chunk)
            else:
                time.sleep(0.002)
        return None

    def close(self) -> None:
        self._ser.close()


class TcpTransport:
    """JSON Lines over WiFi TCP (ESP32 firmware ``net`` module)."""

    def __init__(
        self,
        host: str,
        port: int = 9000,
        connect_timeout: float = 3.0,
        dns_timeout: float = DEFAULT_DNS_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self.dns_timeout = float(dns_timeout)
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        # Connect lazily: the service must start even when the hand is
        # temporarily unreachable (mDNS not ready, device rebooting, ...).

    @property
    def name(self) -> str:
        return f"tcp:{self.host}:{self.port}"

    def _connect(self) -> None:
        self.close()
        address = resolve_host(self.host, self.port, self.dns_timeout)
        try:
            sock = socket.create_connection(
                (address, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            # The cached address may be stale after a DHCP change: drop it so the
            # next reconnect resolves the name again instead of retrying forever.
            invalidate_host(self.host, self.port)
            raise TransportError(
                f"cannot connect to {self.host}:{self.port}: {exc}"
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        self._sock = sock
        self._buf.clear()

    def reconnect(self) -> None:
        self._connect()

    def write(self, data: bytes) -> None:
        if self._sock is None:
            self._connect()
        sock = self._sock
        if sock is None:  # pragma: no cover - _connect raises on failure
            raise TransportError("not connected")
        try:
            sock.sendall(data)
        except OSError as exc:
            raise TransportError(f"send failed: {exc}") from exc

    def readline(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            index = self._buf.find(b"\n")
            if index >= 0:
                line = bytes(self._buf[: index + 1])
                del self._buf[: index + 1]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock = self._sock
            if sock is None:
                # Lazy transport: no connection yet, so no data is available.
                return None
            try:
                readable, _, _ = select.select([sock], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise TransportError(f"select failed: {exc}") from exc
            if not readable:
                return None
            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                raise TransportError(f"recv failed: {exc}") from exc
            if not chunk:
                raise TransportError("connection closed by peer")
            self._buf.extend(chunk)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._buf.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class MockTransport:
    """Spawns a MockDevice and answers writes immediately."""

    def __init__(self) -> None:
        from .mock import MockDevice

        self.device = MockDevice()
        self._responses: list[bytes] = []
        self.closed = False
        # Fault injection for scenario tests.
        self.drop_responses = 0
        self.write_error: Exception | None = None

    def write(self, data: bytes) -> None:
        from .protocol import decode, encode

        if self.write_error is not None:
            error = self.write_error
            self.write_error = None
            raise error
        for line in data.splitlines():
            if not line.strip():
                continue
            message = decode(line)
            responses = self.device.handle(message)
            if self.drop_responses > 0:
                self.drop_responses -= 1
                continue
            for response in responses:
                self._responses.append(encode(response))

    def readline(self, timeout: float) -> bytes | None:
        if self._responses:
            return self._responses.pop(0)
        return None

    def close(self) -> None:
        self.closed = True
