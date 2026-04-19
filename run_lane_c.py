#!/usr/bin/env python3
"""SWE-bench Pro simple runner — one script, no mini-swe-agent dependency.

Usage:
    python run_lane_c.py                          # full pipeline
    python run_lane_c.py --phase execute          # agent execution only
    python run_lane_c.py --phase select           # patch selection only
    python run_lane_c.py --phase export           # export predictions.json
    python run_lane_c.py --phase evaluate         # run official evaluator
    python run_lane_c.py --instance-regex "NodeBB"  # filter instances
    python run_lane_c.py --max-instances 2        # limit for testing
    python run_lane_c.py --telos-mode disabled    # Lane B (no Telos)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telos import TelosBridge

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


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


def build_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(config)
    official_repo_path = resolve_path(
        ROOT,
        os.environ.get("SWE_BENCH_OFFICIAL_REPO_PATH")
        or str(config.get("official_repo_path", "vendor/SWE-bench_Pro-os")),
    )
    runtime["telos_base_url"] = os.environ.get("TELOS_BASE_URL") or str(config.get("telos_base_url", ""))
    runtime["_official_repo_path"] = str(official_repo_path)
    runtime["_dataset_jsonl_path"] = str(resolve_path(official_repo_path, str(config["dataset_jsonl"])))
    runtime["_run_scripts_dir_path"] = str(resolve_path(official_repo_path, str(config["run_scripts_dir"])))
    runtime["_eval_script_path"] = str(resolve_path(official_repo_path, str(config["eval_script"])))
    runtime["_instances_path"] = str(resolve_path(ROOT, str(config["instances_path"])))
    runtime["_leaderboard_json_path"] = str(resolve_path(ROOT, str(config["leaderboard_json"])))
    runtime["_output_root_path"] = str(resolve_path(ROOT, str(config["output_root"])))
    return runtime


def ensure_required_paths(config: dict[str, Any]) -> None:
    required = {
        "official_repo_path": Path(str(config["_official_repo_path"])),
        "dataset_jsonl": Path(str(config["_dataset_jsonl_path"])),
        "run_scripts_dir": Path(str(config["_run_scripts_dir_path"])),
        "eval_script": Path(str(config["_eval_script_path"])),
        "instances_path": Path(str(config["_instances_path"])),
        "leaderboard_json": Path(str(config["_leaderboard_json_path"])),
    }
    missing = [f"{name}={path}" for name, path in required.items() if not path.exists()]
    if missing:
        raise SystemExit(
            "Missing required files/directories:\n- " + "\n- ".join(missing) + "\n"
            "Run scripts/bootstrap_official_repo.sh first, then retry."
        )


def load_instance_ids(path: Path) -> list[str]:
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.append(line)
    return ids


def load_dataset_index(jsonl_path: Path) -> dict[str, dict[str, Any]]:
    """Load the official JSONL into a dict keyed by instance_id."""
    index: dict[str, dict[str, Any]] = {}
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        iid = rec["instance_id"]
        index[iid] = rec
    return index


def parse_instance_info(info_path: Path) -> dict[str, Any]:
    """Parse run_scripts/<instance_id>/instance_info.txt."""
    text = info_path.read_text(encoding="utf-8")
    info: dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        try:
            info[key] = json.loads(val)
        except (json.JSONDecodeError, ValueError):
            info[key] = val
    return info


def get_dockerhub_image(instance_id: str, repo: str) -> str:
    """Build Docker Hub image URI from instance_id and repo.

    Mirrors the logic in helper_code/image_uri.py so we can pull
    public images from docker.io/jefzda/sweap-images without ECR auth.
    """
    repo_base, repo_name_only = repo.lower().split("/")
    hsh = instance_id.replace("instance_", "")

    # element-hq special cases
    if instance_id == "instance_element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan":
        repo_name_only = "element-web"
    elif "element-hq" in repo.lower() and "element-web" in repo.lower():
        repo_name_only = "element"
        if hsh.endswith("-vnan"):
            hsh = hsh[:-5]
    elif hsh.endswith("-vnan"):
        hsh = hsh[:-5]

    tag = f"{repo_base}.{repo_name_only}-{hsh}"
    if len(tag) > 128:
        tag = tag[:128]

    return f"jefzda/sweap-images:{tag}"


# ---------------------------------------------------------------------------
# Docker utilities
# ---------------------------------------------------------------------------

def docker_pull(image: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "pull", image],
        capture_output=True, text=True, timeout=timeout,
    )


def docker_run(image: str, container_name: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "-d", "--name", container_name, image, "sleep", "7200"],
        capture_output=True, text=True, timeout=timeout,
    )


def docker_exec(container: str, command: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, "bash", "-c", command],
        capture_output=True, text=True, timeout=timeout,
    )


def docker_rm(container: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "rm", "-f", container],
        capture_output=True, text=True, timeout=30,
    )


def docker_rmi(image: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "image", "rm", "-f", image],
        capture_output=True, text=True, timeout=120,
    )


def docker_image_prune() -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "image", "prune", "-f"],
        capture_output=True, text=True, timeout=120,
    )


def safe_container_name(run_id: str, instance_id: str, agent_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "_", f"{run_id}_{instance_id}_{agent_id}")
    return slug[:128]


# ---------------------------------------------------------------------------
# Telos scope helpers
# ---------------------------------------------------------------------------

def telos_instance_scope_id(run_id: str, instance_id: str, max_len: int = 128) -> str:
    parts = instance_id.split("__")
    repo = parts[1].split("-")[0] if len(parts) > 1 else "unknown"
    repo_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.replace("/", "__")).strip("-._")[:48]
    digest = hashlib.sha256(f"{run_id}\n{instance_id}".encode()).hexdigest()[:16]
    return f"swebench-instance/{repo_slug}/{digest}"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert software engineer fixing a bug in a GitHub repository.
You are inside a Docker container with the repository checked out at /app.

Your task:
1. Understand the problem from the problem statement and hints below.
2. Explore the codebase to find the root cause.
3. Implement the fix.
4. Run the relevant tests to verify your fix.

Rules:
- Return exactly ONE bash code block per turn. No other text outside the code block.
- Do NOT include THOUGHT, reasoning, or explanation outside the code block.
- Do NOT include command output.
- Do NOT include multiple code blocks.
- When you believe you have fixed the bug, run this exact command to submit:
  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && git diff --no-ext-diff --minimal
"""

