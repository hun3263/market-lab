"""backfill_choisinil.py : 과거 1~2년 동안 날마다 '그날 알 수 있던 자료만으로' 최+신+일 A·B 를 다시 뽑고,
그 뒤 +5·+10·+20거래일 수익률로 검증합니다. (조회·계산 전용)

왜: 관심종목은 매일 바뀌지만 패턴은 반복됩니다. 과거 A·B 를 채워 두면 ① 🔒 탭에서 지난 등장 이력을 함께 보고
    ② "A·B 로 뽑힌 종목이 실제로 그 뒤에 어땠나"를 등급·국면별로 숫자로 확인할 수 있습니다.
입력: backtest_dr.py fetch-kiwoom 이 만든 prices/종목코드.csv(시가·고가·저가·종가·거래대금), listing.csv, prices/_KOSPI.csv·_KOSDAQ.csv
재현 기준 (filter_choisinil.py 와 같은 뜻, 과거 자료로 계산 가능한 부분만)
  최  ① 최근 20거래일 DR 0일차 기준봉(+17%&1,000억 / +23%&700억 / 상한가&300억, DR1강 p2)
      ② RS 80 이상(IBD식 2×3개월+6·9·12개월 수익률, 그날 전 종목 백분위)
      ③ 최근 10거래일 거래대금 상위 30위 안에 3번 이상
      ※ 키움 테마 순위는 과거 자료가 없어 '강한 테마'는 빠집니다 → 실시간보다 '최'가 덜 잡힘 [추정]
  신  최근 20거래일 안에 250일 신고가(고가 기준)
  일  (선택) DART 공시: 그날 기준 최근 10일 안의 IR 개최·임상·품목허가 공시가 있으면 '일' [추정 근사]. DART_API_KEY 필요
한계: 상장폐지 종목이 없는 목록이면 생존편향 / 수수료·슬리피지 미반영(수익률은 종가 기준) / 테마 미반영
사용: python backfill_choisinil.py --prices prices --listing listing.csv --years 2 --out ~/market-lab-private/backfill
"""
import argparse
import bisect
import csv
import datetime as dt
import json
import os
import statistics as st
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import riskinfo

BASE = [(17.0, 1000e8), (23.0, 700e8), (29.5, 300e8)]
REGIME = {"KOSPI": [(-7, "D1"), (-14, "D2")], "KOSDAQ": [(-10, "D1"), (-18, "D2")]}


def read(p):
    with open(p, encoding="utf-8") as f:
        return [{k: (v if k == "date" else float(v)) for k, v in r.items()} for r in csv.DictReader(f)]


def dart_events(key, start, end):
    """[(공시일 'YYYY-MM-DD', 종목코드, 종류)] — IR 개최·임상·품목허가 (+ 일정형 공시)"""
    out = []
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    while s <= e:
        t = min(e, s + dt.timedelta(days=89))
        for ty in ("I", "B"):
            for page in range(1, 30):
                url = "https://opendart.fss.or.kr/api/list.json?" + urllib.parse.urlencode({
                    "crtfc_key": key, "bgn_de": s.strftime("%Y%m%d"), "end_de": t.strftime("%Y%m%d"),
                    "pblntf_ty": ty, "page_no": page, "page_count": 100})
                try:
                    data = json.loads(urllib.request.urlopen(url, timeout=30).read().decode())
                except Exception:
                    break
                if data.get("status") != "000":
                    break
                for it in data.get("list", []):
                    nm = it.get("report_nm", "").replace(" ", "")
                    k = "IR" if "기업설명회" in nm else "임상" if "임상" in nm else "품목허가" if "품목허가" in nm else None
                    if k and it.get("stock_code"):
                        d = it["rcept_dt"]
                        out.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", it["stock_code"], k))
                if int(data.get("total_page", 1)) <= page:
                    break
                time.sleep(0.2)
        s = t + dt.timedelta(days=1)
    return out


