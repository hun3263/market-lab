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
              [추정] 다음날 시가가 -8%↓ 갭하락이면 재료 훼손 의심 → 진입 보류 (11/04 정정: 장중 저가로 판단하면 미래 정보 사용)
  익절·손절   [팩트 方3강 p7·方2강 p40] 목표 수익 5% 전후 / [팩트 方3강 p37] 분할 후에도 -5%↑ 손실이면 판단 틀림 → 손절
              [추정] 첫 매수 후 5거래일 지나면 종가 정리(기간 손절), 체결한 날은 매도 판정 안 함(보수적)
              [추정] 같은 날 익절·손절이 겹치면 손절 우선, 갭이면 시가 체결
  비용        [추정] 왕복 0.25% (backtest_dr.py 와 같음)

입력  backtest_dr.py fetch-kiwoom 이 만든 prices/종목코드.csv (date,open,high,low,close,amount) · listing.csv · _KOSPI/_KOSDAQ.csv
사용  python backtest_bangbae.py --prices prices --listing listing.csv --from 2026-08-04 --to 2026-11-04 --tag 3M --out bt [--minutes fetch]
5분봉  --minutes fetch 면 minutes.py 로 필요한 날짜만 받아 ~/market-lab-private/minutes/5m 에 쌓고 V1·V2 결과를 함께 냄
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


def plan_of(b, i):
    """D0 다음날부터의 진입 계획. 미래 정보를 쓰지 않도록 '그 시점에 알 수 있는 것'만으로 정함
       D1: 직전 종가 -3%·-5% 지정가 → 체결되면 'D1 조정' / 안 되고 종가 +5%↑면 '5일선 대기' / 아니면 'D2 조정 매수'"""
    if i + 1 >= len(b):
        return None
    c0, d1 = b[i]["close"], b[i + 1]
    if d1["open"] <= c0 * (1 - P["crash"]):                    # 시가부터 -8% 갭 = 재료 훼손 의심(시가에 알 수 있음)
        return {"plan": "D1 -8% 갭하락 보류"}
    if d1["low"] <= c0 * (1 - P["dip1"]):
        return {"plan": "D1 조정 3~5%", "days": [i + 1], "lv": lambda k: [c0 * (1 - P["dip1"]), c0 * (1 - P["dip2"])]}
    if d1["close"] / c0 - 1 >= P["up_skip"]:
        def lv(k):
            m = ma(b, k - 1, 5)
            return [m, m * 0.98] if m else []
        return {"plan": "D1 +5%↑ → 5일선 대기", "days": list(range(i + 2, min(len(b), i + 2 + P["ma_wait_days"]))), "lv": lv, "once": True}
    if i + 2 >= len(b):
        return None
    ref = d1["close"]
    if b[i + 2]["open"] <= ref * (1 - P["crash"]):
        return {"plan": "D2 -8% 갭하락 보류"}
    return {"plan": "D1 조정 없음 → D2 조정 매수", "days": [i + 2], "lv": lambda k: [ref * (1 - P["dip1"]), ref * (1 - P["dip2"])]}


def _exit_daily(b, k0, avg):
    tp, sl = avg * (1 + P["tp"]), avg * (1 - P["sl"])
    for kk in range(k0 + 1, len(b)):
        y = b[kk]
        if y["open"] <= sl:
            return y["open"], "손절 -5%(갭)", kk
        if y["low"] <= sl:
            return sl, "손절 -5%", kk
        if y["open"] >= tp:
            return y["open"], "익절 +5%(갭)", kk
        if y["high"] >= tp:
            return tp, "익절 +5%", kk
        if kk - k0 >= P["hold_max"]:
            return y["close"], "기간(5일)", kk
    return None, None, None


def simulate(b, i):
    """[일봉] 지정가 체결 = 저가 ≤ 가격, 체결한 날은 매도 판정 안 함(보수적 근사)"""
    pl = plan_of(b, i)
    if not pl or "days" not in pl:
        return pl
    fills = []
    for k in pl["days"]:
        for lp in pl["lv"](k):
            if lp and b[k]["low"] <= lp:
                fills.append((k, min(lp, b[k]["open"])))
        if fills and pl.get("once"):
            break
        if fills:
            break
    if not fills:
        return {"plan": pl["plan"], "nofill": True}
    k0 = fills[0][0]
    avg = sum(px for _, px in fills) / len(fills)
    out_px, why, kk = _exit_daily(b, k0, avg)
    if out_px is None:
        return None
    return {"plan": pl["plan"], "entry": b[k0]["date"], "buys": len(fills), "avg": round(avg), "exit": why,
            "ret": round((out_px / avg - 1 - P["cost"]) * 100, 2), "hold": kk - k0}


# ── 5분봉 버전 ────────────────────────────────────────────────────────────────
# V1 분봉 정밀: 같은 지정가(-3·-5% / 5일선)를 5분봉 순서대로 체결하고, 체결한 날부터 익절·손절을 봉마다 판정
# V2 분봉 신호: 方4강 p9 '공포 신호'가 나올 때만 매수
#    [추정] 긴 음봉 = 몸통(시가-종가)이 직전 12봉 평균 몸통의 2배 이상인 음봉 / 장중 저점 이탈 = 10:00 이후 그날 최저가 경신
#    지정가 구간(-3% 이하 또는 5일선 이하)에 들어온 뒤 신호 봉 종가에 1차, 1차보다 -2% 아래 다음 신호에 2차
#    한 5분봉 안에서 익절가·손절가가 함께 닿으면 손절 우선(보수적)
BODY_N, BODY_X, LOW_FROM = 12, 2.0, "1000"