import re as _re

_CODE_BLOCK_RE = _re.compile(r"```(?:bash|sh|shell)?\s*\n(.*?)\n```", _re.DOTALL)


def extract_command(llm_output: str) -> str | None:
    """Extract the first bash code block from LLM output."""
    matches = _CODE_BLOCK_RE.findall(llm_output)
    if not matches:
        stripped = llm_output.strip()
        if stripped:
            return stripped.split("\n")[0]
        return None
    # Pick the longest block that looks like a real command
    best = max(matches, key=len)
    if not best.strip():
        return None
    return best.strip()


def _initial_user_message(
    problem_statement: str,
    hints_text: str,
    fail_to_pass: list[str],
    test_files: list[str],
    max_turns: int,
    telos_context: str | None,
) -> str:
    """First user turn: full task context (repeated in thread via message history, not rebuilt)."""
    context = f"<problem_statement>\n{problem_statement}\n</problem_statement>\n"
    if hints_text:
        context += f"<hints>\n{hints_text}\n</hints>\n"
    context += "<failing_tests>\n" + "\n".join(f"- {t}" for t in fail_to_pass) + "\n</failing_tests>\n"
    context += "<test_files>\n" + "\n".join(f"- {t}" for t in test_files) + "\n</test_files>\n"
    if telos_context:
        context += f"<telos_memory>\n{telos_context}\n</telos_memory>\n"
    context += f"\nTurn 1/{max_turns}. Begin by exploring the repository."
    return context


