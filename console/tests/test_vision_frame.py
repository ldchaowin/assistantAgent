import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

from PIL import Image

from phone_console.vision.frame import Frame


def test_from_pil_converts_rgb_to_bgr_and_back() -> None:
    rgb = np.zeros((10, 20, 3), dtype=np.uint8)
    rgb[:, :, 0] = 255  # red in RGB
    frame = Frame.from_pil(Image.fromarray(rgb))
    assert frame.size == (20, 10)
    assert frame.image[0, 0].tolist() == [0, 0, 255]  # BGR
    assert np.array_equal(np.asarray(frame.to_pil()), rgb)


def test_grayscale_is_expanded_to_three_channels() -> None:
    frame = Frame(np.full((5, 6), 128, dtype=np.uint8))
    assert frame.image.shape == (5, 6, 3)
    assert frame.image[0, 0].tolist() == [128, 128, 128]


def test_bgra_drops_alpha() -> None:
    frame = Frame(np.zeros((5, 6, 4), dtype=np.uint8))
    assert frame.image.shape == (5, 6, 3)


def test_invalid_shape_is_rejected() -> None:
    with pytest.raises(ValueError):
        Frame(np.zeros((5, 6, 2), dtype=np.uint8))


def test_crop_returns_requested_region() -> None:
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[20:80, 10:60] = 255
    frame = Frame(image)
    crop = frame.crop((10, 20, 50, 60))
    assert crop.size == (50, 60)
    assert crop.image.min() == 255
    with pytest.raises(ValueError):
        frame.crop((190, 0, 20, 10))


def test_save_creates_parent_directories(tmp_path) -> None:
    frame = Frame(np.zeros((10, 10, 3), dtype=np.uint8))
    target = frame.save(tmp_path / "nested" / "shot.png")
    assert target.is_file()
