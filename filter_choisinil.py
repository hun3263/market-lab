"""filter_choisinil.py : market-lab 수집 자료로 드림로드 '최+신+일' 후보를 고릅니다. (조회·계산 전용, 주문 기능 없음)

  최 (최근에 강한 종목)   : ① 0일차 기준봉(DR1강 p2) 최근 20거래일 발생  ② RS ≥ 80 (heat, 시총 상위 300)
                           ③ 최근 10거래일 상승률·거래대금·RS 목록 반복 등장  ④ 강한 업종/테마 소속
  신 (52주 신고가)        : series.json 'nh'(키움 ka10016, 250일 기준) 최근 20거래일 등장
  일 (일정재료/기대감)    : ① events.csv 수동 일정표 (1~20거래일 이내)  ② 뉴스 제목의 일정 표현 자동 탐지(후보, 사람이 확인)

v2 (2026-11-02): ① events.csv 테마는 '부분 일치'(예: 반도체 → 반도체_후공정), 최근 10거래일 테마 전체로 매칭
                 ② kind=M 은 시장 일정(지수형)으로 따로 표시   ③ 기준봉 종목의 매수 자리(차선·10/20일선 중첩) 추정치 계산

사용:  python filter_choisinil.py --data data [--date 2026-10-30] [--events events.csv] [--out data/choisinil]
결과:  화면 표 + data/choisinil/날짜.json (journal 복기·백테스트 기록용)
"""
import argparse
import csv
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import riskinfo

# ── 기준값 (강의 원문 = [팩트], 나머지 = [추정] 초기값. 백테스트로 조정) ───────────────
BASE_BARS = [(17.0, 1000e8), (23.0, 700e8), (29.5, 300e8)]   # [팩트] DR1강 p2: +17%&1,000억 / +23%&700억 / 상한가(필터 p29의 300억↑ 함께 적용)
LOOKBACK_BASE = 20      # [추정] 기준봉 유효 기간(거래일)
LOOKBACK_NH = 20        # [추정] 신고가 유효 기간(거래일)
RS_MIN = 80             # [추정] IBD식 RS 백분위
APPEAR_MIN = 3          # [추정] 최근 10거래일 목록 등장 횟수
THEME_DAYS = 5          # [추정] 강한 테마 판정 기간
THEME_MIN_DAYS = 3      # [추정] 5일 중 3일 이상 상위 테마
EVENT_WINDOW = (1, 20)  # [팩트] 方3강 p3: 1주 이내 이벤트 5·10일선, 4주 이내 20일선 → 1~20거래일
ZONE_TOL = 0.02         # [추정] 차선과 이평이 ±2% 안이면 '중첩'
MIN_PRICE = 2000        # [팩트] DR1강 p29 필터: 2,000원 미만 제외
MIN_AMOUNT = 300e8      # [팩트] DR1강 p29 필터: 거래대금 300억 미만 제외(기준봉·최근 최대 대금 기준으로 적용)
# [팩트] DR5강 p65 지수 구간 (고점 대비 하락률)
REGIME = {"코스피": [(-7, "D1"), (-14, "D2")], "코스닥": [(-10, "D1"), (-18, "D2")]}

SCHEDULE_WORDS = ("예정", "앞두고", "개최", "발표", "결과 발표", "출시", "승인", "심사", "회의", "정상회담",
                  "방한", "방문", "학회", "공개", "상장 예정", "임상", "선정", "입찰", "착공", "수주 기대", "D-")
DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일|(\d{1,2})/(\d{1,2})|(다음\s*주|이번\s*주|내주|다음\s*달|내달|연내|올해\s*안)")
NEG_WORDS = ("유상증자", "전환사채", "CB", "보호예수", "의무보유", "해제", "철회", "무산", "불발", "사실무근", "소송", "취소")


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def won_to_num(s):
    """'1조 3,557억' → 원 단위 숫자"""
    if not s or s == "-":
        return 0
    s = s.replace(",", "").replace(" ", "")
    v = 0
    m = re.search(r"([\d.]+)조", s)
    if m:
        v += float(m.group(1)) * 1e12
    m = re.search(r"([\d.]+)억", s)
    if m:
        v += float(m.group(1)) * 1e8
    return v


