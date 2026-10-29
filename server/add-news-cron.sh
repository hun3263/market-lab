#!/usr/bin/env bash
# 예약(cron)을 최신 일정으로 다시 등록합니다. 기존 market-lab 예약을 지우고 아래 5개로 교체. 사용법: bash server/add-news-cron.sh
#   평일 12:05 1차 · 17:30 2차 · 20:10 3차 / 매일 07:00 뉴스(최초) · 20:30 뉴스(최종)
DIR="$(cd "$(dirname "$0")/.." && pwd)"
( crontab -l 2>/dev/null | grep -v 'market-lab-job' || true
  echo "5 12 * * 1-5 bash $DIR/server/run.sh noon     # market-lab-job"
  echo "30 17 * * 1-5 bash $DIR/server/run.sh close   # market-lab-job"
  echo "10 20 * * 1-5 bash $DIR/server/run.sh nxt     # market-lab-job"
  echo "0 7 * * * bash $DIR/server/run.sh news-am     # market-lab-job"
  echo "30 20 * * * bash $DIR/server/run.sh news-pm   # market-lab-job"
) | crontab -
echo "현재 예약:"; crontab -l | grep market-lab-job
