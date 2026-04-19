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

## doctl で Droplet を作る

手元の Mac か Linux で `doctl` を使う前提。

DigitalOcean 公式 docs で確認した current CLI は `doctl compute droplet create <name> --size ... --image ... --region ...` 形式。

```bash
brew install doctl
doctl auth init --context personal
doctl compute ssh-key list
```

`doctl compute ssh-key list` で使う key fingerprint を確認したら、4 GB 構成で Droplet を作る。

```bash
export DO_CONTEXT=personal
export DROPLET_NAME=swebench-lane-c-01
export REGION=sgp1
export SIZE=s-2vcpu-4gb
export IMAGE=ubuntu-24-04-x64
export SSH_FINGERPRINT='<your-ssh-key-fingerprint>'

doctl --context "$DO_CONTEXT" compute droplet create "$DROPLET_NAME" \
  --region "$REGION" \
  --image "$IMAGE" \
  --size "$SIZE" \
  --ssh-keys "$SSH_FINGERPRINT" \
  --tag-names swebench-lane-c \
  --enable-monitoring \
  --enable-ipv6 \
  --wait
```

Droplet ID と public IP を確認:

```bash
doctl --context "$DO_CONTEXT" compute droplet list --tag-name swebench-lane-c
```

SSH だけ開ける firewall:

```bash
doctl --context "$DO_CONTEXT" compute firewall create \
  --name swebench-lane-c-ssh \
  --inbound-rules "protocol:tcp,ports:22,address:0.0.0.0/0 protocol:tcp,ports:22,address:::/0" \
  --outbound-rules "protocol:tcp,ports:1-65535,address:0.0.0.0/0 protocol:tcp,ports:1-65535,address:::/0 protocol:udp,ports:1-65535,address:0.0.0.0/0 protocol:udp,ports:1-65535,address:::/0 protocol:icmp,address:0.0.0.0/0 protocol:icmp,address:::/0" \
  --tag-names swebench-lane-c
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
