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
  [팩트] DS005 응답키는 OpenDART 개발가이드에서 확인(2026-11-02): CB cvrqpd_bgd/edd, BW expd_bgd/edd, EB exrqpd_bgd/edd,
         공통 sbd(청약)·pymd(납입)·bddd(결의)·bd_mtd(만기), 유상증자 piicDecsn 은 ssl_bgd/edd(공매도 관련 기간)뿐 → 납입·상장일은 원문에서 찾음
  점검   python riskinfo.py --check 종목코드   (DART 원본 키와 읽은 날짜 대조)
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
# [팩트] OpenDART 개발가이드 DS005 응답키 (2026-11-02 직접 확인: cvbdIsDecsn·bdwtIsDecsn·exbdIsDecsn·piicDecsn)
DATE_MAP = {
    "CB": {"bddd": "이사회 결의", "sbd": "청약", "pymd": "납입", "cvrqpd_bgd": "전환·행사 청구 시작", "cvrqpd_edd": "전환·행사 청구 종료", "bd_mtd": "사채 만기"},
    "BW": {"bddd": "이사회 결의", "sbd": "청약", "pymd": "납입", "expd_bgd": "전환·행사 청구 시작", "expd_edd": "전환·행사 청구 종료", "bd_mtd": "사채 만기"},
    "EB": {"bddd": "이사회 결의", "sbd": "청약", "pymd": "납입", "exrqpd_bgd": "전환·행사 청구 시작", "exrqpd_edd": "전환·행사 청구 종료", "bd_mtd": "사채 만기"},
    "유상증자": {"ssl_bgd": "공매도 관련 기간 시작", "ssl_edd": "공매도 관련 기간 종료"},
    "유무상증자": {},
}
PRICE_KEY = {"CB": "cv_prc", "BW": "ex_prc", "EB": "ex_prc"}                       # 전환가액·행사가액·교환가액(원/주)
RATIO_KEY = {"CB": "cvisstk_tisstk_vs", "BW": "nstk_isstk_tisstk_vs", "EB": "extg_tisstk_vs"}   # 주식총수 대비 비율(%)
FLOOR_KEY = "act_mktprcfl_cvprc_lwtrsprc"                                             # 시가하락 조정(리픽싱) 최저 조정가액
# 유상증자는 상세 API 에 납입일·상장일이 없음 → 공시 원문(document.xml)에서 찾음 [추정: 서식 문구 기준]
DOC_DATES = [("납입", r"납\s*입\s*일"), ("청약", r"청\s*약\s*(?:예정)?\s*일"), ("신주 상장 예정", r"신주의?\s*상장\s*예정일"), ("발행가 확정", r"(?:확정\s*발행가|발행가액\s*확정)\s*일")]
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
            dates = {name: _date(x.get(k)) for k, name in DATE_MAP.get(label, {}).items() if _date(x.get(k))}
            if label in ("유상증자", "유무상증자"):
                dates.update(self._doc_dates(it["rcept_no"]))
            ratio = _num(x.get(RATIO_KEY.get(label, ""), ""))
            if ratio is None:
                n = _num(x.get("nstk_ostk_cnt") or x.get("piic_nstk_ostk_cnt") or "")
                bb = _num(x.get("bfic_tisstk_ostk") or x.get("piic_bfic_tisstk_ostk") or "")
                if n and bb:
                    ratio = round(n / bb * 100, 1)
            price = _num(x.get(PRICE_KEY.get(label, ""), ""))
            floor = _num(x.get(FLOOR_KEY, ""))
            amt = _num(x.get("bd_fta"))
            out.append({"kind": label, "rcept_dt": _date(it["rcept_dt"]), "title": it.get("report_nm", "").strip(),
                        "rcept_no": it["rcept_no"], "dates": dates, "ratio": ratio, "price": price, "floor": floor,
                        "amount억": round(amt / 1e8) if amt else None,
                        "method": x.get("ic_mthn") or x.get("piic_ic_mthn") or x.get("bdis_mthn") or "",
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"})
        out.sort(key=lambda r: r["rcept_dt"] or "", reverse=True)
        return out

    def _doc_dates(self, rcept_no):
        try:
            import zipfile, io, html as H
            z = zipfile.ZipFile(io.BytesIO(self.dart._get("document.xml", rcept_no=rcept_no)))
            txt = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist())
            txt = re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", txt)))
        except Exception:
            return {}
        out = {}
        for name, pat in DOC_DATES:
            m = re.search(pat + r".{0,40}?(\d{4}\s*[년.\-]\s*\d{1,2}\s*[월.\-]\s*\d{1,2})", txt)
            if m and _date(m.group(1)):
                out[name] = _date(m.group(1))
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
        over = [d for d in dil if d.get("price") and close and close >= d["price"]
                and (d["dates"].get("전환·행사 청구 시작") or "9") <= self.today <= (d["dates"].get("전환·행사 청구 종료") or "0")]
        if small and turn is not None and turn > TURN_MAX:
            why.append(f"회전율 {turn * 100:.0f}% (> {TURN_MAX * 100:.0f}%)")
        if small and dil:
            why.append("6개월 내 " + "·".join(sorted({d['kind'] for d in dil})) + " 공시")
        return {"cap억": round(cap / 1e8) if cap else None, "small": small, "turn": turn,
                "dil": dil, "hold": bool(why), "why": why,
                "overhang": [f"{d['kind']} {d['price']:,.0f}원 ≤ 현재가 · 청구기간 중" for d in over]}


def flags_of(a):
    """필터 결과 'flags'(주의) 목록에 붙일 문구"""
    if not a:
        return []
    f = []
    if a["small"]:
        f.append(f"소형주(시총 {a['cap억']:,}억)")
    f += [f"전환물량 주의: {x}" for x in a.get("overhang", [])]
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


def check(code, months=24):
    """점검: DART 원본 응답과 이 프로그램이 읽은 날짜를 나란히 출력 (캐시 무시)"""
    global DIL_MONTHS
    DIL_MONTHS = months
    r = Risk(dt.date.today())
    if not r.dart:
        raise SystemExit("DART_API_KEY 가 없습니다 (source ~/.market-lab.env 후 실행)")
    cc = r.dart.corp_code(code)
    end = dt.date.today()
    bgn = (end - dt.timedelta(days=int(months * 30.5))).strftime("%Y%m%d")
    print(f"■ {code} 고유번호 {cc} · 기간 {bgn}~{end:%Y%m%d}")
    for api, label, _ in KINDS:
        d = r.dart._json(api + ".json", corp_code=cc, bgn_de=bgn, end_de=end.strftime("%Y%m%d")) or {}
        for x in d.get("list", []):
            keys = list(DATE_MAP.get(label, {})) + [PRICE_KEY.get(label, ""), RATIO_KEY.get(label, ""), FLOOR_KEY]
            print(f"  [{label}] 접수 {x.get('rcept_no')} " + " · ".join(f"{k}={x.get(k)}" for k in keys if k and k in x))
            miss = [k for k in DATE_MAP.get(label, {}) if k not in x]
            if miss:
                print(f"     ⚠ 응답에 없는 키: {miss} → riskinfo.py DATE_MAP 확인 필요")
    rows = r._fetch(code)
    print("■ 프로그램이 읽은 결과")
    for x in rows:
        print(f"  {x['kind']} {x['rcept_dt']} {x['dates']} 비율 {x['ratio']} 가격 {x['price']} 최저조정 {x['floor']}")


if __name__ == "__main__":
    # 점검:  python riskinfo.py --check 종목코드 [개월]     (DART 원본 키·날짜 대조)
    #        python riskinfo.py 종목코드 [종가]              (필터와 같은 판정 결과)
    if len(sys.argv) > 2 and sys.argv[1] == "--check":
        check(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 24)
    else:
        r = Risk(dt.date.today())
        code = sys.argv[1] if len(sys.argv) > 1 else "005930"
        print(json.dumps(r.assess(code, float(sys.argv[2]) if len(sys.argv) > 2 else None), ensure_ascii=False, indent=1))
