"""dart.py : 금융감독원 OpenDART API 로 종목 상세 화면에 쓸 '기업 개요(사업보고서 발췌)'와 '최근 3년 재무'를 받습니다.

  - 키: 환경변수 DART_API_KEY (서버의 ~/.market-lab.env 에만 둡니다. GitHub·대화에 올리지 않습니다)
  - 캐시: 회사코드 목록은 ~/.market-lab-cache/corpcode.json (30일마다 갱신)
          종목별 결과는 data/stocks/종목코드.json 안에 저장 → 재무 30일, 기업 개요 120일이 지나야 다시 받습니다.
  - 점검: python dart.py 005930   (키가 있는 서버에서 삼성전자로 시험, 저장 없음)

사용하는 OpenDART API (https://opendart.fss.or.kr)
  corpCode.xml      : 종목코드(6자리) → 고유번호(corp_code) 대응표 (zip)
  company.json      : 기업개황(대표자·설립일·홈페이지·결산월)
  fnlttSinglAcnt.json : 단일회사 주요계정 — 사업보고서 1건에 당기·전기·전전기(3년) 금액이 함께 들어 있음
  list.json         : 공시 검색 → 가장 최근 사업보고서 접수번호(rcept_no)
  document.xml      : 공시 원문(zip 안의 XML) → 'II. 사업의 내용 / 1. 사업의 개요' 앞부분 발췌
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://opendart.fss.or.kr/api/"
CACHE = Path(os.environ.get("MLAB_CACHE", Path.home() / ".market-lab-cache"))
FIN_DAYS, BIZ_DAYS = 30, 120
EXCERPT_CHARS = 600

# 주요계정 이름(회사마다 표기가 조금씩 다름 → 앞부분 일치로 찾기)
ACCOUNTS = {
    "revenue": ("매출액", "수익(매출액)", "영업수익"),
    "op": ("영업이익", "영업손익"),
    "net": ("당기순이익", "당기순손익", "분기순이익"),
    "assets": ("자산총계",),
    "liab": ("부채총계",),
    "equity": ("자본총계",),
}


class Dart:
    def __init__(self, key):
        self.key = key
        self._map = None
        self.calls = 0

    # ── 통신 ──
    def _get(self, api, **params):
        params["crtfc_key"] = self.key
        url = BASE + api + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "market-lab"}),
                                            timeout=30) as r:
                    self.calls += 1
                    time.sleep(0.2)
                    return r.read()
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

    def _json(self, api, **params):
        data = json.loads(self._get(api, **params).decode("utf-8"))
        st = data.get("status")
        if st == "013":  # 조회된 데이터 없음
            return None
        if st != "000":
            raise RuntimeError(f"DART {api} 오류 {st}: {data.get('message')}")  # 020=한도 초과, 010=키 오류
        return data

    # ── 종목코드 → 고유번호 ──
    def corp_code(self, stock_code):
        if self._map is None:
            path = CACHE / "corpcode.json"
            fresh = path.exists() and (time.time() - path.stat().st_mtime) < 30 * 86400
            if not fresh:
                z = zipfile.ZipFile(io.BytesIO(self._get("corpCode.xml")))
                xml = z.read(z.namelist()[0]).decode("utf-8")
                m = {}
                for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):
                    sc = re.search(r"<stock_code>\s*(\d{6})\s*</stock_code>", blk)
                    cc = re.search(r"<corp_code>(\d+)</corp_code>", blk)
                    if sc and cc:
                        m[sc.group(1)] = cc.group(1)
                CACHE.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(m), encoding="utf-8")
            self._map = json.loads(path.read_text(encoding="utf-8"))
        return self._map.get(stock_code)

    # ── 기업개황 ──
    def company(self, cc):
        d = self._json("company.json", corp_code=cc) or {}
        est = d.get("est_dt", "")
        return {"corp": d.get("corp_name", ""), "ceo": d.get("ceo_nm", ""),
                "est": f"{est[:4]}-{est[4:6]}-{est[6:]}" if len(est) == 8 else est,
                "home": d.get("hm_url", ""), "fiscal": (d.get("acc_mt", "") + "월") if d.get("acc_mt") else ""}

    # ── 최근 3년 재무(사업보고서 기준, 연결 우선) ──
    def financials(self, cc, today):
        for year in (today.year - 1, today.year - 2):  # 사업보고서는 보통 3월 말 제출 → 연초엔 전전년도까지 내려감
            d = self._json("fnlttSinglAcnt.json", corp_code=cc, bsns_year=str(year), reprt_code="11011")
            if d and d.get("list"):
                return parse_fin(d["list"], year)
        return None

    # ── 사업보고서 '사업의 개요' 발췌 ──
    def overview(self, cc, today):
        d = self._json("list.json", corp_code=cc, bgn_de=(today - dt.timedelta(days=500)).strftime("%Y%m%d"),
                       end_de=today.strftime("%Y%m%d"), pblntf_ty="A", pblntf_detail_ty="A001",
                       last_reprt_at="Y", page_count="10")
        reps = [r for r in (d or {}).get("list", []) if "사업보고서" in r.get("report_nm", "")]
        if not reps:
            return None
        rep = sorted(reps, key=lambda r: r["rcept_dt"])[-1]
        raw = self._get("document.xml", rcept_no=rep["rcept_no"])
        text = excerpt_from_zip(raw)
        if not text:
            return None
        return {"text": text, "report": rep["report_nm"].strip(), "date": rep["rcept_dt"],
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rep['rcept_no']}"}


# ───────────────────────── 파싱(네트워크 없이 시험 가능한 부분) ─────────────────────────
def _amt(s):
    s = str(s or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_fin(rows, year):
    """fnlttSinglAcnt 응답 → {"basis", "years":[Y-2,Y-1,Y], "revenue":[..], ...} (단위: 원, 없으면 None)"""
    basis = "CFS" if any(r.get("fs_div") == "CFS" for r in rows) else "OFS"
    rows = [r for r in rows if r.get("fs_div") == basis]
    out = {"basis": "연결" if basis == "CFS" else "별도", "years": [year - 2, year - 1, year]}
    for key, names in ACCOUNTS.items():
        r = next((x for n in names for x in rows if x.get("account_nm", "").replace(" ", "").startswith(n)), None)
        if r is None:
            out[key] = [None, None, None]
        else:
            out[key] = [_amt(r.get("bfefrmtrm_amount")), _amt(r.get("frmtrm_amount")), _amt(r.get("thstrm_amount"))]
    return out


def _clean(fragment):
    fragment = re.sub(r"<TABLE.*?</TABLE>", " ", fragment, flags=re.S | re.I)  # 표(숫자 나열)는 건너뜀
    fragment = re.sub(r"<(P|BR|TITLE|SECTION-\d)[^>]*>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def excerpt(xml, limit=EXCERPT_CHARS):
    """사업보고서 XML 에서 '사업의 개요' 절의 앞부분(문장 단위로 limit 자 이내)"""
    m = re.search(r"<TITLE[^>]*>\s*(?:\d+\.\s*)?사업의\s*개요\s*</TITLE>", xml)
    if not m:  # 옛 양식: 'II. 사업의 내용' 바로 아래 본문
        m = re.search(r"<TITLE[^>]*>[^<]*사업의\s*내용\s*</TITLE>", xml)
    if not m:
        return None
    nxt = xml.find("<TITLE", m.end())
    body = _clean(xml[m.end(): nxt if nxt > 0 else m.end() + 30000])
    if not body:
        return None
    if len(body) <= limit:
        return body
    cut = body[:limit]
    end = max(cut.rfind("다."), cut.rfind("니다."))
    return (cut[: end + 2] if end > limit * 0.4 else cut.rstrip()) + " …"


def excerpt_from_zip(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = sorted(z.namelist(), key=lambda n: len(n))  # 본문 파일은 접수번호.xml (첨부보다 이름이 짧음)
    for n in names:
        b = z.read(n)
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                xml = b.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            continue
        t = excerpt(xml)
        if t:
            return t
    return None


# ───────────────────────── 종목 파일 보강 ─────────────────────────
def _stale(stamp, days):
    if not stamp:
        return True
    try:
        return (dt.date.today() - dt.date.fromisoformat(stamp[:10])).days >= days
    except ValueError:
        return True


def enrich(info, code, dart, today):
    """종목 파일(dict)에 profile/fin/biz 를 채웁니다. 무거운 호출을 했으면 True."""
    heavy = False
    cc = info.get("corp_code") or dart.corp_code(code)
    if not cc:
        info["dartNote"] = "DART 고유번호 없음(ETF·리츠·외국기업 등)"
        return False
    info["corp_code"] = cc
    if _stale((info.get("profile") or {}).get("updated"), BIZ_DAYS):
        info["profile"] = dict(dart.company(cc), updated=today.isoformat())
    if _stale((info.get("fin") or {}).get("updated"), FIN_DAYS):
        f = dart.financials(cc, today)
        info["fin"] = dict(f or {"none": True}, updated=today.isoformat())
    if _stale((info.get("biz") or {}).get("updated"), BIZ_DAYS):
        b = dart.overview(cc, today)
        info["biz"] = dict(b or {"none": True}, updated=today.isoformat())
        heavy = True
    return heavy


if __name__ == "__main__":  # 점검: python dart.py 005930
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise SystemExit("[오류] DART_API_KEY 환경변수가 없습니다. ~/.market-lab.env 에 넣었는지 확인하세요.")
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    d = Dart(key)
    today = dt.date.today()
    cc = d.corp_code(code)
    print("1) 고유번호:", cc)
    print("2) 기업개황:", d.company(cc))
    f = d.financials(cc, today)
    print("3) 재무(" + (f or {}).get("basis", "-") + "):", {k: v for k, v in (f or {}).items() if k != "basis"})
    o = d.overview(cc, today)
    print("4) 사업의 개요 발췌:", (o or {}).get("report"), "\n", (o or {}).get("text", "(찾지 못함)")[:300])
    print(f"DART 호출 {d.calls}회")
