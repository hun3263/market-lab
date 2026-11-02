"""backtest_dr.py : 드림로드 'DR 트레이딩(상단눌림)' 규칙 백테스트 (일봉 기준, 조회·계산 전용)

규칙 출처 (드림로드 1강, 2026 봄학기) — [팩트]는 교안 원문, [추정]은 일봉으로 옮기며 정한 근사
  0일차 기준봉 [팩트 p2] : 종가 +17% & 거래대금 1,000억↑ / +23% & 700억↑ / 상한가 (+ 필터 p29: 300억↑)
  종목 선정   [팩트 p14] : 기준봉 이후 5거래일 이내, 0일차 저점 대비 종가상승률 50%↑,
                          선정일 종가 +10%↑ 양봉, 일봉상 직전 고점 돌파
              [추정] 직전 고점 = 기준봉 이전 120거래일 최고가
  6등분선     [팩트 p8]  : 피보나치 0.17·0.34·0.5·0.67·0.84 (저점~고점)
              [추정] n차선 = 고점 − n/6 × (고점 − 0일차 저점), 고점 = 매수 전날까지의 최고가(갱신 반영)
  매수        [팩트 p27] : 2차선 1회 + 3차선 1회 (선정일 종가가 2차선 근처·아래면 3차선·4차선), 1:1:1
              [팩트 p28] : 3차 = 직전 매수가 대비 −6~7% (기본 −6.5%)
              [추정] 매수 기간 = 선정 다음날부터 5거래일
  매도·손절   [팩트 p28] : 1차만 +5% / 2차까지 본전 50% 후 나머지 +5% / 3차까지 본전 70% 후 나머지 +1~3%(기본 +2%)
                          데드라인: 3차 매수 후 −5% / 기간: 1차 후 5일 초과 시 정리 / 1차 후 −4~5% 밀린 뒤 본전 오면 전량
  체결 가정   [추정] 지정가는 저가 ≤ 가격이면 체결(갭 하락 시 시가 체결), 같은 날 손절·익절이 겹치면 손절 우선(보수적)

입력 데이터 (서버에서 fetch 로 1회 생성)
  prices/종목코드.csv : date,open,high,low,close,amount   (amount = 원)
  prices/_KOSPI.csv, prices/_KOSDAQ.csv : date,close         (국면 태깅용)
  listing.csv : code,name,market      ※ 상장폐지 종목까지 포함해야 생존편향이 줄어듭니다

사용
  python backtest_dr.py fetch-kiwoom --start 2023-01-01 --out prices   (market-lab 서버: 키움 키·허용 IP 그대로 사용, 약 20~40분)
  python backtest_dr.py fetch --start 2022-01-01 --end 2026-10-30 --out prices   (pykrx 필요, KRX 접속 가능한 서버)
  python backtest_dr.py run --prices prices --from 2023-01-01 --to 2024-12-31 --tag IS
  python backtest_dr.py run --prices prices --from 2025-01-01 --to 2026-10-30 --tag OOS
"""
import argparse
import csv
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

P = dict(
    base=[(17.0, 1000e8), (23.0, 700e8), (29.5, 300e8)],
    sel_days=5, sel_gain=0.50, sel_candle=10.0, prior_high_n=120,
    buy_days=5, line_a=2, line_b=3, near=0.02, step3=0.065,
    tp1=0.05, tp2=0.05, tp3=0.02, be_cut2=0.5, be_cut3=0.7,
    deadline=0.05, hold_max=5, be_trigger=0.04,
    cost=0.0025,  # [추정] 왕복 수수료+세금+슬리피지. 실제 세율·수수료는 증권사·시점별로 확인 후 수정
    min_price=2000,
)
REGIME_TH = {"KOSPI": [(-7, "D1"), (-14, "D2")], "KOSDAQ": [(-10, "D1"), (-18, "D2")]}  # [팩트] DR5강 p65


def read_csv(p):
    with open(p, encoding="utf-8") as f:
        return [{k: (v if k == "date" else float(v)) for k, v in r.items()} for r in csv.DictReader(f)]


def regime_of(idx_rows, date, mkt="KOSPI"):
    rows = [r for r in idx_rows if r["date"] <= date][-250:]
    if not rows:
        return "NA", 0.0
    hi = max(r["close"] for r in rows)
    dd = (rows[-1]["close"] / hi - 1) * 100
    if dd > -2:
        return "U", dd
    return next((lab for th, lab in REGIME_TH[mkt] if dd >= th), "D3"), dd


