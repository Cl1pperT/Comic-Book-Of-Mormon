"""Claude text reasoning (analyze/validate) through the Anthropic API, with schema-guaranteed JSON output."""
import os
from .errors import ProviderError

# Models that support server-side refusal fallbacks (beta); others make a plain request.
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}


class Claude:
    def __init__(self, store):
        import anthropic
        self.store = store
        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY, or an `ant auth login` profile
        self.models = {"text": os.getenv("CLAUDE_TEXT_MODEL", "claude-opus-5")}
        self.models["validator"] = os.getenv("CLAUDE_VALIDATOR_MODEL", self.models["text"])
        # low | medium | high | xhigh | max; unset uses the model's default. Not supported on Haiku 4.5.
        self.effort = {"text": os.getenv("CLAUDE_TEXT_EFFORT"), "validator": os.getenv("CLAUDE_VALIDATOR_EFFORT")}

    def _parse(self, model, prompt, schema, effort):
        request = {"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}],
                   "output_format": schema}
        if effort:
            request["output_config"] = {"effort": effort}
        if model in FALLBACK_MODELS:
            # On a policy decline the API reruns the request on a fallback model within the same call.
            return self.client.beta.messages.parse(**request, betas=["server-side-fallback-2026-07-01"],
                                                   fallbacks="default")
        return self.client.messages.parse(**request)

    def structured(self, prompt, schema, tag, kind="text"):
        import anthropic
        kind = "validator" if kind == "validator" else "text"
        model = self.models[kind]
        request = {"model": model, "prompt": prompt, "schema": schema.model_json_schema()}
        cache_path = f"api/{tag}.cache.json"
        if getattr(self, "reuse_responses", False) and self.store.path(cache_path).exists():
            cached = self.store.read(cache_path)
            if cached["request"] == request:
                return schema.model_validate_json(cached["text"])
        self.store.write(f"api/{tag}.request.json", {"model": model, "prompt": prompt})
        try:
            response = self._parse(model, prompt, schema, self.effort[kind])
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"Claude rate limit ({model}): {exc.message}") from None
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Claude API {exc.status_code} ({model}): {exc.message}") from None
        except anthropic.APIConnectionError:
            raise ProviderError("Could not reach the Claude API; check the network connection") from None
        self.store.write(f"api/{tag}.response.json", response.to_dict())
        if response.stop_reason == "refusal":
            raise ProviderError(f"Claude declined {tag}; inspect the saved response")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise ProviderError(f"Claude returned no complete structured output for {tag}; inspect the saved response")
        result = response.parsed_output
        self.store.write(cache_path, {"request": request, "text": result.model_dump_json()})
        return result
