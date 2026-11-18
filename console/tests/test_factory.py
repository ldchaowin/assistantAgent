"""Eye-source selection: the USB serial must win over the wireless one."""
import pytest

from phone_console import factory


class _FakeSource:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.serial = kwargs.get("serial")


@pytest.fixture
def fake_eye(monkeypatch):
    """Replace every real eye source with a recorder (no scrcpy/adb needed)."""
    import phone_console.vision.sources as sources
    import phone_console.vision.stream as stream

    captured = {}

    def fake_scrcpy(**kwargs):
        captured["scrcpy"] = kwargs
        return _FakeSource(**kwargs)

    def fake_adb(**kwargs):
        captured["adb"] = kwargs
        return _FakeSource(**kwargs)

    def fake_screenrecord(**kwargs):
        captured["screenrecord"] = kwargs
        return _FakeSource(**kwargs)

    monkeypatch.setattr(stream, "ScrcpySource", fake_scrcpy)
    monkeypatch.setattr(stream, "ScreenrecordSource", fake_screenrecord)
    monkeypatch.setattr(sources, "AdbSource", fake_adb)
    return captured


@pytest.fixture
def usb_serial(monkeypatch):
    import phone_console.vision.adb as adb_module

    monkeypatch.setattr(
        adb_module, "preferred_adb_serial", lambda serial=None, **_kwargs: serial or "USB1"
    )


@pytest.mark.parametrize(
    "flag,key",
    [("scrcpy", "scrcpy"), ("screenrecord", "screenrecord"), ("adb", "adb")],
)
def test_build_source_prefers_a_usb_serial(fake_eye, usb_serial, flag, key) -> None:
    source = factory.build_source(**{flag: ""})
    assert source.serial == "USB1"
    assert key in fake_eye


@pytest.mark.parametrize("flag", ["scrcpy", "screenrecord", "adb"])
def test_build_source_keeps_an_explicit_serial(fake_eye, usb_serial, flag) -> None:
    source = factory.build_source(**{flag: "adb-XXX._adb-tls-connect._tcp"})
    assert source.serial == "adb-XXX._adb-tls-connect._tcp"


def test_build_source_keeps_the_wireless_default(fake_eye, monkeypatch) -> None:
    import phone_console.vision.adb as adb_module

    monkeypatch.setattr(adb_module, "preferred_adb_serial", lambda **_kwargs: None)
    source = factory.build_source(scrcpy="")
    assert source.serial is None
