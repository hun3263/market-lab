"""flow_tick.py : 장중 '시장 전체 투자자별 누적 순매수'를 10분마다 한 번씩 받아 쌓습니다. (조회 전용)

왜: 키움 REST 에는 영웅문 '투자자별 매매동향 차트'처럼 시장 전체의 장중 시간대별 추이를 한 번에 주는 API 가 없습니다.
    대신 업종별투자자순매수(ka10051, 코스피·코스닥 종합 행)를 장중에 여러 번 받아 이어 붙이면 같은 모양의 차트가 됩니다.
저장: ~/.market-lab-cache/flows/YYYY-MM-DD.json  {"코스피": [["09:10", 개인, 외국인, 기관], ...], "코스닥": [...]}  (원 단위 누적)
      → 20:00 회차(collect.py nxt)가 이 파일을 읽어 화면의 '장중 수급 흐름'에 시장 전체 차트를 붙입니다.
예약: bash server/add-flow-cron.sh  (평일 09:05~15:35, 10분 간격)
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

import collect

KST = dt.timezone(dt.timedelta(hours=9))
CACHE = Path(os.environ.get("MLAB_CACHE", Path.home() / ".market-lab-cache")) / "flows"


def main():
    now = dt.datetime.now(KST)
    if now.weekday() >= 5 or not ("0900" <= now.strftime("%H%M") <= "1545"):
        return
    os.environ.setdefault("KIWOOM_EXCHANGE", "KRX")
    src = collect.KiwoomSource("close")
    d = now.date()
    if not src.is_open(d):
        return
    f = src.flows(d)                               # {"코스피": {"개인": 원, "외국인": 원, "기관": 원}, ...}
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{d.isoformat()}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    t = now.strftime("%H:%M")
    for mk, v in f.items():
        rows = [r for r in data.get(mk, []) if r[0] != t]
        rows.append([t, v["개인"], v["외국인"], v["기관"]])
        data[mk] = sorted(rows)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"{t} 저장: " + " / ".join(f"{mk} 외국인 {v['외국인'] / 1e8:+,.0f}억" for mk, v in f.items()))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[경고] 장중 수급 수집 실패: {e}", file=sys.stderr)
