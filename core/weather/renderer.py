from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps


class WeatherRenderer:
    WIDTH = 1080
    HEIGHT = 1440

    def __init__(self, output_dir: Path, config: dict):
        self.output_dir = Path(output_dir)
        self.config = config
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def update_config(self, config: dict) -> None:
        self.config = config

    def _font_candidates(self, bold: bool) -> list[str]:
        configured = str(self.config.get("font_path", "") or "").strip()
        names = [configured] if configured else []
        if os.name == "nt":
            names.extend([
                "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
                "C:/Windows/Fonts/simhei.ttf" if bold else "C:/Windows/Fonts/simsun.ttc",
            ])
        names.extend([
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ])
        return [name for name in names if name]

    def _font(self, size: int, *, bold: bool = False):
        for path in self._font_candidates(bold):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _fit_font(self, draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, min_size: int, *, bold=False):
        for candidate in range(size, min_size - 1, -2):
            font = self._font(candidate, bold=bold)
            box = draw.textbbox((0, 0), text, font=font)
            if box[2] - box[0] <= max_width:
                return font
        return self._font(min_size, bold=bold)

    @staticmethod
    def _truncate_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
        value = str(text or "")
        if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
            return value
        suffix = "…"
        while value and draw.textbbox((0, 0), value + suffix, font=font)[2] > max_width:
            value = value[:-1]
        return value + suffix if value else suffix

    def _load_background(self) -> Image.Image:
        template_path = str(self.config.get("template_path", "") or "").strip()
        if template_path and Path(template_path).is_file():
            with Image.open(template_path) as template:
                return ImageOps.fit(
                    template.convert("RGB"),
                    (self.WIDTH, self.HEIGHT),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )

        image = Image.new("RGB", (self.WIDTH, self.HEIGHT), (231, 244, 250))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.WIDTH, 500), fill=(164, 214, 239))
        draw.ellipse((760, 80, 920, 240), fill=(255, 220, 116))
        for box in ((90, 160, 430, 285), (350, 95, 715, 245), (665, 210, 1035, 330)):
            draw.ellipse(box, fill=(243, 249, 251))
        draw.polygon(
            [(0, 445), (120, 380), (245, 425), (360, 345), (500, 435), (650, 370), (790, 430), (930, 350), (1080, 430), (1080, 535), (0, 535)],
            fill=(107, 160, 142),
        )
        draw.rectangle((0, 500, self.WIDTH, self.HEIGHT), fill=(246, 248, 246))
        return image

    @staticmethod
    def _condition_kind(condition: str) -> str:
        value = str(condition or "").lower()
        if any(token in value for token in ("雷", "thunder")):
            return "thunder"
        if any(token in value for token in ("雪", "snow")):
            return "snow"
        if any(token in value for token in ("雨", "rain", "阵雨", "storm")):
            return "rain"
        if any(token in value for token in ("晴", "sun", "clear")):
            return "sun"
        return "cloud"

    def _draw_icon(self, draw: ImageDraw.ImageDraw, center: tuple[int, int], condition: str, scale: float = 1.0):
        x, y = center
        kind = self._condition_kind(condition)
        sun_r = int(36 * scale)
        if kind == "sun":
            for angle in range(0, 360, 45):
                import math
                dx = int(math.cos(math.radians(angle)) * 58 * scale)
                dy = int(math.sin(math.radians(angle)) * 58 * scale)
                draw.line((x + dx * 0.72, y + dy * 0.72, x + dx, y + dy), fill=(239, 178, 56), width=max(3, int(6 * scale)))
            draw.ellipse((x - sun_r, y - sun_r, x + sun_r, y + sun_r), fill=(249, 199, 76))
            return

        cloud = (238, 244, 246)
        outline = (124, 153, 161)
        draw.ellipse((x - int(57 * scale), y - int(24 * scale), x + int(15 * scale), y + int(38 * scale)), fill=cloud, outline=outline, width=max(2, int(3 * scale)))
        draw.ellipse((x - int(15 * scale), y - int(48 * scale), x + int(54 * scale), y + int(38 * scale)), fill=cloud, outline=outline, width=max(2, int(3 * scale)))
        draw.rounded_rectangle((x - int(64 * scale), y, x + int(65 * scale), y + int(43 * scale)), radius=int(20 * scale), fill=cloud, outline=outline, width=max(2, int(3 * scale)))
        if kind in {"rain", "thunder"}:
            for offset in (-42, 0, 42):
                draw.line((x + int(offset * scale), y + int(58 * scale), x + int((offset - 10) * scale), y + int(84 * scale)), fill=(65, 151, 205), width=max(3, int(7 * scale)))
        elif kind == "snow":
            for offset in (-40, 0, 40):
                cy = y + int(70 * scale)
                draw.line((x + int((offset - 8) * scale), cy, x + int((offset + 8) * scale), cy), fill=(99, 164, 196), width=max(2, int(4 * scale)))
                draw.line((x + int(offset * scale), cy - int(8 * scale), x + int(offset * scale), cy + int(8 * scale)), fill=(99, 164, 196), width=max(2, int(4 * scale)))
        if kind == "thunder":
            draw.polygon([(x + 8, y + 48), (x + 38, y + 48), (x + 18, y + 78), (x + 38, y + 78), (x - 3, y + 120), (x + 8, y + 86), (x - 12, y + 86)], fill=(244, 189, 46))

    def render(self, weather: dict) -> str:
        image = self._load_background()
        draw = ImageDraw.Draw(image, "RGBA")
        ink = (35, 55, 61, 255)
        muted = (83, 111, 116, 255)
        accent = (32, 125, 135, 255)

        location = str(weather["location"])
        tz = ZoneInfo(str(weather.get("timezone") or "UTC"))
        issued_at = datetime.fromisoformat(str(weather["issued_at"])).astimezone(tz)
        date_text = issued_at.strftime("%Y年%m月%d日  %A")
        weekday_map = {
            "Monday": "星期一", "Tuesday": "星期二", "Wednesday": "星期三",
            "Thursday": "星期四", "Friday": "星期五", "Saturday": "星期六", "Sunday": "星期日",
        }
        for english, chinese in weekday_map.items():
            date_text = date_text.replace(english, chinese)

        draw.text((70, 62), "每日天气", font=self._font(34, bold=True), fill=(255, 255, 255, 245))
        location_font = self._fit_font(draw, location, 760, 72, 42, bold=True)
        location = self._truncate_text(draw, location, location_font, 760)
        draw.text((70, 120), location, font=location_font, fill=(255, 255, 255, 255))
        draw.text((74, 220), date_text, font=self._font(30), fill=(247, 252, 253, 245))

        current = weather["current"]
        draw.rounded_rectangle((48, 520, 1032, 865), radius=24, fill=(255, 255, 255, 225), outline=(207, 224, 222, 245), width=2)
        self._draw_icon(draw, (190, 685), current["condition"], 1.25)
        temp = f"{self._format_number(current['temperature_c'])}°"
        draw.text((330, 565), temp, font=self._font(112, bold=True), fill=ink)
        condition = str(current["condition"])
        condition_font = self._fit_font(draw, condition, 390, 46, 28, bold=True)
        condition = self._truncate_text(draw, condition, condition_font, 390)
        draw.text((590, 590), condition, font=condition_font, fill=accent)
        wind_font = self._font(30)
        if current.get("derived_from_daily"):
            details = (
                f"今日预报 {self._format_number(current['forecast_high_c'])}° / "
                f"{self._format_number(current['forecast_low_c'])}°\n暂无实时观测"
            )
        else:
            wind = self._truncate_text(draw, str(current["wind"]), wind_font, 390)
            details = (
                f"体感 {self._format_number(current['feels_like_c'])}°C    "
                f"湿度 {self._format_number(current['humidity_pct'])}%\n{wind}"
            )
        draw.multiline_text((590, 670), details, font=wind_font, fill=muted, spacing=16)

        alert_text = self._alert_text(weather.get("alerts", []))
        if alert_text:
            alert_font = self._fit_font(draw, alert_text, 900, 27, 20, bold=True)
            alert_text = self._truncate_text(draw, alert_text, alert_font, 900)
            draw.rounded_rectangle((70, 795, 1010, 842), radius=12, fill=(255, 238, 197, 245))
            draw.text((92, 803), alert_text, font=alert_font, fill=(134, 82, 25, 255))

        daily = weather["daily"]
        # The fourth label uses its weekday; the first three are easier to scan as relative days.
        day_labels = ["今天", "明天", "后天", self._weekday_label(daily[3]["date"])]
        left, top, gap, card_w, card_h = 48, 900, 16, 234, 390
        for index, item in enumerate(daily):
            x = left + index * (card_w + gap)
            draw.rounded_rectangle((x, top, x + card_w, top + card_h), radius=20, fill=(255, 255, 255, 222), outline=(214, 226, 222, 245), width=2)
            draw.text((x + 20, top + 24), day_labels[index], font=self._font(31, bold=True), fill=ink)
            draw.text((x + 20, top + 68), str(item["date"])[5:].replace("-", "/"), font=self._font(24), fill=muted)
            self._draw_icon(draw, (x + card_w // 2, top + 165), item["condition"], 0.66)
            daily_condition = str(item["condition"])
            condition_font = self._fit_font(draw, daily_condition, card_w - 36, 27, 19, bold=True)
            daily_condition = self._truncate_text(draw, daily_condition, condition_font, card_w - 36)
            box = draw.textbbox((0, 0), daily_condition, font=condition_font)
            draw.text((x + (card_w - (box[2] - box[0])) / 2, top + 232), daily_condition, font=condition_font, fill=accent)
            temps = f"{self._format_number(item['high_c'])}° / {self._format_number(item['low_c'])}°"
            temp_font = self._fit_font(draw, temps, card_w - 36, 34, 25, bold=True)
            temp_box = draw.textbbox((0, 0), temps, font=temp_font)
            draw.text((x + (card_w - (temp_box[2] - temp_box[0])) / 2, top + 285), temps, font=temp_font, fill=ink)

        provider = str(weather.get("provider") or "")
        source = self._source_label(weather.get("sources", []))
        footer = f"更新 {issued_at.strftime('%H:%M')}"
        if source:
            footer += f"  ·  来源 {source}"
        if provider:
            footer += f"  ·  {provider}"
        footer_font = self._fit_font(draw, footer, 940, 22, 16)
        footer = self._truncate_text(draw, footer, footer_font, 940)
        draw.text((70, 1362), footer, font=footer_font, fill=(92, 112, 111, 230))

        digest = hashlib.sha1(f"{location}:{issued_at.isoformat()}".encode("utf-8")).hexdigest()[:10]
        safe_location = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "_", location)[:32] or "weather"
        output_path = self.output_dir / f"weather_{issued_at:%Y%m%d}_{safe_location}_{digest}.png"
        image.save(output_path, format="PNG", optimize=True)
        self._cleanup()
        return str(output_path)

    @staticmethod
    def _format_number(value) -> str:
        if value is None or str(value).strip() == "":
            return "--"
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:.1f}"

    @staticmethod
    def _weekday_label(date_text: str) -> str:
        labels = "一二三四五六日"
        day = datetime.strptime(str(date_text), "%Y-%m-%d").weekday()
        return f"周{labels[day]}"

    @staticmethod
    def _alert_text(alerts) -> str:
        if not alerts:
            return ""
        first = alerts[0]
        if isinstance(first, dict):
            text = first.get("title") or first.get("name") or first.get("description") or ""
        else:
            text = str(first)
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        return f"天气预警：{text[:48]}" if text else ""

    @staticmethod
    def _source_label(sources) -> str:
        if not sources:
            return ""
        first = sources[0]
        if isinstance(first, dict):
            value = first.get("name") or first.get("title") or first.get("url") or ""
        else:
            value = str(first)
        value = re.sub(r"^https?://", "", str(value or "")).split("/")[0]
        return value[:48]

    def _cleanup(self) -> None:
        try:
            raw_max_count = self.config.get("cleanup_max_count", 60)
            max_count = max(0, int(60 if raw_max_count in (None, "") else raw_max_count))
        except Exception:
            max_count = 60
        if max_count <= 0:
            return
        files = sorted(self.output_dir.glob("weather_*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[max_count:]:
            try:
                path.unlink()
            except OSError:
                pass
