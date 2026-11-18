from phone_console import protocol


def test_encode_is_one_compact_line() -> None:
    data = protocol.encode({"id": 1, "cmd": "tap", "x": 10, "y": 20})
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    assert b" " not in data


def test_decode_round_trip() -> None:
    message = {"id": 7, "cmd": "tap", "x": 540, "y": 960}
    assert protocol.decode(protocol.encode(message)) == message


def test_decode_rejects_bad_json() -> None:
    try:
        protocol.decode(b"{not json}")
    except protocol.ProtocolError:
        pass
    else:
        raise AssertionError("ProtocolError not raised")


def test_event_detection() -> None:
    assert protocol.is_event({"evt": "ready"})
    assert not protocol.is_event({"id": 1, "ok": True})
