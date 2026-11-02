#!/usr/bin/env bash
# 장중 10분마다 ① 시장 전체 투자자별 순매수 쌓기(flow_tick) ② 최신일 후보 타점 알림(zone_alert) 예약을 등록합니다 (평일 09:05~15:35). 사용법: bash server/add-flow-cron.sh
DIR="$(cd "$(dirname "$0")/.." && pwd)"
CMD="cd $DIR && set -a && . \$HOME/.market-lab.env && set +a && ./.venv/bin/python flow_tick.py >> \$HOME/market-lab-flow.log 2>&1; ./.venv/bin/python zone_alert.py >> \$HOME/market-lab-flow.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'market-lab-flow' || true
  echo "5-55/10 9-15 * * 1-5 $CMD   # market-lab-flow"
) | crontab -
echo "현재 장중 수급 예약:"; crontab -l | grep market-lab-flow
