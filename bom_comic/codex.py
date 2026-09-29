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


# Codex is a coding agent: by default every call carries ~14k tokens of agent instructions, skills, and tool
# definitions before our prompt. None of it helps with scripture analysis, and on a ChatGPT plan it's the bulk of
# the usage, so each call switches it off (measured: 13.9k -> 4.9k input tokens for a one-line prompt).
LEAN_FEATURES = ["apps", "browser_use", "browser_use_external", "computer_use", "image_generation", "multi_agent",
                 "plugins", "remote_plugin", "skill_search", "skill_mcp_dependency_install", "tool_suggest", "goals",
                 "shell_tool", "unified_exec", "sleep_tool", "view_image", "workspace_dependencies", "worktrees",
                 "mentions_v2", "hooks", "plugin_sharing"]
INSTRUCTIONS = ("You are a careful text-analysis assistant. Follow the user's request exactly and answer with only "
                "the requested JSON. Do not run commands or edit files.")


def usage(stdout):
    """Token usage from `codex exec --json` events (summed over turns)."""
    total = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            for key, value in (event.get("usage") or {}).items():
                total[key] = total.get(key, 0) + value
    return total


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
        # "repair" is the rewrite of a scene the audit rejected: rare enough to afford a stronger model.
        self.models = {"text": text_model, "validator": os.getenv("CODEX_VALIDATOR_MODEL", text_model),
                       "repair": os.getenv("CODEX_REPAIR_MODEL", text_model)}
        self.effort = {"text": os.getenv("CODEX_TEXT_EFFORT"), "validator": os.getenv("CODEX_VALIDATOR_EFFORT"),
                       "repair": os.getenv("CODEX_REPAIR_EFFORT", os.getenv("CODEX_TEXT_EFFORT"))}
        self.timeout = float(os.getenv("CODEX_TIMEOUT", "900"))

    def args(self, folder, schema_path, output_path, model, effort):
        # --ephemeral: hundreds of scene calls shouldn't each leave a saved Codex session behind.
        # --json: events on stdout, for the token usage of each call.
        instructions = (Path(folder) / "instructions.md").as_posix()
        args = [self.command, "exec", "--ephemeral", "--json", "--skip-git-repo-check", "--sandbox", "read-only",
                "--cd", str(folder), "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                "--config", f"model_instructions_file='{instructions}'", "--config", "agents.enabled=false",
                "--config", "skills.max_context_tokens=1"]
        for feature in LEAN_FEATURES:
            args += ["--disable", feature]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--config", f"model_reasoning_effort={effort}"]
        return args + ["-"]  # the prompt arrives on stdin; chapters are too long for a Windows command line

    def structured(self, prompt, schema, tag, kind="text"):
        kind = kind if kind in ("validator", "repair") else "text"
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
            (folder / "instructions.md").write_text(INSTRUCTIONS, encoding="utf-8")
            try:
                done = subprocess.run(self.args(folder, schema_path, output_path, model, effort), input=prompt,
                                      capture_output=True, text=True, encoding="utf-8", timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise ProviderError(f"Codex did not finish {tag} within {self.timeout:.0f}s") from None
            reply = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        self.store.write(f"api/{tag}.response.json", {"returncode": done.returncode, "reply": reply,
                                                       "usage": usage(done.stdout), "stderr": done.stderr[-4000:]})
        if done.returncode != 0 or not reply.strip():
            detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
            if "usage limit" in (done.stderr + done.stdout).lower():
                raise ProviderError(f"Codex usage limit reached during {tag}; resume later with --resume. {detail[0]}")
            raise ProviderError(f"Codex failed on {tag} (exit {done.returncode}): {detail[0]}")
        try:
            result = schema.model_validate_json(reply)
        except ValueError as exc:
            raise ProviderError(f"Codex reply for {tag} did not match the schema; inspect the saved response") from exc
        self.store.write(cache_path, {"request": request, "text": result.model_dump_json()})
        return result
