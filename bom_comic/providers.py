"""All SDK-specific operations live here; providers are injectable in tests."""
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Protocol
from PIL import Image
from .errors import ProviderError, api_error

class ImageProvider(Protocol):
    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None): ...

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
    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None):
        import io
        from google.genai import types
        model = self.config.require("image")
        contents = [prompt]
        for path in reference_images or []:
            with Image.open(path) as im:
                buffer = io.BytesIO()
                im.save(buffer, format="PNG")
                contents.append(types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png"))
        config = {"response_modalities": ["TEXT", "IMAGE"], "automatic_function_calling": {"disable": True}}
        # Gemini takes a fixed ratio list; assembly trims the difference from the layout's frame.
        if aspect_ratio:
            config["image_config"] = {"aspect_ratio": nearest_gemini_aspect(aspect_ratio)}
        response = self._request(model=model, contents=contents, config=config)
        self.store.write(f"api/{Path(output_path).stem}.response.json", response.model_dump(mode="json"))
        for part in response.parts or []:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                with Image.open(io.BytesIO(part.inline_data.data)) as im:
                    im.convert("RGB").save(output_path, "PNG")
                return str(output_path)
        raise ValueError("Gemini returned no image; inspect saved API response")

class Ollama:
    """Local text reasoning (analyze/validate) through a running Ollama server's structured output."""

    def __init__(self, store, url=None):
        self.store = store
        self.url = (url or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.text_model = os.getenv("OLLAMA_TEXT_MODEL", "qwen2.5:14b-instruct")
        self.validator_model = os.getenv("OLLAMA_VALIDATOR_MODEL", self.text_model)

    def _call(self, payload, timeout=600):
        import urllib.error
        import urllib.request
        request = urllib.request.Request(self.url + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"Ollama HTTP {exc.code}: {exc.read().decode(errors='replace')[:2000]}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"Ollama unreachable at {self.url} ({exc.reason}); run 'ollama serve' first") from None

    def structured(self, prompt, schema, tag, kind="text"):
        model = self.text_model if kind == "text" else self.validator_model
        request = {"model": model, "prompt": prompt, "schema": schema.model_json_schema()}
        cache_path = f"api/{tag}.cache.json"
        if getattr(self, "reuse_responses", False) and self.store.path(cache_path).exists():
            cached = self.store.read(cache_path)
            if cached["request"] == request:
                return schema.model_validate_json(cached["text"])
        self.store.write(f"api/{tag}.request.json", {"model": model, "prompt": prompt})
        response = self._call({"model": model, "messages": [{"role": "user", "content": prompt}],
            "format": schema.model_json_schema(), "stream": False, "options": {"temperature": 0.1, "num_ctx": 16384}})
        self.store.write(f"api/{tag}.response.json", response)
        text = response.get("message", {}).get("content", "")
        try:
            result = schema.model_validate_json(text)
        except Exception as exc:
            raise ProviderError(f"Ollama returned invalid structured output for {tag}: {exc}") from None
        self.store.write(cache_path, {"request": request, "text": text})
        return result


GEMINI_ASPECTS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]


def _ratio(aspect):
    w, h = (int(n) for n in aspect.split(":"))
    return w / h


def nearest_gemini_aspect(aspect):
    return min(GEMINI_ASPECTS, key=lambda a: abs(math.log(_ratio(a) / _ratio(aspect))))


def render_size(aspect, pixels=1024 * 1024):
    """About one megapixel (Flux's native scale) in multiples of 16."""
    ratio = _ratio(aspect)
    width = max(16, round(math.sqrt(pixels * ratio) / 16) * 16)
    return width, max(16, round(width / ratio / 16) * 16)


class ComfyUI:
    """Local Flux.1-dev (GGUF) rendering through a running ComfyUI server with ComfyUI-GGUF nodes."""

    def __init__(self, store, url=None):
        self.store = store
        self.url = (url or os.getenv("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")
        self.unet = os.getenv("COMFYUI_UNET", "flux1-dev-Q8_0.gguf")
        self.t5 = os.getenv("COMFYUI_T5", "t5-v1_1-xxl-encoder-Q8_0.gguf")
        self.steps = int(os.getenv("COMFYUI_STEPS", "24"))
        # cfg > 1 is what makes Flux honor the negative prompt (at ~2x render time).
        self.cfg = float(os.getenv("COMFYUI_CFG", "2.0"))
        self.guidance = float(os.getenv("COMFYUI_GUIDANCE", "2.5"))

    def _call(self, path, payload=None, timeout=60):
        import urllib.error
        import urllib.request
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"ComfyUI HTTP {exc.code}: {exc.read().decode(errors='replace')[:2000]}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"ComfyUI unreachable at {self.url} ({exc.reason}); start the server first") from None

    def workflow(self, positive, negative, width, height, seed):
        return {
            "unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.unet}},
            "clip": {"class_type": "DualCLIPLoaderGGUF", "inputs": {"clip_name1": "clip_l.safetensors", "clip_name2": self.t5, "type": "flux"}},
            "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
            "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["clip", 0]}},
            "guided": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["pos", 0], "guidance": self.guidance}},
            "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["clip", 0]}},
            "latent": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "sample": {"class_type": "KSampler", "inputs": {"model": ["unet", 0], "seed": seed, "steps": self.steps, "cfg": self.cfg,
                "sampler_name": "euler", "scheduler": "simple", "positive": ["guided", 0], "negative": ["neg", 0],
                "latent_image": ["latent", 0], "denoise": 1.0}},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
            "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "bom_comic/panel"}},
        }

    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None):
        import io
        import time
        from urllib.parse import urlencode
        from .comic import diffusion_prompts
        stem = Path(output_path).stem
        positive, negative = diffusion_prompts(prompt)
        width, height = render_size(aspect_ratio or "1:1")
        # Seeded from the revision name: reproducible per revision, different on regeneration.
        seed = int(hashlib.sha256(stem.encode()).hexdigest()[:12], 16)
        workflow = self.workflow(positive, negative, width, height, seed)
        self.store.write(f"api/{stem}.request.json", {"positive": positive, "negative": negative, "workflow": workflow})
        prompt_id = json.loads(self._call("/prompt", {"prompt": workflow}))["prompt_id"]
        deadline = time.monotonic() + float(os.getenv("COMFYUI_TIMEOUT", "1800"))
        while not (entry := json.loads(self._call(f"/history/{prompt_id}")).get(prompt_id)):
            if time.monotonic() > deadline:
                raise ProviderError(f"ComfyUI did not finish {stem} in time")
            time.sleep(2)
        self.store.write(f"api/{stem}.response.json", entry)
        images = entry.get("outputs", {}).get("save", {}).get("images")
        if entry.get("status", {}).get("status_str") == "error" or not images:
            raise ProviderError("ComfyUI returned no image; inspect saved API response")
        data = self._call("/view?" + urlencode({k: images[0][k] for k in ("filename", "subfolder", "type")}))
        with Image.open(io.BytesIO(data)) as im:
            im.convert("RGB").save(output_path, "PNG")
        return str(output_path)

class PlaceholderImages:
    """Offline plumbing test, never evidence of visual/scriptural accuracy."""
    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None):
        from PIL import ImageDraw
        w, h = 1000, 550
        if aspect_ratio:
            rw, rh = (int(n) for n in aspect_ratio.split(":"))
            w, h = 1000, round(1000 * rh / rw)
        im = Image.new("RGB", (w, h), "#273340")
        ImageDraw.Draw(im).text((30, 30), "OFFLINE TEST PLACEHOLDER - NOT COMIC ART", fill="white")
        im.save(output_path)
        return str(output_path)
