"""dartfin.py : DART 분기 실적으로 'TTM PER'·'연환산 PER'과 3년치 지수 PER(추정)을 계산합니다. market.py 가 부릅니다.

용어
  TTM 순이익   최근 4개 분기 합 = 직전 사업연도 순이익 + 올해 누적 − 작년 같은 기간 누적
  TTM PER      시가총액 ÷ TTM 순이익                     (지금 실적 기준 PER)
  연환산 PER   시가총액 ÷ (가장 최근 분기 순이익 × 4)     (최근 분기 실적이 1년 이어진다고 가정한 '최근 실적 반영 PER')
  ※ 증권사 컨센서스 기반 '포워드 PER'과는 다릅니다(예상치는 무료 출처가 없음).

DART API
  fnlttMultiAcnt.json  다중회사 주요계정 — 한 번에 20개 회사, 보고서(1분기 11013 / 반기 11012 / 3분기 11014 / 사업 11011)
  분기·반기 보고서: thstrm_amount = 그 분기 3개월, thstrm_add_amount = 연초부터 누적 / 사업보고서: thstrm_amount = 1년

지수 PER(3년) 계산 규칙
  - 날짜 d 에는 'd 에 이미 공시됐을' 실적만 씀: 1분기 5/15, 반기 8/14, 3분기 11/14, 사업 다음해 3/31 이후 반영(법정 기한 기준)
  - Σ(종가 × 현재 상장주식수) ÷ Σ TTM 순이익, TTM 순이익 > 0 인 종목만(적자 제외)
  - 한계: 오늘 기준 시총 상위 300만 사용(과거 구성 변화 미반영), 상장주식수는 현재값, 순이익은 지배주주 몫이 아닌 연결 당기순이익
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

CACHE = Path(os.environ.get("MLAB_CACHE", Path.home() / ".market-lab-cache"))
REPORTS = [("11013", "1분기", (5, 15), 0), ("11012", "반기", (8, 14), 0), ("11014", "3분기", (11, 14), 0),
           ("11011", "사업", (3, 31), 1)]  # (코드, 이름, 반영일(월,일), 반영 연도 +n)
ORDER = {r[0]: i for i, r in enumerate(REPORTS)}
NI_NAMES = ("당기순이익", "분기순이익", "반기순이익", "당기순손익", "분기순손익", "반기순손익")
FIRST_YEAR_BACK = 4  # 올해 포함 5개 사업연도(3년 차트 + TTM 계산용 1년 여유)


def _amt(v):
    s = str(v or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def eff_date(year, reprt):
    _, _, (m, d), add = REPORTS[ORDER[reprt]]
    return dt.date(year + add, m, d)


def parse_multi(rows):
    """fnlttMultiAcnt 응답 → {corp_code: {"ytd": 누적, "q": 그 분기(사업보고서는 None), "fs": CFS|OFS}}"""
    by = {}
    for r in rows:
        nm = (r.get("account_nm") or "").replace(" ", "")
        if not nm.startswith(NI_NAMES):
            continue
        cc, fs = r.get("corp_code"), r.get("fs_div")
        if not cc:
            continue
        cur = by.get(cc)
        if cur and cur["fs"] == "CFS" and fs != "CFS":
            continue  # 연결 우선
        reprt = r.get("reprt_code")
        q = _amt(r.get("thstrm_amount"))
        add = _amt(r.get("thstrm_add_amount"))
        if reprt == "11011":
            by[cc] = {"ytd": q, "q": None, "fs": fs}
        else:
            by[cc] = {"ytd": add if add is not None else (q if reprt == "11013" else None), "q": q, "fs": fs}
    return by


class Fin:
    """분기 실적 캐시: CACHE/dartq/{연도}_{보고서}.json = {"fetched": 날짜, "data": {corp: {...}}, "asked": [corp...]}"""

    def __init__(self, dart=None, mock=False):
        self.dart, self.mock = dart, mock
        self.calls = 0

    def _path(self, year, reprt):
        return CACHE / ("dartq_mock" if self.mock else "dartq") / f"{year}_{reprt}.json"

    def period(self, year, reprt, corps, today, refresh_days=1):
        """corps 중 캐시에 없거나, 공시 기한 전후(최근) 보고서는 하루 1번 다시 물어봄"""
        p = self._path(year, reprt)
        c = _load(p, {"fetched": None, "data": {}, "asked": []})
        recent = today <= eff_date(year, reprt) + dt.timedelta(days=60)
        stale = recent and c.get("fetched") != today.isoformat()
        need = [x for x in corps if x not in set(c["asked"])] if not stale else list(corps)
        if eff_date(year, reprt) - dt.timedelta(days=50) > today:
            return c["data"]  # 아직 공시 시작 전
        if need:
            ok = 0
            for i in range(0, len(need), 20):
                chunk = need[i:i + 20]
                rows = self._fetch(chunk, year, reprt)
                if rows is None:  # 호출 실패 → '물어봄' 표시를 하지 않아 다음에 다시 시도
                    continue
                c["data"].update(parse_multi(rows))
                c["asked"] = sorted(set(c["asked"]) | set(chunk))
                ok += 1
            if ok:
                c["fetched"] = today.isoformat()
                _save(p, c)
        return c["data"]

    def _fetch(self, chunk, year, reprt):
        if self.mock:
            return mock_rows(chunk, year, reprt)
        self.calls += 1
        try:
            d = self.dart._json("fnlttMultiAcnt.json", corp_code=",".join(chunk), bsns_year=str(year), reprt_code=reprt)
        except Exception as e:
            print(f"[경고] DART 다중계정 {year} {reprt}: {e}", file=sys.stderr)
            return None
        return (d or {}).get("list", [])  # d=None 은 '자료 없음(013)' = 정상. 한도 초과·키 오류는 위 except 로 None


def build(fin, corp_of, today):
    """corp_of: {종목코드: corp_code}. 반환: {corp: [(반영일, 기간이름, ttm, 최근분기)]} 반영일 순"""
    corps = sorted(set(corp_of.values()))
    data = {}
    for y in range(today.year - FIRST_YEAR_BACK, today.year + 1):
        for reprt, *_ in REPORTS:
            data[(y, reprt)] = fin.period(y, reprt, corps, today)
    out = {}
    for cc in corps:
        seq = []
        for y in range(today.year - FIRST_YEAR_BACK + 1, today.year + 1):
            fy_prev = (data.get((y - 1, "11011")) or {}).get(cc, {}).get("ytd")
            for reprt, name, *_ in REPORTS:
                cur = (data.get((y, reprt)) or {}).get(cc)
                if not cur or cur.get("ytd") is None:
                    continue
                if reprt == "11011":
                    ttm = cur["ytd"]
                    q3 = (data.get((y, "11014")) or {}).get(cc, {}).get("ytd")
                    q = ttm - q3 if q3 is not None else None
                else:
                    prev = (data.get((y - 1, reprt)) or {}).get(cc, {}).get("ytd")
                    ttm = fy_prev + cur["ytd"] - prev if fy_prev is not None and prev is not None else None
                    q = cur.get("q")
                seq.append((eff_date(y, reprt), f"{y} {name}", ttm, q))
        out[cc] = sorted(seq)
    return out


def latest(seq, on=None):
    """on 날짜에 반영 가능한 마지막 기간(on=None 이면 받아 둔 것 중 마지막)"""
    cand = [s for s in seq if s[2] is not None and (on is None or s[0] <= on)]
    return cand[-1] if cand else None


def stock_metrics(seq, cap):
    """TTM PER, 연환산 PER (적자면 None)"""
    x = latest(seq)
    if not x or not cap:
        return {}
    _, label, ttm, q = x
    out = {"finPeriod": label, "ttmNi": ttm, "qNi": q}
    out["ttmPer"] = round(cap / ttm, 2) if ttm and ttm > 0 else None
    out["annPer"] = round(cap / (q * 4), 2) if q and q > 0 else None
    return out


def index_per_series(seqs, px, shares, market_of, start):
    """일별 지수 PER(추정). px: {code: {날짜: 종가}}. 반환 {"dates":[], "코스피":[], "코스닥":[], "n":{...}}"""
    dates = sorted({d for p in px.values() for d in p if d >= start})
    out = {"dates": [], "코스피": [], "코스닥": [], "n코스피": [], "n코스닥": []}
    # 종목별 반영 기간을 날짜순으로 훑기 위해 미리 정렬
    for ds in dates:
        day = dt.date.fromisoformat(ds)
        cap = {"코스피": 0.0, "코스닥": 0.0}
        ni = {"코스피": 0.0, "코스닥": 0.0}
        cnt = {"코스피": 0, "코스닥": 0}
        for code, seq in seqs.items():
            c = px.get(code, {}).get(ds)
            m = market_of.get(code)
            if c is None or m not in cap:
                continue
            x = latest(seq, day)
            if not x or not x[2] or x[2] <= 0:
                continue
            cap[m] += c * shares[code]
            ni[m] += x[2]
            cnt[m] += 1
        out["dates"].append(ds)
        for m in ("코스피", "코스닥"):
            out[m].append(round(cap[m] / ni[m], 2) if ni[m] > 0 and cnt[m] >= 10 else None)
            out["n" + m].append(cnt[m])
    return out


def mock_rows(chunk, year, reprt):
    import random
    rows = []
    for cc in chunk:
        r = random.Random(cc + str(year))
        base = r.uniform(-2e10, 4e11)
        qs = [base * r.uniform(0.7, 1.3) for _ in range(4)]
        k = ORDER[reprt]
        if reprt == "11011":
            rows.append({"corp_code": cc, "fs_div": "CFS", "account_nm": "당기순이익(손실)", "reprt_code": reprt,
                         "thstrm_amount": str(int(sum(qs)))})
        else:
            rows.append({"corp_code": cc, "fs_div": "CFS", "account_nm": "당기순이익(손실)", "reprt_code": reprt,
                         "thstrm_amount": str(int(qs[k])), "thstrm_add_amount": str(int(sum(qs[:k + 1])))})
    return rows
