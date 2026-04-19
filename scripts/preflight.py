#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_path(base: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def check_command(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, (result.stdout or result.stderr).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the local DigitalOcean runner setup.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = parser.parse_args()

    load_env_file(ROOT / ".env")

    config_path = resolve_path(Path.cwd(), args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    official_repo = resolve_path(
        ROOT,
        os.environ.get("SWE_BENCH_OFFICIAL_REPO_PATH") or config.get("official_repo_path", "vendor/SWE-bench_Pro-os"),
    )
    checks: list[tuple[str, bool, str]] = []

    ok, detail = check_command(["docker", "version", "--format", "{{.Server.Version}}"])
    checks.append(("docker_server", ok, detail))

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    checks.append(("openrouter_api_key", bool(api_key), "set" if api_key else "missing"))

    telos_mode = str(config.get("telos_mode", "disabled"))
    telos_base_url = os.environ.get("TELOS_BASE_URL") or str(config.get("telos_base_url", ""))
    telos_ok = telos_mode != "live" or bool(telos_base_url)
    checks.append(("telos_base_url", telos_ok, telos_base_url or "not required"))

    required_files = {
        "official_repo": official_repo,
        "dataset_jsonl": resolve_path(official_repo, str(config["dataset_jsonl"])),
        "run_scripts_dir": resolve_path(official_repo, str(config["run_scripts_dir"])),
        "eval_script": resolve_path(official_repo, str(config["eval_script"])),
        "instances_path": resolve_path(ROOT, str(config["instances_path"])),
        "leaderboard_json": resolve_path(ROOT, str(config["leaderboard_json"])),
    }
    for name, path in required_files.items():
        checks.append((name, path.exists(), str(path)))

    failed = False
    for name, ok, detail in checks:
        status = "ok" if ok else "fail"
        print(f"{status:>4}  {name}: {detail}")
        if not ok:
            failed = True

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

