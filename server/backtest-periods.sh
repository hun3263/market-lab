#!/bin/bash
# 최근 3개월 · 6개월 · 1년 과거 테스트 (서버에서 실행, 처음 30~60분 / 다시 돌릴 땐 몇 분)
#   ① 키움 일봉 2년치 받기(조회 전용)  ② DR 매매 규칙 + 방배동 재료 매매(일봉·5분봉) 백테스트 3기간
#      5분봉은 재료 매매 후보의 필요한 날짜만 받아 ~/market-lab-private/minutes/5m 에 쌓음(처음 1회 20~40분 [추정], 이후 재사용)  ③ 최+신+일 A·B 과거 재현(1년)
#   ④ 관심종목 탭 암호화본 갱신 → GitHub 반영   ⑤ 요약을 화면·~/market-lab-private/bt/summary.txt·휴대폰 알림으로
# 사용:  nohup bash server/backtest-periods.sh > ~/backtest.log 2>&1 &      진행 확인: tail -f ~/backtest.log
# 주의:  12:05 · 17:30 · 20:10 자동 수집 시간과 겹치지 않게 실행(같은 저장소에 동시에 올리면 충돌)
set -e
cd "$(dirname "$0")/.."; R=$PWD
set -a; . "$HOME/.market-lab.env"; set +a
P="$HOME/market-lab-private/bt"; mkdir -p "$P"
PY="$R/.venv/bin/python"

echo "■ ① 일봉 받기 $(date '+%H:%M')"
cd "$P"
if [ -f prices/_KOSPI.csv ] && [ -n "$(find prices/_KOSPI.csv -mtime -1)" ]; then
  echo "  오늘 받은 자료가 있어 건너뜀"
else
  "$PY" "$R/backtest_dr.py" fetch-kiwoom --start "$(date -d '2 years ago' +%F)" --out prices   # 1년 테스트 + 신고가 판정용 250거래일
fi

echo "■ ② DR 매매 규칙 백테스트 $(date '+%H:%M')"
for t in 1Y:12 6M:6 3M:3; do                        # 1년을 먼저: 5분봉을 한 번 받아두고 6·3개월은 저장분 재사용
  tag=${t%%:*}; m=${t##*:}
  "$PY" "$R/backtest_dr.py" run --prices prices --listing listing.csv --from "$(date -d "$m months ago" +%F)" --to "$(date +%F)" --tag "$tag" --out "$P" > "$P/run_$tag.txt"
  (cd "$R" && "$PY" backtest_bangbae.py --prices "$P/prices" --listing "$P/listing.csv" --from "$(date -d "$m months ago" +%F)" --to "$(date +%F)" --tag "$tag" --out "$P" --minutes "$([ "$tag" = 1Y ] && echo fetch || echo cache)" > "$P/run_bb_$tag.txt")   # 방배동 재료 매매 (+5분봉 V1·V2)
done

echo "■ ③ 최+신+일 A·B 과거 재현(1년) $(date '+%H:%M')"
cd "$R"
"$PY" backfill_choisinil.py --prices "$P/prices" --listing "$P/listing.csv" --years 1 > "$P/backfill.txt"

echo "■ ④ 관심종목 탭 갱신 $(date '+%H:%M')"
"$PY" secure_publish.py
git pull -q --rebase || true
git add data/secure && git commit -qm "과거 테스트 결과(3·6·12개월)" && git push -q || echo "  (올릴 변경 없음 또는 push 실패 → 20:10 자동 수집 때 함께 올라감)"

echo "■ ⑤ 요약"
"$PY" - "$P" <<'EOF' | tee "$P/summary.txt"
import json, sys
from pathlib import Path
P = Path(sys.argv[1])
print("[DR 매매 규칙]  기간 | 거래수 | 승률 | 손익분기승률 | 1건기대값 | 비중10%누적 | 최대낙폭")
for tag, nm in (("3M", "3개월"), ("6M", "6개월"), ("1Y", "1년")):
    f = P / f"report_{tag}.json"
    if f.exists():
        a = json.loads(f.read_text(encoding="utf-8")).get("all") or {}
        print(f"  {nm:4} | {a.get('n', 0):4} | {a.get('win%', '-')}% | {a.get('BEwin%', '-')}% | {a.get('expect%', '-')}% | {a.get('sumAt10%w', '-')}% | {a.get('mddAt10%w', '-')}%")
print("[방배동 재료 매매]  기간·방식 | 거래수 | 승률 | 손익분기승률 | 1건기대값 | 비중10%누적 | 최대낙폭")
for tag, nm in (("3M", "3개월"), ("6M", "6개월"), ("1Y", "1년")):
    f = P / f"report_bb_{tag}.json"
    if f.exists():
        r = json.loads(f.read_text(encoding="utf-8"))
        for key, lab in (("all", "일봉"), ("all_V1", "5분봉 정밀"), ("all_V2", "5분봉 신호")):
            a = r.get(key) or {}
            if a:
                print(f"  {nm} {lab:6} | {a.get('n', 0):4} | {a.get('win%', '-')}% | {a.get('BEwin%', '-')}% | {a.get('expect%', '-')}% | {a.get('sumAt10%w', '-')}% | {a.get('mddAt10%w', '-')}%")
        if r.get("minutes"):
            print(f"  ({nm} 5분봉 요청 {r['minutes']['calls']}회 · 저장 {r['minutes']['sizeMB']}MB)")
b = json.loads((Path.home() / "market-lab-private" / "backfill" / "backfill.json").read_text(encoding="utf-8")).get("report", {})
print("[최+신+일 A·B 등장 후 수익률]  기간 | 건수 | +5일 | +10일 | +20일  (괄호=전 종목 평균)")
for k, x in (b.get("by_period") or {}).items():
    ab, bs = x.get("AB", {}), x.get("base", {})
    print(f"  {k:4} | {ab.get('n', 0):5} | {ab.get('r5', '-')}%({bs.get('r5', '-')}) | {ab.get('r10', '-')}%({bs.get('r10', '-')}) | {ab.get('r20', '-')}%({bs.get('r20', '-')})")
print(f"  DART '일' 조건 {'포함' if b.get('dart') else '미포함(A 등급 없음)'} · {b.get('capRule', '')}")
EOF
if [ -n "$NTFY_TOPIC" ]; then
  curl -s -H "Title: market-lab backtest done" -d "$(cat "$P/summary.txt")" "https://ntfy.sh/$NTFY_TOPIC" > /dev/null || true
fi
echo "완료 $(date '+%H:%M') · 사이트 🔒 관심종목 → 'DR 매매 규칙 백테스트' · '과거 재현 검증' 표에서도 확인"
