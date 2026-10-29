"""market.py : 날짜마다 쌓아 가는 '시장 시계열'과 '히트맵·상대강도(RS)' 자료를 만듭니다. collect.py 가 부릅니다.

만드는 파일
  data/market/series.json   지수 일봉(종가·거래대금), 투자자별 일별 순매수, 상승·하락 종목 수, 대차잔고, 지수 PER(추정)
                            → 화면이 이 파일 하나로 RSI·ADR·누적 수급·거래대금 통계 차트를 그립니다. 매 회차 덮어씀(누적 보존).
  data/heat/날짜.json        시가총액 상위 300 종목(히트맵·RS·PER·업종 PER). 회차마다 덮어씀.

키움 API (조회 전용)
  ka20006 업종일봉   지수 종가·거래대금 (단위는 ka20003 오늘 값과 비교해 자동 판정)
  ka10051 업종별투자자순매수(기준일자 지정) → 6개월 백필 1회, 이후 매일 1일 추가
  ka10068 대차거래추이(기간)   ka10099 종목 리스트(상장주식수·업종명)   ka20002 업종별주가(전 종목 등락률)
  ka10001 주식기본정보(PER·PBR·ROE)   ka10081 일봉(RS 계산용 1년 수익률)

계산 기준
  RS(IBD식): 점수 = 2×3개월 + 6개월 + 9개월 + 12개월 수익률(63·126·189·252거래일), 유니버스 안 백분위 1~99
  지수·업종 PER(추정): Σ시가총액 ÷ Σ(시가총액/PER), PER>0(흑자) 종목만. KRX 공식 지수 PER 과 다를 수 있음
"""
import datetime as dt
import json
import math
import os
import sys
import time
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
UNIVERSE_N = int(os.environ.get("UNIVERSE_N", "300"))
FLOW_BACKFILL_DAYS = 130            # 약 6개월
CACHE = Path(os.environ.get("MLAB_CACHE", Path.home() / ".market-lab-cache"))
MARKETS = (("코스피", "0", "001"), ("코스닥", "1", "101"))


def _num(s):
    return abs(float(str(s).replace(",", "").replace("+", "").strip() or 0))


def _snum(s):
    return float(str(s).replace(",", "").replace("+", "").strip() or 0)