# ── 지수 구간 ────────────────────────────────────────────────────────────────
def index_regime(series, d):
    out = {}
    for name, rows in (series.get("index") or {}).items():
        rows = [r for r in rows if r[0] <= d][-250:]
        if not rows:
            continue
        hi = max(r[1] for r in rows)
        last = rows[-1][1]
        ddp = (last / hi - 1) * 100
        reg = "U"
        if ddp < 0:
            reg = "D3"
            for th, lab in REGIME.get(name, []):
                if ddp >= th:
                    reg = lab
                    break
        if ddp > -2:
            reg = "U"
        out[name] = {"close": last, "high250": hi, "dd": round(ddp, 1), "regime": reg}
    return out


# ── 최: 기준봉 ───────────────────────────────────────────────────────────────
def base_bars(prices):
    """prices = [[YYYY-MM-DD, 종가, 거래대금(원)], ...] → 최근 LOOKBACK_BASE 안의 0일차 기준봉 목록"""
    hits = []
    for i in range(max(1, len(prices) - LOOKBACK_BASE), len(prices)):
        d, c, a = prices[i][:3]
        pc = prices[i - 1][1]
        if not pc:
            continue
        r = round((c / pc - 1) * 100, 2)
        if any(r >= th and a >= amt for th, amt in BASE_BARS):
            hits.append({"date": d, "ret": round(r, 1), "amount억": round(a / 1e8)})
    return hits


def five_day_50(prices, base_idx):
    """[팩트] DR1강 p14: 기준봉 이후 5거래일 이내, 0일차 저점(근사: 전일 종가) 대비 종가상승률 50% 이상"""
    if base_idx < 1:
        return False
    low0 = prices[base_idx - 1][1]
    win = prices[base_idx: base_idx + 5]
    return any(p[1] / low0 - 1 >= 0.5 for p in win)


# ── 최: 강한 테마·업종 ──────────────────────────────────────────────────────
NAMES = {}


def strong_themes(day_files):
    """일별 JSON의 themes(키움 테마 상위 8) → 최근 THEME_DAYS 중 THEME_MIN_DAYS 이상 등장 테마"""
    cnt, pct, members = Counter(), defaultdict(list), defaultdict(set)
    for f in day_files[-THEME_DAYS:]:
        for t in (load(f, {}) or {}).get("themes", []):
            if not isinstance(t.get("stocks"), list):   # 구형식 건너뜀
                continue
            cnt[t["name"]] += 1
            pct[t["name"]].append(t.get("pct", 0))
            for s in t.get("stocks", []):
                members[t["name"]].add(s["code"])
                NAMES.setdefault(s["code"], s.get("name", s["code"]))
    out = {}
    for name, n in cnt.items():
        if n >= THEME_MIN_DAYS:
            out[name] = {"days": n, "avgPct": round(sum(pct[name]) / len(pct[name]), 2), "codes": sorted(members[name])}
    return out


def strong_sectors(heat):
    """heat(시총 상위 300) 업종별: RS≥80 비율과 당일 평균 등락률 → 비율 상위 업종"""
    if not heat:
        return {}
    cols = heat["cols"]
    ix = {c: i for i, c in enumerate(cols)}
    g = defaultdict(list)
    for r in heat["rows"]:
        g[(r[ix["market"]], r[ix["sector"]])].append(r)
    out = {}
    for (mk, sec), rows in g.items():
        if len(rows) < 3:
            continue
        rs = [r[ix["rs"]] for r in rows if r[ix["rs"]] is not None]
        share = sum(1 for x in rs if x >= RS_MIN) / len(rows)
        out[f"{mk}|{sec}"] = {"n": len(rows), "rs80share": round(share, 2),
                              "avgPct": round(sum(r[ix["pct"]] or 0 for r in rows) / len(rows), 2)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["rs80share"])[:8])


# ── 일: 일정 ────────────────────────────────────────────────────────────────
def trading_days_between(d0, d1):
    a, b = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1)
    n, x = 0, a
    while x < b:
        x += dt.timedelta(days=1)
        if x.weekday() < 5:
            n += 1
    return n


def load_events(path, d):
    """events.csv 열: date,code,theme,event,kind(+/-),source  (code 비우면 theme 전체에 적용)"""
    out, macro = [], []
    if not path or not Path(path).exists():
        return out, macro
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r = {k: (v or "").strip() for k, v in r.items() if k}
            try:
                n = trading_days_between(d, r["date"])
            except Exception:
                continue
            r["dday"] = n
            if r.get("kind", "+").upper() == "M":      # 시장 일정: 종목에 붙이지 않고 따로 표시
                if 0 <= n <= EVENT_WINDOW[1]:
                    macro.append(r)
            elif EVENT_WINDOW[0] <= n <= EVENT_WINDOW[1]:
                out.append(r)
    return out, sorted(macro, key=lambda x: x["date"])


