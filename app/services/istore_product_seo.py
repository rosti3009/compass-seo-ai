product_id: str
name: str
url: str | None
category: str | None
title: str
meta_description: str
description_text: str
score: int
issues: list[str]
recommendations: list[str]
suggested_title: str
suggested_meta_description: str
suggested_h1: str
image_count: int
price: str | None

def as_dict(self) -> dict[str, object]:
    return {
        "product_id": self.product_id,
        "name": self.name,
        "url": self.url,
        "category": self.category,
        "title": self.title,
        "meta_description": self.meta_description,
        "description_text": self.description_text,
        "score": self.score,
        "issues": self.issues,
        "recommendations": self.recommendations,
        "suggested_title": self.suggested_title,
        "suggested_meta_description": self.suggested_meta_description,
        "suggested_h1": self.suggested_h1,
        "image_count": self.image_count,
        "price": self.price,
    }