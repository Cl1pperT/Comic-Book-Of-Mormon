"""Compare cheap text-model setups on 1 Nephi 1-4 against the Gemini Pro baseline.

Run from the repo root:  python experiments/text_models.py [setup ...]
Each setup gets its own run in runs/test-<setup>; results go to runs/test-<setup>/timing.json.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
START, END = "1 Nephi 1:1", "1 Nephi 4:38"
BASELINE = ROOT / "runs" / "1-nephi"
CHUNKS = [f"analyze_{c}_{o}" for c, n in ((1, 20), (2, 24), (3, 31), (4, 38)) for o in range(0, n, 6)]

SETUPS = {
    "A-pro": {"analyze": "baseline", "validate": {"GEMINI_VALIDATOR_MODEL": "gemini-3.1-pro-preview"}},
    "B-pro-low": {"analyze": {"GEMINI_TEXT_MODEL": "gemini-3.1-pro-preview", "GEMINI_TEXT_THINKING": "low"},
                  "validate": {"GEMINI_VALIDATOR_MODEL": "gemini-3.8-flash"}},
    "C-flash": {"analyze": {"GEMINI_TEXT_MODEL": "gemini-3.8-flash"},
                "validate": {"GEMINI_VALIDATOR_MODEL": "gemini-3.8-flash"}},
    "E-flash-lite": {"analyze": {"GEMINI_TEXT_MODEL": "gemini-3.5-flash-lite"},
                     "validate": {"GEMINI_VALIDATOR_MODEL": "gemini-3.5-flash-lite"}},
    "D-local": {"analyze": "local", "validate": {"GEMINI_VALIDATOR_MODEL": "gemini-3.8-flash"}},
}


def cli(run, *args, env=None):
    started = time.monotonic()
    result = subprocess.run([PYTHON, "main.py", "--run", str(run), *args], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, **(env or {})})
    return {"seconds": round(time.monotonic() - started, 1), "exit": result.returncode,
            "error": result.stderr.strip()[-2000:] if result.returncode else ""}


def baseline_scenes(run):
    """Setup A reuses the Pro analysis already running for all of 1 Nephi: same prompt, chapters 1-4 only."""
    while not all((BASELINE / "api" / f"{c}.response.json").exists() for c in CHUNKS):
        time.sleep(30)
    scenes = []
    (run / "api").mkdir(exist_ok=True)
    for chunk in CHUNKS:
        response = json.loads((BASELINE / "api" / f"{chunk}.response.json").read_text(encoding="utf-8"))
        shutil.copy2(BASELINE / "api" / f"{chunk}.response.json", run / "api" / f"{chunk}.response.json")
        text = "".join(p["text"] for p in response["candidates"][0]["content"]["parts"] if p.get("text") and not p.get("thought"))
        scenes += json.loads(text)["scenes"]
    for i, scene in enumerate(scenes, 1):
        scene["scene_id"] = f"scene_{i:03d}"
    (run / "scenes.json").write_text(json.dumps(scenes, indent=2, ensure_ascii=False), encoding="utf-8")


def run_setup(name):
    setup, run = SETUPS[name], ROOT / "runs" / f"test-{name}"
    if run.exists():
        shutil.rmtree(run)
    timing = {"init": cli(run, "init", "data/full-scripture.txt", "--start", START, "--end", END)}
    for part in ("characters.json", "visual_style.json"):
        shutil.copy2(BASELINE / "continuity" / part, run / "continuity" / part)
    if setup["analyze"] == "baseline":
        baseline_scenes(run)
        timing["analyze"] = {"seconds": None, "exit": 0, "error": "", "note": "reused from runs/1-nephi"}
    elif setup["analyze"] == "local":
        timing["analyze"] = cli(run, "analyze", "--local", env={"OLLAMA_TEXT_MODEL": "qwen3:14b"})
    else:
        timing["analyze"] = cli(run, "analyze", env=setup["analyze"])
    if timing["analyze"]["exit"] == 0:
        # Pro allows ~25 requests/minute per model; parallel Pro validation trips it.
        workers = "1" if "pro" in setup["validate"]["GEMINI_VALIDATOR_MODEL"] else "4"
        timing["validate"] = cli(run, "validate", "--workers", workers, env=setup["validate"])
    (run / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    print(name, json.dumps(timing))


if __name__ == "__main__":
    for name in sys.argv[1:] or SETUPS:
        run_setup(name)