def buy_zone(prices, bar_date):
    """[추정] 종가 기준 근사 매수 자리. DR1강 p8 6등분선(n차선 = 고점 − n/6 × (고점 − 0일차 저점))
    + 10·20일 이동평균. 저점 = 기준봉 전날 종가, 고점 = 기준봉 이후 최고 종가 (일봉 고가·저가가 없어 종가로 대신함)"""
    idx = next((i for i, p in enumerate(prices) if p[0] == bar_date), None)
    if idx is None or idx < 1 or len(prices) < 20:
        return None
    has_hl = all(len(p) >= 5 and p[3] for p in prices[idx:])
    if has_hl:                                   # collect.py v2 이후: [날짜, 종가, 대금, 고가, 저가]
        low0 = prices[idx][4]                    # [팩트 DR1강 p14] 0일차(기준봉) 저점
        high = max(p[3] for p in prices[idx:])   # 기준봉 이후 최고가(장중 고가)
    else:                                        # 예전 자료: 종가로 근사
        low0 = prices[idx - 1][1]
        high = max(p[1] for p in prices[idx:])
    if high <= low0:
        return None
    closes = [p[1] for p in prices]
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    lines = {n: round(high - n / 6 * (high - low0)) for n in (2, 3, 4)}
    close = closes[-1]
    over = []
    for n, v in lines.items():
        for nm, m in (("10일선", ma10), ("20일선", ma20)):
            if abs(v / m - 1) <= ZONE_TOL:
                over.append(f"{n}차선≈{nm}")
    below = [n for n, v in lines.items() if close <= v]
    pos = f"{max(below)}차선 아래" if below else "2차선 위(아직 눌림 전)"
    return {"basis": "고가·저가" if has_hl else "종가 근사", "low0": low0, "high": high, "lines": lines,
            "ma10": round(ma10), "ma20": round(ma20), "overlap": over, "pos": pos,
            "toLine2%": round((lines[2] / close - 1) * 100, 1)}


def schedule_hints(texts):
    """뉴스 제목에서 일정 표현 탐지 → (후보 문장, 악재 단어 포함 여부). 확정이 아니라 '확인할 목록'"""
    hits = []
    for t in texts:
        if not t:
            continue
        if any(w in t for w in SCHEDULE_WORDS) and DATE_RE.search(t):
            hits.append({"t": t[:80], "neg": any(w in t for w in NEG_WORDS)})
    return hits


