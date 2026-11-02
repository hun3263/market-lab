"""riskinfo.py : 필터 공통 위험 점검 — 시가총액 구간 · CB/BW/EB/유상증자 이력(DART) · 진입보류 판정 (조회 전용)

  시가총액   1,500억 미만 → 필터 대상에서 제외
             1,500억~3,000억 → '소형주' 표시
  진입보류   소형주이면서 ① 당일 거래대금 ÷ 시가총액(회전율) 50% 초과  또는  ② 최근 6개월 CB·BW·EB·유상증자 공시
  희석 일정  모든 후보 종목에 최근 6개월 CB·BW·EB·유상증자 공시와 그 안의 날짜(결의·청약·납입·전환/행사 청구 시작·종료) 표시
             → 관심종목 탭 표와 (잠금 해제 후) 일정 탭에 함께 표시

  시가총액 = 종가 × 상장주식수(키움 ka10099, market.py 가 ~/.market-lab-cache/stocklist.json 에 7일마다 저장)
             없으면 종목 파일 basic.cap(키움 ka10001 시가총액) 사용
  DART      list.json(주요사항보고서 B, 최근 6개월) → 해당 공시가 있으면 DS005 상세 API 로 날짜를 읽음
             캐시 ~/.market-lab-cache/dilution/종목코드.json (하루 1번만 새로 받음)
  [팩트] 기준값은 사용자 결정(2026-11-02): 1,500억 제외 / 1,500~3,000억 소형주 / 회전율 50%·6개월 희석 공시 → 진입보류
  [추정] DS005 응답의 날짜 키 이름은 공개 문서로 일부만 확인(BW expd_bgd·expd_edd, sbd, pymd, bddd).
         CB·EB 는 '_bgd/_edd' 로 끝나는 키를 청구기간으로 읽도록 넓게 처리.
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

CACHE = Path(os.environ.get("MLAB_CACHE", Path.home() / ".market-lab-cache"))
CAP_MIN = 1500e8
CAP_SMALL = 3000e8
TURN_MAX = 0.5
DIL_MONTHS = 6

# (DS005 API, 표시 이름, list.json 공시 제목에 들어가는 말)
KINDS = [("cvbdIsDecsn", "CB", "전환사채권발행결정"),
         ("bdwtIsDecsn", "BW", "신주인수권부사채권발행결정"),
         ("exbdIsDecsn", "EB", "교환사채권발행결정"),
         ("pifricDecsn", "유무상증자", "유무상증자결정"),
         ("piicDecsn", "유상증자", "유상증자결정")]
DATE_KEYS = {"bddd": "이사회 결의", "sbd": "청약", "pymd": "납입", "nstk_lstprd": "신주 상장 예정"}
CALENDAR_KEYS = ("납입", "전환·행사 청구 시작", "신주 상장 예정")   # 일정 탭에 올릴 날짜(물량이 풀릴 수 있는 날)


def _date(v):
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", str(v or ""))
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    except ValueError:
        return None


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


class Risk:
    def __init__(self, today, use_dart=True):
        self.today = today if isinstance(today, str) else today.isoformat()
        p = CACHE / "stocklist.json"
        try:
            self.shares = {k: v.get("shares") for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
        except Exception:
            self.shares = {}
        self.dart = None
        key = os.environ.get("DART_API_KEY")
        if use_dart and key:
            try:
                import dart
                self.dart = dart.Dart(key)
            except Exception as e:
                print(f"[riskinfo] DART 준비 실패: {e}", file=sys.stderr)
        self.dir = CACHE / "dilution"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── 시가총액 ──
    def cap(self, code, close, info=None):
        sh = self.shares.get(code)
        if sh and close:
            return close * sh
        b = (info or {}).get("basic") or {}
        return b.get("cap")

    # ── CB/BW/EB/유상증자 이력 ──
    def dilution(self, code):
        path = self.dir / f"{code}.json"
        try:
            c = json.loads(path.read_text(encoding="utf-8"))
            if c.get("day") == self.today:
                return c["rows"]
        except Exception:
            c = None
        if not self.dart:
            return (c or {}).get("rows", [])
        try:
            rows = self._fetch(code)
        except Exception as e:
            print(f"[riskinfo] {code} DART 실패: {e}", file=sys.stderr)
            return (c or {}).get("rows", [])
        path.write_text(json.dumps({"day": self.today, "rows": rows}, ensure_ascii=False), encoding="utf-8")
        return rows

    def _fetch(self, code):
        cc = self.dart.corp_code(code)
        if not cc:
            return []
        end = dt.date.fromisoformat(self.today)
        bgn = (end - dt.timedelta(days=int(DIL_MONTHS * 30.5))).strftime("%Y%m%d")
        lst = self.dart._json("list.json", corp_code=cc, bgn_de=bgn, end_de=end.strftime("%Y%m%d"),
                              pblntf_ty="B", page_count=100) or {}
        hits = []
        for it in lst.get("list", []):
            nm = re.sub(r"\s", "", it.get("report_nm", ""))
            if "무상증자결정" in nm and "유무상" not in nm:
                continue
            for api, label, key in KINDS:
                if key in nm:
                    hits.append((api, label, it))
                    break
        out, detail = [], {}
        for api, label, it in hits:
            if api not in detail:
                d = self.dart._json(api + ".json", corp_code=cc, bgn_de=bgn, end_de=end.strftime("%Y%m%d")) or {}
                detail[api] = {x.get("rcept_no"): x for x in d.get("list", [])}
            x = detail[api].get(it["rcept_no"], {})
            dates = {}
            for k, name in DATE_KEYS.items():
                if _date(x.get(k)):
                    dates[name] = _date(x.get(k))
            for k, v in x.items():                       # 전환·행사·교환 청구기간(…_bgd / …_edd)
                if k.endswith("_bgd") and _date(v):
                    dates["전환·행사 청구 시작"] = _date(v)
                elif k.endswith("_edd") and _date(v):
                    dates["전환·행사 청구 종료"] = _date(v)
            ratio = None
            for k, v in x.items():                       # 주식총수 대비 비율(%) 키
                if k.endswith("tisstk_vs") and _num(v) is not None:
                    ratio = _num(v)
            if ratio is None:
                n, b = _num(x.get("nstk_ostk_cnt") or x.get("piic_nstk_ostk_cnt")), _num(x.get("bfic_tisstk_ostk") or x.get("piic_bfic_tisstk_ostk"))
                if n and b:
                    ratio = round(n / b * 100, 1)
            price = next((_num(x[k]) for k in ("cv_prc", "ex_prc", "ex_prc_ps") if k in x and _num(x[k])), None)
            amt = _num(x.get("bd_fta"))
            out.append({"kind": label, "rcept_dt": _date(it["rcept_dt"]), "title": it.get("report_nm", "").strip(),
                        "rcept_no": it["rcept_no"], "dates": dates, "ratio": ratio, "price": price,
                        "amount억": round(amt / 1e8) if amt else None,
                        "method": x.get("ic_mthn") or x.get("piic_ic_mthn") or x.get("bdis_mthn") or "",
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"})
        out.sort(key=lambda r: r["rcept_dt"] or "", reverse=True)
        return out

    # ── 종합 판정 ──
    def assess(self, code, close, amount=None, info=None):
        """반환: None(1,500억 미만 → 제외) 또는 {cap억, small, turn, dil, hold, why}"""
        cap = self.cap(code, close, info)
        if cap and cap < CAP_MIN:
            return None
        small = bool(cap and cap < CAP_SMALL)
        turn = round(amount / cap, 2) if cap and amount else None
        dil = self.dilution(code)
        why = []
        if small and turn is not None and turn > TURN_MAX:
            why.append(f"회전율 {turn * 100:.0f}% (> {TURN_MAX * 100:.0f}%)")
        if small and dil:
            why.append("6개월 내 " + "·".join(sorted({d['kind'] for d in dil})) + " 공시")
        return {"cap억": round(cap / 1e8) if cap else None, "small": small, "turn": turn,
                "dil": dil, "hold": bool(why), "why": why}


def flags_of(a):
    """필터 결과 'flags'(주의) 목록에 붙일 문구"""
    if not a:
        return []
    f = []
    if a["small"]:
        f.append(f"소형주(시총 {a['cap억']:,}억)")
    if a["hold"]:
        f.append("진입보류: " + ", ".join(a["why"]))
    elif a["dil"]:
        f.append("희석 공시: " + ", ".join(f"{d['kind']} {d['rcept_dt'][5:]}" for d in a["dil"][:3]))
    return f


def calendar_events(rows_by_code, names, today):
    """희석 공시 날짜 → 일정 탭 항목(잠금 해제 후에만 보이는 비공개 일정)"""
    ev, seen = [], set()
    for code, dil in rows_by_code.items():
        for d in dil:
            items = [("공시", d["rcept_dt"])] + [(k, v) for k, v in d["dates"].items() if k in CALENDAR_KEYS]
            for what, day in items:
                key = (code, d["rcept_no"], what)
                if not day or key in seen:
                    continue
                seen.add(key)
                ev.append({"id": f"dil-{code}-{d['rcept_no']}-{what}", "date": day, "end": day,
                           "title": f"{names.get(code, code)} {d['kind']} {what}" + (f" (주식총수 대비 {d['ratio']}%)" if d.get("ratio") else ""),
                           "cat": "관심·희석", "status": "confirmed", "impact": "-", "themes": [],
                           "src": "DART " + d["title"], "url": d["url"], "verified": True, "private": True})
    ev.sort(key=lambda e: e["date"])
    return ev


if __name__ == "__main__":   # 점검: python riskinfo.py 종목코드 [종가]
    r = Risk(dt.date.today())
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    print(json.dumps(r.assess(code, float(sys.argv[2]) if len(sys.argv) > 2 else None), ensure_ascii=False, indent=1))
