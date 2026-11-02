#!/bin/bash
# 최근 2년 최+신+일 A·B 과거 재현(백필) — 서버에서 한 번만 실행(30~60분 소요)
# 사용: bash server/backfill-once.sh
set -e
cd "$(dirname "$0")/.."
source "$HOME/.market-lab.env"
P="$HOME/market-lab-private/bt"; mkdir -p "$P"
cd "$P"
# 1) 키움 일봉 2년치 받기(조회 전용 API)
"$OLDPWD/.venv/bin/python" "$OLDPWD/backtest_dr.py" fetch-kiwoom --start "$(date -d '2 years ago 30 days ago' +%F)" --out prices
cd "$OLDPWD"
# 2) 그날 알 수 있던 자료만으로 A·B 다시 뽑기 + 그 뒤 수익률 (DART_API_KEY 있으면 '일' 포함)
./.venv/bin/python backfill_choisinil.py --prices "$P/prices" --listing "$P/listing.csv" --years 2
# 3) 관심종목 탭 암호화본 다시 만들기 → 다음 자동 실행 때 사이트 반영(바로 올리려면 아래 두 줄)
./.venv/bin/python secure_publish.py
git add data/secure && git commit -qm "관심종목 백필 반영" && git push -q || true
echo "완료: 사이트 🔒 관심종목 → 과거 등장 지도 '1년' 버튼으로 확인"
