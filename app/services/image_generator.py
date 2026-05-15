import base64
import json
import re
from pathlib import Path

from openai import OpenAI

from app.core.config import settings

BASE_DIR = Path(__file__).resolve().parents[2]
IMAGE_DIR = BASE_DIR / "app" / "static" / "generated-images"


class ImageGenerator:
    def __init__(self) -> None:
        self.client = OpenAI(api_key=settings.openai_api_key)

    def generate_images_for_task(
        self,
        task_id: int,
        image_prompts_json: str | None,
    ) -> list[dict[str, str]]:
        if not image_prompts_json:
            return []

        image_prompts = json.loads(image_prompts_json)
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)

        generated_images = []

        for index, item in enumerate(image_prompts, start=1):
            section = item.get("section", f"image-{index}")
            prompt = item.get("prompt", "")
            alt_text = item.get("alt_text", "")

            if not prompt:
                continue

            filename = f"task-{task_id}-{index}-seo-image.png"
            file_path = IMAGE_DIR / filename

            response = self.client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                quality="medium",
            )

            image_base64 = response.data[0].b64_json
            image_bytes = base64.b64decode(image_base64)

            file_path.write_bytes(image_bytes)

            generated_images.append(
                {
                    "section": section,
                    "alt_text": alt_text,
                    "filename": filename,
                    "url": f"/static/generated-images/{filename}",
                }
            )

        return generated_images

    def _slugify(self, value: str) -> str:
        value = value.lower().strip()
        value = re.sub(r"[^a-z0-9\u0590-\u05ff]+", "-", value)
        value = value.strip("-")
        return value or "image"