IDX = {}


def find_setups(b):
    """b = 일봉 리스트 → 선정(setup) 목록"""
    out, skip_to = [], 0
    for i in range(1, len(b)):
        if i <= skip_to:          # [추정] 진행 중인 셋업 안의 새 기준봉은 같은 거래로 취급(차선 갱신은 simulate 의 고점 갱신으로 반영)
            continue
        r = (b[i]["close"] / b[i - 1]["close"] - 1) * 100
        if not any(r >= th and b[i]["amount"] >= amt for th, amt in P["base"]):
            continue
        low0 = b[i]["low"]
        prior_hi = max([x["high"] for x in b[max(0, i - P["prior_high_n"]):i]] or [0])
        for s in range(i, min(i + P["sel_days"] + 1, len(b))):
            cr = (b[s]["close"] / b[s - 1]["close"] - 1) * 100
            if (b[s]["close"] / low0 - 1 >= P["sel_gain"] and cr >= P["sel_candle"] and b[s]["close"] > b[s]["open"]
                    and b[s]["close"] > prior_hi and b[s]["close"] >= P["min_price"]):
                out.append({"base": i, "sel": s, "low0": low0})
                skip_to = s + P["buy_days"] + P["hold_max"]
                break
    return out


def simulate(b, su):
    """한 건의 셋업 → 거래 결과(dict) 또는 None(미체결)"""
    s, low0 = su["sel"], su["low0"]
    hi = max(x["high"] for x in b[su["base"]:s + 1])
    line = lambda n, h: h - n / 6 * (h - low0)
    la, lb = P["line_a"], P["line_b"]
    if b[s]["close"] <= line(la, hi) * (1 + P["near"]):
        la, lb = la + 1, lb + 1                                 # [팩트] p27-2
    fills, qty, cash_out, cash_in = [], 0.0, 0.0, 0.0
    pos_q, first_day, touched_down, max_buys = 0.0, None, False, 0
    sold_be = False
    last_fill = None
    for d in range(s + 1, len(b)):
        x = b[d]
        if pos_q == 0 and fills:
            break
        if not fills and d > s + P["buy_days"]:
            return None                                        # 매수 기간 내 미체결
        if not fills:
            hi = max(hi, b[d - 1]["high"])
        avg = cash_out / qty if qty else None
        # ── 매수 ──
        targets = []
        if len(fills) == 0:
            targets = [line(la, hi)]
        elif len(fills) == 1:
            targets = [line(lb, hi)]
        elif len(fills) == 2 and not sold_be:
            targets = [fills[-1][1] * (1 - P["step3"])]
        if targets and (d <= s + P["buy_days"] or fills):
            t = targets[0]
            if x["low"] <= t:
                px = min(t, x["open"])
                fills.append((b[d]["date"], px))
                qty += 1; pos_q += 1; cash_out += px
                first_day = first_day or d
                last_fill = d
                avg = cash_out / qty
                max_buys = len(fills)
        if not fills:
            continue
        # ── 손절 (우선) ──
        n = len(fills)
        if n == 3 and x["low"] <= fills[-1][1] * (1 - P["deadline"]):
            px = min(fills[-1][1] * (1 - P["deadline"]), x["open"])
            cash_in += pos_q * px; pos_q = 0; why = "데드라인"
            break
        if d == last_fill:          # [추정] 체결 당일은 고·저 순서를 알 수 없어 익절·본전 판정 보류
            continue
        if x["low"] <= avg * (1 - P["be_trigger"]):
            touched_down = True
        # ── 익절·본전 ──
        if n == 1:
            if touched_down and x["high"] >= avg:
                cash_in += pos_q * avg; pos_q = 0; why = "본전(1차 후 -4%)"; break
            if x["high"] >= avg * (1 + P["tp1"]):
                cash_in += pos_q * avg * (1 + P["tp1"]); pos_q = 0; why = "익절+5%"; break
        else:
            cut = P["be_cut2"] if n == 2 else P["be_cut3"]
            tp = P["tp2"] if n == 2 else P["tp3"]
            if not sold_be and x["high"] >= avg:
                q = pos_q * cut
                cash_in += q * avg; pos_q -= q; sold_be = True
            if sold_be and x["high"] >= avg * (1 + tp):
                cash_in += pos_q * avg * (1 + tp); pos_q = 0; why = f"본전{int(cut*100)}%+잔량{int(tp*100)}%"; break
        # ── 기간 손절 ──
        if first_day is not None and d - first_day >= P["hold_max"]:
            cash_in += pos_q * x["close"]; pos_q = 0; why = "기간(5일)"; break
    else:
        if fills and pos_q:
            cash_in += pos_q * b[-1]["close"]; why = "데이터 종료"
        elif not fills:
            return None
    ret = cash_in / cash_out - 1 - P["cost"]
    return {"base": b[su["base"]]["date"], "sel": b[s]["date"], "entry": fills[0][0], "buys": max_buys,
            "avg": round(cash_out / qty), "ret": round(ret * 100, 2), "exit": why, "hold": d - first_day}


