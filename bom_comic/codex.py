"""Text reasoning (analyze/validate) through the Codex CLI signed in with a ChatGPT account: no API key or
separate billing; usage counts against the ChatGPT plan's limits. Each call is one non-interactive
`codex exec` run in an empty read-only folder, with the reply held to the step's JSON schema."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from .errors import ProviderError


def strict_schema(schema):
    """OpenAI structured output wants closed objects with every property required; defaults are dropped."""
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    return schema


class Codex:
    def __init__(self, store):
        self.store = store
        # The standalone installer's folder is checked too: a new PATH entry only reaches newly started programs.
        installed = Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        self.command = shutil.which(os.getenv("CODEX_COMMAND", "codex")) or (str(installed) if installed.is_file() else None)
        if not self.command:
            raise ProviderError("Codex CLI not found. Install it, then run `codex login` and sign in with ChatGPT.")
        # Unset uses the Codex default model / reasoning effort for the signed-in plan.
        text_model = os.getenv("CODEX_TEXT_MODEL")
        self.models = {"text": text_model, "validator": os.getenv("CODEX_VALIDATOR_MODEL", text_model)}
        self.effort = {"text": os.getenv("CODEX_TEXT_EFFORT"), "validator": os.getenv("CODEX_VALIDATOR_EFFORT")}
        self.timeout = float(os.getenv("CODEX_TIMEOUT", "900"))

    def args(self, folder, schema_path, output_path, model, effort):
        # --ephemeral: hundreds of scene calls shouldn't each leave a saved Codex session behind.
        args = [self.command, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(folder),
                "--output-schema", str(schema_path), "--output-last-message", str(output_path)]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--config", f"model_reasoning_effort={effort}"]
        return args + ["-"]  # the prompt arrives on stdin; chapters are too long for a Windows command line

    def structured(self, prompt, schema, tag, kind="text"):
        kind = "validator" if kind == "validator" else "text"
        model, effort = self.models[kind], self.effort[kind]
        json_schema = strict_schema(schema.model_json_schema())
        request = {"model": model, "effort": effort, "prompt": prompt, "schema": json_schema}
        cache_path = f"api/{tag}.cache.json"
        if getattr(self, "reuse_responses", False) and self.store.path(cache_path).exists():
            cached = self.store.read(cache_path)
            if cached["request"] == request:
                return schema.model_validate_json(cached["text"])
        self.store.write(f"api/{tag}.request.json", {"model": model, "effort": effort, "prompt": prompt})
        # An empty working folder: Codex sees only the prompt, never the repository.
        with tempfile.TemporaryDirectory(prefix="bom_codex_") as folder:
            folder = Path(folder)
            schema_path, output_path = folder / "schema.json", folder / "reply.json"
            schema_path.write_text(json.dumps(json_schema), encoding="utf-8")
            try:
                done = subprocess.run(self.args(folder, schema_path, output_path, model, effort), input=prompt,
                                      capture_output=True, text=True, encoding="utf-8", timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise ProviderError(f"Codex did not finish {tag} within {self.timeout:.0f}s") from None
            reply = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        self.store.write(f"api/{tag}.response.json", {"returncode": done.returncode, "reply": reply,
                                                       "stderr": done.stderr[-4000:]})
        if done.returncode != 0 or not reply.strip():
            detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
            if "limit" in done.stderr.lower():
                raise ProviderError(f"Codex usage limit reached during {tag}; resume later with --resume. {detail[0]}")
            raise ProviderError(f"Codex failed on {tag} (exit {done.returncode}): {detail[0]}")
        try:
            result = schema.model_validate_json(reply)
        except ValueError as exc:
            raise ProviderError(f"Codex reply for {tag} did not match the schema; inspect the saved response") from exc
        self.store.write(cache_path, {"request": request, "text": result.model_dump_json()})
        return result
