"""calendar_build.py : 사이트 '🗓 증시 일정' 탭이 읽는 data/calendar/calendar.json 을 만듭니다. (네트워크 없음, 몇 초)

합치는 자료
  ① calendar_recurring.json  매년·매달 반복 일정(규칙 + 과거 날짜 + 주최측 발표 날짜)
  ② calendar_base.csv        1년에 한 번 붙여 넣는 확정 일정(kind=M 등)
  ③ events.csv               최+신+일 '일' 일정(사람이 확정한 것)
  ④ data/calendar/events_candidates.csv  자동 수집 후보 중 confidence=high 만
과거 반응: 반복 일정의 과거 발생 구간에 대해 코스피·코스닥 등락률(시작 전날 종가 → 종료 다음 날 종가)을
           data/market/series.json 지수 일봉으로 계산해 붙입니다(자료가 있는 기간만).
사용: python calendar_build.py --data data   (run.sh 의 news-am·nxt 회차 뒤에 실행)
"""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
D = dt.date.fromisoformat


def nth_weekday(y, m, n, wd):
    first = dt.date(y, m, 1)
    x = first + dt.timedelta(days=(wd - first.weekday()) % 7)
    return x + dt.timedelta(days=7 * (n - 1))


def expand(item, lo, hi):
    """반복 일정 → [(start, end, status)] status: confirmed(발표·확정) / rule(규칙 계산) / estimated(과거 날짜로 추정)"""
    out, dur = [], max(1, int(item.get("dur", 1)))
    r = item["rule"]
    if r == "list":
        for x in item.get("dates", []):
            s = D(x[0])
            e = D(x[1]) if len(x) > 1 else s
            out.append((s, e, "confirmed"))
    elif r in ("nth_weekday", "fixed"):
        for y in range(lo.year, hi.year + 1):
            for m in (item.get("months") or [item.get("month")]):
                s = (nth_weekday(y, m, item["n"], item["wd"]) if r == "nth_weekday" else dt.date(y, m, item["day"]))
                s += dt.timedelta(days=item.get("offset", 0))
                out.append((s, s + dt.timedelta(days=dur - 1), "rule"))
    elif r == "hist":
        hist = {int(k): (D(v[0]), D(v[1])) for k, v in item.get("hist", {}).items()}
        for s, e in hist.values():
            out.append((s, e, "confirmed"))
        nxt = item.get("next")
        if nxt:
            out.append((D(nxt[0]), D(nxt[1]), "confirmed"))
        last = max([v for v in hist.values()] + ([(D(nxt[0]), D(nxt[1]))] if nxt else []), default=None)
        if last:                                   # 다음 해 추정: 같은 요일·같은 주 근처(52주 뒤)
            s, e = last
            while e < hi:
                ps = s
                s, e = s + dt.timedelta(weeks=52), e + dt.timedelta(weeks=52)
                try:                                    # 52주 뒤가 작년 같은 날짜보다 4일 넘게 이르면 1주 미룸
                    if (s - ps.replace(year=s.year)).days < -3:
                        s, e = s + dt.timedelta(weeks=1), e + dt.timedelta(weeks=1)
                except ValueError:
                    pass
                out.append((s, e, "estimated"))
    return [o for o in out if lo <= o[1] and o[0] <= hi]


def index_move(series, s, e):
    """시작 전날 종가 → 종료 다음 거래일 종가 등락률(%)"""
    res = {}
    for name, rows in (series.get("index") or {}).items():
        before = [r for r in rows if r[0] < s.isoformat()]
        after = [r for r in rows if r[0] > e.isoformat()]
        if before and after and (D(after[0][0]) - e).days <= 7:
            res[name] = round((after[0][1] / before[-1][1] - 1) * 100, 2)
    return res


