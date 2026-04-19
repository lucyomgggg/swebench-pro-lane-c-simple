# DigitalOcean deployment

この repo は host 上で Python を実行し、各 instance は Docker container として動かす。
runner 自体を Docker 化しないので、nested Docker を避けられる。

## 推奨 Droplet

- 最安プランは使わない
- 最低でも Basic 4 GB / 2 vCPU / 80 GB SSD
- full set を最後まで回して official eval まで同じ host でやるなら 8 GB / 4 vCPU / 160 GB SSD が安全

理由は 3 つだけ。

- この runner は issue ごとに Docker image を pull する
- full set は 731 instance ある
- 1 GB や 2 GB の shared CPU だと Docker と repo test 実行で詰まりやすい

今回の script では instance 完了ごとに image を削除するので、ディスクはかなり抑えられる。
それでも 25 GB や 50 GB の最安クラスは余裕がない。

## 最短手順

```bash
git clone https://github.com/lucyomgggg/swebench-pro-lane-c-simple.git
cd swebench-pro-lane-c-simple

./scripts/install_ubuntu.sh
cp .env.example .env
# .env に OPENROUTER_API_KEY を入れる

newgrp docker

./scripts/bootstrap_official_repo.sh
.venv/bin/python scripts/preflight.py --config config.pilot.yaml
.venv/bin/python run_lane_c.py --config config.pilot.yaml --max-instances 1
```

pilot で動作確認したら full set に切り替える。

```bash
.venv/bin/python scripts/preflight.py --config config.yaml
nohup ./scripts/run_full.sh > runner.log 2>&1 &
tail -f runner.log
```

## systemd で動かす

clone 先を `~/swebench-pro-lane-c-simple` にした場合:

```bash
sudo cp deploy/systemd/swebench-lane-c.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now swebench-lane-c
sudo journalctl -u swebench-lane-c -f
```

`.env` を直したら再起動する。

```bash
sudo systemctl restart swebench-lane-c
```