def run_agent(
    container: str,
    model: str,
    temperature: float,
    problem_statement: str,
    hints_text: str,
    fail_to_pass: list[str],
    test_files: list[str],
    max_turns: int,
    max_wall_clock_sec: int,
    docker_timeout: int,
    telos_bridge: TelosBridge | None,
    telos_scope_instance: str,
    log_path: Path,
) -> dict[str, Any]:
    """Run the agent loop. Returns result dict with patch, status, etc."""
    import litellm

    # OpenAI-style message list: first user message keeps full task; later turns append
    # assistant (command) + user (shell output, optionally with Telos hits).
    llm_messages: list[dict[str, str]] = []
    patch: str | None = None
    exit_status = "LimitsExceeded"
    total_cost = 0.0
    format_error_count = 0
    start_time = time.time()

    def elapsed() -> float:
        return time.time() - start_time

    def log(msg: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        line = f"[{ts}] {msg}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)

    log(f"START model={model} temperature={temperature} max_turns={max_turns}")

    last_turn = 0
    for turn in range(max_turns):
        last_turn = turn + 1
        if elapsed() > max_wall_clock_sec:
            log("TIMEOUT: wall clock exceeded")
            exit_status = "ExecutionTimeoutError"
            break

        # Telos search at the start of each turn
        telos_context: str | None = None
        if telos_bridge and telos_bridge.mode == "live":
            try:
                result = telos_bridge.search(
                    query="errors encountered and fixes attempted for this bug",
                    limit=5,
                    scope_kind="swe_bench_instance",
                    scope_id=telos_scope_instance,
                )
                hits = result.get("data", {}).get("results", [])
                if hits:
                    telos_context = "\n".join(
                        f"[score={h.get('score', 0):.3f}] {h.get('content', '')[:500]}"
                        for h in hits[:3]
                    )
            except Exception as e:
                log(f"telos-search error: {e}")

        if not llm_messages:
            llm_messages.append(
                {
                    "role": "user",
                    "content": _initial_user_message(
                        problem_statement,
                        hints_text,
                        fail_to_pass,
                        test_files,
                        max_turns,
                        telos_context,
                    ),
                }
            )

        try:
            response = litellm.completion(
                model=model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + llm_messages,
                temperature=temperature,
                max_tokens=4096,
                num_retries=3,
            )
            content = response.choices[0].message.content or ""
            try:
                total_cost += float(litellm.completion_cost(completion_response=response) or 0.0)
            except Exception:
                pass
        except Exception as e:
            log(f"LLM error: {e}")
            llm_messages.append({"role": "assistant", "content": f"echo LLM_ERROR: {e!s}"})
            llm_messages.append({"role": "user", "content": f"<command_output>\n{e!s}\n</command_output>"})
            continue

        command = extract_command(content)
        if not command:
            log(f"TURN {turn}: no command extracted from LLM output")
            format_error_count += 1
            llm_messages.append({"role": "assistant", "content": content})
            llm_messages.append(
                {
                    "role": "user",
                    "content": "No valid bash code block was found. Return exactly one ```bash``` code block.",
                }
            )
            continue

        log(f"TURN {turn}: {command[:200]}")

        llm_messages.append({"role": "assistant", "content": command})

        # Check for submission marker
        if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command:
            # Extract the diff
            diff_result = docker_exec(container, "git diff --no-ext-diff --minimal", timeout=docker_timeout)
            patch = diff_result.stdout
            if patch.strip():
                exit_status = "Submitted"
                log("SUBMITTED patch extracted")
            else:
                log("SUBMITTED but empty patch")
            break

        # Execute command in container
        try:
            exec_result = docker_exec(container, command, timeout=docker_timeout)
            output = exec_result.stdout + exec_result.stderr
            out_block = f"<command_output>\n{output[:12000]}\n</command_output>"
            if telos_context:
                out_block += f"\n<telos_memory>\n{telos_context}\n</telos_memory>"
            out_block += f"\nTurn {turn + 2}/{max_turns}."
            llm_messages.append({"role": "user", "content": out_block})
            log(f"  exit={exec_result.returncode} output_len={len(output)}")
        except subprocess.TimeoutExpired:
            llm_messages.append(
                {
                    "role": "user",
                    "content": "<command_output>\nCommand timed out.\n</command_output>",
                }
            )
            log("  TIMEOUT")
        except Exception as e:
            llm_messages.append(
                {"role": "user", "content": f"<command_output>\n{e!s}\n</command_output>"}
            )
            log(f"  ERROR: {e}")

    # Write post-hoc telos summary
    if telos_bridge and telos_bridge.mode == "live" and patch:
        try:
            telos_bridge.write(
                content=f"Agent completed with status={exit_status}. Patch has {patch.count(chr(10))} lines. Cost=${total_cost:.4f}.",
                kind="exit_summary",
                scope_kind="swe_bench_instance",
                scope_id=telos_scope_instance,
            )
        except Exception as e:
            log(f"telos-write exit_summary error: {e}")

    log(f"END status={exit_status} turns={last_turn} cost=${total_cost:.4f} elapsed={elapsed():.0f}s")

    return {
        "exit_status": exit_status,
        "patch": patch or "",
        "turns": last_turn,
        "cost_usd": round(total_cost, 6),
        "elapsed_sec": round(elapsed()),
        "diff_lines": patch.count("\n") if patch else 0,
        "changed_files": _extract_changed_files(patch) if patch else [],
        "format_error_count": format_error_count,
    }


def _extract_changed_files(patch: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):].strip()
            if path and path not in seen:
                seen.add(path)
                files.append(path)
    return files