def _ymd(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _unit(raw, known_won):
    """원본 숫자(raw)와 원 단위 기준값(known_won)을 비교해 1·천·백만·억 중 맞는 배수를 고릅니다."""
    if not raw or not known_won:
        return None
    r = known_won / raw
    return min((1, 1e3, 1e6, 1e8), key=lambda c: abs(math.log10(r) - math.log10(c)))


def is_common(code, name):
    """보통주만(우선주·스팩·ETF·ETN·리츠 제외). 우선주는 종목코드 끝자리가 0 이 아님."""
    from collect import is_etf
    n = name or ""
    return (code[-1] == "0" and not is_etf(n) and "스팩" not in n and "기업인수목적" not in n
            and not n.endswith("리츠") and "ETN" not in n)


# ───────────────────────── 키움에서 받기 ─────────────────────────
class KiwoomMarket:
    def __init__(self, src):
        self.s = src

    def index_daily(self, d, known):
        """{"코스피": [[날짜, 종가, 거래대금(원)], ...오래된 순]}. known: {"코스피": 오늘 지수 거래대금(원)}"""
        out, notes = {}, []
        for name, _, code in MARKETS:
            rows = self.s._rows(self.s._call("/api/dostk/chart", "ka20006",
                                             {"inds_cd": code, "base_dt": d.strftime("%Y%m%d")}, pages=2), "inds_dt_pole_qry")
            rows = sorted((r for r in rows if r.get("dt")), key=lambda r: r["dt"])[-300:]
            today = next((r for r in rows if r["dt"] == d.strftime("%Y%m%d")), None)
            unit = _unit(_num(today["trde_prica"]), known.get(name)) if today else None
            if unit is None:
                unit = 1e6
                notes.append(f"{name} 지수 일봉 거래대금 단위를 판정하지 못해 백만원으로 가정")
            # 업종 지수 가격은 소수점 2자리를 100배한 정수로 오는 경우가 있어 오늘 지수와 비교해 맞춤
            px_div = 1.0
            if today and known.get(name + "_close"):
                ratio = _num(today["cur_prc"]) / known[name + "_close"]
                px_div = 100.0 if 50 < ratio < 200 else 1.0
            out[name] = [[_ymd(r["dt"]), round(_num(r["cur_prc"]) / px_div, 2), int(_num(r["trde_prica"]) * unit)]
                         for r in rows]
        return out, notes

    def flows_on(self, d):
        """기준일 d 의 {"코스피": {개인, 외국인, 기관}, ...} (원). KRX 기준(일별 시계열 일관성)."""
        res = {}
        for name, mk, code in MARKETS:
            rows = self.s._rows(self.s._call("/api/dostk/sect", "ka10051", {
                "mrkt_tp": mk, "amt_qty_tp": "0", "stex_tp": "1", "base_dt": d.strftime("%Y%m%d")}), "inds_netprps")
            r = next((x for x in rows if str(x.get("inds_cd", "")).startswith(code)), None)
            if r is None:
                return None
            e = 100_000_000
            res[name] = {"개인": int(_snum(r["ind_netprps"]) * e), "외국인": int(_snum(r["frgnr_netprps"]) * e),
                         "기관": int(_snum(r["orgn_netprps"]) * e)}
        return res

    def lending_series(self, d, days=200):
        rows = self.s._rows(self.s._call("/api/dostk/slb", "ka10068", {
            "all_tp": "1", "strt_dt": (d - dt.timedelta(days=days)).strftime("%Y%m%d"),
            "end_dt": d.strftime("%Y%m%d")}, pages=3), "dbrt_trde_trnsn")
        out = {}
        for r in rows:
            if r.get("dt") and _num(r.get("rmnd")) > 0:  # 당일 행은 공표 전이라 0
                out[_ymd(r["dt"])] = [int(_num(r["rmnd"])), int(_num(r["remn_amt"]) * 1_000_000),
                                      int(_snum(r.get("dbrt_trde_irds") or 0))]
        return [[k] + v for k, v in sorted(out.items())]

    def stock_list(self):
        """전 종목 {code: {name, shares, sector, market}} — 7일 캐시"""
        path = CACHE / "stocklist.json"
        if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
            return load_json(path, {})
        m = {}
        for label, mk in (("코스피", "0"), ("코스닥", "10")):
            rows = self.s._rows(self.s._call("/api/dostk/stkinfo", "ka10099", {"mrkt_tp": mk}, pages=5), "list")
            for r in rows:
                if r.get("code"):
                    m[r["code"]] = {"name": r.get("name", ""), "shares": int(_num(r.get("listCount") or 0)),
                                    "sector": (r.get("upName") or "기타").strip() or "기타", "market": label,
                                    "state": r.get("state", "")}
        save_json(path, m)
        return m

    def all_prices(self):
        """전 종목 {code: (현재가, 등락률, 전일대비)} — ka20002 종합 업종"""
        out = {}
        for _, mk, code in MARKETS:
            rows = self.s._rows(self.s._call("/api/dostk/sect", "ka20002",
                                             {"mrkt_tp": mk, "inds_cd": code, "stex_tp": self.s.exchange}, pages=25),
                                "inds_stkpc")
            for r in rows:
                c = r["stk_cd"].split("_")[0]
                sig = r.get("pred_pre_sig")
                p = _snum(r.get("flu_rt") or 0)
                ch = _snum(r.get("pred_pre") or 0)
                if sig in ("4", "5"):
                    p, ch = -abs(p), -abs(ch)
                out[c] = (int(_num(r["cur_prc"])), p, int(ch))
        return out

    def basic(self, code):
        r = self.s._call("/api/dostk/stkinfo", "ka10001", {"stk_cd": code})[0]

        def f(k):
            v = str(r.get(k, "")).replace(",", "").replace("+", "").strip()
            try:
                return float(v) if v not in ("", "-") else None
            except ValueError:
                return None
        return {"per": f("per"), "pbr": f("pbr"), "roe": f("roe"), "eps": f("eps"), "bps": f("bps"),
                "mac": f("mac"), "hi250": f("250hgst"), "lo250": f("250lwst"), "for": f("for_exh_rt")}

    def closes(self, code, d):
        return [c for _, c, _ in self.s.history(code, d, n=260)]

    def closes_long(self, code, d):
        """{날짜: 수정종가} 약 3년. 캐시(CACHE/px/코드.json)가 짧으면 2페이지(약 1,200거래일), 아니면 1페이지만 받아 합침"""
        path = CACHE / "px" / f"{code}.json"
        px = load_json(path, {})
        pages = 2 if len(px) < 700 else 1
        rows = self.s._rows(self.s._call("/api/dostk/chart", "ka10081",
                                         {"stk_cd": code, "base_dt": d.strftime("%Y%m%d"), "upd_stkpc_tp": "1"}, pages=pages),
                            "stk_dt_pole_chart_qry")
        new = {_ymd(r["dt"]): int(_num(r["cur_prc"])) for r in rows if r.get("dt") and r.get("cur_prc")}
        if new and px:
            # 수정주가가 바뀌었으면(액면분할 등) 겹치는 날 비율로 옛 값 보정
            common = [k for k in new if k in px and px[k]]
            if common:
                k = max(common)
                ratio = new[k] / px[k]
                if abs(ratio - 1) > 0.01:
                    px = {a: round(b * ratio) for a, b in px.items()}
        px.update(new)
        cut = (d - dt.timedelta(days=1200)).isoformat()
        px = {a: b for a, b in sorted(px.items()) if a >= cut}
        save_json(path, px)
        return px

    def program_intraday(self, d):
        """프로그램 매매 분별 누적 순매수(백만원 → 원) {시장: [[시각, 전체, 차익, 비차익], ...]} — ka90005"""
        out = {}
        for name, code in (("코스피", "P00101"), ("코스닥", "P10102")):
            rows = self.s._rows(self.s._call("/api/dostk/mrkcond", "ka90005", {
                "date": d.strftime("%Y%m%d"), "amt_qty_tp": "1", "mrkt_tp": code, "min_tic_tp": "1", "stex_tp": "1"},
                pages=6), "prm_trde_trnsn")
            pts = {}
            for r in rows:
                t = str(r.get("cntr_tm", ""))[:4]
                if len(t) == 4 and "0900" <= t <= "1535":
                    pts[t] = [f"{t[:2]}:{t[2:]}", int(_snum(r.get("all_netprps") or 0) * 1e6),
                              int(_snum(r.get("dfrt_trde_netprps") or 0) * 1e6), int(_snum(r.get("ndiffpro_trde_netprps") or 0) * 1e6)]
            out[name] = [pts[k] for k in sorted(pts)]
        return out

    def investor_intraday(self, code):
        """대형주 장중 투자자 누적 순매수 [[시각, 외국인, 기관]] — ka10064(종목 단위, 개인 항목 없음). 금액 단위는 백만원으로 추정"""
        rows = self.s._rows(self.s._call("/api/dostk/chart", "ka10064", {
            "mrkt_tp": "000", "amt_qty_tp": "1", "trde_tp": "0", "stk_cd": code}, pages=3), "opmr_invsr_trde_chart")
        pts = {}
        for r in rows:
            t = str(r.get("tm", ""))[:4]
            if len(t) == 4 and "0900" <= t <= "1535":
                pts[t] = [f"{t[:2]}:{t[2:]}", int(_snum(r.get("frgnr_invsr") or 0) * 1e6), int(_snum(r.get("orgn") or 0) * 1e6)]
        return [pts[k] for k in sorted(pts)]


class MockMarket:
    """네트워크 없이 화면을 점검하기 위한 가짜 시계열(실제 시장과 무관)"""
    SECTORS = ["반도체", "전기전자", "금융", "운수장비", "화학", "제약", "서비스업", "철강금속", "유통", "건설", "기계", "통신"]

    def __init__(self, src):
        self.s = src
        import random
        self.r = random.Random(7)

    def _days(self, d, n):
        out, x = [], d
        while len(out) < n:
            if x.weekday() < 5:
                out.append(x)
            x -= dt.timedelta(days=1)
        return sorted(out)

    def index_daily(self, d, known):
        out = {}
        for name, base, amt in (("코스피", 3000.0, 12e12), ("코스닥", 900.0, 6e12)):
            px, rows = base * 0.8, []
            for x in self._days(d, 260):
                px *= 1 + self.r.uniform(-0.018, 0.02)
                rows.append([x.isoformat(), round(px, 2), int(amt * self.r.uniform(0.6, 1.5))])
            out[name] = rows
        return out, []

    def flows_on(self, d):
        r = self.r
        return {mk: {"개인": int(r.uniform(-1, 1) * 8e11), "외국인": int(r.uniform(-1, 1) * 9e11),
                     "기관": int(r.uniform(-1, 1) * 5e11)} for mk in ("코스피", "코스닥")}

    def lending_series(self, d, days=200):
        sh, out = 3.0e9, []
        for x in self._days(d - dt.timedelta(days=1), 120):
            net = int(self.r.uniform(-3e7, 4e7))
            sh += net
            out.append([x.isoformat(), int(sh), int(sh * 42000), net])
        return out

    def stock_list(self):
        m = {}
        for i in range(1, 361):
            code = f"{100000 + i * 7:06d}"[:-1] + "0"
            m[code] = {"name": f"샘플{i:03d}", "shares": int(1e8 / (i ** 0.9)) + 1_000_000,
                       "sector": self.SECTORS[i % len(self.SECTORS)], "market": "코스피" if i % 3 else "코스닥", "state": ""}
        m["005930"] = {"name": "삼성전자", "shares": 5_900_000_000, "sector": "전기전자", "market": "코스피", "state": ""}
        m["000660"] = {"name": "SK하이닉스", "shares": 728_000_000, "sector": "전기전자", "market": "코스피", "state": ""}
        return m

    def all_prices(self):
        out = {}
        for c in self.stock_list():
            p = round(self.r.gauss(0.5, 2.6), 2)
            px = int(self.r.uniform(5000, 300000))
            out[c] = (px, p, int(px * p / 100))
        out["005930"], out["000660"] = (70000, 1.1, 760), (140000, -1.2, -1700)
        return out

    def basic(self, code):
        per = round(self.r.uniform(-15, 45), 1)
        return {"per": per if abs(per) > 2 else None, "pbr": round(self.r.uniform(0.4, 4), 2),
                "roe": round(self.r.uniform(-10, 25), 1), "eps": 1000, "bps": 20000, "mac": None,
                "hi250": None, "lo250": None, "for": round(self.r.uniform(2, 50), 1)}

    def closes(self, code, d):
        return list(self.closes_long(code, d).values())[-260:]

    def closes_long(self, code, d):
        import random
        rr = random.Random(code)
        px, out = 10000.0, {}
        drift = rr.uniform(-0.0005, 0.0012)
        for x in self._days(d, 800):
            px *= 1 + drift + rr.uniform(-0.022, 0.022)
            out[x.isoformat()] = round(px)
        return out

    def program_intraday(self, d):
        out = {}
        for m in ("코스피", "코스닥"):
            v, a, b, pts = 0, 0, 0, []
            for i in range(0, 391, 5):
                t = 9 * 60 + i
                da, db = self.r.gauss(0, 2e9), self.r.gauss(3e8, 6e9)
                a, b = a + da, b + db
                pts.append([f"{t // 60:02d}:{t % 60:02d}", int(a + b), int(a), int(b)])
            out[m] = pts
        return out

    def investor_intraday(self, code):
        f, o, pts = 0, 0, []
        for i in range(0, 391, 5):
            t = 9 * 60 + i
            f += self.r.gauss(2e8, 3e9)
            o += self.r.gauss(-1e8, 2e9)
            pts.append([f"{t // 60:02d}:{t % 60:02d}", int(f), int(o)])
        return pts


def backend(src):
    # collect.py 가 직접 실행되면 모듈 이름이 __main__ 이라 isinstance 대신 클래스 이름으로 판별
    return MockMarket(src) if type(src).__name__ == "MockSource" else KiwoomMarket(src)


# ───────────────────────── 계산 ─────────────────────────
def rs_scores(closes_by_code):
    """IBD식 RS: 2×r63 + r126 + r189 + r252 (데이터가 짧으면 가능한 구간만), 백분위 1~99"""
    raw = {}
    for c, cl in closes_by_code.items():
        if len(cl) < 127:
            continue
        def ret(n):
            return cl[-1] / cl[-1 - n] - 1 if len(cl) > n and cl[-1 - n] else None
        parts = [(2, ret(63)), (1, ret(126)), (1, ret(189)), (1, ret(252))]
        parts = [(w, r) for w, r in parts if r is not None]
        raw[c] = {"score": sum(w * r for w, r in parts) / sum(w for w, _ in parts) * 5,
                  "r3": ret(63), "r6": ret(126), "r12": ret(252)}
    order = sorted(raw, key=lambda c: raw[c]["score"])
    n = len(order)
    for i, c in enumerate(order):
        raw[c]["rs"] = max(1, min(99, int(round((i + 1) / n * 99)))) if n else None
    return raw


def harmonic_per(items):
    """Σ시총 ÷ Σ(시총/PER), PER>0 만. (흑자 종목 기준 추정 PER, 표본 수)"""
    pos = [(cap, per) for cap, per in items if per and per > 0 and cap]
    if not pos:
        return None, 0
    return round(sum(c for c, _ in pos) / sum(c / p for c, p in pos), 2), len(pos)


def amount_stats(rows, today):
    """지수 거래대금: 오늘 vs 전일·1주(5)·1달(20)·6개월(120)·12개월(250) 최고(오늘 제외)"""
    past = [a for t, _, a in rows if t < today]
    tod = next((a for t, _, a in rows if t == today), None)
    if tod is None or not past:
        return None
    out = {"today": tod, "prev": past[-1]}
    for k, n in (("w1", 5), ("m1", 20), ("m6", 120), ("y1", 250)):
        out[k] = max(past[-n:])
    return out


# ───────────────────────── 회차마다 실행 ─────────────────────────
def update(src, d, session, out_dir, idx, breadth, notes):
    """series.json 갱신 + 히트맵 유니버스 생성. 반환: (series 요약 dict, heat dict 또는 None)"""
    mk = backend(src)
    path = Path(out_dir) / "market" / "series.json"
    ser = load_json(path, {})
    ser.setdefault("index", {})
    ser.setdefault("flows", {})
    ser.setdefault("breadth", {})
    ser.setdefault("per", {})
    today = d.isoformat()
    log = []

    def safe(label, fn, default=None):
        try:
            return fn()
        except Exception as e:
            notes.append(f"수집 실패: {label} ({type(e).__name__})")
            print(f"[경고] {label} 실패: {e}", file=sys.stderr)
            return default

    # 1) 지수 일봉
    known = {}
    for i in idx:
        known[i["name"]] = i["amount"]
        known[i["name"] + "_close"] = i["close"]
    got = safe("지수 일봉", lambda: mk.index_daily(d, known))
    if got:
        ser["index"], n2 = got
        notes.extend(n2)

    # 2) 상승·하락 종목 수 (ADR 용, 오늘 값 덮어쓰기). 처음에는 이미 저장된 날짜 파일에서 채움
    for f in sorted(Path(out_dir).glob("20*.json")):
        if f.stem in ser["breadth"] or f.stem >= today:
            continue
        dd = load_json(f, {})
        sn = (dd.get("sessions") or {}).get("close") or (dd.get("sessions") or {}).get(dd.get("latest") or "", {})
        b0 = (sn or {}).get("breadth")
        if b0:
            ser["breadth"][f.stem] = {m: [b["up"], b["down"], b["flat"]] for m, b in b0.items()}
    if breadth:
        ser["breadth"][today] = {m: [b["up"], b["down"], b["flat"]] for m, b in breadth.items()}

    # 3) 투자자별 일별 순매수: 오늘(KRX 정규장 마감 이후 회차) + 6개월 백필(없는 날짜만)
    if session in ("close", "nxt"):
        f = safe("투자자 일별(오늘)", lambda: mk.flows_on(d))
        if f:
            ser["flows"][today] = f
        days = [r[0] for r in ser["index"].get("코스피", [])][-FLOW_BACKFILL_DAYS:]
        missing = [x for x in days if x not in ser["flows"] and x < today]
        if missing:
            done, same = 0, 0
            for x in reversed(missing):  # 최근부터
                v = safe(f"투자자 일별 {x}", lambda x=x: mk.flows_on(dt.date.fromisoformat(x)))
                if not v:
                    break
                if f and v == f:  # 기준일이 무시되고 오늘 값이 오면 중단
                    same += 1
                    if same >= 2:
                        notes.append("ka10051 기준일자 지정이 반영되지 않아 과거 수급 백필을 중단했습니다.")
                        for k in [k for k, vv in ser["flows"].items() if vv == f and k != today]:
                            del ser["flows"][k]
                        break
                ser["flows"][x] = v
                done += 1
            log.append(f"투자자 일별 백필 {done}일")

    # 4) 대차잔고 추이
    lend = safe("대차잔고 추이", lambda: mk.lending_series(d))
    if lend:
        ser["lending"] = lend

    # 5) 히트맵·RS 유니버스
    heat = safe("히트맵 유니버스", lambda: build_universe(mk, d, session, out_dir, log))
    if heat:
        for m, v in heat["indexPer"].items():
            if v["per"]:
                ser["per"].setdefault(today, {})[m] = v["per"]

    # 오래된 키 정리(1년 반)
    cut = (d - dt.timedelta(days=550)).isoformat()
    for key in ("flows", "breadth", "per", "nh", "nl"):
        if key in ser:
            ser[key] = {k: v for k, v in sorted(ser[key].items()) if k >= cut}
    ser["updated"] = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    save_json(path, ser)

    stats = {m: amount_stats(rows, today) for m, rows in ser["index"].items()}
    if log:
        print(" · ".join(log))
    return {"stats": stats, "lendingLast": (ser.get("lending") or [None])[-1]}, heat


def build_universe(mk, d, session, out_dir, log):
    """시총 상위 N 종목. 무거운 PER·RS 계산은 2차(close) 또는 캐시가 없을 때만, 나머지 회차는 가격만 갱신."""
    cache_path = CACHE / "universe.json"
    cache = load_json(cache_path, {})
    full = session == "close" or not cache.get("basics")  # 2차에 무겁게 1번, 나머지 회차는 캐시 재사용
    lst = mk.stock_list()
    prices = mk.all_prices()
    rows = []
    for code, (px, p, ch) in prices.items():
        info = lst.get(code)
        if not info or not px or not is_common(code, info["name"]) or "정지" in info.get("state", ""):
            continue
        rows.append({"code": code, "name": info["name"], "market": info["market"], "sector": info["sector"],
                     "price": px, "pct": p, "chg": ch, "cap": px * info["shares"]})
    rows.sort(key=lambda r: -r["cap"])
    rows = rows[:UNIVERSE_N]

    if full:
        t0 = time.time()
        basics, closes = {}, {}
        for r in rows:
            try:
                basics[r["code"]] = mk.basic(r["code"])
            except Exception as e:
                print(f"[경고] 기본정보 {r['name']}: {e}", file=sys.stderr)
            try:
                closes[r["code"]] = list(mk.closes_long(r["code"], d).values())[-260:]
            except Exception as e:
                print(f"[경고] 일봉 {r['name']}: {e}", file=sys.stderr)
        rs = rs_scores(closes)
        cache = {"date": d.isoformat(), "session": session, "basics": basics,
                 "rs": {c: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()} for c, x in rs.items()}}
        log.append(f"유니버스 {len(rows)}종목 PER·RS 계산 {time.time() - t0:.0f}초")
    cache["rows"] = [r["code"] for r in rows]
    cache["shares"] = {r["code"]: lst[r["code"]]["shares"] for r in rows}
    cache["market"] = {r["code"]: r["market"] for r in rows}
    save_json(cache_path, cache)

    basics, rs = cache.get("basics", {}), cache.get("rs", {})
    out = []
    for r in rows:
        b, x = basics.get(r["code"], {}), rs.get(r["code"], {})
        out.append([r["code"], r["name"], r["market"], r["sector"], round(r["cap"] / 1e8), r["pct"],
                    b.get("per"), b.get("pbr"), b.get("roe"), x.get("rs"), r["price"], r["chg"],
                    None if x.get("r3") is None else round(x["r3"] * 100, 1),
                    None if x.get("r12") is None else round(x["r12"] * 100, 1)])
    # 업종·지수 PER(추정)
    sector_per, index_per = {}, {}
    for m in ("코스피", "코스닥"):
        sub = [o for o in out if o[2] == m]
        per, n = harmonic_per([(o[4], o[6]) for o in sub])
        index_per[m] = {"per": per, "n": n, "of": len(sub)}
        for sec in sorted({o[3] for o in sub}):
            ss = [o for o in sub if o[3] == sec]
            p2, n2 = harmonic_per([(o[4], o[6]) for o in ss])
            sector_per[f"{m}|{sec}"] = {"per": p2, "n": n2, "of": len(ss)}
    heat = {"date": d.isoformat(), "session": session, "asof": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "metaDate": cache.get("date"),
            "cols": ["code", "name", "market", "sector", "cap억", "pct", "per", "pbr", "roe", "rs", "price", "chg", "r3", "r12"],
            "rows": out, "sectorPer": sector_per, "indexPer": index_per,
            "note": "PER·RS 는 " + str(cache.get("date")) + " 2차(17:30) 기준. 업종·지수 PER 은 흑자 종목만으로 계산한 추정치."}
    save_json(Path(out_dir) / "heat" / f"{d.isoformat()}.json", heat)
    return heat


