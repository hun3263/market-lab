"""secure_publish.py : 관심종목(최신일·방배동) 결과를 '암호화'해서 사이트에 올립니다. (🔒 관심종목 탭 전용)

왜: GitHub 저장소와 GitHub Pages 는 공개라서, 파일을 그대로 올리면 누구나 볼 수 있습니다.
    결과 원본은 서버의 비공개 폴더(~/market-lab-private)에만 두고, 사이트에는 비밀번호로 잠근 파일(data/secure/watch.json)만 올립니다.
방식: PBKDF2-SHA256(60만 회)로 비밀번호 → 256비트 키, AES-256-GCM 암호화. 브라우저(WebCrypto)가 같은 방식으로 풉니다.
담는 것
  days      최근 60일 최+신+일 결과(최신일만 C 등급 포함)
  backfill  backfill_choisinil.py 로 과거 1~2년을 재현한 A·B 기록(있으면, 최근 250거래일)
  score     종목별 '등장 점수' = 최근 20거래일 A(3점)·B(2점)를 하루 0.9배씩 줄여 더한 값 ÷ 'A 20일 연속' 최대값 × 100
  outcome   처음 A·B 로 나온 날 이후 +5·+10·+20거래일 수익률 (data/stocks 일봉 또는 백필 계산값)
  mat       종목별 테마·재료: 최근 등장 사유와 기사 제목, DART 일정 후보
  bangbae   bangbae_filter.py 결과(있으면)
설정: ~/.market-lab.env 에 WATCH_PASS=긴비밀번호 / 서버: ./.venv/bin/pip install cryptography
"""
import base64
import csv

import riskinfo
import datetime as dt
import json
import os
from pathlib import Path

PRIV = Path(os.environ.get("MLAB_PRIVATE", Path.home() / "market-lab-private"))
DATA = Path(os.environ.get("MLAB_DATA", "data"))
OUT = DATA / "secure" / "watch.json"
ITER = 600_000
KEEP_DAYS, KEEP_BACKFILL = 60, 250
W = {"A": 3, "B": 2}
DECAY, WIN = 0.9, 20
MAXS = 3 * sum(DECAY ** k for k in range(WIN))


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def slim_row(r, full=True):
    keep = ("code", "name", "grade", "score", "close", "choi", "shin", "il", "why", "flags", "themes", "zone",
            "cap", "small", "hold", "dil") if full \
        else ("code", "name", "grade", "score", "close", "why", "cap", "small", "hold", "dil", "flags")
    return {k: r.get(k) for k in keep if r.get(k) is not None}


def fwd_returns(prices, date):
    """prices [[날짜, 종가, ...]] 에서 date 종가 대비 +5·+10·+20거래일 수익률(%)"""
    idx = next((i for i, p in enumerate(prices) if p[0] >= date), None)
    if idx is None:
        return {}
    base = prices[idx][1]
    out = {}
    for k in (5, 10, 20):
        if idx + k < len(prices) and base:
            out[f"r{k}"] = round((prices[idx + k][1] / base - 1) * 100, 1)
    return out


def materials(code, cand):
    """종목 상세 파일의 최근 등장 사유·기사 + DART 일정 후보"""
    info = load(DATA / "stocks" / f"{code}.json", {}) or {}
    apps = sorted(info.get("appear", []), key=lambda a: a["date"])[-3:]
    news, seen = [], set()
    for a in reversed(apps):
        for n in a.get("news", []) or []:
            if isinstance(n, dict) and n.get("t") and n["t"] not in seen:
                seen.add(n["t"]); news.append({"t": n["t"], "u": n.get("u", ""), "s": n.get("s", ""), "d": n.get("d", a["date"][5:])})
    return {"reason": [{"d": a["date"], "t": a.get("reason", "")} for a in reversed(apps) if a.get("reason")][:3],
            "news": news[:4], "dart": cand.get(code, [])[:4], "sector": (info.get("basic") or {}).get("sector", "")}


