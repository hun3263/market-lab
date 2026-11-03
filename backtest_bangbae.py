"""backtest_bangbae.py : 방배동선수 '재료 매매' 일봉 백테스트 (조회·계산 전용, 주문 기능 없음)

테스트하는 것 = 방배동 매매 3종 중 일봉만으로 과거 재현이 되는 '재료 매매'
  시장 매매  → 과거 테마 순위 기록이 없어 제외 (market-lab 테마 기록이 쌓이면 추가)
  이벤트 매매 → 과거 '행사 날짜'를 일봉·공시 목록만으로 알 수 없어 제외 (DART 공시일 ≠ 행사일)

규칙 출처 — [팩트] 방배동 교안 원문 / [추정] 일봉으로 옮기며 정한 근사
  재료일 D0   [추정·사용자 결정 11/02] +10%&1,000억 / +15%&500억 / +20%&300억 중 하나, 2,000원↑
              [사용자 결정 11/02] 시총 1,500억 미만 제외 (과거 시총 = 현재 상장주식수 × 그날 종가 [추정])
  다음날 대응 [팩트 方4강 p8] 조정 없이 상승 마감 → 진입 X (장중 -3%↑ 밀리면 가능)
                              다음날 +5%↑ 상승 → 진입 X, 5일선 대기 / 5% 이하 → 다음날 조정 시 매수
                              조정 3~5% → 진입 O
  분할        [팩트 方4강 p7] 첫 매수 20~30%, 평균 5구간
              [추정] 1차 = 기준가 -3%, 2차 = 기준가 -5% (1:1). 기준가 = 직전 종가
              [추정] D1 장중 -8%↑ 급락 = 재료 훼손 의심 → 진입 보류
  익절·손절   [팩트 方3강 p7·方2강 p40] 목표 수익 5% 전후 / [팩트 方3강 p37] 분할 후에도 -5%↑ 손실이면 판단 틀림 → 손절
              [추정] 첫 매수 후 5거래일 지나면 종가 정리(기간 손절), 체결한 날은 매도 판정 안 함(보수적)
              [추정] 같은 날 익절·손절이 겹치면 손절 우선, 갭이면 시가 체결
  비용        [추정] 왕복 0.25% (backtest_dr.py 와 같음)

입력  backtest_dr.py fetch-kiwoom 이 만든 prices/종목코드.csv (date,open,high,low,close,amount) · listing.csv · _KOSPI/_KOSDAQ.csv
사용  python backtest_bangbae.py --prices prices --listing listing.csv --from 2026-08-04 --to 2026-11-04 --tag 3M --out bt
"""
import argparse
import csv
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import backtest_dr as dr
import riskinfo

P = dict(tiers=[(10.0, 1000e8), (15.0, 500e8), (20.0, 300e8)], min_price=2000,
         dip1=0.03, dip2=0.05, crash=0.08, up_skip=0.05, ma_wait_days=5,
         tp=0.05, sl=0.05, hold_max=5, cost=0.0025)


def ma(b, i, n):
    if i + 1 < n:
        return None
    return sum(x["close"] for x in b[i + 1 - n:i + 1]) / n


def setup(b, i, shares):
    """i = 재료일(D0) 인덱스 → 조건 판정"""
    if i < 1:
        return None
    x, p = b[i], b[i - 1]
    pct = round((x["close"] / p["close"] - 1) * 100, 2)          # 반올림: 15.0% 가 14.9999 로 빠지지 않게
    if x["close"] < P["min_price"]:
        return None
    tier = next((f"+{th:g}%&{a / 1e8:,.0f}억" for th, a in P["tiers"] if pct >= th and x["amount"] >= a), None)
    if not tier:
        return None
    if shares and x["close"] * shares < riskinfo.CAP_MIN:
        return None
    small = bool(shares and x["close"] * shares < riskinfo.CAP_SMALL)
    return {"d0": x["date"], "pct0": round(pct, 1), "tier": tier, "small": small}


