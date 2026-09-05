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

from pdf_extraction import split_value_unit_description

# Google periodically retires older model IDs for new accounts/projects.
# Try the current flash-tier model first, then fall back to older ones still
# active for some accounts -- so a single retirement doesn't hard-break this
# feature until the code is next updated.
DEFAULT_MODEL = "gemini-3.5-flash"
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]

# DeepSeek's API is OpenAI-compatible REST, so it's called with plain
# `requests` rather than a dedicated SDK -- one fewer optional dependency.
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"


class AIExtractionError(Exception):
    """Raised for any AI-extraction failure with a message safe to show the user."""


def gemini_available() -> bool:
    try:
        import google.genai  # noqa: F401
        return True
    except ImportError:
        return False


def deepseek_available() -> bool:
    try:
        import requests  # noqa: F401
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

    Returns { key: {"value": str, "unit": str, "description": str, "raw": str,
    "found": bool} } -- the same shape as pdf_extraction.extract_key_strings
    (each value split into value/unit/description), so callers can treat
    both uniformly.
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
        if val_str == "NF":
            results[k] = {"value": "", "unit": "", "description": "", "raw": "NF", "found": False}
        else:
            parts = split_value_unit_description(val_str)
            results[k] = {**parts, "raw": val_str, "found": True}
    return results


def _build_cross_doc_prompt(question: str, snippets: list[dict]) -> str:
    """Shared prompt for cross-document Q&A -- same wording regardless of
    which model answers it, so results are comparable across providers."""
    doc_blocks = []
    for s in snippets:
        text = (s.get("text") or "").strip() or "(no extracted text)"
        doc_blocks.append(f"### {s.get('name', '')} (ID: {s.get('doc_id', '')})\n{text}")

    return (
        "You are answering a question using ONLY the documents provided "
        "below -- do not use outside knowledge. Each document is delimited "
        "by a heading giving its name and ID.\n\n"
        "If the answer isn't in any of the documents, say so plainly instead "
        "of guessing.\n\n"
        f"Question: {question}\n\n"
        "Documents:\n\n" + "\n\n".join(doc_blocks) + "\n\n"
        "Respond with ONLY a single JSON object of the form "
        '{"answer": "<your answer, in the same language as the question>", '
        '"cited_doc_ids": ["<ID of each document you actually used>", ...]}. '
        "No explanation, no markdown code fences, just the JSON object."
    )


def _parse_cross_doc_answer(raw: str) -> dict:
    """Shared response parsing for cross-document Q&A -- see
    _build_cross_doc_prompt. Returns {"answer": str, "cited_doc_ids": [str, ...]}."""
    parsed = _extract_json_object(raw)
    if not isinstance(parsed, dict):
        raise AIExtractionError("AI 返回的内容格式不正确（不是一个 JSON 对象）。")

    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        raise AIExtractionError("AI 未返回有效的回答。")
    cited = parsed.get("cited_doc_ids") or []
    if not isinstance(cited, list):
        cited = []
    cited_doc_ids = [str(c).strip() for c in cited if str(c).strip()]

    return {"answer": answer, "cited_doc_ids": cited_doc_ids}


def _check_cross_doc_inputs(question: str, snippets: list[dict], api_key: str, label: str) -> str:
    """Shared validation for cross-document Q&A. Returns the trimmed question."""
    if not api_key:
        raise AIExtractionError(f"未配置 {label} API Key，请先在设置中添加。")
    question = (question or "").strip()
    if not question:
        raise AIExtractionError("请输入问题。")
    if not snippets:
        raise AIExtractionError("没有可供检索的文档（可能都还没有可提取的文字）。")
    return question


