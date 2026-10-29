#!/usr/bin/env bash
# 사용법: bash server/run.sh noon|close|nxt|news-am|news-pm
# 하는 일: 최신 코드 받기 → 시황(또는 주요 뉴스) 수집 → data 폴더 변경분을 GitHub 에 올리기 (cron 이 호출합니다)
#          실행 결과는 data/status.json 에 남기고(감시용), 실패하면 휴대폰 알림(NTFY_TOPIC 설정 시)
set -u
SESSION="${1:?사용법: run.sh noon|close|nxt|news-am|news-pm}"
cd "$(dirname "$0")/.." || exit 1
LOG="$HOME/market-lab.log"

# 같은 회차가 겹쳐 실행되지 않도록 잠금
exec 9>/tmp/market-lab.lock
flock -n 9 || { echo "$(date '+%F %T') 이미 실행 중이라 건너뜀 ($SESSION)" >>"$LOG"; exit 0; }

set -a; . "$HOME/.market-lab.env"; set +a

notify() {   # $1=제목 $2=본문. NTFY_TOPIC 이 비어 있으면 아무것도 안 함
  [ -n "${NTFY_TOPIC:-}" ] || return 0
  curl -s -m 10 -H "Title: $1" -H "Tags: warning" -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || true
}

{
  echo "=== $(date '+%F %T') 회차: $SESSION ==="
  git pull --rebase --autostash -q || echo "[경고] git pull 실패"

  OUT=$(mktemp)
  ok=0
  for try in 1 2 3; do                       # 일시적인 통신 오류는 1분 간격으로 최대 3번 재시도
    case "$SESSION" in
      news-am) CMD=(./.venv/bin/python newsdigest.py --edition am) ;;
      news-pm) CMD=(./.venv/bin/python newsdigest.py --edition pm) ;;
      *)       CMD=(./.venv/bin/python collect.py --session "$SESSION") ;;
    esac
    if "${CMD[@]}" 2>&1 | tee "$OUT"; [ "${PIPESTATUS[0]}" = 0 ]; then ok=1; break; fi
    echo "[재시도 $try/3] 수집 실패, 60초 후 다시 시도"; sleep 60
  done

  # 결과를 data/status.json 에 기록 (GitHub 감시 작업이 읽음)
  if [ "$ok" = 1 ]; then
    if grep -q "휴장일이거나" "$OUT"; then RESULT=closed; else RESULT=ok; fi
  else
    RESULT=fail
    echo "[실패] $SESSION 수집을 끝내지 못했습니다"
    notify "market-lab $SESSION 실패" "$(date '+%m/%d %H:%M') $(tail -3 "$OUT" | tr '\n' ' ' | cut -c1-300)"
  fi
  WARN=$(grep -c "수집 실패" "$OUT" || true)
  ./.venv/bin/python - "$SESSION" "$RESULT" "$WARN" <<'PY'
import json, sys, datetime as dt, pathlib
p = pathlib.Path("data/status.json")
try: s = json.loads(p.read_text(encoding="utf-8"))
except Exception: s = {}
now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
s[sys.argv[1]] = {"date": now.date().isoformat(), "time": now.strftime("%H:%M"), "result": sys.argv[2], "warnings": int(sys.argv[3] or 0)}
p.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
PY
  rm -f "$OUT"

  git add data
  if ! git diff --cached --quiet; then
    git commit -q -m "data: $(TZ=Asia/Seoul date +%F) ${SESSION} 자동 수집 (${RESULT})"
    pushed=0
    for try in 1 2 3; do
      git push -q && { echo "[완료] GitHub 에 올림"; pushed=1; break; }
      echo "[재시도 $try/3] push 실패"; sleep 5; git pull --rebase -q
    done
    [ "$pushed" = 1 ] || notify "market-lab push 실패" "$(date '+%m/%d %H:%M') $SESSION 수집은 됐지만 GitHub 업로드 실패"
  else
    echo "변경 없음"
  fi
} >>"$LOG" 2>&1
