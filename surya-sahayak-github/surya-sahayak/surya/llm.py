"""Thin Bedrock helper shared by the bill reader and quote auditor.

One function, `ask_vision_json`, sends an image or PDF plus a prompt to a
Bedrock model through the Converse API and returns parsed JSON.
"""
from __future__ import annotations

import io
import json
import os
import re

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-southeast-2"
VISION_MODEL = os.getenv("BILL_MODEL_ID", "apac.amazon.nova-lite-v1:0")

MAX_IMAGE_BYTES = 3_500_000      # Converse limit is 3.75 MB per image
MAX_SIDE_PX = 2000               # plenty for reading a bill, keeps calls fast

_client = None


def client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client("bedrock-runtime", region_name=REGION,
                               config=Config(read_timeout=120, retries={"max_attempts": 3}))
    return _client


def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Fix phone-photo rotation, shrink, and re-encode as JPEG under the size limit."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)          # phone photos are often stored sideways
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    for quality in (88, 80, 70, 60):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue(), "jpeg"
    im.thumbnail((1400, 1400))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=70)
    return buf.getvalue(), "jpeg"


def content_block(data: bytes, filename: str) -> dict:
    """Build a Converse content block for an image or a PDF."""
    if filename.lower().endswith(".pdf") or data[:4] == b"%PDF":
        return {"document": {"format": "pdf", "name": "uploaded document",
                             "source": {"bytes": data}}}
    img, fmt = prepare_image(data)
    return {"image": {"format": fmt, "source": {"bytes": img}}}


def parse_json(text: str) -> dict:
    """Pull the first {...} object out of a model reply (handles ```json fences)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON in model output: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Unbalanced JSON in model output")


def ask_vision_json(data: bytes, filename: str, prompt: str, model_id: str | None = None,
                    max_tokens: int = 2000) -> dict:
    resp = client().converse(
        modelId=model_id or VISION_MODEL,
        messages=[{"role": "user", "content": [content_block(data, filename), {"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
    return parse_json(text)
