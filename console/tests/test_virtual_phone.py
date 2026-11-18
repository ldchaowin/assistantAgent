import pytest

np = pytest.importorskip("numpy")

from phone_console.device import DeviceError, DeviceTimeout
from phone_console.virtual import VirtualPhone
from phone_console.vision.frame import Frame


def make_phone(frames: int = 3, timeout: float = 0.2, advance: bool = True) -> VirtualPhone:
    return VirtualPhone(
        [Frame(np.zeros((100, 100, 3), dtype=np.uint8)) for _ in range(frames)],
        timeout=timeout,
        advance=advance,
    )


def test_virtual_phone_needs_frames() -> None:
    with pytest.raises(ValueError):
        VirtualPhone([])


def test_tap_advances_and_is_recorded() -> None:
    phone = make_phone(3)
    assert phone.screen == (1080, 2400)
    phone.tap(10, 20)
    assert phone.index == 1
    assert phone.mock.taps == [(10, 20)]


def test_swipe_key_text_advance() -> None:
    phone = make_phone(4)
    phone.swipe(1, 2, 3, 4, 100)
    assert phone.index == 1
    phone.key("BACK")
    assert phone.index == 2
    phone.text("abc")
    assert phone.index == 3
    assert phone.mock.swipes == [(1, 2, 3, 4, 100)]
    assert phone.mock.keys == ["BACK"]
    assert phone.mock.texts == ["abc"]


def test_last_frame_stays() -> None:
    phone = make_phone(1)
    phone.tap(1, 1)
    assert phone.index == 0


def test_advance_can_be_disabled() -> None:
    phone = make_phone(3, advance=False)
    phone.tap(1, 1)
    assert phone.index == 0


def test_reset_returns_to_first_frame() -> None:
    phone = make_phone(3)
    phone.tap(1, 1)
    assert phone.index == 1
    phone.reset()
    assert phone.index == 0
    assert phone.mock.state == "READY"


def test_from_dir_sorts_images(tmp_path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    Image.new("RGB", (30, 40), (1, 2, 3)).save(tmp_path / "02.png")
    Image.new("RGB", (10, 20), (4, 5, 6)).save(tmp_path / "01.png")
    phone = VirtualPhone.from_dir(tmp_path)
    assert phone.current.size == (10, 20)
    phone.tap(1, 1)
    assert phone.current.size == (30, 40)


def test_busy_is_retryable_by_the_caller() -> None:
    phone = make_phone(2)
    phone.mock.inject_busy(1)
    with pytest.raises(DeviceError) as exc:
        phone.tap(1, 1)
    assert exc.value.code == "BUSY"
    assert phone.index == 0
    phone.tap(1, 1)
    assert phone.index == 1


def test_dropped_response_times_out_without_advancing() -> None:
    phone = make_phone(2, timeout=0.05)
    phone.transport.drop_responses = 1
    with pytest.raises(DeviceTimeout):
        phone.tap(1, 1)
    assert phone.index == 0
    # The mock device did execute the tap; only the response was lost.
    assert len(phone.mock.taps) == 1
    phone.tap(1, 1)
    assert phone.index == 1


def test_fault_state_blocks_until_hello() -> None:
    phone = make_phone(2)
    phone.mock.inject_fault()
    with pytest.raises(DeviceError) as exc:
        phone.tap(1, 1)
    assert exc.value.code == "NOT_READY"
    assert phone.mock.state == "FAULT"
    phone.hello()
    phone.tap(1, 1)
    assert phone.index == 1


def test_write_error_is_raised() -> None:
    phone = make_phone(2)
    phone.transport.write_error = ConnectionError("simulated disconnect")
    with pytest.raises(ConnectionError):
        phone.tap(1, 1)
    assert phone.index == 0
    phone.tap(1, 1)
    assert phone.index == 1
