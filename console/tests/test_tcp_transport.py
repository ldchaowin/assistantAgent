import json
import socket
import threading

import pytest

from phone_console.client import PhoneClient
from phone_console.device import Device
from phone_console.factory import build_phone
from phone_console.mock import MockDevice
from phone_console.service import create_server
from phone_console.transport import TcpTransport, TransportError


class FakeFirmware:
    """A TCP server speaking the firmware JSON-Lines protocol."""

    def __init__(self) -> None:
        self.device = MockDevice()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.host, self.port = self._sock.getsockname()
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self.connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closing.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        with conn:
            while not self._closing.is_set():
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    with self._lock:
                        responses = self.device.handle(message)
                    for response in responses:
                        conn.sendall(
                            json.dumps(response, ensure_ascii=False).encode() + b"\n"
                        )

    def close(self) -> None:
        self._closing.set()
        for conn in self.connections:
            try:
                conn.close()
            except OSError:
                pass
        self._sock.close()


@pytest.fixture
def firmware():
    server = FakeFirmware()
    try:
        yield server
    finally:
        server.close()


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_tcp_transport_roundtrip(firmware) -> None:
    transport = TcpTransport(firmware.host, firmware.port)
    try:
        transport.write(b'{"id":1,"cmd":"ping","ts":7}\n')
        line = transport.readline(2.0)
        assert line is not None
        message = json.loads(line)
        assert message["evt"] == "pong"
        assert message["ts"] == 7
    finally:
        transport.close()


def test_tcp_transport_timeout(firmware) -> None:
    transport = TcpTransport(firmware.host, firmware.port)
    try:
        assert transport.readline(0.05) is None
    finally:
        transport.close()


def test_tcp_transport_is_lazy_and_reports_connection_refused() -> None:
    transport = TcpTransport("127.0.0.1", free_port(), connect_timeout=0.5)
    assert transport._sock is None
    with pytest.raises(TransportError, match="cannot connect"):
        transport.write(b'{"id":1,"cmd":"ping"}\n')
    transport.close()


def test_tcp_transport_reconnect(firmware) -> None:
    transport = TcpTransport(firmware.host, firmware.port)
    try:
        transport.reconnect()
        transport.write(b'{"id":1,"cmd":"ping","ts":1}\n')
        assert json.loads(transport.readline(2.0))["evt"] == "pong"
    finally:
        transport.close()


def test_device_over_tcp(firmware) -> None:
    device = Device(TcpTransport(firmware.host, firmware.port))
    try:
        hello = device.hello()
        assert hello["fw"] == "0.3.1-mock"
        status = device.status()
        assert status["ok"] is True
        assert status["wifi"]["state"] == "off"
    finally:
        device.close()


def test_build_phone_over_tcp(firmware) -> None:
    phone = build_phone(mock=False, tcp=f"{firmware.host}:{firmware.port}")
    try:
        assert phone.hello()["evt"] == "ready"
        assert phone.status()["ok"] is True
    finally:
        phone.close()


def test_service_status_over_tcp(firmware) -> None:
    phone = build_phone(mock=False, tcp=f"{firmware.host}:{firmware.port}")
    server = create_server(phone, host="127.0.0.1", port=0, token="")
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        client = PhoneClient(f"http://{host}:{port}", timeout=3.0)
        payload = client.status()
        assert payload["ok"] is True
        assert payload["capture"]["name"] == "none"
    finally:
        server.shutdown()
        server.server_close()
        phone.close()


def test_cli_status_over_tcp(firmware, capsys) -> None:
    from phone_console.cli import main

    assert main(["--tcp", f"{firmware.host}:{firmware.port}", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["wifi"]["state"] == "off"


def test_cli_wifi_set_over_tcp(firmware, capsys) -> None:
    from phone_console.cli import main

    code = main(
        [
            "--tcp",
            f"{firmware.host}:{firmware.port}",
            "wifi",
            "set",
            "Office",
            "password123",
            "--wait",
            "2",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["wifi"]["state"] == "connected"
    assert firmware.device.wifi["ssid"] == "Office"


def test_mock_and_tcp_are_mutually_exclusive(capsys) -> None:
    from phone_console.cli import main

    assert main(["--mock", "--tcp", "127.0.0.1:9000", "status"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
