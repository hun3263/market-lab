"""bangbae_filter.py : 방배동선수 4가지 매매(재료·시장·이벤트·스윙) 기준으로 '내일 볼 종목'을 고릅니다. (조회·계산 전용)

교안 근거 (방배동선수 강의, 2023 겨울학기)
  매매 4종류 (1강 p34)  이벤트 = 한 달 이내 일정 / 재료 = 발생된 재료 / 중기·스윙 / 시장 = 당일 형성된 테마
  시세 연속성 (2강 p11)  신선함(비교 대상이 없음, 2강 p12)·확장성·연관성 + 시장 분위기
  재료 매매 NEXT DAY (4강 p8)  조정 없이 상승 마감 → 다음날 진입 X(장중 -3%↑면 가능) → 그날 +5%↑면 5일선 대기, +5% 이하면 조정 시 매수
                               조정(3~5%) 있었으면 진입 O.  첫 매수 20~30%, 평균 5구간 (4강 p7)
  시장 매매 당일 조건 (5강 p35) ① 강한 이슈의 집단 테마 형성 첫날 대장 +20%↑ ② 상한가일 때 차순위 +20%↑ ③ 재료 미반영 판단 +15%↑
  이벤트 매매 (3강 p3)  1주 이내 5·10일선, 4주 이내 20일선 / 선반영 종목은 1주 전부터 매수 금지 (3강 p11)
  스윙 체크 (6강 p5)    부채비율·매출채권회전율·현금흐름·오버행 → 여기서는 DART 재무로 부채비율·영업적자만 자동 표시
[추정] 숫자로 바꾼 기준: 재료일(D0) = +10%&1,000억 / +15%&500억 / +20%&300억 중 하나 / D1 '조정' = 장중 저가가 D0 종가 대비 -3% 이하 /
       신선함 = 최근 5거래일 상위 테마에 처음 등장 / 확장성 = 상승 종목 비율 60%↑ / 연관성 = 같은 날 상위 테마와 이름 단어 공유 또는 테마 기사 존재 /
       선반영 = 최근 20거래일 +30% 이상
사용: python bangbae_filter.py --data data --events events.csv --out ~/market-lab-private/bangbae
"""
import argparse
import csv
import datetime as dt
import json
import re
from pathlib import Path

import riskinfo

MAT_MIN = 10.0         # [추정] 재료일 최소 상승률(%)
# [추정] 재료일(D0) = 상승률과 거래대금을 함께 봄(하나만 맞으면 됨). 오를수록 적은 대금도 인정.
#        DR1강 p2 기준봉(+17%&1,000억 / +23%&700억)보다 한 단계 낮춘 '재료 확인' 수준 → 백테스트로 조정
MAT_TIERS = [(10.0, 1000e8), (15.0, 500e8), (20.0, 300e8)]
PRE_RUN = 30.0         # [추정] 선반영 판단(최근 20거래일 수익률 %)
EV_DAYS = 20


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def ma(cl, n):
    return round(sum(cl[-n:]) / n) if len(cl) >= n else None


def tdays(a, b):
    x, n = dt.date.fromisoformat(a), 0
    y = dt.date.fromisoformat(b)
    while x < y:
        x += dt.timedelta(days=1)
        n += x.weekday() < 5
    return n


def fin_flags(info):
    f, out = info.get("fin") or {}, []
    try:
        liab, eq, op = f["liab"][-1], f["equity"][-1], f["op"][-1]
        if eq and liab / eq > 2:
            out.append(f"부채비율 {liab / eq * 100:.0f}%")
        if op is not None and op < 0:
            out.append("영업적자")
    except Exception:
        pass
    return out


