#!/usr/bin/env bash
# 서버에서 딱 한 번 실행하는 설치 스크립트입니다.  사용법:  bash server/setup.sh
set -e
cd "$(dirname "$0")/.."
DIR="$(pwd)"

echo "[1/5] 서버 시계를 한국 시간으로 맞춥니다(예약 시각이 한국 시간 기준이 되도록)"
sudo timedatectl set-timezone Asia/Seoul

echo "[2/5] 필요한 프로그램 설치"
sudo apt-get update -y
sudo apt-get install -y git python3-venv

echo "[3/5] 파이썬 환경 만들기"
python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt

echo "[4/5] 비밀키 파일 준비"
if [ ! -f "$HOME/.market-lab.env" ]; then
  cp server/env.example "$HOME/.market-lab.env"
  chmod 600 "$HOME/.market-lab.env"
  echo "  → $HOME/.market-lab.env 를 열어 키움 앱키/시크릿을 입력하세요 (nano $HOME/.market-lab.env)"
fi
git config user.name "market-lab-server"
git config user.email "market-lab-server@users.noreply.github.com"
chmod +x server/run.sh

echo "[5/5] 예약 실행 등록 (평일 12:05 / 17:30 / 20:10, 뉴스 매일 20:00 + 월 08:00)"
( crontab -l 2>/dev/null | grep -v 'market-lab-job' || true
  echo "5 12 * * 1-5 $DIR/server/run.sh noon  # market-lab-job"
  echo "30 17 * * 1-5 $DIR/server/run.sh close # market-lab-job"
  echo "10 20 * * 1-5 $DIR/server/run.sh nxt   # market-lab-job"
  echo "0 20 * * * $DIR/server/run.sh news-pm  # market-lab-job"
  echo "0 8 * * 1 $DIR/server/run.sh news-am   # market-lab-job"
) | crontab -
crontab -l | grep market-lab-job
echo "설치 완료. 다음: 키 입력 → 'server/run.sh close' 로 수동 1회 시험"
