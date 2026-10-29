"""v5check.py : 수집 결과에 v5 새 항목이 제대로 들어갔는지 요약해서 보여줍니다(저장·수정 없음).
사용: ./.venv/bin/python server/v5check.py [자료 폴더=data] [날짜=오늘]"""
import datetime as dt
import json
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
day = sys.argv[2] if len(sys.argv) > 2 else (dt.datetime.utcnow() + dt.timedelta(hours=9)).date().isoformat()


def load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  (읽기 실패: {p} {type(e).__name__})")
        return {}


print(f"■ 자료 폴더 {root} · 날짜 {day}")
D = load(root / f"{day}.json")
ss = D.get("sessions", {})
print(f"회차: {list(ss)} · 맨 위 회차 {D.get('latest')}")
for k, v in ss.items():
    ind = v.get("indicators") or {}
    nh, nl = ind.get("newHigh") or {}, ind.get("newLow") or {}
    it = v.get("intraday") or {}
    prog = {m: len(r) for m, r in (it.get("program") or {}).items()}
    big = {m: len(r) for m, r in (it.get("big") or {}).items()}
    print(f"[{k}] 기준 {v.get('asof')} · 신고가 {nh.get('count')}종(목록 {len(nh.get('all', []))}, 기록 {nh.get('days')}일)"
          f" · 신저가 {nl.get('count')}종(목록 {len(nl.get('all', []))}) · 장중 프로그램 {prog or '-'} · 대형주 {big or '-'}")
    rep = sorted(nh.get("all", []), key=lambda s: -(s.get("c20") or 0))[:3]
    if rep:
        print("   반복 신고가:", ", ".join(f"{s['name']} {s.get('c20')}회/연속{s.get('streak')}" for s in rep))
    if v.get("notes"):
        print("   메모:", " | ".join(v["notes"])[:400])

S = load(root / "market" / "series.json")
pl = S.get("perLong") or {}
if pl.get("dates"):
    last = lambda k: next((x for x in reversed(pl[k]) if x is not None), None)
    print(f"지수 PER 3년: {pl['dates'][0]} ~ {pl['dates'][-1]} ({len(pl['dates'])}일) · 마지막 코스피 {last('코스피')}배"
          f"(종목 {pl['n코스피'][-1]}) · 코스닥 {last('코스닥')}배(종목 {pl['n코스닥'][-1]}) · 갱신 {pl.get('updated')}")
    print("  ↳ 참고: 같은 날 키움 PER 조화평균", (S.get("per") or {}).get(day))
else:
    print("지수 PER 3년: 아직 없음(2차 17:30 수집 때 DART로 계산)")
print(f"신고가 기록: {len(S.get('nh', {}))}일 · 신저가 기록: {len(S.get('nl', {}))}일")

H = load(root / "heat" / f"{day}.json")
cols = H.get("cols", [])
if "ttmPer" in cols:
    i, j, f = cols.index("ttmPer"), cols.index("annPer"), cols.index("finPeriod")
    rows = H.get("rows", [])
    got = [r for r in rows if r[i] is not None]
    print(f"히트맵 {len(rows)}종 중 TTM PER 계산 {len(got)}종 · 예:", ", ".join(f"{r[1]} 키움{r[6]}/TTM{r[i]}/연환산{r[j]}({r[f]})" for r in rows[:3]))
else:
    print("히트맵: TTM PER 열 없음(DART 키 확인)")

N = load(root / "news" / f"{day}.json")
for k, e in (N.get("editions") or {}).items():
    items = [it for c in e.get("categories", []) for it in c.get("items", [])]
    print(f"뉴스 {k}: {e.get('label')} · {e.get('from')}~{e.get('to')} · {len(items)}건 · 기사수(k) 표시 {sum('k' in it for it in items)}건")
