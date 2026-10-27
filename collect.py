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
        self._names, self.breadth, self.amount_by_code = {}, {}, {}
        _force_ipv4()  # 서버가 IPv6 로 접속하면 등록된 IPv4 와 달라 인증이 막히므로 IPv4 만 쓰게 고정

    # ── 통신 ──
    def _http(self, path, headers, body):
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
        """(500억↑ 상승률 상위, 거래대금 상위) 행 목록. 종목코드의 '_AL' 같은 접미사는 뗍니다."""
        amt_rows = self._rows(self._call("/api/dostk/rkinfo", "ka10032",
                                         {"mrkt_tp": "000", "mang_stk_incls": "0", "stex_tp": self.exchange}, pages=3),
                              "trde_prica_upper")
        gain_rows = self._rows(self._call("/api/dostk/rkinfo", "ka10027", {
            "mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "16", "crd_cnd": "0",
            "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "5000", "stex_tp": self.exchange}),
            "pred_pre_flu_rt_upper")

        def cd(x):
            return x["stk_cd"].split("_")[0]

        for r in amt_rows + gain_rows:
            self._names[cd(r)] = r["stk_nm"]
        self.amount_by_code = {cd(r): int(_num(r["trde_prica"]) * 1_000_000) for r in amt_rows}

        def row(r):
            code = cd(r)
            return {"code": code, "market": "", "name": r["stk_nm"], "price": int(_num(r["cur_prc"])),
                    "pct": _snum(r["flu_rt"]), "amount": self.amount_by_code.get(code, 0)}

        up = [row(r) for r in gain_rows]
        top = [row(r) for r in amt_rows if self.include_etf or not is_etf(r["stk_nm"])]
        return up, top


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


# ───────────────────────── 뉴스 ─────────────────────────
def headlines(name, days=2, limit=3):
    """구글 뉴스 RSS 에서 종목명으로 최근 기사 제목을 가져옵니다. 실패하면 빈 목록."""
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{name} 주가")
           + "&hl=ko&gl=KR&ceid=KR:ko")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate"))
        except Exception:
            continue
        if (now - when).days > days or not title:
            continue
        out.append(title)
        if len(out) >= limit:
            break
    return out


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
def build_snapshot(src, d, session, use_news=True, api_key=None):
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
        up_raw, top_raw = safe("종목 순위", lambda: src.rankings(d), ([], []))
    else:
        stocks = safe("종목 시세", lambda: src.stocks(d), [])
        up_raw = sorted([s for s in stocks if s["pct"] > 0 and s["amount"] >= AMOUNT_MIN], key=lambda s: -s["pct"])
        top_raw = sorted(stocks, key=lambda s: -s["amount"])

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
    if partial and any(s["amount"] == 0 for s in up):
        notes.append("일부 종목의 거래대금을 순위 상위 범위에서 찾지 못해 0으로 표시됩니다.")

    # 뉴스
    heads = {}
    if use_news:
        for s in {x["code"]: x for x in up + top_amt}.values():
            heads[s["name"]] = headlines(s["name"])
            time.sleep(0.3)

    # 사유·테마
    ai = None
    if api_key and heads:
        ai = ai_enrich([{"name": n, "pct": next(s["pct"] for s in up + top_amt if s["name"] == n),
                         "headlines": h} for n, h in heads.items()], api_key)

    def reason(name):
        if ai and ai.get("reasons", {}).get(name):
            return ai["reasons"][name]
        h = heads.get(name)
        return ("기사: " + h[0]) if h else "근거 기사 확인 안 됨"

    def row(s):
        return {"name": s["name"], "close": f"{s['price']:,}", "price": pct(s["pct"]),
                "amount": won(s["amount"]), "reason": reason(s["name"]),
                "pctv": round(s["pct"], 4), "amtv": int(s["amount"])}

    pct_by_name = {s["name"]: s["pct"] for s in up + top_amt}
    if ai and ai.get("themes"):
        themes = [{"name": t.get("name", ""),
                   "stocks": ", ".join(f"{n} {pct(pct_by_name[n])}" if n in pct_by_name else n
                                       for n in t.get("stocks", [])),
                   "action": t.get("action", "")} for t in ai["themes"]]
    else:
        themes = [{"name": "(자동 분류 미설정)", "stocks": "-",
                   "action": "테마 묶음은 ANTHROPIC_API_KEY 를 설정하면 자동 생성됩니다."}]
        notes.append("상승 사유는 기사 제목 기반이며 테마 묶음은 생성되지 않았습니다.")

    # 요약(규칙 기반)
    summary = [" / ".join(f"{i['name']} {i['close']} ({i['change']}), 거래대금 {i['value']}" for i in indices)]
    k = flows.get("코스피")
    if k:
        summary.append(f"코스피 수급: 외국인 {signed_won(k['외국인'])}, 개인 {signed_won(k['개인'])}, "
                       f"기관 {signed_won(k['기관'])}")
    # 분모는 종목별 거래대금 합계(분자와 같은 출처)를 써서 100% 초과 오류를 막습니다
    if partial:  # 키움: 분모는 코스피 지수 거래대금(KRX), 분자는 순위표의 두 종목 합계
        kospi_amt = next((i["amount"] for i in idx if i["name"] == "코스피"), 0)
        mega = sum(src.amount_by_code.get(c, 0) for c in MEGA)
        if kospi_amt and mega and basis != "KRX":
            summary.append(f"삼성전자+SK하이닉스 거래대금(통합) {won(mega)} (코스피 지수 거래대금은 KRX 기준이라 비율은 생략)")
        elif kospi_amt and mega:
            summary.append(f"삼성전자+SK하이닉스 거래대금 {won(mega)}, 코스피 거래대금의 {mega / kospi_amt * 100:.1f}%")
        for mk in ("코스피", "코스닥"):
            if mk in src.breadth:
                u, dn, flat = src.breadth[mk]
                summary.append(f"{mk} 상승 {u} : 하락 {dn} (보합 {flat})")
    else:
        kospi_amt = sum(s["amount"] for s in stocks if s["market"] == "코스피")
        mega = sum(s["amount"] for s in stocks if s["code"] in MEGA and s["market"] == "코스피")
        if kospi_amt and mega:
            summary.append(f"삼성전자+SK하이닉스 거래대금 {won(mega)}, 코스피 거래대금의 {mega / kospi_amt * 100:.1f}%")
        for mk in ("코스피", "코스닥"):
            u = sum(1 for s in stocks if s["market"] == mk and s["pct"] > 0)
            dn = sum(1 for s in stocks if s["market"] == mk and s["pct"] < 0)
            if u + dn:
                summary.append(f"{mk} 상승 {u} : 하락 {dn}")

    head = next((i for i in indices if i["name"] == "코스피"), indices[0])
    kf = flows.get("코스피", {}).get("외국인")
    headline = f"코스피 {head['change']}" + (f" · 외국인 {signed_won(kf)}" if kf is not None else "")

    return {"session": session, "label": SESSIONS[session]["label"], "basis": basis,
            "asof": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "headline": headline, "indices": indices, "flows": flows, "summary": summary,
            "top500": [row(s) for s in up], "themes": themes, "topAmount": [row(s) for s in top_amt],
            "notes": notes}


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
    for k in ("indices", "summary", "top500", "themes", "topAmount", "notes"):  # 맨 위에는 가장 늦은 회차를 복사
        day[k] = s.get(k, [])
    day["headline"] = f"{SESSIONS[latest]['short']} · {s['headline']}"
    day["latest"] = latest
    day["compare"] = make_compare(sessions)
    return day


# ───────────────────────── 실행 ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="일일 시황 자동 수집 (하루 3회)")
    ap.add_argument("--session", choices=list(SESSIONS), default="close")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 한국 시간 오늘)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--mock", action="store_true", help="가짜 데이터로 점검")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--check", action="store_true", help="키움 연결만 점검(저장 없음): 인증·지수 조회가 되는지 확인")
    a = ap.parse_args()

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

    snap = build_snapshot(src, d, a.session, use_news=not a.no_news, api_key=os.environ.get("ANTHROPIC_API_KEY"))

    out = Path(a.out_dir) / f"{d.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    day = merge_day(out, d, a.session, snap)
    out.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    have = [SESSIONS[k]["short"] for k in ORDER if k in day["sessions"]]
    print(f"저장 완료: {out} · {SESSIONS[a.session]['short']} 반영 · 쌓인 회차: {', '.join(have)} · 비교 {len(day['compare'])}건")


if __name__ == "__main__":
    main()
