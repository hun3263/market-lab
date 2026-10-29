#!/usr/bin/env bash
# 모바일에서 한 줄로 점검:  bash server/status.sh
cd "$(dirname "$0")/.." || exit 1
echo "── 최근 로그 ──"; tail -25 "$HOME/market-lab.log"
echo "── 실행 중 ──"; pgrep -af "collect.py|newsdigest.py" || echo "없음"
echo "── 회차별 마지막 결과(data/status.json) ──"; cat data/status.json 2>/dev/null || echo "아직 없음"
echo "── 예약 ──"; crontab -l | grep market-lab-job
echo "── 코드 버전 ──"; git log -1 --format='%h %ad %s' --date=format:'%m-%d %H:%M'
