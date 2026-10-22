#!/usr/bin/env python3
"""collect.py : 장 마감 후 하루치 시황을 모아 data/날짜.json 으로 저장합니다.

모듈 구성 (위에서 아래로 한 단계씩 실행됩니다)
  1. 개장일 판단     : 주말이거나 지수 데이터가 비어 있으면 '휴장'으로 보고 조용히 종료
  2. 지수·수급       : 코스피/코스닥 종가·등락·거래대금, 개인/외국인/기관 순매수
  3. 종목 시세       : 전 종목에서 [500억 이상 & 상승률 TOP15], [거래대금 TOP15] 선별
  4. 뉴스 헤드라인   : 선별된 종목별 최근 기사 제목 수집 (상승 사유의 근거)
  5. 요약            : 규칙 기반 3~4줄 요약 + (키가 있으면) 사유·테마 문장 생성
  6. 저장            : data/날짜.json (기존 파일의 수동 입력 칸은 보존)

원칙: 2번(지수)만 필수입니다. 나머지 단계가 실패하면 그 칸에 사유를 남기고 계속 진행합니다.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
AMOUNT_MIN = 50_000_000_000  # 500억 원
TOP_N = 15
MEGA = {"005930": "삼성전자", "000660": "SK하이닉스"}  # 거래대금 쏠림 지표용


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
    """pykrx 로 KRX 정규장 데이터를 읽습니다. 출처가 바뀌면 이 클래스만 교체하면 됩니다."""

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


class MockSource:
    """네트워크 없이 파이프라인을 점검하기 위한 가짜 데이터. 실제 시장과 무관합니다."""

    def is_open(self, d):
        return d.weekday() < 5

    def indices(self, d):
        return [{"name": "코스피", "close": 3000.0, "prev": 3030.0, "amount": 12_000_000_000_000},
                {"name": "코스닥", "close": 900.0, "prev": 890.0, "amount": 6_000_000_000_000}]

    def flows(self, d):
        return {"코스피": {"개인": -467_800_000_000, "외국인": -1_296_900_000_000, "기관": 1_010_500_000_000},
                "코스닥": {"개인": 419_600_000_000, "외국인": -255_700_000_000, "기관": -179_700_000_000}}

    def stocks(self, d):
        rows = [{"code": "005930", "market": "코스피", "price": 70000, "pct": 1.1, "amount": 6_800_000_000_000},
                {"code": "000660", "market": "코스피", "price": 140000, "pct": -1.2, "amount": 7_700_000_000_000}]
        for i in range(1, 41):
            rows.append({"code": f"9{i:05d}", "market": "코스피" if i % 2 else "코스닥",
                         "price": 1000 * i, "pct": (i % 13) - 4.0 + i / 100,
                         "amount": (900 - i * 18) * 1_000_000_000})
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


# ───────────────────────── 하루치 조립 ─────────────────────────
def build_day(src, d, use_news=True, api_key=None):
    notes = ["거래대금·종목 순위는 KRX 정규장 기준이며 NXT(대체거래소) 거래는 포함하지 않습니다."]

    def safe(label, fn, default):
        try:
            return fn()
        except Exception as e:
            notes.append(f"수집 실패: {label} ({type(e).__name__})")
            print(f"[경고] {label} 실패: {e}", file=sys.stderr)
            return default

    # 2. 지수·수급 (필수)
    idx = src.indices(d)
    flows = safe("투자자별 수급", lambda: src.flows(d), {})
    stocks = safe("종목 시세", lambda: src.stocks(d), [])

    indices = []
    for i in idx:
        change = (i["close"] / i["prev"] - 1) * 100
        f = flows.get(i["name"])
        flow = (f"개인 {signed_won(f['개인'])} / 외인 {signed_won(f['외국인'])} / 기관 {signed_won(f['기관'])}"
                if f else "수집 실패")
        indices.append({"name": i["name"], "close": f"{i['close']:,.2f}", "change": pct(change),
                        "value": won(i["amount"]), "flow": flow, "_change": change})

    # 3. 종목 선별 (스팩 제외)
    def clean(rows):
        out = []
        for s in rows:
            s["name"] = src.name(s["code"])
            if "스팩" not in s["name"]:
                out.append(s)
        return out

    up = clean(sorted([s for s in stocks if s["pct"] > 0 and s["amount"] >= AMOUNT_MIN],
                      key=lambda s: -s["pct"])[:TOP_N * 2])[:TOP_N]
    top_amt = clean(sorted(stocks, key=lambda s: -s["amount"])[:TOP_N * 2])[:TOP_N]

    # 4. 뉴스
    heads = {}
    if use_news:
        for s in {x["code"]: x for x in up + top_amt}.values():
            heads[s["name"]] = headlines(s["name"])
            time.sleep(0.3)

    # 5. 사유·테마
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
                "amount": won(s["amount"]), "reason": reason(s["name"])}

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
    if flows:
        k = flows.get("코스피")
        if k:
            summary.append(f"코스피 수급: 외국인 {signed_won(k['외국인'])}, 개인 {signed_won(k['개인'])}, "
                           f"기관 {signed_won(k['기관'])}")
    # 분모는 종목별 거래대금 합계(분자와 같은 출처)를 써서 100% 초과 오류를 막습니다
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
    k = flows.get("코스피", {}).get("외국인")
    headline = f"코스피 {head['change']}" + (f" · 외국인 {signed_won(k)}" if k is not None else "")
    for i in indices:
        i.pop("_change", None)

    return {"date": d.isoformat(), "headline": headline, "indices": indices, "summary": summary,
            "top500": [row(s) for s in up], "themes": themes, "topAmount": [row(s) for s in top_amt],
            "notes": notes}


# ───────────────────────── 실행 ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="일일 시황 자동 수집")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 한국 시간 오늘)")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--mock", action="store_true", help="가짜 데이터로 점검")
    ap.add_argument("--no-news", action="store_true")
    a = ap.parse_args()

    d = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(KST).date()
    if a.mock:
        src = MockSource()
    else:
        try:
            src = KrxSource()
        except ImportError:
            raise SystemExit("[오류] pykrx 가 설치되지 않았습니다. pip install -r requirements.txt")

    try:
        opened = src.is_open(d)
    except Exception as e:
        raise SystemExit(f"[오류] 개장 여부를 확인하지 못했습니다(출처 접속 문제 가능): {e}")
    if not opened:
        print(f"{d} 는 휴장일이거나 데이터가 아직 없습니다. 저장하지 않고 종료합니다.")
        return

    day = build_day(src, d, use_news=not a.no_news, api_key=os.environ.get("ANTHROPIC_API_KEY"))

    out = Path(a.out_dir) / f"{d.isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():  # 직접 써 둔 칸(스터디·매매 아이디어)은 보존
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
            for k in ("study", "ideas"):
                if k in old:
                    day[k] = old[k]
        except json.JSONDecodeError:
            pass
    out.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장 완료: {out} (수집 메모 {len(day['notes'])}건)")


if __name__ == "__main__":
    main()