def rs_top(heat, n=15):
    """RS 상위 n (RS 같으면 3개월 수익률 순)"""
    if not heat:
        return []
    rows = [r for r in heat["rows"] if r[9] is not None]
    rows.sort(key=lambda r: (-r[9], -(r[12] or 0)))
    return [{"code": r[0], "name": r[1], "market": r[2], "sector": r[3], "price": r[10], "pct": r[5], "chg": r[11],
             "amount": 0, "rs": r[9], "r3": r[12], "r12": r[13], "per": r[6]} for r in rows[:n]]


# ───────────────────────── v5: DART 재무(TTM·연환산 PER), 지수 PER 3년, 장중 흐름, 신고가 기록 ─────────────────────────
def fin_update(src, d, session, out_dir, heat, extra, notes):
    """extra: {종목코드: {"price":..,"market":..}} (오늘 목록 종목). 반환 {종목코드: {ttmPer, annPer, finPeriod, ...}}
    DART 조회는 2차(close)에서만, 다른 회차는 캐시로 계산. 지수 PER 3년 시계열도 2차에서 다시 계산."""
    import dartfin
    mock = type(src).__name__ == "MockSource"
    key = os.environ.get("DART_API_KEY", "").strip()
    if not (key or mock):
        return {}
    dart = None
    if not mock:
        import dart as dart_mod
        dart = dart_mod.Dart(key)
    ucache = load_json(CACHE / "universe.json", {})
    shares, market_of = dict(ucache.get("shares", {})), dict(ucache.get("market", {}))
    codes = list(dict.fromkeys(list(shares) + list(extra)))
    corp_of = {}
    for c in codes:
        cc = ("C" + c) if mock else dart.corp_code(c)
        if cc:
            corp_of[c] = cc

    class _NoFetch(dartfin.Fin):  # 2차가 아니면 캐시만 읽음(캐시 파일은 건드리지 않음)
        def period(self, year, reprt, corps, today, refresh_days=1):
            return dartfin._load(self._path(year, reprt), {}).get("data", {})
    fin = (dartfin.Fin(dart, mock) if session == "close" or mock else _NoFetch(dart, mock))
    seqs_cc = dartfin.build(fin, corp_of, d)
    seqs = {c: seqs_cc.get(cc, []) for c, cc in corp_of.items()}

    # 종목별 PER 3종
    lst = None
    metrics = {}
    for c in codes:
        price = (extra.get(c) or {}).get("price")
        sh = shares.get(c)
        if (price is None or sh is None) and heat:
            r = next((x for x in heat["rows"] if x[0] == c), None)
            if r:
                price, sh = price or r[10], sh or (r[4] * 1e8 / r[10] if r[10] else None)
        if sh is None:
            if lst is None:
                lst = load_json(CACHE / "stocklist.json", {}) if not mock else backend(src).stock_list()
            sh = (lst.get(c) or {}).get("shares")
        if price and sh:
            m = dartfin.stock_metrics(seqs.get(c, []), price * sh)
            if m:
                metrics[c] = m
    if heat:  # 히트맵 행에 덧붙여 저장(화면의 종목 팝업이 사용)
        if "ttmPer" not in heat["cols"]:
            heat["cols"] += ["ttmPer", "annPer", "finPeriod"]
            for r in heat["rows"]:
                m = metrics.get(r[0], {})
                r += [m.get("ttmPer"), m.get("annPer"), m.get("finPeriod")]
        save_json(Path(out_dir) / "heat" / f"{d.isoformat()}.json", heat)

    # 지수 PER 3년 (2차 또는 아직 없을 때)
    path = Path(out_dir) / "market" / "series.json"
    ser = load_json(path, {})
    if session == "close" or not ser.get("perLong") or mock:
        px = {c: load_json(CACHE / "px" / f"{c}.json", {}) for c in shares} if not mock else \
             {c: backend(src).closes_long(c, d) for c in shares}
        long = dartfin.index_per_series({c: seqs.get(c, []) for c in shares}, px, shares, market_of,
                                        (d - dt.timedelta(days=365 * 3)).isoformat())
        long["method"] = ("DART 분기 실적 TTM 순이익(공시 기한 이후 반영) · 현재 시총 상위 300 · 적자 제외 · "
                          "주식수는 현재값 → 추정치")
        long["updated"] = d.isoformat()
        ser["perLong"] = long
        save_json(path, ser)
        if dart:
            print(f"DART 분기 실적 호출 {fin.calls}회 · 지수 PER {len(long['dates'])}일 계산")
    return metrics


