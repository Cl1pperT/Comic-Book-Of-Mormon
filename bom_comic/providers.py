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
    RATE_LIMIT_RETRIES = 6

    def _request(self, **kwargs):
        import re
        import time
        from google.genai.errors import APIError
        for attempt in range(self.RATE_LIMIT_RETRIES + 1):
            try:
                return self.client.models.generate_content(**kwargs)
            except APIError as exc:
                message = api_error(exc, self.config.api_key)
                self.store.event("api_error", model=kwargs.get("model"), message=message)
                # A 429 is rejected, not billed, so waiting out the per-minute limit costs nothing.
                daily = "per_day" in message
                if getattr(exc, "code", None) != 429 or daily or attempt == self.RATE_LIMIT_RETRIES:
                    raise ProviderError(message) from None
                wait = re.search(r"retry in ([\d.]+)s", str(getattr(exc, "message", "")) + str(exc))
                time.sleep(min(90.0, float(wait.group(1)) + 2) if wait else 30.0 * (attempt + 1))

    def structured(self, prompt, schema, tag, kind="text"):
        model = self.config.require("validator" if kind == "validator" else "text")  # rewrites use the text model
        request = {"model": model, "prompt": prompt, "schema": schema.model_json_schema()}
        cache_path = f"api/{tag}.cache.json"
        if getattr(self, "reuse_responses", False) and self.store.path(cache_path).exists():
            cached = self.store.read(cache_path)
            if cached["request"] == request:
                return schema.model_validate_json(cached["text"])
        self.store.write(f"api/{tag}.request.json", {"model": model, "prompt": prompt})
        config = {"response_mime_type": "application/json", "response_json_schema": schema.model_json_schema(),
                  "automatic_function_calling": {"disable": True}}
        # Thinking tokens bill as output; "low" trades some reasoning depth for a large cost cut.
        level = os.getenv(f"GEMINI_{'VALIDATOR' if kind == 'validator' else 'TEXT'}_THINKING")
        if level:
            config["thinking_config"] = {"thinking_level": level}
        response = self._request(model=model, contents=prompt, config=config)
        self.store.write(f"api/{tag}.response.json", response.model_dump(mode="json"))
        result = schema.model_validate_json(response.text)
        self.store.write(cache_path, {"request": request, "text": response.text})
        return result
    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None):
        import io
        from google.genai import types
        model = self.config.require("image")
        contents = [prompt]
        for reference in reference_images or []:
            # A (label, path) pair names the image right before it, e.g. which character a portrait shows.
            label, path = reference if isinstance(reference, tuple) else (None, reference)
            if label:
                contents.append(f"Reference portrait: {label}")
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
        except TimeoutError:
            raise ProviderError(f"Ollama did not answer within {timeout}s") from None

    def structured(self, prompt, schema, tag, kind="text"):
        model = self.validator_model if kind == "validator" else self.text_model
        request = {"model": model, "prompt": prompt, "schema": schema.model_json_schema()}
        cache_path = f"api/{tag}.cache.json"
        if getattr(self, "reuse_responses", False) and self.store.path(cache_path).exists():
            cached = self.store.read(cache_path)
            if cached["request"] == request:
                return schema.model_validate_json(cached["text"])
        self.store.write(f"api/{tag}.request.json", {"model": model, "prompt": prompt})
        # num_predict caps runaway generation (a known failure under JSON-constrained decoding);
        # a smaller num_ctx keeps a 14B model entirely on a 12GB GPU.
        options = {"temperature": 0.1, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
                   "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "4096"))}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "format": schema.model_json_schema(), "stream": False, "options": options}
        # Reasoning models (e.g. Qwen3) think before answering; OLLAMA_THINK=false skips it for speed.
        if os.getenv("OLLAMA_THINK", "").lower() in ("false", "0", "no"):
            payload["think"] = False
        response = self._call(payload)
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
        # Panels with reference portraits render through FLUX.1 Kontext, which keeps those faces.
        self.kontext_unet = os.getenv("COMFYUI_KONTEXT_UNET", "flux1-kontext-dev-Q8_0.gguf")
        self.kontext_cfg = float(os.getenv("COMFYUI_KONTEXT_CFG", "1.0"))
        self.kontext_guidance = float(os.getenv("COMFYUI_KONTEXT_GUIDANCE", "2.5"))

    def kontext_text(self, prompt, names):
        """The edit Kontext applies to the Flux draft: only the referenced people change."""
        from .comic import visible_people
        others = [p for p in visible_people(prompt) if p not in names]
        text = ("Edit this comic panel so that " + ", ".join(names) + " match the reference images: give each the "
                "face, hair, build, and clothing of their reference. Keep everything else the same: composition, poses, "
                "framing, background, lighting, colors, and the hand-inked comic illustration style. ")
        # Kontext otherwise gives the reference face and clothes to everyone in the frame.
        if others:
            text += ("Leave everyone else (" + ", ".join(others) + ") as they are; never give them a reference face "
                     "or clothing. ")
        return text

    def upload(self, path):
        """Put a reference image in ComfyUI's input folder; returns the name LoadImage uses."""
        import urllib.error
        import urllib.request
        from uuid import uuid4
        path = Path(path)
        boundary = uuid4().hex
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
            "Content-Type: image/png\r\n\r\n".encode(), path.read_bytes(),
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()])
        request = urllib.request.Request(self.url + "/upload/image", data=body,
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                uploaded = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(f"Could not upload {path.name} to ComfyUI: {exc}") from None
        return f"{uploaded['subfolder']}/{uploaded['name']}" if uploaded.get("subfolder") else uploaded["name"]

    def kontext_workflow(self, positive, negative, edit, width, height, seed, images):
        """Two steps in one ComfyUI job: plain Flux draws the panel (style and negative prompt intact), then
        Kontext edits that draft so the referenced people match their portraits."""
        workflow = self.workflow(positive, negative, width, height, seed)
        workflow["save_draft"] = dict(workflow.pop("save"), inputs={"images": ["decode", 0], "filename_prefix": "bom_comic/draft"})
        workflow.update({
            "k_unet": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": self.kontext_unet}},
            "k_pos": {"class_type": "CLIPTextEncode", "inputs": {"text": edit, "clip": ["clip", 0]}},
            # The draft's latent is the image being edited (index 0); portraits follow it.
            "k_draft": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["k_pos", 0], "latent": ["sample", 0]}},
        })
        conditioning = "k_draft"
        for i, name in enumerate(images):
            workflow[f"k_load{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            workflow[f"k_scale{i}"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": [f"k_load{i}", 0]}}
            workflow[f"k_encode{i}"] = {"class_type": "VAEEncode", "inputs": {"pixels": [f"k_scale{i}", 0], "vae": ["vae", 0]}}
            workflow[f"k_ref{i}"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": [conditioning, 0], "latent": [f"k_encode{i}", 0]}}
            conditioning = f"k_ref{i}"
        workflow.update({
            # Keeps the draft and each portrait distinct instead of blending them.
            "k_multi": {"class_type": "FluxKontextMultiReferenceLatentMethod",
                        "inputs": {"conditioning": [conditioning, 0], "reference_latents_method": "index"}},
            "k_guided": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["k_multi", 0], "guidance": self.kontext_guidance}},
            "k_neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["k_pos", 0]}},
            "k_sample": {"class_type": "KSampler", "inputs": {"model": ["k_unet", 0], "seed": seed, "steps": self.steps,
                "cfg": self.kontext_cfg, "sampler_name": "euler", "scheduler": "simple", "positive": ["k_guided", 0],
                "negative": ["k_neg", 0], "latent_image": ["sample", 0], "denoise": 1.0}},
            "k_decode": {"class_type": "VAEDecode", "inputs": {"samples": ["k_sample", 0], "vae": ["vae", 0]}},
            "save": {"class_type": "SaveImage", "inputs": {"images": ["k_decode", 0], "filename_prefix": "bom_comic/panel"}},
        })
        return workflow

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

    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None, seed=None):
        """seed: fixed by the caller to compare settings on the same drawing (see compare.py); otherwise derived
        from the revision name, so each revision is reproducible and a regeneration differs."""
        import io
        import time
        from urllib.parse import urlencode
        from .comic import diffusion_prompts
        stem = Path(output_path).stem
        positive, negative = diffusion_prompts(prompt)
        width, height = render_size(aspect_ratio or "1:1")
        if seed is None:
            seed = int(hashlib.sha256(stem.encode()).hexdigest()[:12], 16)
        references = [r if isinstance(r, tuple) else (Path(r).stem, r) for r in reference_images or []]
        edit = None
        if references:
            edit = self.kontext_text(prompt, [label for label, _ in references])
            workflow = self.kontext_workflow(positive, negative, edit, width, height, seed,
                                             [self.upload(path) for _, path in references])
        else:
            workflow = self.workflow(positive, negative, width, height, seed)
        self.store.write(f"api/{stem}.request.json", {"positive": positive, "negative": negative, "edit": edit,
                                                      "workflow": workflow,
                                                      "references": [str(path) for _, path in references]})
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
        fetch = lambda image: self._call("/view?" + urlencode({k: image[k] for k in ("filename", "subfolder", "type")}))
        with Image.open(io.BytesIO(fetch(images[0]))) as im:
            im.convert("RGB").save(output_path, "PNG")
        # Two-step renders keep the Flux draft beside the final panel, to see what the Kontext edit changed.
        draft = entry.get("outputs", {}).get("save_draft", {}).get("images")
        if draft:
            with Image.open(io.BytesIO(fetch(draft[0]))) as im:
                im.convert("RGB").save(Path(output_path).with_name(stem + "_draft.png"), "PNG")
        return str(output_path)

