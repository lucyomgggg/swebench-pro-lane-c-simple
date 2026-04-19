#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_public_instance_ids(dataset_path: str, split: str) -> list[str]:
    try:
        from datasets import load_dataset
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("datasets is required: pip install datasets") from exc

    dataset = load_dataset(dataset_path, split=split)
    seen: set[str] = set()
    instance_ids: list[str] = []
    for row in dataset:
        instance_id = str(row.get("instance_id") or "").strip()
        if not instance_id or instance_id in seen:
            continue
        seen.add(instance_id)
        instance_ids.append(instance_id)
    if not instance_ids:
        raise SystemExit("no instance ids found")
    return instance_ids


def sha256_sorted_instance_ids(instance_ids: list[str]) -> str:
    import hashlib

    payload = "\n".join(sorted(instance_ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the canonical public SWE-bench Pro instance list.")
    parser.add_argument("--dataset-path", default="ScaleAI/SWE-bench_Pro")
    parser.add_argument("--split", default="test")
    parser.add_argument("--output", default=str(ROOT / "data/public-test-instance-ids.txt"))
    args = parser.parse_args()

    instance_ids = load_public_instance_ids(args.dataset_path, args.split)
    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = (ROOT / output_path).resolve()
    lines = [
        "# Generated from the public Hugging Face SWE-bench Pro test split.",
        f"# dataset_path: {args.dataset_path}",
        f"# split: {args.split}",
        f"# instance_count: {len(instance_ids)}",
        f"# manifest_sha256: {sha256_sorted_instance_ids(instance_ids)}",
        "",
        *instance_ids,
    ]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"output={output_path}")
    print(f"instance_count={len(instance_ids)}")


if __name__ == "__main__":
    main()

