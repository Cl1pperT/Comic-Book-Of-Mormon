"""All SDK-specific operations live here; providers are injectable in tests."""
from pathlib import Path
from typing import Protocol
from PIL import Image
from .errors import ProviderError, api_error

class ImageProvider(Protocol):
    def generate_image(self, prompt, reference_images=None, output_path=None): ...

class Gemini:
    def __init__(self, config, store):
        from google import genai
        self.config, self.store = config, store
        self.client = genai.Client(api_key=config.api_key)
    def _request(self, **kwargs):
        from google.genai.errors import APIError
        try:
            return self.client.models.generate_content(**kwargs)
        except APIError as exc:
            message = api_error(exc, self.config.api_key)
            self.store.event("api_error", model=kwargs.get("model"), message=message)
            raise ProviderError(message) from None

    def structured(self, prompt, schema, tag, kind="text"):
        model = self.config.require(kind)
        request = {"model": model, "prompt": prompt, "schema": schema.model_json_schema()}
        cache_path = f"api/{tag}.cache.json"
        if getattr(self, "reuse_responses", False) and self.store.path(cache_path).exists():
            cached = self.store.read(cache_path)
            if cached["request"] == request:
                return schema.model_validate_json(cached["text"])
        self.store.write(f"api/{tag}.request.json", {"model": model, "prompt": prompt})
        response = self._request(model=model, contents=prompt,
            config={"response_mime_type": "application/json", "response_json_schema": schema.model_json_schema(), "automatic_function_calling": {"disable": True}})
        self.store.write(f"api/{tag}.response.json", response.model_dump(mode="json"))
        result = schema.model_validate_json(response.text)
        self.store.write(cache_path, {"request": request, "text": response.text})
        return result
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
        response = self._request(model=model, contents=contents,
            config={"response_modalities": ["TEXT", "IMAGE"], "automatic_function_calling": {"disable": True}})
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
