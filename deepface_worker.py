#!/usr/bin/env python3
"""
Deepface worker — called by dms_server.py via subprocess.
Reads a JSON command from stdin, writes a JSON result to stdout.
Must run in the deepface venv (Python 3.12 + TensorFlow + deepface).
"""
from __future__ import annotations
import base64, io, json, sys, os

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")   # suppress TF noise
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")

cmd = json.loads(sys.stdin.read())
action = cmd.get("action")


def _crop_b64(img, bbox: dict) -> str:
    try:
        from PIL import Image
        if not isinstance(img, Image.Image):
            img = Image.open(img).convert("RGB")
        x, y, w, h = bbox.get("x", 0), bbox.get("y", 0), bbox.get("w", 50), bbox.get("h", 50)
        margin = max(int(min(w, h) * 0.2), 10)
        x0, y0 = max(0, x - margin), max(0, y - margin)
        x1, y1 = min(img.width, x + w + margin), min(img.height, y + h + margin)
        crop = img.crop((x0, y0, x1, y1)).resize((120, 120))
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


if action == "represent":
    from deepface import DeepFace
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    results = []
    for item in cmd.get("items", []):
        doc_id = item["doc_id"]
        path = item["path"]
        try:
            reps = DeepFace.represent(
                img_path=path,
                model_name="Facenet512",
                detector_backend="opencv",
                enforce_detection=False,
                align=True,
            )
            from PIL import Image
            img = Image.open(path).convert("RGB")
            faces = []
            for rep in reps:
                raw = rep.get("facial_area", {})
                bbox = {"x": raw.get("x", 0), "y": raw.get("y", 0),
                        "w": raw.get("w", 50), "h": raw.get("h", 50)}
                faces.append({
                    "bbox": bbox,
                    "confidence": rep.get("face_confidence", 0),
                    "embedding": rep.get("embedding", []),
                    "crop_b64": _crop_b64(img, bbox),
                })
            results.append({"doc_id": doc_id, "faces": faces})
        except Exception as e:
            results.append({"doc_id": doc_id, "faces": [], "error": str(e)})

    json.dump({"results": results}, sys.stdout)

else:
    json.dump({"error": f"Unknown action: {action}"}, sys.stdout)

sys.stdout.flush()