def main():
    global SHARES
    SHARES = riskinfo.Risk(dt.date.today(), use_dart=False).shares
    small_n = 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="prices")
    ap.add_argument("--listing", default="listing.csv")
    ap.add_argument("--years", type=float, default=2)
    ap.add_argument("--out", default=str(Path.home() / "market-lab-private" / "backfill"))
    a = ap.parse_args()
    pr = Path(a.prices)
    names = {}
    if Path(a.listing).exists():
        names = {r["code"]: (r["name"], r.get("market", "KOSPI")) for r in csv.DictReader(open(a.listing, encoding="utf-8"))}
    bars = {f.stem: read(f) for f in sorted(pr.glob("[0-9]*.csv"))}
    bars = {c: b for c, b in bars.items() if len(b) >= 60}
    idx = {k: read(pr / f"_{k}.csv") for k in ("KOSPI", "KOSDAQ") if (pr / f"_{k}.csv").exists()}
    dates = sorted({r["date"] for b in bars.values() for r in b})
    start = (dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=int(365 * a.years))).isoformat()
    test_days = [d for d in dates if d >= start]
    pos = {c: {r["date"]: i for i, r in enumerate(b)} for c, b in bars.items()}
    print(f"종목 {len(bars)} · 기간 {test_days[0]} ~ {test_days[-1]} ({len(test_days)}거래일)")

    ev = defaultdict(list)
    key = os.environ.get("DART_API_KEY", "").strip()
    if key:
        for d, c, k in dart_events(key, (dt.date.fromisoformat(start) - dt.timedelta(days=15)).isoformat(), dates[-1]):
            ev[c].append((d, k))
        print(f"DART 일정형 공시 {sum(len(v) for v in ev.values())}건")

    # 날짜별 RS 백분위와 거래대금 순위 미리 계산
    def ret(b, i, n):
        return b[i]["close"] / b[i - n]["close"] - 1 if i >= n and b[i - n]["close"] else None
    rs_pct, amt_top = {}, {}
    for d in test_days:
        sc, am = {}, []
        for c, b in bars.items():
            i = pos[c].get(d)
            if i is None:
                continue
            rr = [ret(b, i, n) for n in (63, 126, 189, 252)]
            if all(x is not None for x in rr):
                sc[c] = 2 * rr[0] + rr[1] + rr[2] + rr[3]
            am.append((b[i]["amount"], c))
        order = sorted(sc, key=sc.get)
        rs_pct[d] = {c: round((k + 1) / len(order) * 99) for k, c in enumerate(order)} if order else {}
        amt_top[d] = {c for _, c in sorted(am, reverse=True)[:30]}

    # 종목별 미리 계산: 250일 신고가 여부(prefix 합), 기준봉 여부
    from collections import deque
    nh_pre, base_at = {}, {}
    for c, b in bars.items():
        dq, flags = deque(), []
        for i, r in enumerate(b):                         # 단조 큐로 직전 250일 고가 최댓값
            while dq and dq[0] < i - 250:
                dq.popleft()
            flags.append(1 if i >= 250 and dq and r["high"] >= b[dq[0]]["high"] else 0)
            while dq and b[dq[-1]]["high"] <= r["high"]:
                dq.pop()
            dq.append(i)
        pre = [0]
        for f in flags:
            pre.append(pre[-1] + f)
        nh_pre[c] = pre
        ba = []
        for i in range(len(b)):
            r = (b[i]["close"] / b[i - 1]["close"] - 1) * 100 if i and b[i - 1]["close"] else 0
            ba.append(r if any(r >= th and b[i]["amount"] >= amt for th, amt in BASE) else None)
        base_at[c] = ba
    regime_at = {}
    for mk, rows_ in idx.items():
        closes = [x["close"] for x in rows_]
        for k, x in enumerate(rows_):
            win = closes[max(0, k - 249):k + 1]
            dd = (x["close"] / max(win) - 1) * 100
            regime_at[(mk, x["date"])] = "U" if dd > -2 else next((lab for th, lab in REGIME[mk] if dd >= th), "D3")

    out_days, outcome = [], defaultdict(dict)
    rows_eval = []
    for di, d in enumerate(test_days):
        recent10 = test_days[max(0, di - 9):di + 1]
        rows = []
        for c, b in bars.items():
            i = pos[c].get(d)
            if i is None or i < 21 or b[i]["close"] < 2000:
                continue
            why = []
            for j in range(i, max(0, i - 20), -1):         # ① 최근 20거래일 기준봉(가장 최근 것)
                if base_at[c][j] is not None:
                    why.append(f"기준봉 {b[j]['date'][5:]} {base_at[c][j]:+.0f}%")
                    break
            rs = rs_pct[d].get(c)
            if rs is not None and rs >= 80:
                why.append(f"RS {rs}")
            nt = sum(1 for x in recent10 if c in amt_top.get(x, ()))
            if nt >= 3:
                why.append(f"대금상위 {nt}회")
            choi = bool(why)
            shin = nh_pre[c][i + 1] - nh_pre[c][max(0, i - 19)] > 0
            il = [k for (ed, k) in ev.get(c, []) if (dt.date.fromisoformat(d) - dt.date.fromisoformat(ed)).days in range(0, 11)]
            score = int(choi) + int(shin) + int(bool(il))
            if score < 2:
                continue
            sh = SHARES.get(c)
            if sh and b[i]["close"] * sh < riskinfo.CAP_MIN:     # 시총 1,500억 미만 제외 [추정: 현재 주식수 × 그날 종가]
                continue
            g = "A" if score == 3 else "B"
            if sh and b[i]["close"] * sh < riskinfo.CAP_SMALL:
                small_n += 1
            w = {"최": why, "신": "250일 신고가(20일 내)" if shin else "", "일": ",".join(sorted(set(il)))}
            rows.append({"code": c, "name": names.get(c, (c,))[0], "grade": g, "score": score, "close": b[i]["close"], "why": w})
            o = {}
            for k in (5, 10, 20):
                if i + k < len(b):
                    o[f"r{k}"] = round((b[i + k]["close"] / b[i]["close"] - 1) * 100, 1)
            if i + 10 < len(b):
                o["mdd10"] = round((min(x["low"] for x in b[i + 1:i + 11]) / b[i]["close"] - 1) * 100, 1)
            outcome[c][d] = o
            mk = names.get(c, ("", "KOSPI"))[1]
            reg = regime_at.get((mk, d), "NA")
            rows_eval.append({"grade": g, "regime": reg, **o})
        out_days.append({"date": d, "rows": rows})

    def summ(rs):
        res = {"n": len(rs)}
        for k in ("r5", "r10", "r20", "mdd10"):
            v = [x[k] for x in rs if k in x]
            if v:
                res[k] = round(st.mean(v), 2)
                if k != "mdd10":
                    res[k + "_win%"] = round(sum(1 for x in v if x > 0) / len(v) * 100, 1)
        return res
    base_rows = []
    for d in test_days[::5]:                               # 기준선: 같은 날 전 종목 평균(5일 간격 표본)
        for c, b in bars.items():
            i = pos[c].get(d)
            if i is not None and i + 20 < len(b) and b[i]["close"] >= 2000:
                base_rows.append({"r5": (b[i + 5]["close"] / b[i]["close"] - 1) * 100, "r10": (b[i + 10]["close"] / b[i]["close"] - 1) * 100,
                                  "r20": (b[i + 20]["close"] / b[i]["close"] - 1) * 100})
    report = {"period": [test_days[0], test_days[-1]], "stocks": len(bars), "dart": bool(key),
              "all_AB": summ(rows_eval), "A": summ([x for x in rows_eval if x["grade"] == "A"]),
              "B": summ([x for x in rows_eval if x["grade"] == "B"]), "baseline_all_stocks": summ(base_rows),
              "by_regime": {r: summ([x for x in rows_eval if x["regime"] == r]) for r in sorted({x["regime"] for x in rows_eval})},
              "smallCap": small_n, "capRule": "시총 1,500억 미만 제외(현재 상장주식수 × 그날 종가로 추정)" if SHARES else "주식수 자료 없음 → 시총 제외 미적용",
              "note": "수익률=등장일 종가 기준, 비용 미반영. 같은 종목이 여러 날 연속 등장하면 각각 1건으로 셈(겹침 있음). 테마·실시간 목록 조건은 과거 재현 불가로 제외."}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    Path(a.out, "backfill.json").write_text(json.dumps({"days": out_days, "outcome": outcome, "report": report}, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