def intraday(src, d, notes):
    """3차(20:00)·2차(17:30)에서: 프로그램 매매 분별 + 삼성전자·SK하이닉스 외국인·기관 장중 흐름"""
    mk = backend(src)
    out = {}
    try:
        out["program"] = mk.program_intraday(d)
    except Exception as e:
        notes.append(f"수집 실패: 프로그램 매매 장중 ({type(e).__name__})")
    big = {}
    for code, name in (("005930", "삼성전자"), ("000660", "SK하이닉스")):
        try:
            big[name] = mk.investor_intraday(code)
        except Exception as e:
            notes.append(f"수집 실패: {name} 장중 수급 ({type(e).__name__})")
    if big:
        out["big"] = big
    return out


def highs_history(out_dir, d, nh_codes, nl_codes):
    """52주 신고가·신저가 종목 기록을 series.json 에 쌓고, 종목별 최근 20·60거래일 등장 횟수와 연속일을 계산"""
    path = Path(out_dir) / "market" / "series.json"
    ser = load_json(path, {})
    for key in ("nh", "nl"):
        ser.setdefault(key, {})
    for f in sorted(Path(out_dir).glob("20*.json")):  # 처음에는 저장된 날짜 파일(상위 12개만)로 채움
        if f.stem in ser["nh"] or f.stem >= d.isoformat():
            continue
        dd = load_json(f, {})
        sn = (dd.get("sessions") or {}).get(dd.get("latest") or "", {})
        ind = (sn or {}).get("indicators") or {}
        if ind.get("newHigh"):
            ser["nh"][f.stem] = [x["code"] for x in ind["newHigh"].get("all", ind["newHigh"].get("top", []))]
        if ind.get("newLow"):
            ser["nl"][f.stem] = [x["code"] for x in ind["newLow"].get("all", ind["newLow"].get("top", []))]
    ser["nh"][d.isoformat()] = nh_codes
    ser["nl"][d.isoformat()] = nl_codes
    save_json(path, ser)

    def counts(key, codes):
        days = sorted(ser[key])
        res = {}
        for c in codes:
            c20 = sum(1 for x in days[-20:] if c in ser[key][x])
            c60 = sum(1 for x in days[-60:] if c in ser[key][x])
            streak = 0
            for x in reversed(days):
                if c in ser[key][x]:
                    streak += 1
                else:
                    break
            res[c] = {"c20": c20, "c60": c60, "streak": streak}
        return res, len(days)
    a, na = counts("nh", nh_codes)
    b, nb = counts("nl", nl_codes)
    return a, b, na
