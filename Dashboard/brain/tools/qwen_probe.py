import sys
import ollama

IMG = r"tools\f3_frames_v2\0.5m_centre.jpg"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5vl:3b"
PROMPT = (
    'Find "the red ball" in this image from a small floor robot\'s camera.\n'
    "If it is visible, give its bounding box in pixel coordinates.\n"
    "Respond ONLY with JSON: "
    '{"visible": true/false, "bbox_2d": [x1, y1, x2, y2] or null, '
    '"confidence": "high"|"medium"|"low", "description": "one short sentence"}'
)
img = open(IMG, "rb").read()
client = ollama.Client()

for fmt in ("json", None):
    for rp in (1.3, 1.0):
        kwargs = dict(
            model=MODEL,
            messages=[{"role": "user", "content": PROMPT, "images": [img]}],
            options={"num_ctx": 8192, "repeat_penalty": rp, "num_predict": 300},
        )
        if fmt:
            kwargs["format"] = fmt
        r = client.chat(**kwargs)
        print(f"format={fmt!s:5} repeat_penalty={rp}: {r['message']['content'].strip()[:200]!r}\n")