# ── 메인 ────────────────────────────────────────────────────────────────────
def run(data, d=None, events=None, out=None):
    data = Path(data)
    day_files = sorted(p for p in data.glob("20*.json"))
    if d:
        day_files = [p for p in day_files if p.stem <= d]
    d = day_files[-1].stem
    RISK, capout = riskinfo.Risk(d), []
    series = load(data / "market" / "series.json", {})
    heat = load(data / "heat" / f"{d}.json") or (load(sorted((data / "heat").glob("20*.json"))[-1]) if (data / "heat").exists() else None)
    regime = index_regime(series, d)

    # 신: 최근 LOOKBACK_NH 거래일 신고가
    nh_days = sorted(k for k in (series.get("nh") or {}) if k <= d)[-LOOKBACK_NH:]
    nh_cnt = Counter(c for k in nh_days for c in series["nh"][k])
    nh_last = {}
    for k in nh_days:
        for c in series["nh"][k]:
            nh_last[c] = k

    # 최: 목록 반복 등장 (최근 10거래일, 마지막 회차 기준)
    appear, names, last_amt = Counter(), {}, {}
    for f in day_files[-10:]:
        dd = load(f, {}) or {}
        for sn in (dd.get("sessions") or {}).values():
            for x in ((sn.get("indicators") or {}).get("newHigh") or {}).get("all", []):
                NAMES.setdefault(x["code"], x.get("name", x["code"]))
        for key in ("top500", "topAmount", "rs15"):
            for s in dd.get(key, []):
                if "code" not in s:      # 초기(구형식) 파일은 종목코드가 없어 건너뜀
                    continue
                appear[s["code"]] += 1
                names[s["code"]] = s["name"]
                last_amt[s["code"]] = max(last_amt.get(s["code"], 0), won_to_num(s.get("amount")))
    rs_map = {}
    if heat:
        ix = {c: i for i, c in enumerate(heat["cols"])}
        for r in heat["rows"]:
            rs_map[r[ix["code"]]] = (r[ix["rs"]], r[ix["sector"]], r[ix["market"]])
            names.setdefault(r[ix["code"]], r[ix["name"]])
    themes = strong_themes(day_files)
    theme_of = defaultdict(list)
    for tn, t in themes.items():
        for c in t["codes"]:
            theme_of[c].append(tn)
    all_theme_of = defaultdict(set)          # 최근 10거래일 상위 테마 전체 소속(일정 매칭용)
    for f in day_files[-10:]:
        for t in (load(f, {}) or {}).get("themes", []):
            if isinstance(t.get("stocks"), list):
                for x in t["stocks"]:
                    all_theme_of[x["code"]].add(t["name"])
    sectors = strong_sectors(heat)
    ev, macro = load_events(events, d)
    cal = load(data / "calendar" / "calendar.json")        # 증시 일정 탭 자료가 있으면 시장 일정은 거기서 가져옴
    if cal:
        macro = [{"date": x["date"], "event": x["title"] + {"confirmed": "", "rule": " (규칙 계산)"}.get(x["status"], " (추정)"),
                  "dday": trading_days_between(d, x["date"]), "source": x.get("src", "")}
                 for x in cal["events"] if x["cat"] in ("매크로·금리", "지수·수급", "정책·외교", "실적·공시")
                 and x["date"] >= d and trading_days_between(d, x["date"]) <= EVENT_WINDOW[1]]

    # 후보 모집단 = 신고가 ∪ 반복 등장 ∪ 강한 테마 구성 ∪ 종목 상세 파일 보유 종목
    stock_dir = data / "stocks"
    universe = set(nh_cnt) | {c for c, n in appear.items() if n >= 2} | set(theme_of)
    universe |= {p.stem for p in stock_dir.glob("*.json")} if stock_dir.exists() else set()

    rows = []
    for c in sorted(universe):
        info = load(stock_dir / f"{c}.json", {}) or {}
        name = info.get("name") or names.get(c) or NAMES.get(c, c)
        if "스팩" in name or "기업인수목적" in name:
            continue
        prices = info.get("prices") or []
        prices = [p for p in prices if p[0] <= d]
        close = prices[-1][1] if prices else None
        maxamt = max([p[2] for p in prices[-20:]] + [last_amt.get(c, 0)]) if prices else last_amt.get(c, 0)

        bars = base_bars(prices) if len(prices) > 2 else []
        b50 = any(five_day_50(prices, next(i for i, p in enumerate(prices) if p[0] == b["date"])) for b in bars)
        rs = rs_map.get(c, (None,))[0]
        sec = rs_map.get(c, (None, None, None))
        sec_key = f"{sec[2]}|{sec[1]}" if sec[1] else None

        choi_why = []
        if bars:
            choi_why.append(f"기준봉 {bars[-1]['date'][5:]} {bars[-1]['ret']:+.0f}%/{bars[-1]['amount억']:,}억" + (" ·5일50%" if b50 else ""))
        if rs is not None and rs >= RS_MIN:
            choi_why.append(f"RS {rs}")
        if appear[c] >= APPEAR_MIN:
            choi_why.append(f"목록 {appear[c]}회")
        if theme_of.get(c):
            choi_why.append("강한테마:" + ",".join(theme_of[c][:2]))
        if sec_key in sectors:
            choi_why.append("강한업종:" + sec[1])
        choi = bool(choi_why)

        shin = nh_cnt[c] > 0
        shin_why = f"신고가 {nh_cnt[c]}회(최근 {nh_last[c][5:]})" if shin else ""

        my_ev = [e for e in ev if e.get("code") == c or
                 (not e.get("code") and e.get("theme") and any(e["theme"] in tn for tn in all_theme_of.get(c, ())))]
        texts = [a.get("reason", "") for a in info.get("appear", [])]
        for a in info.get("appear", []):
            texts += [n.get("t", "") for n in a.get("news", []) if isinstance(n, dict)]
        hints = schedule_hints(texts)
        il = bool([e for e in my_ev if e.get("kind", "+") != "-"])
        il_why = "; ".join(f"D-{e['dday']} {e['event']}" for e in my_ev)
        if not il and hints and not all(h["neg"] for h in hints):
            il_why = "뉴스후보(확인필요): " + hints[0]["t"]

        flags = []
        if close is not None and close < MIN_PRICE:
            flags.append("2,000원 미만")
        if maxamt and maxamt < MIN_AMOUNT:
            flags.append("대금 300억 미만")
        if any(h["neg"] for h in hints) or any(e.get("kind") == "-" for e in my_ev):
            flags.append("악재 일정/단어")

        zone = buy_zone(prices, bars[-1]["date"]) if bars else None
        score = int(choi) + int(shin) + int(il)
        if score == 0:
            continue
        risk = RISK.assess(c, close, prices[-1][2] if prices else None, info)   # 시총·희석 공시(riskinfo.py)
        if risk is None:
            capout.append(name)
            continue
        flags += riskinfo.flags_of(risk)
        grade = {3: "A 최신일", 2: "B", 1: "C"}[score]
        rows.append({"cap": risk["cap억"], "small": risk["small"], "hold": risk["hold"], "dil": risk["dil"],"code": c, "name": name, "grade": grade, "score": score, "close": close,
                     "choi": choi, "shin": shin, "il": il, "ilHint": bool(hints) and not il,
                     "why": {"최": choi_why, "신": shin_why, "일": il_why}, "flags": flags,
                     "themes": sorted(all_theme_of.get(c, ())), "zone": zone})

    rows.sort(key=lambda r: (-r["score"], -int(r["ilHint"]), r["flags"] != [], r["name"]))
    result = {"date": d, "regime": regime, "strongThemes": themes, "strongSectors": sectors,
              "events": ev, "macro": macro, "params": {"RS_MIN": RS_MIN, "LOOKBACK_NH": LOOKBACK_NH, "EVENT_WINDOW": EVENT_WINDOW},
              "nhDays": len(nh_days), "rows": rows,
              "capOut": {"min억": int(riskinfo.CAP_MIN / 1e8), "n": len(capout), "names": capout[:30]}}
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{d}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def show(res, limit=30):
    print(f"■ 기준일 {res['date']}  (신고가 기록 {res['nhDays']}거래일치)")
    for k, v in res["regime"].items():
        print(f"  {k}: {v['close']:,.2f}  250일 고점 대비 {v['dd']}% → 구간 {v['regime']}")
    print("■ 강한 테마(최근 5일 중 3일↑ 상위 8):", ", ".join(f"{k}({v['days']}일, 평균 {v['avgPct']}%)" for k, v in res["strongThemes"].items()) or "없음")
    print("■ 강한 업종(RS80 비율):", ", ".join(f"{k}({v['rs80share']:.0%})" for k, v in list(res["strongSectors"].items())[:5]) or "없음")
    for m in res.get("macro", []):
        print(f"■ 시장 일정 D-{m['dday']} {m['date']} {m['event']}")
    print(f"■ 후보 {len(res['rows'])}개 (상위 {limit})")
    for r in res["rows"][:limit]:
        mark = "".join(x if r[k] else "·" for x, k in (("최", "choi"), ("신", "shin"), ("일", "il")))
        print(f"  [{r['grade']:6}] {mark} {r['name']}({r['code']}) {r['close'] or '-'}")
        for k in ("최", "신", "일"):
            w = r["why"][k]
            if w:
                print(f"        {k}: {', '.join(w) if isinstance(w, list) else w}")
        z = r.get("zone")
        if z:
            ln = " / ".join(f"{n}차선 {v:,}" for n, v in z["lines"].items())
            print(f"        매수자리(추정·{z['basis']}): {ln} | 10일선 {z['ma10']:,} 20일선 {z['ma20']:,} | 현재 {z['pos']}"
                  + (f" | 중첩: {', '.join(z['overlap'])}" if z["overlap"] else ""))
        if r["flags"]:
            print(f"        ⚠ {', '.join(r['flags'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--date")
    ap.add_argument("--events", default="events.csv")
    ap.add_argument("--out")
    ap.add_argument("--limit", type=int, default=30)
    a = ap.parse_args()
    show(run(a.data, a.date, a.events, a.out), a.limit)