# ---------------------------------------------------------------------------
# Patch selection (deterministic-v2)
# ---------------------------------------------------------------------------

EXIT_STATUS_RANK = {
    "Submitted": 0,
    "Unknown": 1,
    "LimitsExceeded": 2,
    "ExecutionTimeoutError": 3,
    "FormatError": 4,
}


def _candidate_sort_key(row: dict[str, Any]) -> tuple:
    exit_rank = EXIT_STATUS_RANK.get(row.get("exit_status", "Unknown"), 5)
    format_errors = int(row.get("format_error_count", 0))
    passes = int(row.get("targeted_passes", 0))
    failures = int(row.get("targeted_failures", 0))
    diff_lines = int(row.get("diff_lines", 10**9))
    changed_files = row.get("changed_files", [])
    changed_file_count = len(changed_files) if isinstance(changed_files, list) else 10**6
    agent_id = str(row.get("agent_id", ""))
    return (exit_rank, format_errors, -passes, failures, diff_lines, changed_file_count, agent_id)


def _looks_like_unified_diff(text: str) -> bool:
    if "diff --git " in text:
        return True
    has_old = has_new = False
    for line in text.splitlines():
        if line.startswith("--- "):
            has_old = True
        elif line.startswith("+++ "):
            has_new = True
        elif has_old and has_new and line.startswith("@@"):
            return True
    return False