def _signal(bars, j):
    x = bars[j]
    if x["close"] >= x["open"]:
        return False
    prev = bars[max(0, j - BODY_N):j]
    body = abs(x["open"] - x["close"])
    avg = sum(abs(y["open"] - y["close"]) for y in prev) / len(prev) if prev else 0
    big = avg > 0 and body >= BODY_X * avg
    newlow = x["t"] >= LOW_FROM and prev and x["low"] < min(y["low"] for y in bars[:j])
    return bool(big or newlow)


def simulate_min(b, i, M, code, variant):
    pl = plan_of(b, i)
    if not pl or "days" not in pl:
        return pl
    need = [b[k]["date"] for k in range(pl["days"][0], min(len(b), pl["days"][-1] + P["hold_max"] + 2))]
    have = M.ensure(code, need)
    if b[pl["days"][0]]["date"] not in have:
        return {"plan": pl["plan"], "nomin": True}
    fills, k0 = [], None
    for k in pl["days"]:
        bars = M.load(code).get(b[k]["date"]) or []
        lv = [x for x in pl["lv"](k) if x]
        if not bars or not lv:
            continue
        for j, x in enumerate(bars):
            if variant == "V1":
                for lp in lv[len(fills):]:
                    if x["low"] <= lp:
                        fills.append((k, j, min(lp, x["open"])))
            else:
                ok = x["close"] <= lv[0] if not fills else x["close"] <= fills[0][2] * 0.98
                if ok and len(fills) < 2 and _signal(bars, j):
                    fills.append((k, j, x["close"]))
            if len(fills) >= 2:
                break
        if fills:
            k0 = k
            break
    if not fills:
        return {"plan": pl["plan"], "nofill": True}
    avg = sum(f[2] for f in fills) / len(fills)
    tp, sl = avg * (1 + P["tp"]), avg * (1 - P["sl"])
    j_last = fills[-1][1]
    for kk in range(k0, len(b)):
        bars = M.load(code).get(b[kk]["date"])
        if not bars:                                              # 분봉이 빠진 날은 일봉으로 판정
            y = b[kk]
            if kk > k0:
                if y["low"] <= sl:
                    return _res(pl, b, k0, fills, avg, min(sl, y["open"]), "손절 -5%", kk)
                if y["high"] >= tp:
                    return _res(pl, b, k0, fills, avg, max(tp, y["open"]), "익절 +5%", kk)
        else:
            for j, x in enumerate(bars):
                if kk == k0 and j <= j_last:
                    continue
                if x["low"] <= sl:
                    return _res(pl, b, k0, fills, avg, min(sl, x["open"]), "손절 -5%", kk)
                if x["high"] >= tp:
                    return _res(pl, b, k0, fills, avg, max(tp, x["open"]), "익절 +5%", kk)
        if kk - k0 >= P["hold_max"]:
            return _res(pl, b, k0, fills, avg, b[kk]["close"], "기간(5일)", kk)
    return None


def _res(pl, b, k0, fills, avg, px, why, kk):
    return {"plan": pl["plan"], "entry": b[k0]["date"], "buys": len(fills), "avg": round(avg), "exit": why,
            "ret": round((px / avg - 1 - P["cost"]) * 100, 2), "hold": kk - k0}


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
    M = None
    if a.minutes:
        import minutes
        src = None
        if a.minutes == "fetch":
            import collect
            src = collect.KiwoomSource("close")
        M = minutes.Minutes(src)
    trades, skipped = [], defaultdict(int)
    mtr = {"V1": [], "V2": []}
    for f in sorted(pr.glob("[0-9]*.csv")):
        b = dr.read_csv(f)
        for i in range(1, len(b) - 1):
            if not (a.frm <= b[i]["date"] <= a.to):
                continue
            su = setup(b, i, shares.get(f.stem))
            if not su:
                continue
            t = simulate(b, i)
            if M and t and "days" not in t:
                for v in mtr:
                    tm = simulate_min(b, i, M, f.stem, v)
                    if tm and "ret" in tm:
                        tm.update(su)
                        tm["code"], tm["name"], tm["variant"] = f.stem, mk.get(f.stem, {}).get("name", ""), v
                        mtr[v].append(tm)
                    elif tm:
                        skipped[f"[{v}] " + tm["plan"] + (" · 분봉 없음" if tm.get("nomin") else " · 미체결" if tm.get("nofill") else "")] += 1
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
    if M:
        rep["minutes"] = {"calls": M.calls, "sizeMB": M.size_mb(), "rule": "V1 = 같은 지정가를 5분봉 순서로 체결·청산 / V2 = 方4강 p9 공포 신호(긴 음봉·장중 저점 이탈) 진입"}
        for v, tl in mtr.items():
            rep["all_" + v] = dr.stats(tl)
            g = defaultdict(list)
            for t in tl:
                g[t["plan"]].append(t)
            rep["by_plan_" + v] = {k: dr.stats(x) for k, x in sorted(g.items())}
            with open(out / f"trades_bb_{v}_{a.tag}.csv", "w", newline="", encoding="utf-8-sig") as f:
                if tl:
                    w = csv.DictWriter(f, fieldnames=list(tl[0].keys())); w.writeheader(); w.writerows(tl)
    (out / f"report_bb_{a.tag}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("tag", "period", "all", "all_V1", "all_V2", "by_plan", "skipped", "minutes") if k in rep}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="prices")
    ap.add_argument("--listing", default="listing.csv")
    ap.add_argument("--from", dest="frm", default="2000-01-01")
    ap.add_argument("--to", default="2100-01-01")
    ap.add_argument("--tag", default="ALL")
    ap.add_argument("--out", default="bt")
    ap.add_argument("--minutes", choices=["fetch", "cache"], help="5분봉 비교: fetch = 키움에서 받아 쌓기(서버), cache = 저장된 것만")
    run(ap.parse_args())