class CachedChecks:
    """Offline validation: reuse a saved verdict when the check would be byte-identical; otherwise, rather than
    calling a model, flag the scene so the human scene review has to cover it (approval then needs a note)."""

    def __init__(self, store, model):
        self.store, self.model = store, model

    def structured(self, prompt, schema, tag, kind="validator"):
        request = {"model": self.model, "prompt": prompt, "schema": schema.model_json_schema()}
        path = f"api/{tag}.cache.json"
        if self.store.path(path).exists() and self.store.read(path)["request"] == request:
            return schema.model_validate_json(self.store.read(path)["text"])
        return schema(status="PASS WITH WARNINGS", issues=[
            "Changed since the last automated check and not re-checked by a model (offline); "
            "verify against the source during human review."])


class PlaceholderImages:
    """Offline plumbing test, never evidence of visual/scriptural accuracy."""
    def generate_image(self, prompt, reference_images=None, output_path=None, aspect_ratio=None):
        from PIL import ImageDraw
        w, h = 1000, 550
        if aspect_ratio:
            rw, rh = (int(n) for n in aspect_ratio.split(":"))
            w, h = 1000, round(1000 * rh / rw)
        im = Image.new("RGB", (w, h), "#273340")
        draw = ImageDraw.Draw(im)
        draw.text((30, 30), "OFFLINE TEST PLACEHOLDER - NOT COMIC ART", fill="white")
        for i, reference in enumerate(reference_images or []):
            label = reference[0] if isinstance(reference, tuple) else Path(reference).name
            draw.text((30, 60 + 20 * i), f"Reference portrait attached: {label}", fill="white")
        im.save(output_path)
        return str(output_path)