def select_patches(run_dir: Path) -> int:
    """Select one patch per instance using deterministic-v2. Returns count selected."""
    instances_root = run_dir / "instances"
    if not instances_root.is_dir():
        return 0
    selected_count = 0
    for instance_root in sorted(p for p in instances_root.iterdir() if p.is_dir()):
        candidates: list[dict] = []
        valid_candidates: list[dict] = []
        for result_path in sorted((instance_root / "candidates").glob("*/result.json")):
            row = json.loads(result_path.read_text(encoding="utf-8"))
            patch_path = result_path.parent / "patch.diff"
            if not patch_path.is_file():
                continue
            patch_text = patch_path.read_text(encoding="utf-8")
            row["is_valid_patch"] = _looks_like_unified_diff(patch_text)
            row["agent_id"] = result_path.parent.name
            candidates.append(row)
            if row["is_valid_patch"]:
                valid_candidates.append(row)

        selected_root = instance_root / "selected"
        selected_root.mkdir(parents=True, exist_ok=True)

        if not valid_candidates:
            (selected_root / "selection.json").write_text(json.dumps({
                "instance_id": instance_root.name,
                "winner": None,
                "selector": "deterministic-v2",
                "candidate_count": len(candidates),
                "valid_candidate_count": 0,
            }, indent=2) + "\n", encoding="utf-8")
            continue

        valid_candidates.sort(key=_candidate_sort_key)
        winner = valid_candidates[0]
        # Copy patch to selected
        source_patch = instance_root / "candidates" / winner["agent_id"] / "patch.diff"
        (selected_root / "patch.diff").write_text(
            source_patch.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (selected_root / "selection.json").write_text(json.dumps({
            "instance_id": instance_root.name,
            "winner": {k: v for k, v in winner.items() if k != "patch"},
            "selector": "deterministic-v2",
            "candidate_count": len(candidates),
            "valid_candidate_count": len(valid_candidates),
        }, indent=2) + "\n", encoding="utf-8")
        selected_count += 1
    return selected_count


# ---------------------------------------------------------------------------
# Export predictions
# ---------------------------------------------------------------------------

def export_predictions(run_dir: Path, run_id: str) -> list[dict]:
    predictions: list[dict] = []
    instances_root = run_dir / "instances"
    for instance_root in sorted(p for p in instances_root.iterdir() if p.is_dir()):
        patch_path = instance_root / "selected" / "patch.diff"
        if not patch_path.is_file():
            continue
        patch_text = patch_path.read_text(encoding="utf-8")
        if not _looks_like_unified_diff(patch_text):
            continue
        predictions.append({
            "instance_id": instance_root.name,
            "patch": patch_text,
            "prefix": run_id,
        })
    out_path = run_dir / "predictions.json"
    out_path.write_text(json.dumps(predictions, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(predictions)} predictions to {out_path}")
    return predictions


# ---------------------------------------------------------------------------
# Evaluation & comparison
# ---------------------------------------------------------------------------

def run_evaluation(run_dir: Path, config: dict[str, Any]) -> None:
    eval_script = Path(str(config["_eval_script_path"]))
    if not eval_script.exists():
        print(f"WARNING: eval script not found at {eval_script}, skipping evaluation")
        return

    predictions = run_dir / "predictions.json"
    if not predictions.exists():
        print("WARNING: predictions.json not found, skipping evaluation")
        return

    eval_dir = run_dir / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(eval_script),
        "--raw_sample_path", str(config["_dataset_jsonl_path"]),
        "--patch_path", str(predictions),
        "--output_dir", str(eval_dir),
        "--scripts_dir", str(config["_run_scripts_dir_path"]),
        "--num_workers", str(config.get("eval_num_workers", 1)),
        "--dockerhub_username", "jefzda",
    ]
    print(f"Running evaluation: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(config["_official_repo_path"]))


def compare_to_leaderboard(run_dir: Path, config: dict[str, Any]) -> None:
    lb_path = Path(str(config["_leaderboard_json_path"]))
    if not lb_path.exists():
        print(f"WARNING: leaderboard JSON not found at {lb_path}, skipping comparison")
        return

    eval_results_path = run_dir / "eval" / "eval_results.json"
    if not eval_results_path.exists():
        print("WARNING: eval results not found, skipping comparison")
        return

    eval_results = json.loads(eval_results_path.read_text(encoding="utf-8"))
    if not isinstance(eval_results, dict):
        print(f"WARNING: unexpected eval_results shape ({type(eval_results)}), skipping comparison")
        return
    resolved = sum(1 for v in eval_results.values() if v is True)
    n = len(eval_results)

    rate = resolved / n if n > 0 else 0.0
    lo, hi = _wilson_ci(resolved, n)

    lb = json.loads(lb_path.read_text(encoding="utf-8"))
    entries = lb.get("entries", [])
    meta = lb.get("meta", {})
    expected_total = meta.get("benchmark_total_instances", 0)
    direct_comparable = expected_total > 0 and n == expected_total

    lines = [
        f"# vs leaderboard: `{run_dir.name}`",
        "",
        f"- **Observed resolve rate**: {rate:.4f} (n={n}, successes={resolved})",
        f"- **Wilson 95% interval**: [{lo:.4f}, {hi:.4f}]",
        f"- **Directly comparable to benchmark total**: {direct_comparable}",
    ]
    if expected_total:
        lines.append(f"- **Benchmark total instances**: {expected_total}")
    if not direct_comparable:
        lines.append("- **Warning**: This run does not cover the full benchmark total, deltas are directional only.")

    lines.extend([
        "",
        "| frontier label | published | delta | run low - pub |",
        "|---|---:|---:|---:|",
    ])
    for ent in entries:
        label = ent.get("label", "")
        pub = ent.get("resolve_rate", 0.0)
        delta = rate - pub
        guard = lo - pub
        lines.append(f"| {label} | {pub:.4f} | {delta:+.4f} | {guard:+.4f} |")

    report = "\n".join(lines) + "\n"
    report_path = run_dir / "vs-leaderboard.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nComparison report written to {report_path}")
    print(report)


def _wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    rad = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, center - rad), min(1.0, center + rad)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def execute_phase(
    config: dict[str, Any],
    run_dir: Path,
    run_id: str,
    instance_ids: list[str],
    dataset_index: dict[str, dict[str, Any]],
    *,
    resume: bool = True,
) -> None:
    model = config["model"]
    agent_count = config["agent_count"]
    temperatures = config["agent_temperatures"]
    max_turns = config["max_turns"]
    max_wall = config["max_wall_clock_sec"]
    docker_timeout = config["docker_timeout_sec"]
    telos_mode = config["telos_mode"]
    telos_base_url = str(config.get("telos_base_url", ""))
    run_scripts_dir = Path(str(config["_run_scripts_dir_path"]))
    remove_image_after_instance = bool(config.get("docker_remove_image_after_instance", True))
    prune_after_instance = bool(config.get("docker_prune_after_instance", False))

    print(f"=== EXECUTE PHASE ===")
    print(f"  instances: {len(instance_ids)}, agents: {agent_count}, model: {model}")
    print(f"  telos: {telos_mode}")

    total_runs = len(instance_ids) * agent_count
    completed = 0
    failed = 0
    skipped = 0

    for instance_id in instance_ids:
        instance_data = dataset_index.get(instance_id)
        if not instance_data:
            print(f"  WARNING: {instance_id} not in dataset index, skipping")
            skipped += 1
            continue

        repo = instance_data.get("repo", "")
        if not repo:
            print(f"  WARNING: {instance_id} has no repo field, skipping")
            skipped += 1
            continue

        docker_image = get_dockerhub_image(instance_id, repo)

        print(f"  Pulling {docker_image[:80]}...")
        result = docker_pull(docker_image, timeout=docker_timeout)
        if result.returncode != 0:
            print(f"  WARNING: docker pull failed for {docker_image[:60]}: {result.stderr[:200]}")
            skipped += agent_count
            continue

        problem_statement = instance_data.get("problem_statement", "")
        hints_text = instance_data.get("hints_text", "")
        fail_to_pass = instance_data.get("FAIL_TO_PASS", [])

        # Load test files from run_scripts
        info_path = run_scripts_dir / instance_id / "instance_info.txt"
        test_files = []
        if info_path.exists():
            info = parse_instance_info(info_path)
            tf = info.get("Test Files", [])
            if isinstance(tf, list):
                test_files = tf

        instance_scope = telos_instance_scope_id(run_id, instance_id)

        try:
            for agent_idx in range(agent_count):
                agent_id = f"agent-{agent_idx:02d}"
                temperature = temperatures[agent_idx % len(temperatures)]

                # Check if already completed
                result_path = run_dir / "instances" / instance_id / "candidates" / agent_id / "result.json"
                if resume and result_path.exists():
                    skipped += 1
                    continue

                cname = safe_container_name(run_id, instance_id, agent_id)

                # Clean up any stale container
                docker_rm(cname)

                # Start container
                start_result = docker_run(docker_image, cname)
                if start_result.returncode != 0:
                    print(f"  FAIL: docker run for {cname}: {start_result.stderr[:100]}")
                    failed += 1
                    continue

                bridge: TelosBridge | None = None
                if telos_mode == "live":
                    bridge = TelosBridge(
                        mode="live",
                        base_url=telos_base_url,
                        monad_id=agent_id,
                    )

                candidate_dir = run_dir / "instances" / instance_id / "candidates" / agent_id
                candidate_dir.mkdir(parents=True, exist_ok=True)
                log_path = candidate_dir / "agent.log"

                try:
                    result = run_agent(
                        container=cname,
                        model=model,
                        temperature=temperature,
                        problem_statement=problem_statement,
                        hints_text=hints_text,
                        fail_to_pass=fail_to_pass,
                        test_files=test_files,
                        max_turns=max_turns,
                        max_wall_clock_sec=max_wall,
                        docker_timeout=docker_timeout,
                        telos_bridge=bridge,
                        telos_scope_instance=instance_scope,
                        log_path=log_path,
                    )
                except Exception as e:
                    print(f"  FAIL: {instance_id}/{agent_id}: {e}")
                    result = {
                        "exit_status": "Unknown",
                        "patch": "",
                        "turns": 0,
                        "cost_usd": 0,
                        "elapsed_sec": 0,
                        "diff_lines": 0,
                        "changed_files": [],
                    }
                    failed += 1
                finally:
                    docker_rm(cname)

                result["instance_id"] = instance_id
                result["agent_id"] = agent_id
                result["model"] = model
                result["temperature"] = temperature
                result["docker_image"] = docker_image
                (candidate_dir / "result.json").write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8"
                )

                if result.get("patch"):
                    (candidate_dir / "patch.diff").write_text(result["patch"], encoding="utf-8")

                completed += 1
                print(
                    f"  [{completed}/{total_runs}] {instance_id}/{agent_id} -> {result['exit_status']} "
                    f"turns={result['turns']} cost=${result['cost_usd']:.4f}"
                )
        finally:
            if remove_image_after_instance:
                remove_result = docker_rmi(docker_image)
                if remove_result.returncode != 0:
                    err = (remove_result.stderr or remove_result.stdout).strip()
                    print(f"  WARNING: docker image rm failed for {docker_image[:60]}: {err[:200]}")
            if prune_after_instance:
                prune_result = docker_image_prune()
                if prune_result.returncode != 0:
                    err = (prune_result.stderr or prune_result.stdout).strip()
                    print(f"  WARNING: docker image prune failed: {err[:200]}")

    # Write run summary
    summary = {
        "run_id": run_id,
        "lane_id": config["lane_id"],
        "model": model,
        "telos_mode": telos_mode,
        "total_runs": total_runs,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\n=== EXECUTE COMPLETE: {completed} completed, {failed} failed, {skipped} skipped ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="SWE-bench Pro simple runner")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--phase", choices=["execute", "select", "export", "evaluate", "all"], default="all")
    parser.add_argument("--instance-regex", default=None)
    parser.add_argument("--max-instances", type=int, default=None)
    parser.add_argument("--telos-mode", default=None, help="Override telos_mode from config")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    config = load_config(resolve_path(Path.cwd(), args.config))
    if args.telos_mode:
        config["telos_mode"] = args.telos_mode
    config = build_runtime_config(config)
    ensure_required_paths(config)

    run_id = args.run_id or f"{config['lane_id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    output_root = Path(str(config["_output_root_path"]))
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Run ID: {run_id}")
    print(f"Output:  {run_dir}")

    # Save config copy
    (run_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    # Load instance IDs
    instances_path = Path(str(config["_instances_path"]))
    instance_ids = load_instance_ids(instances_path)
    if args.instance_regex:
        instance_ids = [iid for iid in instance_ids if re.search(args.instance_regex, iid)]
    if args.max_instances:
        instance_ids = instance_ids[:args.max_instances]

    print(f"Instances: {len(instance_ids)}")

    # Load dataset index for image_name and problem_statement
    dataset_jsonl = Path(str(config["_dataset_jsonl_path"]))
    dataset_index = load_dataset_index(dataset_jsonl)
    print(f"Dataset index: {len(dataset_index)} records loaded")

    phase = args.phase

    if phase in ("execute", "all"):
        execute_phase(
            config,
            run_dir,
            run_id,
            instance_ids,
            dataset_index,
            resume=not args.no_resume,
        )

    if phase in ("select", "all"):
        print("\n=== SELECT PHASE ===")
        selected = select_patches(run_dir)
        print(f"Selected patches for {selected} instances")

    if phase in ("export", "all"):
        print("\n=== EXPORT PHASE ===")
        export_predictions(run_dir, run_id)

    if phase in ("evaluate", "all"):
        print("\n=== EVALUATE PHASE ===")
        run_evaluation(run_dir, config)

    if phase in ("evaluate", "all"):
        print("\n=== COMPARE PHASE ===")
        compare_to_leaderboard(run_dir, config)

    print(f"\nDone. Results in {run_dir}")


if __name__ == "__main__":
    main()
