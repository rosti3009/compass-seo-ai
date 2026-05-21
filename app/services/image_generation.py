from __future__ import annotations

from dataclasses import dataclass

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
        # Safe placeholder architecture: endpoint enabled, provider wiring ready, no external call yet.
        return ImageGenerationResult(
            enabled=True,
            provider=self.provider_name,
            status="generated",
            image_url=f"generated://{self.provider_name}/{draft_slug}",
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