def stats(trades):
    if not trades:
        return {}
    r = [t["ret"] for t in trades]
    w = [x for x in r if x > 0]
    l = [x for x in r if x <= 0]
    aw = st.mean(w) if w else 0
    al = -st.mean(l) if l else 0
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in r:                                              # 1건당 MAX 비중 10% 가정 [팩트 DR1강 p27]
        eq += x * 0.10
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"n": len(r), "win%": round(len(w) / len(r) * 100, 1), "avgWin%": round(aw, 2), "avgLoss%": round(al, 2),
            "payoff": round(aw / al, 2) if al else None, "BEwin%": round(al / (aw + al) * 100, 1) if (aw + al) else None,
            "expect%": round(st.mean(r), 2), "sumAt10%w": round(eq, 1), "mddAt10%w": round(mdd, 1)}


def run(a):
    pr = Path(a.prices)
    for k in ("KOSPI", "KOSDAQ"):
        f = pr / f"_{k}.csv"
        if f.exists():
            IDX[k] = read_csv(f)
    mk = {}
    if (pr.parent / "listing.csv").exists() or Path(a.listing).exists():
        with open(a.listing, encoding="utf-8") as f:
            mk = {r["code"]: r for r in csv.DictReader(f)}
    trades = []
    for f in sorted(pr.glob("[0-9]*.csv")):
        b = read_csv(f)
        for su in find_setups(b):
            if not (a.frm <= b[su["sel"]]["date"] <= a.to):
                continue
            t = simulate(b, su)
            if t:
                t["code"] = f.stem
                t["name"] = mk.get(f.stem, {}).get("name", "")
                m = "KOSDAQ" if mk.get(f.stem, {}).get("market") == "KOSDAQ" else "KOSPI"
                t["regime"], t["idxDD"] = regime_of(IDX[m], t["entry"], m) if m in IDX else ("NA", 0)
                t["idxDD"] = round(t["idxDD"], 1)
                trades.append(t)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with open(out / f"trades_{a.tag}.csv", "w", newline="", encoding="utf-8-sig") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys())); w.writeheader(); w.writerows(trades)
    rep = {"tag": a.tag, "period": [a.frm, a.to], "params": P, "all": stats(trades)}
    for key in ("regime", "buys", "exit"):
        g = defaultdict(list)
        for t in trades:
            g[str(t[key])].append(t)
        rep["by_" + key] = {k: stats(v) for k, v in sorted(g.items())}
    g = defaultdict(list)
    for t in trades:
        g[t["entry"][:4]].append(t)
    rep["by_year"] = {k: stats(v) for k, v in sorted(g.items())}
    (out / f"report_{a.tag}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("tag", "period", "all", "by_regime", "by_buys")}, ensure_ascii=False, indent=1))


def fetch(a):
    """pykrx 로 일봉 저장 (KRX 접속 가능한 서버에서 실행). 상장폐지 종목 포함을 위해 기간 중 여러 날짜의 종목 목록을 합칩니다."""
    from pykrx import stock
    import pandas as pd
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    days = pd.bdate_range(a.start, a.end, freq="3MS")
    codes = {}
    for d in list(days) + [pd.Timestamp(a.end)]:
        ds = d.strftime("%Y%m%d")
        for m in ("KOSPI", "KOSDAQ"):
            for c in stock.get_market_ticker_list(ds, market=m):
                codes.setdefault(c, (stock.get_market_ticker_name(c), m))
    with open(Path(a.out).parent / "listing.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["code", "name", "market"])
        for c, (n, m) in sorted(codes.items()):
            w.writerow([c, n, m])
    s, e = a.start.replace("-", ""), a.end.replace("-", "")
    for c in sorted(codes):
        df = stock.get_market_ohlcv(s, e, c)
        if df.empty:
            continue
        with open(out / f"{c}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "open", "high", "low", "close", "amount"])
            for idx, r in df.iterrows():
                if r["시가"] == 0:
                    continue
                w.writerow([idx.strftime("%Y-%m-%d"), r["시가"], r["고가"], r["저가"], r["종가"], r.get("거래대금", r["종가"] * r["거래량"])])
    for name, tk in (("KOSPI", "1001"), ("KOSDAQ", "2001")):
        df = stock.get_index_ohlcv(s, e, tk)
        with open(out / f"_{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "close"])
            for idx, r in df.iterrows():
                w.writerow([idx.strftime("%Y-%m-%d"), r["종가"]])


def fetch_kiwoom(a):
    """키움 ka10081(수정주가 일봉) 로 전 종목 OHLC·거래대금 저장. 상장폐지 종목은 목록에 없어 생존편향이 남습니다(결과 해석 시 주의).
    지수는 series.json 대신 업종일봉 ka20006(001 코스피, 101 코스닥)."""
    import datetime as dt
    import collect
    import market
    src = collect.KiwoomSource("close")
    mk = market.backend(src)
    lst = mk.stock_list()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    with open(out.parent / "listing.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["code", "name", "market"])
        for c, v in sorted(lst.items()):
            w.writerow([c, v["name"], "KOSDAQ" if v["market"] == "코스닥" else "KOSPI"])
    start = a.start.replace("-", "")
    today = dt.date.today().strftime("%Y%m%d")
    n = 0
    for c, v in sorted(lst.items()):
        if collect.is_etf(v["name"]) or "스팩" in v["name"] or (out / f"{c}.csv").exists():
            continue
        rows = []
        try:
            for pg in src._call("/api/dostk/chart", "ka10081", {"stk_cd": c, "base_dt": today, "upd_stkpc_tp": "1"}, pages=4):
                rows += pg.get("stk_dt_pole_chart_qry", [])
        except Exception as e:
            print(f"[건너뜀] {c} {v['name']}: {e}")
            continue
        rows = sorted({r["dt"]: r for r in rows if r.get("dt") and r["dt"] >= start}.values(), key=lambda r: r["dt"])
        with open(out / f"{c}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "open", "high", "low", "close", "amount"])
            for r in rows:
                g = lambda k: abs(float(str(r.get(k) or 0).replace(",", "").replace("+", "") or 0))
                if g("open_pric") == 0:
                    continue
                w.writerow([f"{r['dt'][:4]}-{r['dt'][4:6]}-{r['dt'][6:]}", g("open_pric"), g("high_pric"), g("low_pric"),
                            g("cur_prc"), g("trde_prica") * 1e6])          # 거래대금 단위 백만원(collect.py 실측 주석) → 원
        n += 1
        if n % 100 == 0:
            print(f"{n}종목 저장")
    for name, code in (("KOSPI", "001"), ("KOSDAQ", "101")):
        rows = []
        for pg in src._call("/api/dostk/chart", "ka20006", {"inds_cd": code, "base_dt": today}, pages=4):
            rows += pg.get("inds_dt_pole_qry", [])
        with open(out / f"_{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "close"])
            for r in sorted(rows, key=lambda r: r.get("dt", "")):
                if r.get("dt", "") >= start:
                    w.writerow([f"{r['dt'][:4]}-{r['dt'][4:6]}-{r['dt'][6:]}", abs(float(str(r["cur_prc"]).replace(",", "").replace("+", ""))) / 100])
    print(f"완료: {n}종목 + 지수 2개 → {out}  (지수 값 단위는 market.py index_daily 와 같은지 확인: 100으로 나눔 [추정])")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch"); f.add_argument("--start"); f.add_argument("--end"); f.add_argument("--out", default="prices")
    k = sp.add_parser("fetch-kiwoom"); k.add_argument("--start", default="2023-01-01"); k.add_argument("--out", default="prices")
    r = sp.add_parser("run"); r.add_argument("--prices", default="prices"); r.add_argument("--listing", default="listing.csv")
    r.add_argument("--from", dest="frm", default="2000-01-01"); r.add_argument("--to", default="2100-01-01")
    r.add_argument("--tag", default="ALL"); r.add_argument("--out", default="bt")
    a = ap.parse_args()
    {"fetch": fetch, "fetch-kiwoom": fetch_kiwoom}.get(a.cmd, run)(a)
