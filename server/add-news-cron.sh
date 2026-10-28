#!/usr/bin/env bash
# 이미 설치된 서버에 '주요 뉴스' 예약 2개만 추가합니다(기존 예약은 그대로). 사용법: bash server/add-news-cron.sh
DIR="$(cd "$(dirname "$0")/.." && pwd)"
( crontab -l 2>/dev/null | grep -v 'run.sh news-' || true
  echo "0 20 * * * $DIR/server/run.sh news-pm  # market-lab-job"
  echo "0 8 * * 1 $DIR/server/run.sh news-am   # market-lab-job"
) | crontab -
echo "현재 예약:"; crontab -l | grep market-lab-job
