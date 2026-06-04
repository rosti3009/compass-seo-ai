from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import base64
from pathlib import Path

PLACEHOLDER_PNG_BYTES = bytes.fromhex(
    "89504E470D0A1A0A0000000D4948445200000001000000010802000000907753DE"
    "0000000C49444154789C63F8FFFF3F0005FE02FE0A0DAF0F0000000049454E44AE426082"
)

from openai import OpenAI

from app.core.config import settings

REALISTIC_IMAGE_RULES = (
    "horizontal image, photorealistic commercial quality BBQ magazine photography, "
    "natural realistic lighting, realistic metal/stone/wood textures, realistic food, "
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
    error: str | None = None


class BaseImageProvider:
    """Provider-neutral image API for article image packages."""

    provider_name = "none"

    def generate_image(self, prompt: str, *, draft_slug: str, image_key: str = "featured_image") -> ImageGenerationResult:
        raise NotImplementedError

    def generate_hero_image(self, prompt: str, *, draft_slug: str) -> ImageGenerationResult:
        return self.generate_image(prompt, draft_slug=draft_slug, image_key="featured_image")


class DisabledImageProvider(BaseImageProvider):
    provider_name = "none"

    def generate_image(self, prompt: str, *, draft_slug: str, image_key: str = "featured_image") -> ImageGenerationResult:
        return ImageGenerationResult(
            enabled=False,
            provider=self.provider_name,
            status="planned",
            message_he="יצירת תמונה לא פעילה כרגע — קיים תכנון תמונה בלבד",
        )


class FluxImageProvider(DisabledImageProvider):
    provider_name = "flux"


class IdeogramImageProvider(DisabledImageProvider):
    provider_name = "ideogram"


class RecraftImageProvider(DisabledImageProvider):
    provider_name = "recraft"


def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (None, None)
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height)


class OpenAIImageProvider(BaseImageProvider):
    provider_name = "openai"

    def __init__(self) -> None:
        self.client = OpenAI(api_key=settings.openai_api_key)

    def generate_image(self, prompt: str, *, draft_slug: str, image_key: str = "featured_image") -> ImageGenerationResult:
        try:
            response = self.client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1536x1024",
            )
            item = response.data[0] if response.data else None
            b64_json = getattr(item, "b64_json", None) if item else None
            if not b64_json:
                return ImageGenerationResult(
                    enabled=True,
                    provider=self.provider_name,
                    status="failed",
                    message_he="יצירת התמונה נכשלה — הספק לא החזיר תוכן תמונה.",
                    error="OpenAI image provider returned no image bytes",
                )
            image_bytes = base64.b64decode(b64_json)
            static_root = Path("app/static/generated-images")
            static_root.mkdir(parents=True, exist_ok=True)
            filename = f"{draft_slug}.png"
            image_path = static_root / filename
            image_path.write_bytes(image_bytes)
            width, height = _png_dimensions(image_bytes)
            return ImageGenerationResult(
                enabled=True,
                provider=self.provider_name,
                status="generated",
                image_url=f"/static/generated-images/{filename}",
                width=width,
                height=height,
                generated_at=datetime.now(UTC).isoformat(),
                message_he="התמונה נוצרה ונשמרה בהצלחה.",
            )
        except Exception as exc:
            return ImageGenerationResult(
                enabled=True,
                provider=self.provider_name,
                status="failed",
                message_he="יצירת התמונה נכשלה בספק OpenAI.",
                error=str(exc),
            )


IMAGE_PROVIDER_REGISTRY: dict[str, type[BaseImageProvider]] = {
    "openai": OpenAIImageProvider,
    "flux": FluxImageProvider,
    "ideogram": IdeogramImageProvider,
    "recraft": RecraftImageProvider,
    "none": DisabledImageProvider,
    "": DisabledImageProvider,
}


def get_image_provider() -> BaseImageProvider:
    raw = (getattr(settings, "image_provider", None) or "").strip().lower()
    if raw == "openai" and not settings.openai_api_key:
        return DisabledImageProvider()
    provider_cls = IMAGE_PROVIDER_REGISTRY.get(raw, DisabledImageProvider)
    return provider_cls()


def build_realistic_hero_prompt(base_prompt: str) -> str:
    clean = (base_prompt or "premium bbq hero").strip()
    if "photorealistic commercial quality BBQ magazine photography" in clean:
        return clean
    return f"{clean}. {REALISTIC_IMAGE_RULES}"
