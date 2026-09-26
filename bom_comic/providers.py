"""All SDK-specific operations live here; providers are injectable in tests."""
from pathlib import Path
from typing import Protocol
from PIL import Image

class ImageProvider(Protocol):
    def generate_image(self, prompt, reference_images=None, output_path=None): ...

class Gemini:
    def __init__(self, config, store):
        from google import genai
        self.config, self.store = config, store
        self.client = genai.Client(api_key=config.api_key)
    def structured(self, prompt, schema, tag, kind="text"):
        model = self.config.require(kind)
        self.store.write(f"api/{tag}.request.json", {"model": model, "prompt": prompt})
        response = self.client.models.generate_content(model=model, contents=prompt,
            config={"response_mime_type": "application/json", "response_schema": schema})
        self.store.write(f"api/{tag}.response.json", response.model_dump(mode="json"))
        return schema.model_validate_json(response.text)
    def generate_image(self, prompt, reference_images=None, output_path=None):
        import io
        from google.genai import types
        model = self.config.require("image")
        contents = [prompt]
        for path in reference_images or []:
            with Image.open(path) as im:
                buffer = io.BytesIO()
                im.save(buffer, format="PNG")
                contents.append(types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png"))
        response = self.client.models.generate_content(model=model, contents=contents,
            config={"response_modalities": ["TEXT", "IMAGE"]})
        self.store.write(f"api/{Path(output_path).stem}.response.json", response.model_dump(mode="json"))
        for part in response.parts or []:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                with Image.open(io.BytesIO(part.inline_data.data)) as im:
                    im.convert("RGB").save(output_path, "PNG")
                return str(output_path)
        raise ValueError("Gemini returned no image; inspect saved API response")

class PlaceholderImages:
    """Offline plumbing test, never evidence of visual/scriptural accuracy."""
    def generate_image(self, prompt, reference_images=None, output_path=None):
        from PIL import ImageDraw
        im = Image.new("RGB", (1000, 550), "#273340")
        ImageDraw.Draw(im).text((30, 30), "OFFLINE TEST PLACEHOLDER - NOT COMIC ART", fill="white")
        im.save(output_path)
        return str(output_path)
