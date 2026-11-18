#!/usr/bin/env python3
"""Generate the synthetic phone screens used by the demo scenarios.

Run from the repo root:
    .venv/bin/python tools/make_scenario_assets.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "scenarios" / "frames"
W, H = 1080, 2400

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_font_path: str | None = None
_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    global _font_path
    if size in _fonts:
        return _fonts[size]
    if _font_path is None:
        for candidate in FONT_CANDIDATES:
            if Path(candidate).is_file():
                _font_path = candidate
                break
        if _font_path is None:
            raise SystemExit("no CJK font found; add one to FONT_CANDIDATES")
    _fonts[size] = ImageFont.truetype(_font_path, size)
    return _fonts[size]


def new_screen(title: str, subtitle: str | None = None) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (W, H), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    draw.text((60, 40), "9:41", font=font(34), fill=(30, 30, 30))
    draw.text((60, 130), title, font=font(72), fill=(10, 10, 10))
    if subtitle:
        draw.text((60, 225), subtitle, font=font(34), fill=(120, 120, 120))
    return image, draw


def centered(
    draw: ImageDraw.ImageDraw, y: float, text: str, size: int, fill: tuple[int, int, int]
) -> None:
    face = font(size)
    box = draw.textbbox((0, 0), text, font=face)
    draw.text(((W - (box[2] - box[0])) / 2, y - box[1]), text, font=face, fill=fill)


def row(
    draw: ImageDraw.ImageDraw,
    y: int,
    label: str,
    value: str | None = None,
    icon: tuple[int, int, int] = (0, 122, 255),
) -> None:
    draw.rounded_rectangle([40, y, 1040, y + 150], radius=24, fill=(255, 255, 255))
    draw.ellipse([80, y + 45, 140, y + 105], fill=icon)
    draw.text((180, y + 50), label, font=font(44), fill=(20, 20, 20))
    if value:
        draw.text((760, y + 50), value, font=font(40), fill=(120, 120, 120))
    draw.text((975, y + 48), ">", font=font(44), fill=(180, 180, 180))


def button(
    draw: ImageDraw.ImageDraw,
    y: int,
    label: str,
    fill: tuple[int, int, int] = (0, 122, 255),
    text_fill: tuple[int, int, int] = (255, 255, 255),
) -> None:
    draw.rounded_rectangle([60, y, 1020, y + 120], radius=30, fill=fill)
    face = font(46)
    box = draw.textbbox((0, 0), label, font=face)
    draw.text(
        (60 + (960 - (box[2] - box[0])) / 2, y + (120 - (box[3] - box[1])) / 2 - box[1]),
        label,
        font=face,
        fill=text_fill,
    )


def app_icon(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    fill: tuple[int, int, int],
) -> None:
    size = 180
    draw.rounded_rectangle([x, y, x + size, y + size], radius=36, fill=fill)
    face = font(52)
    box = draw.textbbox((0, 0), label, font=face)
    draw.text(
        (x + (size - (box[2] - box[0])) / 2, y + (size - (box[3] - box[1])) / 2 - box[1]),
        label,
        font=face,
        fill=(255, 255, 255),
    )


def bottom_nav(draw: ImageDraw.ImageDraw, active: str = "首页") -> None:
    y = 2200
    draw.line([(0, y), (W, y)], fill=(220, 220, 220), width=2)
    for index, label in enumerate(["首页", "我的", "购物车"]):
        face = font(34)
        box = draw.textbbox((0, 0), label, font=face)
        color = (0, 122, 255) if label == active else (130, 130, 130)
        draw.text(
            (180 + index * 360 - (box[2] - box[0]) / 2, y + 40),
            label,
            font=face,
            fill=color,
        )


def settings() -> None:
    out = OUT / "settings"
    out.mkdir(parents=True, exist_ok=True)
    source = Image.open(ROOT / "docs" / "assets" / "eye-demo.png").convert("RGB")
    source.save(out / "01-settings.png")

    image, draw = new_screen("无线局域网")
    row(draw, 360, "WLAN", "已连接")
    row(draw, 530, "自动加入", "开")
    row(draw, 700, "私有地址", "关闭")
    button(draw, 900, "完成")
    image.save(out / "02-wlan.png")

    source.save(out / "03-settings-back.png")
    print("settings:", sorted(path.name for path in out.glob("*.png")))


def price() -> None:
    out = OUT / "price"
    out.mkdir(parents=True, exist_ok=True)

    image, draw = new_screen("桌面")
    app_icon(draw, 100, 400, "京东", (200, 30, 30))
    app_icon(draw, 360, 400, "淘宝", (255, 120, 0))
    app_icon(draw, 620, 400, "设置", (100, 100, 100))
    bottom_nav(draw, "首页")
    image.save(out / "01-home.png")

    image, draw = new_screen("京东")
    row(draw, 360, "我的")
    row(draw, 530, "购物车")
    row(draw, 700, "我的订单")
    bottom_nav(draw, "首页")
    image.save(out / "02-jd-home.png")

    image, draw = new_screen("我的")
    row(draw, 360, "客户服务")
    row(draw, 530, "我的订单")
    row(draw, 700, "设置")
    bottom_nav(draw, "我的")
    image.save(out / "03-jd-my.png")

    image, draw = new_screen("客户服务")
    row(draw, 360, "价格保护")
    row(draw, 530, "退换/售后")
    row(draw, 700, "在线客服")
    image.save(out / "04-jd-service.png")

    image, draw = new_screen("价格保护")
    button(draw, 360, "一键申请")
    row(draw, 530, "申请记录")
    row(draw, 700, "规则说明")
    image.save(out / "05-jd-protect.png")

    image, draw = new_screen("价格保护")
    centered(draw, 480, "申请成功", 96, (0, 150, 80))
    draw.text((300, 650), "差价将原路退回", font=font(38), fill=(90, 90, 90))
    button(draw, 820, "完成")
    image.save(out / "06-jd-done.png")
    print("price:", sorted(path.name for path in out.glob("*.png")))


def game() -> None:
    out = OUT / "game"
    out.mkdir(parents=True, exist_ok=True)

    image, draw = new_screen("每日任务")
    row(draw, 360, "开始挑战")
    row(draw, 530, "任务奖励")
    row(draw, 700, "每日签到")
    image.save(out / "01-game-home.png")

    image, draw = new_screen("战斗中")
    centered(draw, 400, "第 3 关", 72, (30, 30, 30))
    button(draw, 700, "继续")
    image.save(out / "02-game-battle.png")

    image, draw = new_screen("胜利")
    centered(draw, 430, "胜利", 120, (220, 150, 0))
    row(draw, 700, "领取奖励")
    button(draw, 900, "返回")
    image.save(out / "03-game-victory.png")
    print("game:", sorted(path.name for path in out.glob("*.png")))


def main() -> None:
    settings()
    price()
    game()


if __name__ == "__main__":
    main()