def answer_question_with_gemini(question: str, snippets: list[dict],
                                 api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Answer a natural-language question against a set of documents' text.

    snippets: [{"doc_id": str, "name": str, "text": str}, ...] -- already
    assembled by the caller (name/SN/description/metadata/OCR text). Unlike
    extract_keys_with_gemini, this sends text only, no page images, so it can
    reason over many documents at once within one prompt.

    Returns {"answer": str, "cited_doc_ids": [str, ...]}. Callers should
    treat cited_doc_ids as untrusted and cross-check them against the actual
    candidate set before showing them as clickable citations.
    """
    question = _check_cross_doc_inputs(question, snippets, api_key, "Gemini")

    try:
        from google import genai
    except ImportError:
        raise AIExtractionError(
            "未安装 google-genai 库。请在运行此程序的 Python 中执行：pip install google-genai")

    prompt = _build_cross_doc_prompt(question, snippets)

    client = genai.Client(api_key=api_key)
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    response = None
    last_error = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(model=candidate, contents=[prompt])
            last_error = None
            break
        except Exception as e:
            last_error = e
            msg = str(e)
            if "NOT_FOUND" in msg or "404" in msg or "no longer available" in msg:
                continue
            break

    if last_error is not None:
        raise AIExtractionError(f"调用 Gemini API 失败：{last_error}")

    raw = getattr(response, "text", None) or ""
    return _parse_cross_doc_answer(raw)


def answer_question_with_deepseek(question: str, snippets: list[dict],
                                   api_key: str, model: str = DEEPSEEK_DEFAULT_MODEL) -> dict:
    """DeepSeek equivalent of answer_question_with_gemini -- same prompt and
    response contract, so callers (and the UI) can treat both providers
    identically. See that function's docstring for the snippets/return shape.
    """
    question = _check_cross_doc_inputs(question, snippets, api_key, "DeepSeek")
    prompt = _build_cross_doc_prompt(question, snippets)
    raw = _deepseek_chat([{"role": "user", "content": prompt}], api_key, model=model)
    return _parse_cross_doc_answer(raw)


def comment_on_trend_with_gemini(param_name: str, unit: str, points: list[dict],
                                  api_key: str, lang: str = "zh", model: str = DEFAULT_MODEL) -> dict:
    """Fit a trend to a parameter-vs-time series (from FolderPlotDialog) and
    get a plain-language comment on it.

    points: [{"date": "YYYY-MM-DD", "value": float}, ...], already sorted
    chronologically by the caller. Sent as plain numbers -- no document
    text/images involved, so this is cheap even on a large series.

    Returns {"comment": str}.
    """
    if not api_key:
        raise AIExtractionError("未配置 Gemini API Key，请先在设置中添加。")
    param_name = (param_name or "").strip()
    if not param_name:
        raise AIExtractionError("缺少参数名称。")
    if not points:
        raise AIExtractionError("没有可供分析的数据点。")

    try:
        from google import genai
    except ImportError:
        raise AIExtractionError(
            "未安装 google-genai 库。请在运行此程序的 Python 中执行：pip install google-genai")

    unit_suffix = f" {unit}" if unit else ""
    rows = "\n".join(f"- {p.get('date')}: {p.get('value')}{unit_suffix}" for p in points)
    lang_instruction = "Respond in English." if lang == "en" else "Respond in Chinese (简体中文)."

    prompt = (
        "You are a data analyst reviewing a time series of measured values "
        f"for the parameter \"{param_name}\"{f' (unit: {unit})' if unit else ''}, "
        "one value per document, sorted chronologically:\n\n"
        f"{rows}\n\n"
        "Fit a trend to this data (say whether it looks roughly linear, "
        "flat, or something else, and give an approximate rate of change "
        "over time if a linear fit is reasonable), then give a short, "
        "plain-language comment covering the overall direction, any "
        "anomalies or outliers, and anything worth a reviewer's attention. "
        "Keep it concise -- a few sentences to a short paragraph, meant as "
        "a quick read next to a chart, not a formal report.\n\n"
        f"{lang_instruction}\n\n"
        "Respond with ONLY a single JSON object of the form "
        '{"comment": "<your analysis>"}. No explanation, no markdown code '
        "fences, just the JSON object."
    )

    client = genai.Client(api_key=api_key)
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    response = None
    last_error = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(model=candidate, contents=[prompt])
            last_error = None
            break
        except Exception as e:
            last_error = e
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

    comment = str(parsed.get("comment") or "").strip()
    if not comment:
        raise AIExtractionError("AI 未返回有效的分析结果。")

    return {"comment": comment}


def _deepseek_chat(messages: list[dict], api_key: str,
                    model: str = DEEPSEEK_DEFAULT_MODEL, timeout: int = 60) -> str:
    """POST one chat-completion request to DeepSeek's OpenAI-compatible API.
    Returns the assistant's reply text."""
    try:
        import requests
    except ImportError:
        raise AIExtractionError(
            "未安装 requests 库。请在运行此程序的 Python 中执行：pip install requests")

    try:
        resp = requests.post(
            f"{DEEPSEEK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "stream": False},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise AIExtractionError(f"调用 DeepSeek API 失败（网络错误）：{e}")

    if resp.status_code != 200:
        raise AIExtractionError(f"调用 DeepSeek API 失败：HTTP {resp.status_code} {resp.text[:300]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIExtractionError("DeepSeek 返回的内容格式不正确。")


def answer_question_about_document(question: str, doc_name: str, doc_text: str,
                                    api_key: str, provider: str = "gemini") -> dict:
    """Answer a free-form question about ONE document's already-extracted
    text (OCR text, description, metadata -- whatever the caller assembled),
    using either Gemini or DeepSeek.

    Unlike answer_question_with_gemini (which reasons over many documents
    and returns citations), this is a single-document Q&A with a plain-text
    answer -- the caller already knows which document it asked about.

    Returns {"answer": str, "model": str}.
    """
    question = (question or "").strip()
    if not question:
        raise AIExtractionError("请输入问题。")
    if not api_key:
        label = "DeepSeek" if provider == "deepseek" else "Gemini"
        raise AIExtractionError(f"未配置 {label} API Key，请先在设置中添加。")

    text = (doc_text or "").strip() or "(no extracted text available for this document)"
    prompt = (
        "You are answering a question about ONE document, using ONLY the "
        "text extracted from it below -- do not use outside knowledge. If "
        "the answer isn't in the text, say so plainly instead of guessing.\n\n"
        f"Document: {doc_name}\n\n{text}\n\n"
        f"Question: {question}\n\n"
        "Answer concisely, in the same language as the question."
    )

    if provider == "deepseek":
        answer = _deepseek_chat([{"role": "user", "content": prompt}], api_key).strip()
        model_used = DEEPSEEK_DEFAULT_MODEL
        if not answer:
            raise AIExtractionError("AI 未返回有效的回答。")
        return {"answer": answer, "model": model_used}

    try:
        from google import genai
    except ImportError:
        raise AIExtractionError(
            "未安装 google-genai 库。请在运行此程序的 Python 中执行：pip install google-genai")

    client = genai.Client(api_key=api_key)
    candidates = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]
    response = None
    last_error = None
    model_used = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(model=candidate, contents=[prompt])
            model_used = candidate
            last_error = None
            break
        except Exception as e:
            last_error = e
            msg = str(e)
            if "NOT_FOUND" in msg or "404" in msg or "no longer available" in msg:
                continue
            break

    if last_error is not None:
        raise AIExtractionError(f"调用 Gemini API 失败：{last_error}")

    answer = (getattr(response, "text", None) or "").strip()
    if not answer:
        raise AIExtractionError("AI 未返回有效的回答。")
    return {"answer": answer, "model": model_used}


# Style presets for polish_description_text(). Keep the wording tight -- these
# are appended to a fixed instruction block, not shown to the user.
_POLISH_STYLES = {
    "polish": (
        "Fix grammar, spelling, punctuation and awkward phrasing. Merge "
        "fragments into complete sentences. Keep it close to the original "
        "length -- do not pad."
    ),
    "organize": (
        "Reorganize the notes into a clear structure: group related points "
        "together, put them in a sensible order, and use short paragraphs or "
        "a bullet list ('- ' prefix) when there are several distinct points. "
        "Fix grammar, spelling and punctuation along the way."
    ),
    "concise": (
        "Rewrite as a tight, well-structured summary: drop filler and "
        "repetition, keep every concrete fact, fix grammar and punctuation."
    ),
}


def polish_description_text(text: str, api_key: str, provider: str = "gemini",
                            lang: str = "auto", style: str = "organize") -> dict:
    """Clean up a free-form description/notes block the user typed or dictated.

    Dictated notes in particular come in as run-on text with no punctuation;
    this organizes and polishes them WITHOUT inventing facts. The caller shows
    the result next to the original so the user can accept or discard it.

    lang:  "auto" (keep the note's own language), "zh" or "en" to force one.
    style: one of _POLISH_STYLES ("polish" | "organize" | "concise").

    Returns {"text": str, "model": str}.
    """
    text = (text or "").strip()
    if not text:
        raise AIExtractionError("没有可整理的文字。")
    if not api_key:
        label = "DeepSeek" if provider == "deepseek" else "Gemini"
        raise AIExtractionError(f"未配置 {label} API Key，请先在设置中添加。")

    style_instruction = _POLISH_STYLES.get(style) or _POLISH_STYLES["organize"]
    if lang == "zh":
        lang_instruction = "Write the result in Chinese (简体中文)."
    elif lang == "en":
        lang_instruction = "Write the result in English."
    else:
        lang_instruction = (
            "Write the result in the SAME language as the input (if the input "
            "is Chinese, respond in Chinese; if English, respond in English)."
        )

    prompt = (
        "You are an editor tidying up a photo/document description that "
        "someone typed or dictated. It may be a run-on with no punctuation.\n\n"
        "Rules:\n"
        "- Preserve every fact, name, number, date and measurement exactly. "
        "Do NOT add information, opinions or details that aren't in the notes.\n"
        "- Do NOT remove any distinct piece of information.\n"
        f"- {style_instruction}\n"
        f"- {lang_instruction}\n"
        "- Return ONLY the cleaned-up text -- no preamble, no explanation, no "
        "markdown code fences, no surrounding quotes.\n\n"
        "Notes to clean up:\n"
        "-----\n"
        f"{text}\n"
        "-----"
    )

    if provider == "deepseek":
        out = _deepseek_chat([{"role": "user", "content": prompt}], api_key).strip()
        if not out:
            raise AIExtractionError("AI 未返回有效的结果。")
        return {"text": out, "model": DEEPSEEK_DEFAULT_MODEL}

    try:
        from google import genai
    except ImportError:
        raise AIExtractionError(
            "未安装 google-genai 库。请在运行此程序的 Python 中执行：pip install google-genai")

    client = genai.Client(api_key=api_key)
    candidates = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]
    response = None
    last_error = None
    model_used = None
    for candidate in candidates:
        try:
            response = client.models.generate_content(model=candidate, contents=[prompt])
            model_used = candidate
            last_error = None
            break
        except Exception as e:
            last_error = e
            msg = str(e)
            if "NOT_FOUND" in msg or "404" in msg or "no longer available" in msg:
                continue
            break

    if last_error is not None:
        raise AIExtractionError(f"调用 Gemini API 失败：{last_error}")

    out = (getattr(response, "text", None) or "").strip()
    # Models sometimes wrap the answer in a ``` fence despite being told not to.
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out).strip()
    if not out:
        raise AIExtractionError("AI 未返回有效的结果。")
    return {"text": out, "model": model_used}
