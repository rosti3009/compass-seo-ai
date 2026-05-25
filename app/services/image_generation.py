from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PLACEHOLDER_PNG_BYTES = bytes.fromhex(
    "89504E470D0A1A0A0000000D4948445200000001000000010802000000907753DE"
    "0000000C49444154789C63F8FFFF3F0005FE02FE0A0DAF0F0000000049454E44AE426082"
)

from app.core.config import settings

REALISTIC_IMAGE_RULES = (
    "horizontal hero image, ultra realistic premium BBQ photography, natural lighting, "
    "no text inside image, no fake logos, no unrealistic meat"
)


@dataclass(frozen=True)
class ImageGenerationResult:
    enabled: bool
    provider: str
    status: str
    image_url: str | None = None
    width: int | None = None
    height: int | None = None
    generated_at: str | None = None
    message_he: str = ""


class BaseImageProvider:
    provider_name = "none"

    def generate_hero_image(self, prompt: str, *, draft_slug: str) -> ImageGenerationResult:
        raise NotImplementedError


class DisabledImageProvider(BaseImageProvider):
    provider_name = "none"

    def generate_hero_image(self, prompt: str, *, draft_slug: str) -> ImageGenerationResult:
        return ImageGenerationResult(
            enabled=False,
            provider=self.provider_name,
            status="planned",
            message_he="יצירת תמונה לא פעילה כרגע — קיים תכנון תמונה בלבד",
        )


class StubEnabledProvider(BaseImageProvider):
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name

    def generate_hero_image(self, prompt: str, *, draft_slug: str) -> ImageGenerationResult:
        static_root = Path("app/static/generated-images")
        static_root.mkdir(parents=True, exist_ok=True)
        filename = f"{draft_slug}.png"
        placeholder_path = static_root / filename
        if not placeholder_path.exists():
            placeholder_path.write_bytes(PLACEHOLDER_PNG_BYTES)

        return ImageGenerationResult(
            enabled=True,
            provider=self.provider_name,
            status="generated",
            image_url=f"/static/generated-images/{filename}",
            width=1,
            height=1,
            generated_at=datetime.now(UTC).isoformat(),
            message_he=f"הופעל ספק תמונות: {self.provider_name}. נשמר prompt בטוח וריאליסטי.",
        )


def get_image_provider() -> BaseImageProvider:
    raw = (getattr(settings, "image_provider", None) or "").strip().lower()
    if raw in {"openai", "stability"}:
        return StubEnabledProvider(raw)
    return DisabledImageProvider()


def build_realistic_hero_prompt(base_prompt: str) -> str:
    clean = (base_prompt or "premium bbq hero").strip()
    return f"{clean}. {REALISTIC_IMAGE_RULES}"