def dup_of_recurring(ev, s, title):
    """같은 날 반복 일정과 핵심 단어가 겹치면 중복으로 봄 (예: 'MSCI 11월 정기변경 발표' ↔ 'MSCI 정기변경 발표')"""
    import re
    stop = {"미국", "한국", "중국", "발표", "결정", "결과", "예정", "현지", "한국시간", "확인", "필요", "관례"}
    words = {w for w in re.findall(r"[가-힣A-Za-z]{2,}", title)} - stop
    for x in ev:
        if x["recurring"] and D(x["date"]) == s:
            if words & set(re.findall(r"[가-힣A-Za-z]{2,}", x["title"])):
                return True
    return False


def read_csv(path):
    if not path or not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--recurring", default="calendar_recurring.json")
    ap.add_argument("--base", default="calendar_base.csv")
    ap.add_argument("--events", default="events.csv")
    a = ap.parse_args()
    today = dt.datetime.now(KST).date()
    lo, hi = today - dt.timedelta(days=730), today + dt.timedelta(days=400)
    data = Path(a.data)
    try:
        series = json.loads((data / "market" / "series.json").read_text(encoding="utf-8"))
    except Exception:
        series = {}
    rec = json.loads(Path(a.recurring).read_text(encoding="utf-8"))
    ev = []
    for it in rec["items"]:
        occ = expand(it, lo, hi)
        past = []
        for s, e, st in sorted(occ):
            if e < today:
                past.append({"start": s.isoformat(), "end": e.isoformat(), "move": index_move(series, s, e)})
        for s, e, st in occ:
            ev.append({"id": it["id"], "date": s.isoformat(), "end": e.isoformat(), "title": it["name"], "cat": it["cat"],
                       "status": st, "recurring": True, "impact": it.get("impact", ""), "themes": it.get("themes", ""),
                       "src": it.get("src", ""), "url": it.get("url", ""),
                       "verified": not it.get("src", "").startswith("기억")})
        # 반복 일정별 과거 기록은 한 번만 따로 저장(화면 툴팁용)
        it["_past"] = past
    seen = {(x["date"], x["title"]) for x in ev}
    for path, origin in ((a.base, "base"), (a.events, "events"), (data / "calendar" / "events_candidates.csv", "candidates")):
        for r in read_csv(path):
            if origin == "candidates" and r.get("confidence") != "high":
                continue
            try:
                s = D(r["date"])
            except Exception:
                continue
            if not (lo <= s <= hi):
                continue
            title = r.get("event", "")
            if (s.isoformat(), title) in seen or dup_of_recurring(ev, s, title):
                continue
            kind = r.get("kind", "")
            cat = "매크로·금리" if kind == "M" else ("종목 일정(악재)" if kind == "-" else "종목·테마 일정")
            ev.append({"id": "", "date": s.isoformat(), "end": s.isoformat(), "title": title, "cat": cat,
                       "status": "confirmed" if origin != "candidates" else "candidate", "recurring": False,
                       "impact": "", "themes": r.get("theme", ""), "code": r.get("code", ""),
                       "src": r.get("source", ""), "url": r.get("url", ""), "verified": True, "origin": origin})
            seen.add((s.isoformat(), title))
    ev.sort(key=lambda x: (x["date"], x["cat"], x["title"]))
    past = {it["id"]: it["_past"] for it in rec["items"]}
    out = {"generated": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "today": today.isoformat(),
           "range": [lo.isoformat(), hi.isoformat()], "events": ev, "past": past,
           "cats": sorted({x["cat"] for x in ev})}
    (data / "calendar").mkdir(parents=True, exist_ok=True)
    (data / "calendar" / "calendar.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    up = [x for x in ev if today.isoformat() <= x["date"] <= (today + dt.timedelta(days=30)).isoformat()]
    print(f"일정 {len(ev)}건 저장(반복 {sum(x['recurring'] for x in ev)} · 앞으로 30일 {len(up)}) → {data / 'calendar' / 'calendar.json'}")


if __name__ == "__main__":
    main()