def simulate(b, i):
    """D0 다음날부터 방배동 4강 p8 흐름대로 진입 → 익절·손절·기간"""
    if i + 1 >= len(b):
        return None
    c0 = b[i]["close"]
    d1 = b[i + 1]
    dip = 1 - d1["low"] / c0
    r1 = d1["close"] / c0 - 1
    if dip >= P["crash"]:
        return {"plan": "D1 급락 보류"}
    # 진입 계획: (진입 시작 인덱스, 기준가 또는 이평, 계획 이름)
    if dip >= P["dip1"]:
        plan, start, ref = "D1 조정 3~5%", i + 1, c0
    elif r1 >= P["up_skip"]:
        plan, start, ref = "D1 +5%↑ → 5일선 대기", i + 2, None
    else:
        plan, start, ref = "D1 조정 없음 → D2 조정 매수", i + 2, d1["close"]
    fills = []                                                 # [(인덱스, 가격)]
    if ref is None:                                            # 5일선 닿는 날 1차, 거기서 -2% 2차 [추정]
        for k in range(start, min(len(b), start + P["ma_wait_days"])):
            m = ma(b, k - 1, 5)
            if m and b[k]["low"] <= m:
                px = min(m, b[k]["open"])
                fills.append((k, px))
                if b[k]["low"] <= px * 0.98:
                    fills.append((k, px * 0.98))
                break
    elif start < len(b):
        k = start
        if k == i + 2 and 1 - b[k]["low"] / ref >= P["crash"]:   # D2 도 급락이면 보류
            return {"plan": "D2 급락 보류"}
        for lv in (P["dip1"], P["dip2"]):                        # 1차 -3%, 2차 -5%
            lp = ref * (1 - lv)
            if b[k]["low"] <= lp:
                fills.append((k, min(lp, b[k]["open"])))
    if not fills:
        return {"plan": plan, "nofill": True}
    k0 = fills[0][0]
    avg = sum(px for _, px in fills) / len(fills)
    tp, sl = avg * (1 + P["tp"]), avg * (1 - P["sl"])
    out_px, why, kk = None, None, None
    for kk in range(k0 + 1, len(b)):
        y = b[kk]
        if y["open"] <= sl:
            out_px, why = y["open"], "손절 -5%(갭)"; break
        if y["low"] <= sl:
            out_px, why = sl, "손절 -5%"; break
        if y["open"] >= tp:
            out_px, why = y["open"], "익절 +5%(갭)"; break
        if y["high"] >= tp:
            out_px, why = tp, "익절 +5%"; break
        if kk - k0 >= P["hold_max"]:
            out_px, why = y["close"], "기간(5일)"; break
    if out_px is None:
        return None                                            # 데이터 끝 → 제외
    return {"plan": plan, "entry": b[k0]["date"], "buys": len(fills), "avg": round(avg), "exit": why,
            "ret": round((out_px / avg - 1 - P["cost"]) * 100, 2), "hold": kk - k0}


def run(a):
    pr = Path(a.prices)
    for k in ("KOSPI", "KOSDAQ"):
        f = pr / f"_{k}.csv"
        if f.exists():
            dr.IDX[k] = dr.read_csv(f)
    mk = {}
    if Path(a.listing).exists():
        with open(a.listing, encoding="utf-8") as f:
            mk = {r["code"]: r for r in csv.DictReader(f)}
    shares = riskinfo.Risk(dt.date.today(), use_dart=False).shares
    trades, skipped = [], defaultdict(int)
    for f in sorted(pr.glob("[0-9]*.csv")):
        b = dr.read_csv(f)
        for i in range(1, len(b) - 1):
            if not (a.frm <= b[i]["date"] <= a.to):
                continue
            su = setup(b, i, shares.get(f.stem))
            if not su:
                continue
            t = simulate(b, i)
            if not t:
                continue
            if "ret" not in t:
                skipped[t["plan"] + (" · 미체결" if t.get("nofill") else "")] += 1
                continue
            t.update(su)
            t["code"], t["name"] = f.stem, mk.get(f.stem, {}).get("name", "")
            m = "KOSDAQ" if mk.get(f.stem, {}).get("market") == "KOSDAQ" else "KOSPI"
            t["regime"] = dr.regime_of(dr.IDX[m], t["entry"], m)[0] if m in dr.IDX else "NA"
            trades.append(t)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with open(out / f"trades_bb_{a.tag}.csv", "w", newline="", encoding="utf-8-sig") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys())); w.writeheader(); w.writerows(trades)
    rep = {"tag": a.tag, "period": [a.frm, a.to], "params": P, "all": dr.stats(trades), "skipped": dict(skipped),
           "sharesKnown": bool(shares)}
    for key in ("plan", "tier", "regime", "exit", "buys", "small"):
        g = defaultdict(list)
        for t in trades:
            g[str(t[key])].append(t)
        rep["by_" + key] = {k: dr.stats(v) for k, v in sorted(g.items())}
    (out / f"report_bb_{a.tag}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("tag", "period", "all", "by_plan", "skipped")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="prices")
    ap.add_argument("--listing", default="listing.csv")
    ap.add_argument("--from", dest="frm", default="2000-01-01")
    ap.add_argument("--to", default="2100-01-01")
    ap.add_argument("--tag", default="ALL")
    ap.add_argument("--out", default="bt")
    run(ap.parse_args())
