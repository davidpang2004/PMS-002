"""AI-assisted key-parameter extraction using Google Gemini (opt-in).

This is a separate path from pdf_extraction.extract_key_strings: instead of
regex-matching a keyword in OCR'd text, it sends the document's page images
directly to a multimodal model and asks it to read each field's value off
the page. This is off by default and only runs when the user has explicitly
saved a Gemini API key (see /api/ai-settings in dms_server.py) -- nothing
about a document ever leaves the machine unless the user turns this on.
"""
from __future__ import annotations

import json
import re

# Google periodically retires older model IDs for new accounts/projects.
# Try the current flash-tier model first, then fall back to older ones still
# active for some accounts -- so a single retirement doesn't hard-break this
# feature until the code is next updated.
DEFAULT_MODEL = "gemini-3.5-flash"
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]


class AIExtractionError(Exception):
    """Raised for any AI-extraction failure with a message safe to show the user."""


def gemini_available() -> bool:
    try:
        import google.genai  # noqa: F401
        return True
    except ImportError:
        return False


def _extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise AIExtractionError("AI 返回的内容不是有效的 JSON，无法解析提取结果。")


def extract_keys_with_gemini(image_bytes_list: list[bytes], keys: list[str],
                              api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Send document page images to Gemini and ask it to read each key's
    value directly off the page (bypassing OCR text-matching entirely).

    Returns { key: {"value": str, "found": bool} } -- the same shape as
    pdf_extraction.extract_key_strings, so callers can treat both uniformly.
    """
    if not image_bytes_list:
        raise AIExtractionError("没有可发送给 AI 的页面图像（文档可能不是 PDF 或图片）。")
    if not api_key:
        raise AIExtractionError("未配置 Gemini API Key，请先在设置中添加。")

    clean_keys = [str(k).strip() for k in keys if str(k).strip()]
    if not clean_keys:
        raise AIExtractionError("没有要提取的参数。")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise AIExtractionError(
            "未安装 google-genai 库。请在运行此程序的 Python 中执行：pip install google-genai")

    prompt = (
        "You are extracting specific field values from a scanned document or "
        "certificate image. For each of the following field names, find its "
        "value exactly as printed in the document (keep numbers, units, and "
        "formatting as shown). If a field genuinely does not appear anywhere "
        "in the document, use the literal string \"NF\" for it.\n\n"
        "Field names to extract:\n"
        + "\n".join(f"- {k}" for k in clean_keys)
        + "\n\nRespond with ONLY a single JSON object mapping each field name "
        "to its extracted value as a string. No explanation, no markdown "
        "code fences, just the JSON object."
    )

    contents = [prompt]
    for img_bytes in image_bytes_list:
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))

    client = genai.Client(api_key=api_key)
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    response = None
    last_error = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(model=candidate, contents=contents)
            last_error = None
            break
        except Exception as e:
            last_error = e
            # Only fall through to the next model if this one is gone/retired;
            # any other error (bad key, quota, network) should surface as-is.
            msg = str(e)
            if "NOT_FOUND" in msg or "404" in msg or "no longer available" in msg:
                continue
            break

    if last_error is not None:
        raise AIExtractionError(f"调用 Gemini API 失败：{last_error}")

    raw = getattr(response, "text", None) or ""
    parsed = _extract_json_object(raw)
    if not isinstance(parsed, dict):
        raise AIExtractionError("AI 返回的内容格式不正确（不是一个 JSON 对象）。")

    results = {}
    for k in clean_keys:
        val = parsed.get(k)
        if val is None:
            for pk, pv in parsed.items():
                if str(pk).strip().lower() == k.lower():
                    val = pv
                    break
        val_str = str(val).strip() if val is not None else "NF"
        if not val_str:
            val_str = "NF"
        results[k] = {"value": val_str, "found": val_str != "NF"}
    return results
