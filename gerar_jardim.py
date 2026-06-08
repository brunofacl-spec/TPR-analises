"""
Gera visualização do jardim da Aroeira usando Gemini via inference.sh
Corre: python3 gerar_jardim.py foto_jardim.jpg
"""
import sys, base64, json, urllib.request
from inferencesh import inference  # pip install inferencesh

API_KEY = "1nfsh-081fgb06c0h1gsz6nwdkvnwskn"

PROMPT = """Transform this front garden of a modern house in Aroeira, Charneca da Caparica, Portugal into its finished landscaped state, faithful to the architectural project.

Current state: bare sandy/dirt ground, large light grey porcelain stepping stone path leading diagonally to the dark anthracite RAL7016 pedestrian gate, cream/white rendered walls, grey tile terrace on left side, dark anthracite vertical-slat metal fence.

Transform to finished garden:
- Replace all bare sandy ground with lush well-maintained green lawn (relvado)
- Add 3 medium aroeira shrubs (Pistacia lentiscus, dense dark green rounded shape) evenly spaced along the cream perimeter wall background
- Add low border plantings of agapanthus (blue flowers) and lavender along the stepping stone path edges
- Keep the large light grey porcelain stepping stone path exactly as positioned
- Keep the dark anthracite RAL7016 metal gate and vertical fence slats unchanged
- Keep the cream/white rendered walls unchanged
- Keep the covered terrace with grey tiles on the left unchanged
- Tall Pinus pinea (stone pines) visible beyond the fence as in original photo
- Photorealistic render, soft afternoon Atlantic light, light blue sky with some clouds, Portugal coastal atmosphere"""

def encode_image(path):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    ext = path.split(".")[-1].lower()
    mime = "image/jpeg" if ext in ["jpg", "jpeg"] else "image/png"
    return f"data:{mime};base64,{data}"

def save_output(images):
    for i, img in enumerate(images):
        if isinstance(img, str) and img.startswith("data:"):
            header, b64 = img.split(",", 1)
            ext = "png" if "png" in header else "jpg"
            out = f"jardim_aroeira_{i}.{ext}"
            with open(out, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"✅ Guardado: {out}")
        elif isinstance(img, str) and img.startswith("http"):
            out = f"jardim_aroeira_{i}.jpg"
            urllib.request.urlretrieve(img, out)
            print(f"✅ Guardado: {out}")

if __name__ == "__main__":
    foto_path = sys.argv[1] if len(sys.argv) > 1 else "jardim.jpg"

    client = inference(api_key=API_KEY)

    print(f"📸 A carregar imagem: {foto_path}")
    img = encode_image(foto_path)

    print("🌿 A gerar jardim...")
    result = client.run({
        "app": "google/gemini-3-1-flash-image-preview",
        "input": {
            "prompt": PROMPT,
            "images": [img],
            "aspect_ratio": "9:16",
            "resolution": "2K",
            "num_images": 2
        }
    })

    output = result.get("output", result)
    images = output.get("images", []) if isinstance(output, dict) else (output if isinstance(output, list) else [])

    if images:
        save_output(images)
    else:
        print("Resposta completa:", json.dumps(result, indent=2)[:1000])
