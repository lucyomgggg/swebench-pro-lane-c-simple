# SWE-bench Pro Lane C Simple Runner

`simple` を standalone repo として切り出した版。
DigitalOcean に clone して、そのまま full public set の Lane C を回せる形にしてある。

## 何が入っているか

- `run_lane_c.py`: 実行、select、export、evaluate までを 1 本で回す runner
- `config.yaml`: full public set 731 件向け
- `config.pilot.yaml`: 25 件の pilot 向け
- `data/public-test-instance-ids.txt`: canonical public test split
- `scripts/install_ubuntu.sh`: Ubuntu 初期セットアップ
- `scripts/bootstrap_official_repo.sh`: official `SWE-bench_Pro-os` checkout を取得
- `scripts/preflight.py`: 実行前チェック

## Quick Start

```bash
git clone https://github.com/lucyomgggg/swebench-pro-lane-c-simple.git
cd swebench-pro-lane-c-simple

./scripts/install_ubuntu.sh
cp .env.example .env
# OPENROUTER_API_KEY を入れる

newgrp docker
./scripts/bootstrap_official_repo.sh
.venv/bin/python scripts/preflight.py --config config.pilot.yaml
.venv/bin/python run_lane_c.py --config config.pilot.yaml --max-instances 1
```

pilot が通ったら full set:

```bash
.venv/bin/python scripts/preflight.py --config config.yaml
nohup ./scripts/run_full.sh > runner.log 2>&1 &
tail -f runner.log
```

## 主な変更点

- `simple` 単体で動くように root 依存を除去
- full public set 731 件を既定設定に変更
- instance ごとに Docker image を削除して、full set でもディスクが膨らみすぎないように変更
- DigitalOcean 用の bootstrap / preflight / systemd 例を追加

## phases

- `execute`: patch 生成
- `select`: 最良 patch を 1 つ選ぶ
- `export`: `predictions.json` を書く
- `evaluate`: official evaluator を走らせる
- `all`: 上を順番に実行

## よく使う command

```bash
# full set
.venv/bin/python run_lane_c.py --config config.yaml

# pilot
.venv/bin/python run_lane_c.py --config config.pilot.yaml

# select だけやり直す
.venv/bin/python run_lane_c.py --config config.yaml --phase select --run-id <run_id>

# export だけやり直す
.venv/bin/python run_lane_c.py --config config.yaml --phase export --run-id <run_id>
```

## DigitalOcean

詳細は [docs/DIGITALOCEAN.md](docs/DIGITALOCEAN.md)。
結論だけ言うと、最安 Droplet は非推奨。
