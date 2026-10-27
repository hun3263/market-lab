#!/usr/bin/env python3
"""collect.py : 하루 3회(1차 12:00 / 2차 17:30 / 3차 20:00) 시황을 모아 data/날짜.json 에 회차별로 쌓고,
회차 사이의 변화를 비교해 함께 저장합니다.

  python collect.py --session noon    # 1차 12:00 장중 스냅샷
  python collect.py --session close   # 2차 17:30 정규장 마감 후 (KRX)
  python collect.py --session nxt     # 3차 20:00 NXT 종료 후 (KRX+NXT 합산)

데이터 출처
  - 환경변수 KIWOOM_APPKEY / KIWOOM_SECRETKEY 가 있으면 세 회차 모두 키움 REST API 로 받습니다
    (키움에 등록된 허용 IP 의 서버에서만 동작). 1·2차는 KRX 기준, 3차는 KRX+NXT 통합 기준입니다.
  - 키가 없으면 2차만 pykrx(선택 설치)로 받고, 1·3차는 '건너뜀'으로 조용히 끝납니다(오류 아님).
  - 비교 기능은 두 회차 이상 쌓였을 때만 생깁니다.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
AMOUNT_MIN = 50_000_000_000  # 500억 원
TOP_N = 15
DOWN_N = 5                      # 하락 TOP
DOWN_AMOUNT_CND = os.environ.get("DOWN_AMOUNT_CND", "0")  # 하락 순위 거래대금 조건(키움 코드, 0=조건 없음, 1000=100억↑)
STOCK_DIR = "stocks"            # data/stocks/종목코드.json : 종목 상세 화면용
MEGA = {"005930": "삼성전자", "000660": "SK하이닉스"}  # 거래대금 쏠림 지표용

SESSIONS = {
    "noon": {"short": "1차 12:00", "label": "1차 · 12:00 장중"},
    "close": {"short": "2차 17:30", "label": "2차 · 17:30 정규장 마감"},
    "nxt": {"short": "3차 20:00", "label": "3차 · 20:00 NXT 종료 후 (KRX+NXT 합산)"},
}
ORDER = ["noon", "close", "nxt"]
SESSION_NOTE = {
    "noon": "12:00 무렵의 장중 스냅샷입니다(KRX 기준). 오후 장에서 달라질 수 있습니다.",
    "close": "거래대금·종목 순위는 KRX 정규장 기준이며 NXT(대체거래소) 거래는 포함하지 않습니다.",
    "nxt": "KRX+NXT 통합 기준입니다. 지수는 KRX 15:30 종가 그대로입니다.",
}


# ───────────────────────── 보조 함수: 숫자 → 읽기 쉬운 문자열 ─────────────────────────
def won(v):
    """원 단위 정수 → '21조 4,000억' 형태"""
    v = int(round(v))
    sign = "-" if v < 0 else ""
    jo, eok = divmod(abs(v) // 100_000_000, 10_000)
    if jo and eok:
        body = f"{jo}조 {eok:,}억"
    elif jo:
        body = f"{jo}조"
    else:
        body = f"{eok:,}억"
    return sign + body


def signed_won(v):
    return ("+" if v > 0 else "") + won(v)


def pct(x):
    return f"{x:+.2f}%"


# ───────────────────────── 데이터 출처 ─────────────────────────
class KrxSource:
    """pykrx 로 KRX 정규장 데이터를 읽습니다(2차 전용). 출처가 바뀌면 이 클래스만 교체하면 됩니다."""

    def __init__(self):
        from pykrx import stock  # 설치 안 되어 있으면 여기서 ImportError
        self.stock = stock
        self._names = {}

    @staticmethod
    def _s(d):
        return d.strftime("%Y%m%d")

    def is_open(self, d):
        if d.weekday() >= 5:
            return False
        df = self.stock.get_index_ohlcv(self._s(d), self._s(d), "1001")
        return df is not None and len(df) > 0

    def indices(self, d):
        out = []
        start = d - dt.timedelta(days=10)
        for name, code in (("코스피", "1001"), ("코스닥", "2001")):
            df = self.stock.get_index_ohlcv(self._s(start), self._s(d), code)
            if df.index[-1].date() != d:
                raise RuntimeError(f"{name} 지수에 {d} 데이터가 없습니다")
            last, prev = df.iloc[-1], df.iloc[-2]
            out.append({"name": name, "close": float(last["종가"]),
                        "prev": float(prev["종가"]), "amount": int(last["거래대금"])})
        return out

    def flows(self, d):
        res = {}
        for name, market in (("코스피", "KOSPI"), ("코스닥", "KOSDAQ")):
            df = self.stock.get_market_trading_value_by_investor(self._s(d), self._s(d), market)
            res[name] = {"개인": int(df.loc["개인", "순매수"]),
                         "외국인": int(df.loc["외국인", "순매수"]),
                         "기관": int(df.loc["기관합계", "순매수"])}
        return res

    def stocks(self, d):
        rows = []
        for market, label in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            df = self.stock.get_market_ohlcv(self._s(d), market=market)
            for code, r in df.iterrows():
                if int(r["거래대금"]) <= 0:
                    continue
                rows.append({"code": code, "market": label, "price": int(r["종가"]),
                             "pct": float(r["등락률"]), "amount": int(r["거래대금"])})
        return rows

    def name(self, code):
        if code not in self._names:
            self._names[code] = self.stock.get_market_ticker_name(code)
        return self._names[code]

    def history(self, code, d, n=130):
        df = self.stock.get_market_ohlcv(self._s(d - dt.timedelta(days=n * 3 // 2)), self._s(d), code)
        return [(i.strftime("%Y%m%d"), int(r["종가"]), float(r["거래대금"])) for i, r in df.tail(n).iterrows()]


ETF_PREFIX = ("KODEX", "TIGER", "RISE", "SOL ", "ACE ", "PLUS ", "HANARO", "KOSEF", "ARIRANG", "KBSTAR",
              "TIMEFOLIO", "WON ", "1Q ", "BNK ", "HK ", "KIWOOM ", "마이티", "파워 ", "SMART ", "TRUSTON", "VITA ", "UNICORN")
ETF_WORD = ("ETN", "인버스", "레버리지", "커버드콜", "단일종목", "선물", "액티브", "TOP10", "TOP2", "CD금리", "머니마켓")


def is_etf(name):
    n = name.strip()
    return n.startswith(ETF_PREFIX) or any(w in n for w in ETF_WORD)


_ipv4_done = False


def _force_ipv4():
    """이 프로그램의 모든 접속이 IPv4 로만 나가게 합니다(키움에는 서버의 고정 IPv4 만 등록되어 있음)."""
    global _ipv4_done
    if _ipv4_done:
        return
    import socket
    orig = socket.getaddrinfo

    def v4_only(host, port, family=0, type=0, proto=0, flags=0):
        return orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = v4_only
    _ipv4_done = True


def _num(s):
    """'+13620' '-1,234' → 부호를 떼고 숫자만(가격·거래대금용). 방향 부호는 별도 필드가 담당합니다."""
    return abs(float(str(s).replace(",", "").replace("+", "").strip() or 0))


def _snum(s):
    """부호를 살려서 숫자로(전일대비·순매수용)"""
    return float(str(s).replace(",", "").replace("+", "").strip() or 0)


def _sig_num(val, sig=None):
    """전일대비 금액. 키움은 값에 +/- 를 붙이거나 별도 기호(1·2 상승, 4·5 하락, 3 보합)로 줍니다. 둘 다 반영."""
    if val in (None, ""):
        return 0
    v = _snum(val)
    if sig in ("4", "5") and v > 0:
        v = -v
    elif sig in ("1", "2") and v < 0:
        v = -v
    return int(v)


def _sig_pct(val, sig=None):
    if val in (None, ""):
        return 0.0
    v = _snum(val)
    if sig in ("4", "5") and v > 0:
        v = -v
    return v


class KiwoomSource:
    """키움증권 REST API(운영 도메인)로 읽기 전용 시세를 받습니다. 1·2·3차 모두 이 출처를 씁니다.

    - 환경변수: KIWOOM_APPKEY, KIWOOM_SECRETKEY (필수) / KIWOOM_BASE(기본 https://api.kiwoom.com)
                KIWOOM_EXCHANGE(KRX|ALL, 기본: nxt 회차만 ALL=KRX+NXT 통합) / INCLUDE_ETF=1(ETF도 순위에 포함)
    - 허용 IP로 등록된 서버에서만 인증됩니다(키움 정책). 주문 API는 호출하지 않습니다.
    - 단위(확인): 순위·지수 거래대금=백만원, 업종별 순매수=억원 → 내부에서 원 단위로 환산합니다.
    """
    partial_universe = True  # 전종목이 아니라 순위 상위만 받으므로, 합계·상승하락은 지수 API 값을 씁니다

    def __init__(self, session):
        self.appkey = os.environ.get("KIWOOM_APPKEY", "").strip()
        self.secret = os.environ.get("KIWOOM_SECRETKEY", "").strip()
        if not (self.appkey and self.secret):
            raise SystemExit("[오류] KIWOOM_APPKEY / KIWOOM_SECRETKEY 환경변수가 없습니다.")
        self.base = os.environ.get("KIWOOM_BASE", "https://api.kiwoom.com").rstrip("/")
        ex = os.environ.get("KIWOOM_EXCHANGE", "ALL" if session == "nxt" else "KRX").upper()
        self.exchange = {"KRX": "1", "NXT": "2", "ALL": "3"}[ex]
        self.basis = {"1": "KRX", "2": "NXT", "3": "KRX+NXT 통합"}[self.exchange]
        self.include_etf = os.environ.get("INCLUDE_ETF") == "1"
        self._token = None
        self._names, self.breadth, self.amount_by_code, self.market_by_code = {}, {}, {}, {}
        self._last = 0.0
        _force_ipv4()  # 서버가 IPv6 로 접속하면 등록된 IPv4 와 달라 인증이 막히므로 IPv4 만 쓰게 고정

    # ── 통신 ──
    def _http(self, path, headers, body):
        wait = 0.25 - (time.time() - self._last)  # 호출 간격 최소 0.25초(키움 호출 제한 대비)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json;charset=UTF-8", **headers})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode()), r.headers
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:  # 호출 제한: 잠시 쉬고 재시도
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"HTTP {e.code} {path}: {e.read().decode(errors='replace')[:200]}")
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"연결 실패 {path}: {e}")

    def _tok(self):
        if not self._token:
            data, _ = self._http("/oauth2/token", {}, {"grant_type": "client_credentials",
                                                      "appkey": self.appkey, "secretkey": self.secret})
            if not data.get("token"):
                raise RuntimeError(f"토큰 발급 실패: {data.get('return_msg', data)}")
            self._token = data["token"]
        return self._token

    def _call(self, path, api_id, body, pages=1):
        """연속조회(cont-yn)를 따라가며 응답 본문 목록을 돌려줍니다."""
        out, cont, nxt = [], "N", ""
        for _ in range(pages):
            data, h = self._http(path, {"api-id": api_id, "authorization": f"Bearer {self._tok()}",
                                        "cont-yn": cont, "next-key": nxt}, body)
            if str(data.get("return_code", 0)) != "0":
                raise RuntimeError(f"{api_id} 오류: {data.get('return_msg')}")
            out.append(data)
            if h.get("cont-yn") != "Y":
                break
            cont, nxt = "Y", h.get("next-key", "")
            time.sleep(0.4)
        return out

    @staticmethod
    def _rows(pages, key):
        return [r for p in pages for r in p.get(key, [])]

    # ── 공통 규격(KrxSource 와 같은 이름) ──
    def is_open(self, d):
        """휴장일에는 API가 전일 값을 그대로 돌려주므로, 오늘자 일봉이 있는지로 개장 여부를 판단합니다."""
        if d.weekday() >= 5:
            return False
        pages = self._call("/api/dostk/chart", "ka10081",
                           {"stk_cd": "005930", "base_dt": d.strftime("%Y%m%d"), "upd_stkpc_tp": "1"})
        return any(r.get("dt") == d.strftime("%Y%m%d") for r in self._rows(pages, "stk_dt_pole_chart_qry")[:5])

    def indices(self, d):
        out = []
        for name, code in (("코스피", "001"), ("코스닥", "101")):
            rows = self._rows(self._call("/api/dostk/sect", "ka20003", {"inds_cd": code}), "all_inds_idex")
            r = next((x for x in rows if x.get("stk_cd") == code), None)
            if r is None:
                raise RuntimeError(f"{name} 종합지수 행을 찾지 못했습니다")
            close = _num(r["cur_prc"])
            out.append({"name": name, "close": close, "prev": close - _snum(r["pred_pre"]),
                        "amount": int(_num(r["trde_prica"]) * 1_000_000)})
            self.breadth[name] = (int(r["rising"]), int(r["fall"]), int(r["stdns"]))
        return out

    def flows(self, d):
        res = {}
        for name, mk in (("코스피", "0"), ("코스닥", "1")):
            rows = self._rows(self._call("/api/dostk/sect", "ka10051",
                                         {"mrkt_tp": mk, "amt_qty_tp": "0", "stex_tp": self.exchange}), "inds_netprps")
            r = next((x for x in rows if str(x.get("inds_cd", "")).startswith("001" if mk == "0" else "101")), None)
            if r is None:
                raise RuntimeError(f"{name} 종합 수급 행을 찾지 못했습니다")
            eok = 100_000_000
            res[name] = {"개인": int(_snum(r["ind_netprps"]) * eok), "외국인": int(_snum(r["frgnr_netprps"]) * eok),
                         "기관": int(_snum(r["orgn_netprps"]) * eok)}
        return res

    def stocks(self, d):
        return []  # 순위 API 로 대신합니다(rankings)

    def name(self, code):
        return self._names.get(code, code)

    def rankings(self, d):
        """{"up": 500억↑ 상승률 상위, "top": 거래대금 상위, "down": 하락률 상위} 행 목록.
        시장(코스피/코스닥)을 알기 위해 순위 API 를 시장별(001/101)로 나눠 부른 뒤 합칩니다.
        종목코드의 '_AL' 같은 접미사는 뗍니다."""
        amt_rows, gain_rows, down_rows = [], [], []
        for mk, label in (("001", "코스피"), ("101", "코스닥")):
            rows = self._rows(self._call("/api/dostk/rkinfo", "ka10032",
                                         {"mrkt_tp": mk, "mang_stk_incls": "0", "stex_tp": self.exchange}, pages=2),
                              "trde_prica_upper")
            amt_rows += [dict(r, _mk=label) for r in rows]
            base = {"mrkt_tp": mk, "trde_qty_cnd": "0000", "stk_cnd": "16", "crd_cnd": "0", "updown_incls": "1",
                    "pric_cnd": "0", "stex_tp": self.exchange}
            rows = self._rows(self._call("/api/dostk/rkinfo", "ka10027", dict(base, sort_tp="1", trde_prica_cnd="5000")),
                              "pred_pre_flu_rt_upper")
            gain_rows += [dict(r, _mk=label) for r in rows]
            # 하락 TOP5: 거래대금 조건은 DOWN_AMOUNT_CND(기본 0=조건 없음). ETF·ETN 은 stk_cnd=16 으로 제외
            rows = self._rows(self._call("/api/dostk/rkinfo", "ka10027",
                                         dict(base, sort_tp="3", trde_prica_cnd=DOWN_AMOUNT_CND)), "pred_pre_flu_rt_upper")
            down_rows += [dict(r, _mk=label) for r in rows]

        def cd(x):
            return x["stk_cd"].split("_")[0]

        for r in amt_rows + gain_rows + down_rows:
            self._names[cd(r)] = r["stk_nm"]
            self.market_by_code[cd(r)] = r["_mk"]
        self.amount_by_code = {cd(r): int(_num(r["trde_prica"]) * 1_000_000) for r in amt_rows}

        def row(r):
            code = cd(r)
            return {"code": code, "market": r["_mk"], "name": r["stk_nm"], "price": int(_num(r["cur_prc"])),
                    "pct": _snum(r["flu_rt"]), "chg": _sig_num(r.get("pred_pre"), r.get("pred_pre_sig")),
                    "amount": self.amount_by_code.get(code, 0)}

        up = sorted((row(r) for r in gain_rows), key=lambda s: -s["pct"])
        top = sorted((row(r) for r in amt_rows if self.include_etf or not is_etf(r["stk_nm"])), key=lambda s: -s["amount"])
        down = sorted((row(r) for r in down_rows if not is_etf(r["stk_nm"])), key=lambda s: s["pct"])
        return {"up": up, "top": top, "down": down}

    # ── 확장: 일봉(평균 거래대금·주가 차트), 상한·하한, 테마, 선행지표 ──
    def history(self, code, d, n=130):
        """최근 일봉 [(YYYYMMDD, 종가, 거래대금 원)] 오래된 순. 통합(nxt) 회차는 '_AL' 코드로 받아 기준을 맞춥니다.
        일봉 거래대금 단위는 문서에 없어 순위 API(백만원)와 비교해 자동 판정합니다(collect 의 calibrate)."""
        body = {"stk_cd": code + ("_AL" if self.exchange == "3" else ""), "base_dt": d.strftime("%Y%m%d"), "upd_stkpc_tp": "1"}
        try:
            rows = self._rows(self._call("/api/dostk/chart", "ka10081", body), "stk_dt_pole_chart_qry")
        except RuntimeError:
            if body["stk_cd"] == code:
                raise
            rows = self._rows(self._call("/api/dostk/chart", "ka10081", dict(body, stk_cd=code)),  # 통합 코드가 안 되면 KRX 로
                              "stk_dt_pole_chart_qry")
        out = [(r["dt"], int(_num(r["cur_prc"])), _num(r.get("trde_prica", 0))) for r in rows[:n] if r.get("dt")]
        return sorted(out)

    def limits(self, d):
        """{"코스피": (상한, 하한), "코스닥": (...)} — ka20001 업종현재가"""
        out = {}
        for name, mk, code in (("코스피", "0", "001"), ("코스닥", "1", "101")):
            r = self._call("/api/dostk/sect", "ka20001", {"mrkt_tp": mk, "inds_cd": code})[0]
            out[name] = (int(_num(r.get("upl", 0))), int(_num(r.get("lst", 0))))
        return out

    def themes(self, d, n=8, k=6):
        """오늘 등락률 상위 테마 n개와 각 테마의 상위 구성종목 k개 (ka90001 → ka90002)"""
        groups = self._rows(self._call("/api/dostk/thme", "ka90001", {
            "qry_tp": "0", "date_tp": "1", "flu_pl_amt_tp": "3", "stex_tp": self.exchange}), "thema_grp")
        out = []
        for g in groups[:n]:
            comp = self._rows(self._call("/api/dostk/thme", "ka90002", {
                "thema_grp_cd": g["thema_grp_cd"], "stex_tp": self.exchange, "date_tp": "1"}), "thema_comp_stk")
            comp = sorted(comp, key=lambda r: -_sig_pct(r.get("flu_rt"), r.get("flu_sig")))
            out.append({"name": g.get("thema_nm", ""), "code": g.get("thema_grp_cd", ""),
                        "pct": _sig_pct(g.get("flu_rt"), g.get("flu_sig")),
                        "rise": int(_num(g.get("rising_stk_num", 0))), "fall": int(_num(g.get("fall_stk_num", 0))),
                        "n": int(_num(g.get("stk_num", 0))),
                        "stocks": [{"code": r["stk_cd"].split("_")[0], "name": r.get("stk_nm", ""),
                                    "pct": _sig_pct(r.get("flu_rt"), r.get("flu_sig"))} for r in comp[:k]]})
        return out

    def new_high_low(self, d, kind):
        """52주(250일) 신고가(kind=1)·신저가(kind=2) 종목 목록 — ka10016, ETF·ETN·스팩 제외"""
        rows = self._rows(self._call("/api/dostk/stkinfo", "ka10016", {
            "mrkt_tp": "000", "ntl_tp": str(kind), "high_low_close_tp": "1", "stk_cnd": "1",
            "trde_qty_tp": "00000", "crd_cnd": "0", "updown_incls": "1", "dt": "250",
            "stex_tp": self.exchange}, pages=5), "ntl_pric")
        out = []
        for r in rows:
            nm = r.get("stk_nm", "")
            if is_etf(nm) or "스팩" in nm or "기업인수목적" in nm:
                continue
            out.append({"code": r["stk_cd"].split("_")[0], "name": nm,
                        "pct": _sig_pct(r.get("flu_rt"), r.get("pred_pre_sig"))})
        return out

    def lending(self, d):
        """시장 전체 대차거래 최근 일자 — ka10068. 공표가 하루 늦어 장중에는 전일 값일 수 있습니다."""
        rows = self._rows(self._call("/api/dostk/slb", "ka10068", {
            "all_tp": "1", "strt_dt": (d - dt.timedelta(days=14)).strftime("%Y%m%d"),
            "end_dt": d.strftime("%Y%m%d")}), "dbrt_trde_trnsn")
        rows = sorted((r for r in rows if r.get("dt")), key=lambda r: r["dt"], reverse=True)
        if not rows:
            return None
        r = rows[0]
        return {"date": r["dt"], "new": _num(r.get("dbrt_trde_cntrcnt")), "repay": _num(r.get("dbrt_trde_rpy")),
                "net": _snum(r.get("dbrt_trde_irds") or 0), "shares": _num(r.get("rmnd")),
                "amount_raw": _num(r.get("remn_amt")),
                "prev_amount_raw": _num(rows[1].get("remn_amt")) if len(rows) > 1 else None}


def pick_source(session, mock):
    """어떤 출처를 쓸지 정합니다. 키움 키가 있으면 키움, 없으면 2차만 pykrx, 그 외는 None(건너뜀)."""
    if mock:
        return MockSource(session)
    if os.environ.get("KIWOOM_APPKEY"):
        return KiwoomSource(session)
    if session == "close":
        try:
            return KrxSource()
        except ImportError:
            raise SystemExit("[오류] KIWOOM_APPKEY 도 pykrx 도 없습니다.")
    return None


class MockSource:
    """네트워크 없이 파이프라인을 점검하기 위한 가짜 데이터. 실제 시장과 무관합니다.
    회차마다 값이 조금씩 달라지도록 만들어 비교 기능을 시험할 수 있습니다."""
    hist_unit = 1_000_000
    K = {"noon": 3, "close": 7, "nxt": 11}
    SC = {"noon": 0.45, "close": 1.0, "nxt": 1.1}
    KOSPI = {"noon": 2985.0, "close": 3000.0, "nxt": 3000.0}
    KOSDAQ = {"noon": 893.0, "close": 900.0, "nxt": 900.0}

    def __init__(self, session="close"):
        self.session = session

    def is_open(self, d):
        return d.weekday() < 5

    def indices(self, d):
        s = self.session
        return [{"name": "코스피", "close": self.KOSPI[s], "prev": 3030.0, "amount": int(12e12 * self.SC[s])},
                {"name": "코스닥", "close": self.KOSDAQ[s], "prev": 890.0, "amount": int(6e12 * self.SC[s])}]

    def flows(self, d):
        k = self.SC[self.session]
        return {"코스피": {"개인": int(-467.8e9 * k), "외국인": int(-1296.9e9 * k), "기관": int(1010.5e9 * k)},
                "코스닥": {"개인": int(419.6e9 * k), "외국인": int(-255.7e9 * k), "기관": int(-179.7e9 * k)}}

    def stocks(self, d):
        s = self.session
        k, sc = self.K[s], self.SC[s]
        rows = [{"code": "005930", "market": "코스피", "price": 70000, "pct": 1.1, "amount": int(6.8e12 * sc)},
                {"code": "000660", "market": "코스피", "price": 140000, "pct": -1.2, "amount": int(7.7e12 * sc)}]
        for i in range(1, 41):
            key = (i * k) % 41
            rows.append({"code": f"9{i:05d}", "market": "코스피" if i % 2 else "코스닥",
                         "price": 1000 * i, "pct": ((i * (k + 2)) % 13) - 4.0 + i / 100,
                         "amount": int((900 - key * 20) * 1e9 * sc)})
        return rows

    def name(self, code):
        return {"005930": "삼성전자", "000660": "SK하이닉스"}.get(code, f"테스트종목{code[-2:]}") + \
               ("스팩" if code.endswith("07") else "")

    # ── 확장 기능의 가짜 값(화면 점검용) ──
    def history(self, code, d, n=130):
        import random
        rnd = random.Random(code)
        out, px, day = [], 10000.0 + rnd.random() * 50000, d
        while len(out) < n:  # 오늘부터 거꾸로 n 거래일
            if day.weekday() < 5:
                px *= 1 + rnd.uniform(-0.03, 0.032)
                out.append((day.strftime("%Y%m%d"), int(px), rnd.uniform(1e5, 4e5)))  # 거래대금 '백만원' 단위 흉내
            day -= dt.timedelta(days=1)
        return sorted(out)

    def limits(self, d):
        return {"코스피": (3, 1), "코스닥": (5, 2)}

    def themes(self, d, n=8, k=6):
        names = ["해운", "2차전지", "HBM", "원전", "방산", "조선", "바이오", "로봇"]
        return [{"name": t, "code": str(100 + i), "pct": round(4.5 - i * 0.9, 2), "rise": 12 - i, "fall": 2 + i, "n": 20,
                 "stocks": [{"code": f"9{(i * 5 + j) % 40 + 1:05d}", "name": f"테스트종목{((i * 5 + j) % 40 + 1):02d}",
                             "pct": round(9 - j * 1.7 - i * 0.5, 2)} for j in range(k)]} for i, t in enumerate(names[:n])]

    def new_high_low(self, d, kind):
        cnt = {"noon": 4, "close": 8, "nxt": 8}[self.session] if kind == 1 else 3
        return [{"code": f"9{i:05d}", "name": f"테스트종목{i:02d}", "pct": 5.0 - i} for i in range(1, cnt + 1)]

    def lending(self, d):
        return {"date": (d - dt.timedelta(days=1)).strftime("%Y%m%d"), "new": 41_000_000, "repay": 38_500_000,
                "net": 2_500_000, "shares": 2_310_000_000, "amount_raw": 101_234_567, "prev_amount_raw": 100_100_000}


# ───────────────────────── 뉴스 ─────────────────────────
UP_WORDS = ("상승", "급등", "강세", "상한가", "신고가", "수주", "계약", "공급", "흑자", "호실적", "최대", "승인",
            "선정", "기대", "돌파", "매수", "인수", "합병", "협력", "개발", "출시", "수혜", "반등")
DOWN_WORDS = ("하락", "급락", "약세", "하한가", "신저가", "적자", "부진", "유상증자", "전환사채", "CB", "소송", "취소",
              "해지", "철회", "매도", "우려", "쇼크", "블록딜", "오버행", "리콜", "조사")


def _rss(query):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return ET.fromstring(r.read())


def news(name, direction="up", days=2, limit=3):
    """구글 뉴스 RSS 에서 종목 관련 최근 기사 최대 3건 [{t 제목, u 링크, s 언론사, d 날짜}].
    - 검색어: "종목명" (따옴표로 정확히 일치) + 최근 2일
    - 정렬: 제목에 종목명이 있고, 방향(상승/하락)에 맞는 단어가 많을수록 위로 → '변동 이유'에 가까운 기사 우선
    - 한계: 제목 단어로 고르는 것이라 '진짜 이유'인지는 사람이 확인해야 합니다. 실패하면 빈 목록."""
    try:
        root = _rss(f'"{name}" when:{days}d')
    except Exception:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    words = UP_WORDS if direction == "up" else DOWN_WORDS
    seen, cands = set(), []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        src = (item.findtext("source") or "").strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate"))
        except Exception:
            continue
        if not title or not link or (now - when).days > days:
            continue
        if src and title.endswith(" - " + src):  # 구글 뉴스 제목 끝의 ' - 언론사' 를 떼어 언론사 칸으로
            title = title[: -len(src) - 3].strip()
        key = "".join(ch for ch in title if ch.isalnum())[:24]  # 같은 기사 재전송(제목 거의 같음) 제거
        if key in seen:
            continue
        seen.add(key)
        score = (3 if name in title else 0) + sum(1 for w in words if w in title) + (when.timestamp() / 1e10)
        cands.append((score, {"t": title, "u": link, "s": src, "d": when.astimezone(KST).strftime("%m/%d %H:%M")}))
    cands.sort(key=lambda x: -x[0])
    return [c for _, c in cands[:limit]]


def headlines(name, days=2, limit=3):
    """(이전 버전 호환) 기사 제목만"""
    return [n["t"] for n in news(name, "up", days, limit)]


# ───────────────────────── 선택 기능: Anthropic API 로 사유·테마 문장 생성 ─────────────────────────
def ai_enrich(items, api_key):
    """items: [{name, pct, headlines}] → {"reasons": {...}, "themes": [...]} 또는 None"""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        payload = json.dumps(items, ensure_ascii=False)
        prompt = (
            "아래는 오늘 한국 증시에서 거래대금이 큰 종목과 그 종목의 최근 기사 제목입니다.\n"
            "규칙:\n"
            "1) 사유는 제공된 기사 제목에 근거한 내용만 한 문장(60자 이내)으로 씁니다. 근거가 없으면 '근거 기사 확인 안 됨'.\n"
            "2) 종목들을 공통 재료로 묶어 테마 3~6개를 만듭니다. 테마마다 name, stocks(종목명 목록), "
            "action(조건부 점검 포인트 한 문장, 매수·매도 지시 금지)을 씁니다.\n"
            "3) 반드시 JSON 하나만 출력: "
            '{"reasons": {"종목명": "사유"}, "themes": [{"name": "", "stocks": [""], "action": ""}]}\n\n'
            f"자료:\n{payload}"
        )
        resp = client.messages.create(model="claude-sonnet-5-5", max_tokens=4000,
                                      messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:
        print(f"[경고] 사유·테마 생성 실패 → 기사 제목으로 대체합니다: {e}", file=sys.stderr)
        return None


# ───────────────────────── 한 회차 조립 ─────────────────────────
def calibrate(hist, amount_by_code, d, default):
    """일봉 거래대금 단위 자동 판정: 순위 API 거래대금(원) ÷ 일봉 당일 거래대금(원본 숫자)의 중앙값을
    1·천·백만·억 중 가장 가까운 값으로 맞춥니다. 판정 근거가 없으면 출처별 기본값."""
    today = d.strftime("%Y%m%d")
    ratios = []
    for code, h in hist.items():
        raw = next((a for t, _, a in h if t == today), 0)
        if raw > 0 and amount_by_code.get(code):
            ratios.append(amount_by_code[code] / raw)
    if not ratios:
        return default, False
    ratios.sort()
    med = ratios[len(ratios) // 2]
    import math
    best = min((1, 1e3, 1e6, 1e8), key=lambda c: abs(math.log10(med) - math.log10(c)))
    return best, True


def avg_amounts(h, d, scale):
    """오늘 이전 7거래일·20거래일 평균 거래대금(원)과 오늘 일봉 거래대금(원)"""
    today = d.strftime("%Y%m%d")
    past = [a * scale for t, _, a in h if t < today]
    tod = next((a * scale for t, _, a in h if t == today), 0)
    a7 = sum(past[-7:]) / len(past[-7:]) if past else 0
    a20 = sum(past[-20:]) / len(past[-20:]) if past else 0
    return a7, a20, tod


def build_snapshot(src, d, session, use_news=True, api_key=None, prev=None):
    """한 회차의 화면용 자료(snap)와 종목 상세 파일용 자료(extra)를 만듭니다.
    prev: 직전 거래일 같은 회차의 snap(선행지표 '전일 → 오늘' 비교용, 없으면 None)"""
    notes = [SESSION_NOTE[session]]

    def safe(label, fn, default):
        try:
            return fn()
        except Exception as e:
            notes.append(f"수집 실패: {label} ({type(e).__name__})")
            print(f"[경고] {label} 실패: {e}", file=sys.stderr)
            return default

    # 지수·수급 (필수)
    idx = src.indices(d)
    flows = safe("투자자별 수급", lambda: src.flows(d), {})
    partial = getattr(src, "partial_universe", False)  # 순위 상위만 받는 출처(키움)인지
    basis = getattr(src, "basis", "KRX")
    if partial:
        stocks = []
        rk = safe("종목 순위", lambda: src.rankings(d), {"up": [], "top": [], "down": []})
        up_raw, top_raw, down_raw = rk["up"], rk["top"], rk["down"]
    else:
        stocks = safe("종목 시세", lambda: src.stocks(d), [])
        for s in stocks:
            s.setdefault("chg", int(round(s["price"] - s["price"] / (1 + s["pct"] / 100))) if s["pct"] > -100 else 0)
        up_raw = sorted([s for s in stocks if s["pct"] > 0 and s["amount"] >= AMOUNT_MIN], key=lambda s: -s["pct"])
        top_raw = sorted(stocks, key=lambda s: -s["amount"])
        down_raw = sorted([s for s in stocks if s["pct"] < 0], key=lambda s: s["pct"])

    indices = []
    for i in idx:
        change = (i["close"] / i["prev"] - 1) * 100
        f = flows.get(i["name"])
        flow = (f"개인 {signed_won(f['개인'])} / 외인 {signed_won(f['외국인'])} / 기관 {signed_won(f['기관'])}"
                if f else "수집 실패")
        indices.append({"name": i["name"], "close": f"{i['close']:,.2f}", "change": pct(change),
                        "value": won(i["amount"]), "flow": flow, "pctv": round(change, 4)})

    # 종목 선별 (스팩 제외)
    def clean(rows):
        out = []
        for s in rows:
            s["name"] = s.get("name") or src.name(s["code"])
            if "스팩" not in s["name"] and "기업인수목적" not in s["name"]:  # SPAC 은 이름 표기가 두 가지
                out.append(s)
        return out

    up = clean(up_raw[:TOP_N * 2])[:TOP_N]
    top_amt = clean(top_raw[:TOP_N * 2])[:TOP_N]
    down = clean(down_raw[:DOWN_N * 3])[:DOWN_N]
    every = {x["code"]: x for x in down + top_amt + up}  # 화면에 나오는 모든 종목(중복 제거)

    # 일봉 → 7일·20일 평균 거래대금, 주가 흐름(상세 화면)
    hist = {}
    if hasattr(src, "history"):
        for code in every:
            h = safe(f"일봉 {every[code]['name']}", lambda c=code: src.history(c, d), None)
            if h:
                hist[code] = h
    amt_known = {c: s["amount"] for c, s in every.items() if s.get("amount")}
    scale, judged = calibrate(hist, amt_known, d, getattr(src, "hist_unit", 1e6 if partial else 1))
    if hist and not judged:
        notes.append("일봉 거래대금 단위를 자동 판정하지 못해 기본값을 썼습니다(평균 대비 비율 확인 필요).")
    for code, s in every.items():
        a7, a20, tod = avg_amounts(hist.get(code, []), d, scale)
        if not s.get("amount") and tod:
            s["amount"] = int(tod)  # 순위표에 없던 종목(주로 하락 TOP)은 일봉 거래대금으로 채움
        s["avg7"], s["avg20"] = a7, a20
    if partial and any(s["amount"] == 0 for s in up):
        notes.append("일부 종목의 거래대금을 찾지 못해 0으로 표시됩니다.")

    # 뉴스(기사 최대 3건, 링크 포함)
    arts = {}
    if use_news:
        downs = {s["code"] for s in down} | {s["code"] for s in top_amt if s["pct"] < 0}
        for code, s in every.items():
            arts[code] = news(s["name"], "down" if code in downs else "up")
            time.sleep(0.3)

    # 사유(선택: AI 요약)
    ai = None
    if api_key and arts:
        ai = ai_enrich([{"name": s["name"], "pct": s["pct"], "headlines": [a["t"] for a in arts.get(c, [])]}
                        for c, s in every.items()], api_key)

    def reason(s):
        if ai and ai.get("reasons", {}).get(s["name"]):
            return ai["reasons"][s["name"]]
        a = arts.get(s["code"])
        return a[0]["t"] if a else "근거 기사 확인 안 됨"

    def ratio(a, b):
        return round(a / b, 2) if a and b else None

    def row(s):
        return {"code": s["code"], "name": s["name"], "market": s.get("market", ""),
                "close": f"{s['price']:,}", "price": pct(s["pct"]),
                "chg": (f"{s['chg']:+,}" if s.get("chg") else "0") + "원",
                "amount": won(s["amount"]) if s["amount"] else "-",
                "avg7": won(s["avg7"]) if s.get("avg7") else "-", "avg20": won(s["avg20"]) if s.get("avg20") else "-",
                "r7": ratio(s["amount"], s.get("avg7")), "r20": ratio(s["amount"], s.get("avg20")),
                "reason": reason(s), "news": arts.get(s["code"], []),
                "pctv": round(s["pct"], 4), "chgv": int(s.get("chg") or 0), "amtv": int(s["amount"])}

    # 테마: 키움 테마(ka90001/2) → 실패 시 AI 묶음(키가 있을 때) → 안내 문구
    listed = set(every)
    themes = safe("테마", lambda: src.themes(d), []) if hasattr(src, "themes") else []
    for t in themes:
        for x in t["stocks"]:
            x["inList"] = x["code"] in listed
    if not themes and ai and ai.get("themes"):
        pct_by_name = {s["name"]: s["pct"] for s in every.values()}
        themes = [{"name": t.get("name", ""), "ai": True, "action": t.get("action", ""),
                   "stocks": [{"name": n, "pct": pct_by_name.get(n), "inList": True} for n in t.get("stocks", [])]}
                  for t in ai["themes"]]
    if not themes:
        themes = [{"name": "(테마 수집 실패)", "stocks": "-", "action": "다음 회차에 다시 시도합니다."}]

    # 상승·하락 종목 수(+상한·하한)
    breadth = {}
    lim = safe("상한·하한", lambda: src.limits(d), {}) if hasattr(src, "limits") else {}
    for mk in ("코스피", "코스닥"):
        if partial and mk in src.breadth:
            u, dn, flat = src.breadth[mk]
        elif not partial and stocks:
            u = sum(1 for s in stocks if s["market"] == mk and s["pct"] > 0)
            dn = sum(1 for s in stocks if s["market"] == mk and s["pct"] < 0)
            flat = sum(1 for s in stocks if s["market"] == mk and s["pct"] == 0)
        else:
            continue
        upper, lower = lim.get(mk, (None, None))
        breadth[mk] = {"up": u, "down": dn, "flat": flat, "upper": upper, "lower": lower}

    # 선행지표: 52주 신고가·신저가, 대차잔고
    indicators = {}
    pind = (prev or {}).get("indicators") or {}
    for key, kind in (("newHigh", 1), ("newLow", 2)):
        if hasattr(src, "new_high_low"):
            lst = safe("52주 신고가" if kind == 1 else "52주 신저가", lambda k=kind: src.new_high_low(d, k), None)
            if lst is not None:
                p = pind.get(key) or {}
                indicators[key] = {"count": len(lst), "prev": p.get("count"), "prevDate": (prev or {}).get("_date"),
                                   "top": sorted(lst, key=lambda x: -x["pct"] if kind == 1 else x["pct"])[:12]}
    if hasattr(src, "lending"):
        lend = safe("대차거래", lambda: src.lending(d), None)
        if lend:
            unit = 1_000_000  # 잔고금액 단위: 백만원으로 추정(미검증) → 원
            indicators["lending"] = {
                "date": f"{lend['date'][:4]}-{lend['date'][4:6]}-{lend['date'][6:]}",
                "net": f"{lend['net']:+,.0f}주", "shares": f"{lend['shares'] / 1e8:,.2f}억주",
                "amount": won(lend["amount_raw"] * unit),
                "delta": signed_won((lend["amount_raw"] - lend["prev_amount_raw"]) * unit)
                if lend.get("prev_amount_raw") else None,
                "unitNote": "잔고금액 단위는 백만원으로 추정(검증 전)"}
    indicators["unsupported"] = "시장 전체 공매도 거래대금·신용융자잔고는 키움 API에 없어 아직 표시하지 않습니다."

    # 요약(규칙 기반)
    summary = [" / ".join(f"{i['name']} {i['close']} ({i['change']}), 거래대금 {i['value']}" for i in indices)]
    k = flows.get("코스피")
    if k:
        summary.append(f"코스피 수급: 외국인 {signed_won(k['외국인'])}, 개인 {signed_won(k['개인'])}, "
                       f"기관 {signed_won(k['기관'])}")
    kospi_amt = next((i["amount"] for i in idx if i["name"] == "코스피"), 0)
    if partial:
        mega = sum(src.amount_by_code.get(c, 0) for c in MEGA)
    else:
        mega = sum(s["amount"] for s in stocks if s["code"] in MEGA and s["market"] == "코스피")
    if kospi_amt and mega and basis != "KRX":
        summary.append(f"삼성전자+SK하이닉스 거래대금(통합) {won(mega)} (코스피 지수 거래대금은 KRX 기준이라 비율은 생략)")
    elif kospi_amt and mega:
        summary.append(f"삼성전자+SK하이닉스 거래대금 {won(mega)}, 코스피 거래대금의 {mega / kospi_amt * 100:.1f}%")
    for mk, b in breadth.items():
        summary.append(f"{mk} 상승 {b['up']} : 하락 {b['down']} (보합 {b['flat']})")
    nh = indicators.get("newHigh")
    if nh:
        summary.append(f"52주 신고가 {nh['count']}종목" + (f" (직전 거래일 {nh['prev']}종목)" if nh["prev"] is not None else ""))
    if session == "noon":
        notes.append("평균 거래대금 대비 비율은 장중 누적 거래대금 기준이라 마감 때보다 낮게 나옵니다.")

    head = next((i for i in indices if i["name"] == "코스피"), indices[0])
    kf = flows.get("코스피", {}).get("외국인")
    headline = f"코스피 {head['change']}" + (f" · 외국인 {signed_won(kf)}" if kf is not None else "")

    snap = {"session": session, "label": SESSIONS[session]["label"], "basis": basis,
            "asof": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "headline": headline, "indices": indices, "flows": flows, "breadth": breadth, "indicators": indicators,
            "summary": summary, "top500": [row(s) for s in up], "themes": themes,
            "topAmount": [row(s) for s in top_amt], "down5": [row(s) for s in down], "notes": notes}
    extra = {"hist": hist, "scale": scale,
             "lists": {"top500": snap["top500"], "topAmount": snap["topAmount"], "down5": snap["down5"]}}
    return snap, extra



# ───────────────────────── 회차 간 비교 ─────────────────────────
def compare_pair(sa, sb, ka, kb):
    out = {"from": ka, "to": kb, "title": f"{SESSIONS[ka]['short']} → {SESSIONS[kb]['short']}",
           "indices": [], "flows": [], "lists": [], "notes": []}

    ia = {i["name"]: i for i in sa.get("indices", [])}
    all_zero = True
    for i in sb.get("indices", []):
        a = ia.get(i["name"])
        if a and "pctv" in a and "pctv" in i:
            dlt = i["pctv"] - a["pctv"]
            all_zero = all_zero and abs(dlt) < 1e-9
            out["indices"].append({"name": i["name"], "a": a["change"], "b": i["change"], "delta": f"{dlt:+.2f}%p"})
            out["notes"].append(f"{i['name']} 등락률 {a['change']} → {i['change']} ({dlt:+.2f}%p)")
    if sa.get("basis") and sb.get("basis") and sa["basis"] != sb["basis"]:
        out["notes"].append(f"집계 기준이 다릅니다({sa['basis']} → {sb['basis']}). 거래대금·순위 변화에는 NXT 거래분이 함께 반영됩니다.")
    if kb == "nxt" and out["indices"] and all_zero:
        out["notes"].append("지수는 15:30 이후 갱신되지 않아 변화가 없습니다. NXT 시간대의 변화는 종목·거래대금에서 봅니다.")

    fa, fb = sa.get("flows") or {}, sb.get("flows") or {}
    for mk in ("코스피", "코스닥"):
        if mk in fa and mk in fb:
            for who in ("외국인", "기관", "개인"):
                x, y = fa[mk][who], fb[mk][who]
                out["flows"].append({"name": f"{mk} {who}", "a": signed_won(x), "b": signed_won(y),
                                     "delta": signed_won(y - x)})
    if "코스피" in fa and "코스피" in fb:
        out["notes"].append(f"코스피 외국인 순매수 {signed_won(fa['코스피']['외국인'])} → {signed_won(fb['코스피']['외국인'])}")

    for title, key in (("거래대금 TOP15", "topAmount"), ("500억↑ & 상승률 TOP15", "top500")):
        ra, rb = sa.get(key) or [], sb.get(key) or []
        if not ra or not rb:
            continue
        rka = {r["name"]: n + 1 for n, r in enumerate(ra)}
        rkb = {r["name"]: n + 1 for n, r in enumerate(rb)}
        da, db = {r["name"]: r for r in ra}, {r["name"]: r for r in rb}
        new = [n for n in rkb if n not in rka]
        gone = [n for n in rka if n not in rkb]
        moves = []
        for n in rkb:
            if n in rka and "pctv" in da[n] and "pctv" in db[n]:
                dr = rka[n] - rkb[n]  # 양수면 순위 상승
                dp = db[n]["pctv"] - da[n]["pctv"]
                if dr or abs(dp) >= 1.0:
                    moves.append({"name": n, "rank": f"{rka[n]}위 → {rkb[n]}위",
                                  "pct": f"{da[n]['price']} → {db[n]['price']}",
                                  "amount": f"{da[n]['amount']} → {db[n]['amount']}",
                                  "_k": abs(dr) * 2 + abs(dp)})
        moves.sort(key=lambda m: -m["_k"])
        for m in moves:
            m.pop("_k")
        out["lists"].append({"name": title, "new": new[:8], "gone": gone[:8], "moves": moves[:8]})
        out["notes"].append(f"{title}: 신규 진입 {len(new)}종목, 이탈 {len(gone)}종목")
    return out


def make_compare(sessions):
    pairs = [("noon", "close"), ("noon", "nxt"), ("close", "nxt")]
    return [compare_pair(sessions[a], sessions[b], a, b) for a, b in pairs if a in sessions and b in sessions]


# ───────────────────────── 저장(날짜 파일에 회차를 합치기) ─────────────────────────
def merge_day(path, d, session, snap):
    day = {}
    if path.exists():  # 직접 써 둔 칸(study, ideas 등)은 그대로 남습니다
        try:
            day = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            day = {}
    day["date"] = d.isoformat()
    sessions = day.setdefault("sessions", {})
    sessions[session] = snap
    latest = [k for k in ORDER if k in sessions][-1]
    s = sessions[latest]
    for k in ("indices", "summary", "top500", "themes", "topAmount", "down5", "breadth", "indicators", "notes"):  # 맨 위에는 가장 늦은 회차를 복사
        day[k] = s.get(k, [])
    day["headline"] = f"{SESSIONS[latest]['short']} · {s['headline']}"
    day["latest"] = latest
    day["compare"] = make_compare(sessions)
    return day


# ───────────────────────── 종목 상세 파일(data/stocks/코드.json) ─────────────────────────
LIST_LABEL = {"top500": "500억↑ 상승률", "topAmount": "거래대금 상위", "down5": "하락 TOP5"}


def prev_snapshot(out_dir, d, session):
    """직전 거래일 파일에서 같은 회차(없으면 가장 늦은 회차) snap 을 찾습니다. 선행지표 '전일 → 오늘' 비교용."""
    files = sorted(p for p in Path(out_dir).glob("*.json") if p.stem < d.isoformat())
    for p in reversed(files):
        try:
            day = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        ss = day.get("sessions") or {}
        snap = ss.get(session) or (ss.get(day.get("latest")) if day.get("latest") else None)
        if snap:
            return dict(snap, _date=day.get("date"))
    return None


def update_stock_files(out_dir, d, session, extra, dart_key=None, dart_budget=12):
    """오늘 화면에 나온 종목마다 상세 파일을 갱신합니다: 주가 흐름(일봉), 등장 기록(날짜·사유·기사), DART 정보."""
    folder = Path(out_dir) / STOCK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dart, heavy = None, 0
    if dart_key:
        try:
            import dart as dart_mod
            dart = dart_mod.Dart(dart_key)
        except Exception as e:
            print(f"[경고] DART 준비 실패: {e}", file=sys.stderr)
    seen = {}
    for key, rows in extra["lists"].items():
        for rank, r in enumerate(rows, 1):
            if r.get("code"):
                seen.setdefault(r["code"], []).append((key, rank, r))
    for code, hits in seen.items():
        path = folder / f"{code}.json"
        try:
            info = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError:
            info = {}
        r0 = hits[0][2]
        info.update({"code": code, "name": r0["name"], "market": r0.get("market", ""),
                     "updated": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")})
        h = extra["hist"].get(code)
        if h:  # 일봉: 날짜별로 덮어쓰며 최근 130거래일 유지
            px = {p[0]: p for p in info.get("prices", [])}
            for t, c, a in h:
                ds = f"{t[:4]}-{t[4:6]}-{t[6:]}"
                px[ds] = [ds, c, int(a * extra["scale"])]
            info["prices"] = [px[k] for k in sorted(px)][-130:]
        apps = [a for a in info.get("appear", []) if not (a["date"] == d.isoformat() and a["list"] in {k for k, _, _ in hits})]
        for key, rank, r in hits:
            apps.append({"date": d.isoformat(), "session": session, "list": key, "label": LIST_LABEL[key], "rank": rank,
                         "close": r["close"], "price": r["price"], "chg": r["chg"], "amount": r["amount"],
                         "r20": r.get("r20"), "reason": r["reason"], "news": r.get("news", [])})
        info["appear"] = sorted(apps, key=lambda a: (a["date"], a["list"]))[-80:]
        if dart and heavy < dart_budget:
            try:
                import dart as dart_mod
                if dart_mod.enrich(info, code, dart, d):
                    heavy += 1
            except Exception as e:
                info["dartNote"] = f"DART 조회 실패: {type(e).__name__}"
                print(f"[경고] DART {r0['name']}: {e}", file=sys.stderr)
        path.write_text(json.dumps(info, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    msg = f"종목 상세 {len(seen)}개 갱신"
    if dart:
        msg += f" · DART 호출 {dart.calls}회(사업보고서 원문 {heavy}건)"
    print(msg)


# ───────────────────────── 실행 ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="일일 시황 자동 수집 (하루 3회)")
    ap.add_argument("--session", choices=list(SESSIONS), default="close")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 한국 시간 오늘)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--mock", action="store_true", help="가짜 데이터로 점검")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--check", action="store_true", help="키움 연결만 점검(저장 없음): 인증·지수 조회가 되는지 확인")
    ap.add_argument("--check-new", action="store_true", help="새로 추가한 키움 API 응답 원본 1건씩 출력(단위 확인용, 저장 없음)")
    a = ap.parse_args()

    if a.check_new:
        src = KiwoomSource(a.session)
        d = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(KST).date()
        tests = [("ka10027 하락률(코스피)", "/api/dostk/rkinfo", "ka10027", {"mrkt_tp": "001", "sort_tp": "3", "trde_qty_cnd": "0000",
                  "stk_cnd": "16", "crd_cnd": "0", "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "1"}, "pred_pre_flu_rt_upper"),
                 ("ka10081 일봉(삼성전자)", "/api/dostk/chart", "ka10081", {"stk_cd": "005930", "base_dt": d.strftime("%Y%m%d"), "upd_stkpc_tp": "1"}, "stk_dt_pole_chart_qry"),
                 ("ka10032 거래대금(코스피)", "/api/dostk/rkinfo", "ka10032", {"mrkt_tp": "001", "mang_stk_incls": "0", "stex_tp": "1"}, "trde_prica_upper"),
                 ("ka20001 업종현재가(코스피)", "/api/dostk/sect", "ka20001", {"mrkt_tp": "0", "inds_cd": "001"}, None),
                 ("ka90001 테마그룹", "/api/dostk/thme", "ka90001", {"qry_tp": "0", "date_tp": "1", "flu_pl_amt_tp": "3", "stex_tp": "1"}, "thema_grp"),
                 ("ka10016 52주 신고가", "/api/dostk/stkinfo", "ka10016", {"mrkt_tp": "000", "ntl_tp": "1", "high_low_close_tp": "1", "stk_cnd": "1",
                  "trde_qty_tp": "00000", "crd_cnd": "0", "updown_incls": "1", "dt": "250", "stex_tp": "1"}, "ntl_pric"),
                 ("ka10068 대차거래추이", "/api/dostk/slb", "ka10068", {"all_tp": "1", "strt_dt": (d - dt.timedelta(days=7)).strftime("%Y%m%d"),
                  "end_dt": d.strftime("%Y%m%d")}, "dbrt_trde_trnsn")]
        for label, path, api, body, key in tests:
            try:
                pages = src._call(path, api, body)
                rows = src._rows(pages, key) if key else [{k: v for k, v in pages[0].items() if not isinstance(v, list)}]
                print(f"\n[{label}] {len(rows)}행 · 첫 행:", json.dumps(rows[0] if rows else {}, ensure_ascii=False)[:600])
            except Exception as e:
                print(f"\n[{label}] 실패: {e}")
        return

    if a.check:  # 서버에서 가장 먼저 돌려볼 점검 명령
        src = KiwoomSource(a.session)
        print(f"접속 주소: {src.base} / 집계 기준: {src.basis}")
        try:
            src._tok()
            print("1) 토큰 발급: 성공 (앱키·시크릿·허용 IP 모두 통과)")
            ix = src.indices(dt.datetime.now(KST).date())
            print("2) 지수 조회: 성공 → " + ", ".join(f"{i['name']} {i['close']:,.2f}" for i in ix))
            print("3) 오늘 개장 여부:", "개장" if src.is_open(dt.datetime.now(KST).date()) else "휴장 또는 개장 전")
        except Exception as e:
            print(f"[실패] {e}")
            raise SystemExit(1)
        return

    d = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(KST).date()
    src = pick_source(a.session, a.mock)
    if src is None:
        print(f"[건너뜀] {SESSIONS[a.session]['label']}: KIWOOM_APPKEY 가 없어 데이터 출처가 없습니다. 저장하지 않습니다.")
        return

    try:
        opened = src.is_open(d)
    except Exception as e:
        raise SystemExit(f"[오류] 개장 여부를 확인하지 못했습니다(출처 접속 문제 가능): {e}")
    if not opened:
        print(f"{d} 는 휴장일이거나 데이터가 아직 없습니다. 저장하지 않고 종료합니다.")
        return

    prev = prev_snapshot(a.out_dir, d, a.session)
    snap, extra = build_snapshot(src, d, a.session, use_news=not a.no_news,
                                 api_key=os.environ.get("ANTHROPIC_API_KEY"), prev=prev)

    out = Path(a.out_dir) / f"{d.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    day = merge_day(out, d, a.session, snap)
    out.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    try:  # 상세 파일은 실패해도 본 데이터 저장에는 영향 없음
        update_stock_files(a.out_dir, d, a.session, extra,
                           dart_key=None if a.mock else os.environ.get("DART_API_KEY", "").strip() or None)
    except Exception as e:
        print(f"[경고] 종목 상세 파일 갱신 실패: {e}", file=sys.stderr)
    have = [SESSIONS[k]["short"] for k in ORDER if k in day["sessions"]]
    print(f"저장 완료: {out} · {SESSIONS[a.session]['short']} 반영 · 쌓인 회차: {', '.join(have)} · 비교 {len(day['compare'])}건")


if __name__ == "__main__":
    main()
