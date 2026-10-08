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

# deepface/TensorFlow print banners and warnings to stdout, which would corrupt the
# JSON result. Send everything to stderr and keep the real stdout for the result only.
_result_out = sys.stdout
sys.stdout = sys.stderr


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


_MAX_DETECT_SIDE = 2000  # px, longest side of the image handed to deepface

if action == "represent":
    from deepface import DeepFace
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    # want_crops=False skips the per-face thumbnail (recognize never shows
    # them). stream=True writes one JSON line per photo as soon as it's done,
    # instead of one object at the end, so the server can cache each result
    # right away and keep them even if the run is cut short.
    want_crops = cmd.get("crops", True)
    stream = cmd.get("stream", False)

    def _emit(r):
        if stream:
            _result_out.write(json.dumps(r) + "\n")
            _result_out.flush()
        else:
            results.append(r)

    results = []
    for item in cmd.get("items", []):
        doc_id = item["doc_id"]
        path = item["path"]
        try:
            import numpy as np
            from PIL import Image
            img = Image.open(path).convert("RGB")
            # Big scans/photos (40+ MP) make OpenCV run out of memory, and faces don't
            # need that resolution — detect on a downscaled copy, then map boxes back
            # to original-image pixels (which is what the server crops from).
            scale = min(1.0, _MAX_DETECT_SIDE / max(img.width, img.height))
            small = img if scale == 1.0 else img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS)
            reps = DeepFace.represent(
                img_path=np.asarray(small)[:, :, ::-1].copy(),  # RGB -> BGR for deepface
                model_name="Facenet512",
                detector_backend="opencv",
                enforce_detection=False,
                align=True,
            )
            del small
            faces = []
            for rep in reps:
                raw = rep.get("facial_area", {})
                bbox = {k: round(raw.get(k, d) / scale)
                        for k, d in (("x", 0), ("y", 0), ("w", 50), ("h", 50))}
                faces.append({
                    "bbox": bbox,
                    "confidence": rep.get("face_confidence", 0),
                    "embedding": rep.get("embedding", []),
                    "crop_b64": _crop_b64(img, bbox) if want_crops else "",
                })
            _emit({"doc_id": doc_id, "faces": faces})
        except Exception as e:
            _emit({"doc_id": doc_id, "faces": [], "error": str(e)})

    if not stream:
        json.dump({"results": results}, _result_out)

else:
    json.dump({"error": f"Unknown action: {action}"}, _result_out)

_result_out.flush()