def run(data, events, d=None):
    data = Path(data)
    files = sorted(p for p in data.glob("20*.json"))
    if d:
        files = [p for p in files if p.stem <= d]
    d = files[-1].stem
    days = [load(f, {}) for f in files[-6:]]
    today = days[-1]
    stocks = lambda c: load(data / "stocks" / f"{c}.json", {}) or {}

    # ── 시세 연속성: 테마 점수 ──
    seen_before = {t["name"] for x in days[:-1] for t in x.get("themes", []) if isinstance(t.get("stocks"), list)}
    if not any(isinstance(t.get("stocks"), list) for x in days[:-1] for t in x.get("themes", [])):
        seen_before = None                                  # 비교할 과거 테마 자료가 없으면 신선함 판단 보류
    words_today = {t["name"]: set(re.findall(r"[가-힣A-Za-z0-9]{2,}", t["name"])) for t in today.get("themes", []) if isinstance(t.get("stocks"), list)}
    themes = []
    for t in today.get("themes", []):
        if not isinstance(t.get("stocks"), list):
            continue
        fresh = seen_before is not None and t["name"] not in seen_before
        ext = bool(t.get("n")) and t.get("rise", 0) / max(1, t["n"]) >= 0.6
        conn = any(words_today[t["name"]] & w for n, w in words_today.items() if n != t["name"])   # 같은 날 이름이 겹치는 테마가 함께 강함 = 업종 확산
        st = sorted(t["stocks"], key=lambda s: -(s.get("pct") or 0))
        lead, second = (st + [None, None])[:2]
        cond = []
        if fresh and lead and lead["pct"] >= 20:
            cond.append("① 테마 형성 첫날 대장 +20%↑")
        if lead and lead["pct"] >= 29.5 and second and second["pct"] >= 20:
            cond.append("② 상한가 + 차순위 +20%↑")
        themes.append({"name": t["name"], "pct": t.get("pct"), "rise": t.get("rise"), "n": t.get("n"),
                       "fresh": fresh, "ext": ext, "conn": conn, "cont": int(fresh) + int(ext) + int(conn),
                       "lead": lead, "second": second, "market": cond,
                       "news": [{"t": n.get("t"), "u": n.get("u")} for n in (t.get("news") or [])[:2]]})
    themes.sort(key=lambda x: (-x["cont"], -(x["pct"] or 0)))
    theme_of = {}
    for x in days:
        for t in x.get("themes", []):
            if isinstance(t.get("stocks"), list):
                for s in t["stocks"]:
                    theme_of.setdefault(s["code"], set()).add(t["name"])

    # ── 재료 매매: 최근 3거래일 재료일 → 오늘(=다음날 대응 기준) 상태 ──
    mat = []
    for x in days[-3:]:
        for s in x.get("top500", []):
            try:
                pct = float(str(s.get("price", "0")).replace("%", "").replace("+", ""))
            except ValueError:
                continue
            amt = s.get("amtv") or 0
            tier = next(((th, a) for th, a in MAT_TIERS if pct >= th and amt >= a), None)
            if not s.get("code") or pct < MAT_MIN or not tier:
                continue
            info = stocks(s["code"])
            px = [p for p in info.get("prices", []) if p[0] <= d]
            i0 = next((i for i, p in enumerate(px) if p[0] == x["date"]), None)
            if i0 is None:
                continue
            after = px[i0 + 1:]
            p0 = px[i0]
            cl = [p[1] for p in px]
            if not after:                                   # 오늘이 재료일(D0) → 내일(D1) 대응 계획
                plan = "내일 3~5% 조정 시 진입 O / 조정 없이 오르면 진입 X(장중 -3%↑ 밀릴 때만 가능)"
                zone = [round(p0[1] * 0.95), round(p0[1] * 0.97)]
            else:                                           # D1 결과로 판정 (4강 p8 흐름도)
                q = after[0]
                q_lo = q[4] if len(q) > 4 and q[4] else q[1]
                r1 = (q[1] / p0[1] - 1) * 100
                dip = (1 - q_lo / p0[1]) * 100              # D1 장중 최대 하락폭(재료일 종가 대비)
                if dip >= 8:
                    plan = f"D1 장중 -{dip:.1f}% 급락 → 3~5% 조정이 아니라 재료 훼손 의심, 진입 보류 (손절 기준 -5%, 3강 p37)"
                    zone = []
                elif dip >= 3:
                    plan = f"D1 장중 -{dip:.1f}% 조정 발생(진입 구간) · D1 종가 {r1:+.1f}%"
                    zone = [round(p0[1] * 0.95), round(p0[1] * 0.97)]
                elif r1 >= 5:
                    plan, zone = f"D1 조정 없이 +{r1:.1f}% → D2 진입 X, 5일선 대기", [ma(cl, 5)] * 2
                else:
                    plan = f"D1 조정 없이 {r1:+.1f}% → 다음날 조정 시 매수"
                    zone = [round(q[1] * 0.95), round(q[1] * 0.97)]
                if len(after) > 1:
                    plan += f" (이후 {len(after) - 1}거래일 경과, 현재 {(cl[-1] / p0[1] - 1) * 100:+.1f}%)"
            mat.append({"code": s["code"], "name": s["name"], "d0": x["date"], "pct0": pct, "amt0": round(amt / 1e8),
                        "cond": f"+{tier[0]:g}%↑ & 거래대금 {tier[1] / 1e8:,.0f}억↑", "reason": s.get("reason", ""),
                        "news": [{"t": n.get("t"), "u": n.get("u")} for n in (s.get("news") or [])[:2]],
                        "close": cl[-1] if cl else None, "plan": plan, "zone": zone,
                        "themes": sorted(theme_of.get(s["code"], ()))[:3],
                        "flags": fin_flags(info) + (["신규·재상장 의심(일봉 5개 미만)"] if len(px) < 5 else [])})
    best = {}
    for m in mat:                                           # 같은 종목은 가장 최근 재료일만
        if m["code"] not in best or m["d0"] > best[m["code"]]["d0"]:
            best[m["code"]] = m
    mat = sorted(best.values(), key=lambda m: (m["d0"], m["pct0"]), reverse=True)

    # ── 이벤트 매매: 20거래일 안 일정 ──
    evs = []
    if events and Path(events).exists():
        evs += [dict(r, src="events.csv") for r in csv.DictReader(open(events, encoding="utf-8-sig"))]
    cc = data / "calendar" / "events_candidates.csv"
    if cc.exists():
        evs += [dict(r, src="DART·뉴스 후보") for r in csv.DictReader(open(cc, encoding="utf-8-sig")) if r.get("confidence") == "high" and r.get("kind") != "M"]
    cal = load(data / "calendar" / "calendar.json", {}) or {}
    evs += [{"date": x["date"], "code": "", "theme": x.get("themes", ""), "event": x["title"], "kind": "+", "src": "반복 일정"}
            for x in cal.get("events", []) if x.get("themes")]
    ev_rows = []
    for e in evs:
        try:
            n = tdays(d, e["date"])
        except Exception:
            continue
        if e["date"] <= d or n > EV_DAYS or e.get("kind") == "-":
            continue
        codes = [e["code"]] if e.get("code") else [c for c, ts in theme_of.items() if e.get("theme") and any(e["theme"] in t for t in ts)]
        for c in codes[:12]:
            info = stocks(c)
            cl = [p[1] for p in info.get("prices", []) if p[0] <= d]
            if len(cl) < 20:
                continue
            run20 = (cl[-1] / cl[-21] - 1) * 100 if len(cl) > 20 else 0
            line = ("5·10일선", ma(cl, 5), ma(cl, 10)) if n <= 5 else ("20일선", ma(cl, 20), None)
            pre = run20 >= PRE_RUN
            ev_rows.append({"code": c, "name": info.get("name", c), "date": e["date"], "dday": n, "event": e.get("event", ""), "src": e["src"],
                            "close": cl[-1], "line": line[0], "ma": [v for v in line[1:] if v], "run20": round(run20, 1),
                            "plan": ("선반영(20일 +%.0f%%) → 이벤트 1주 전부터 매수 금지, 기대감 없으면 사전 매도" % run20) if pre
                            else f"{line[0]} 근처 분할 매수 대기 · 이벤트 전 매도",
                            "flags": fin_flags(info)})
    ev_rows.sort(key=lambda r: (r["dday"], r["name"]))

    # ── 공통: 시총 1,500억 미만 제외 · 소형주/진입보류 · CB·BW·유증 일자 (riskinfo.py) ──
    risk, capout = riskinfo.Risk(d), []

    def screen(rows):
        out = []
        for r in rows:
            info = stocks(r["code"])
            px = [p for p in info.get("prices", []) if p[0] <= d]
            a = risk.assess(r["code"], r.get("close") or (px[-1][1] if px else None), px[-1][2] if px else None, info)
            if a is None:
                capout.append(r["name"])
                continue
            r["flags"] = list(r.get("flags") or []) + riskinfo.flags_of(a)
            r.update({"cap": a["cap억"], "small": a["small"], "hold": a["hold"], "dil": a["dil"]})
            out.append(r)
        return out

    mat, ev_rows = screen(mat), screen(ev_rows)
    return {"date": d, "themes": themes, "material": mat[:30], "event": ev_rows[:40],
            "capOut": {"min억": int(riskinfo.CAP_MIN / 1e8), "n": len(set(capout)), "names": sorted(set(capout))[:30]},
            "rules": "방배동 1강 p34 · 2강 p11 · 3강 p3·p11 · 4강 p7·p8 · 5강 p35 · 6강 p5 (숫자 기준은 [추정], 파일 머리말 참고)"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--events", default="events.csv")
    ap.add_argument("--date")
    ap.add_argument("--out")
    a = ap.parse_args()
    res = run(a.data, a.events, a.date)
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / f"{res['date']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"■ {res['date']} 방배동 후보 · 테마 {len(res['themes'])} · 재료 {len(res['material'])} · 이벤트 {len(res['event'])}")
    for t in res["themes"][:5]:
        print(f"  테마 {t['name']} 연속성 {t['cont']}/3 (신선 {int(t['fresh'])}·확장 {int(t['ext'])}·연관 {int(t['conn'])}) {('· ' + ', '.join(t['market'])) if t['market'] else ''}")
    for m in res["material"][:8]:
        print(f"  재료 {m['name']} {m['d0'][5:]} +{m['pct0']:.0f}% → {m['plan']} | 구간 {m['zone']}")
    for e in res["event"][:8]:
        print(f"  이벤트 D-{e['dday']} {e['name']} {e['event'][:24]} → {e['plan']}")