def build_payload():
    files = sorted((PRIV / "choisinil").glob("20*.json"))[-KEEP_DAYS:]
    if not files:
        raise SystemExit(f"[오류] {PRIV / 'choisinil'} 에 필터 결과가 없습니다 (filter_choisinil.py --out 경로 확인)")
    days = []
    for i, f in enumerate(files):
        d = json.loads(f.read_text(encoding="utf-8"))
        latest = i == len(files) - 1
        rows = [slim_row(r) for r in d["rows"] if r["score"] >= 2] + \
               ([slim_row(r, False) for r in d["rows"] if r["score"] < 2] if latest else [])
        days.append({"date": d["date"], "regime": d.get("regime"), "strongThemes": d.get("strongThemes"),
                     "strongSectors": d.get("strongSectors") if latest else None, "macro": d.get("macro") if latest else None,
                     "nhDays": d.get("nhDays"), "rows": rows})
    # 백필(과거 재현) A·B
    bf = load(PRIV / "backfill" / "backfill.json", {}) or {}
    bdays = [x for x in bf.get("days", []) if x["date"] < days[0]["date"]][-KEEP_BACKFILL:]
    # 등장 이력(백필 + 실시간)
    timeline = {}
    for x in bdays + days:
        for r in x["rows"]:
            if r.get("grade", "C")[0] in W:
                t = timeline.setdefault(r["code"], {"name": r["name"], "seen": []})
                t["seen"].append([x["date"], r["grade"][0]])
    all_dates = [x["date"] for x in bdays + days]
    recent = all_dates[-WIN:]
    cand = {}
    for r in csv.DictReader(open(DATA / "calendar" / "events_candidates.csv", encoding="utf-8-sig")) if (DATA / "calendar" / "events_candidates.csv").exists() else []:
        if r.get("code") and r.get("confidence") in ("high", "mid"):
            cand.setdefault(r["code"], []).append({"date": r["date"], "event": r["event"], "kind": r.get("kind", ""), "url": r.get("url", "")})
    score, outcome, mat = {}, {}, {}
    bf_px = bf.get("outcome", {})
    for code, t in timeline.items():
        g = dict(t["seen"])
        s = sum(W[g[d]] * DECAY ** (len(recent) - 1 - k) for k, d in enumerate(recent) if d in g)
        streak = 0
        for d in reversed(all_dates):
            if d in g:
                streak += 1
            else:
                break
        score[code] = {"score": round(100 * s / MAXS, 1), "n20": sum(1 for d in recent if d in g), "streak": streak,
                       "first": t["seen"][0][0], "last": t["seen"][-1][0], "n": len(t["seen"])}
        info = load(DATA / "stocks" / f"{code}.json", {}) or {}
        o = fwd_returns(info.get("prices", []), t["seen"][0][0]) or bf_px.get(code, {}).get(t["seen"][0][0], {})
        if o:
            outcome[code] = o
        if score[code]["n20"]:
            mat[code] = materials(code, cand)
    bang = sorted((PRIV / "bangbae").glob("20*.json"))
    bb = load(bang[-1]) if bang else None
    # 희석 공시(CB·BW·EB·유증) 일정 → 일정 탭(잠금 해제 시에만 보임). 최신일 마지막 날 + 방배동 후보 전체
    dil, nm = {}, {}
    for r in days[-1]["rows"] + (bb or {}).get("material", []) + (bb or {}).get("event", []):
        if r.get("dil"):
            dil[r["code"]] = r["dil"]
            nm[r["code"]] = r["name"]
    return {"generated": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
            "days": days, "backfill": {"days": bdays, "report": bf.get("report")},
            "timeline": {c: t["seen"] for c, t in timeline.items()}, "names": {c: t["name"] for c, t in timeline.items()},
            "score": score, "outcome": outcome, "mat": mat,
            "drbt": {k: {x: v for x, v in (load(PRIV / "bt" / f"report_{t}.json", {}) or {}).items() if x in ("period", "all", "by_regime", "by_buys")}
                     for k, t in (("최근 3개월", "3M"), ("최근 6개월", "6M"), ("최근 1년", "1Y")) if (PRIV / "bt" / f"report_{t}.json").exists()},
            "bbbt": {k: {x: v for x, v in (load(PRIV / "bt" / f"report_bb_{t}.json", {}) or {}).items() if x in ("period", "all", "by_plan", "by_tier", "by_regime", "by_small", "skipped")}
                     for k, t in (("최근 3개월", "3M"), ("최근 6개월", "6M"), ("최근 1년", "1Y")) if (PRIV / "bt" / f"report_bb_{t}.json").exists()},
            "bangbae": bb, "cal": riskinfo.calendar_events(dil, nm, days[-1]["date"])}


def encrypt(obj, password):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER, "cipher": "AES-256-GCM", "salt": b(salt), "iv": b(iv), "ct": b(ct)}


def main():
    pw = os.environ.get("WATCH_PASS", "")
    if len(pw) < 12:
        raise SystemExit("[오류] WATCH_PASS 가 없거나 12자 미만입니다. ~/.market-lab.env 에 긴 비밀번호를 넣으세요.")
    payload = build_payload()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(encrypt(payload, pw)), encoding="utf-8")
    top = sorted(payload["score"].items(), key=lambda kv: -kv[1]["score"])[:3]
    print(f"🔒 암호화 저장: {OUT} (실시간 {len(payload['days'])}일 + 백필 {len(payload['backfill']['days'])}일) "
          f"점수 상위: " + ", ".join(f"{payload['names'][c]} {v['score']}" for c, v in top))


if __name__ == "__main__":
    main()
