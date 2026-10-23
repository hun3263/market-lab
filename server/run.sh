#!/usr/bin/env bash
# 사용법: server/run.sh noon|close|nxt
# 하는 일: 최신 코드 받기 → 시황 수집 → data 폴더 변경분을 GitHub 에 올리기 (cron 이 호출합니다)
set -u
SESSION="${1:?사용법: run.sh noon|close|nxt}"
cd "$(dirname "$0")/.." || exit 1
LOG="$HOME/market-lab.log"

# 같은 회차가 겹쳐 실행되지 않도록 잠금
exec 9>/tmp/market-lab.lock
flock -n 9 || { echo "$(date '+%F %T') 이미 실행 중이라 건너뜀 ($SESSION)" >>"$LOG"; exit 0; }

set -a; . "$HOME/.market-lab.env"; set +a

{
  echo "=== $(date '+%F %T') 회차: $SESSION ==="
  git pull --rebase --autostash -q || echo "[경고] git pull 실패"

  ok=0
  for try in 1 2 3; do                       # 일시적인 통신 오류는 1분 간격으로 최대 3번 재시도
    if ./.venv/bin/python collect.py --session "$SESSION"; then ok=1; break; fi
    echo "[재시도 $try/3] 수집 실패, 60초 후 다시 시도"; sleep 60
  done
  [ "$ok" = 1 ] || echo "[실패] $SESSION 수집을 끝내지 못했습니다"

  git add data
  if ! git diff --cached --quiet; then
    git commit -q -m "data: $(TZ=Asia/Seoul date +%F) ${SESSION} 자동 수집"
    for try in 1 2 3; do
      git push -q && { echo "[완료] GitHub 에 올림"; break; }
      echo "[재시도 $try/3] push 실패"; sleep 5; git pull --rebase -q
    done
  else
    echo "변경 없음(휴장일이거나 저장할 데이터 없음)"
  fi
} >>"$LOG" 2>&1
