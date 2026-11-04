"""minutes.py : 키움 5분봉(ka10080) 받기·쌓기 (조회 전용, 주문 기능 없음)

저장  ~/market-lab-private/minutes/5m/종목코드.csv.gz   (GitHub·사이트에는 올리지 않음)
      한 줄 = ts(YYYYMMDDHHMM),open,high,low,close,vol
      _empty.json = 받으려 했지만 키움에 자료가 없던 날짜(보관 기간 밖·휴장) → 다시 묻지 않음
[팩트] 2026-11-04 서버 시험: base_dt 지정 시 한 번에 900봉(약 11거래일), 2025-11-03 이전은 없음 → 보관 약 1년
[팩트] 응답 목록 stk_min_pole_chart_qry 는 최신 → 과거 순, 가격에 +/- 부호가 붙어 옴
용량 [추정] 5분봉 하루 약 80봉 × 45바이트, 압축하면 종목·일당 약 1KB
"""
import csv
import gzip
import io
import json
import os
import time
from collections import defaultdict
from pathlib import Path

PRIV = Path(os.environ.get("MLAB_PRIVATE", Path.home() / "market-lab-private"))
DIR = PRIV / "minutes" / "5m"
PAUSE = 0.25          # [추정] 조회 초당 5회 제한보다 여유 있게


def _num(v):
    try:
        return abs(float(str(v).replace(",", "").replace("+", "") or 0))
    except ValueError:
        return 0.0


class Minutes:
    def __init__(self, src=None):
        """src = collect.KiwoomSource (없으면 저장된 자료만 읽음)"""
        self.src = src
        DIR.mkdir(parents=True, exist_ok=True)
        self.cache = {}
        self.calls = 0
        p = DIR / "_empty.json"
        self.empty = json.loads(p.read_text()) if p.exists() else {}

    def _path(self, code):
        return DIR / f"{code}.csv.gz"

    def load(self, code):
        """{날짜 'YYYY-MM-DD': [{t:'HHMM', open, high, low, close, vol}, ...시간순]}"""
        if code in self.cache:
            return self.cache[code]
        out = defaultdict(list)
        p = self._path(code)
        if p.exists():
            with gzip.open(p, "rt") as f:
                for r in csv.reader(f):
                    ts = r[0]
                    out[f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"].append(
                        {"t": ts[8:12], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "vol": float(r[5])})
        for v in out.values():
            v.sort(key=lambda x: x["t"])
        self.cache[code] = dict(out)
        return self.cache[code]

    def _save(self, code, days):
        buf = io.StringIO()
        w = csv.writer(buf)
        for d in sorted(days):
            for x in days[d]:
                w.writerow([d.replace("-", "") + x["t"], x["open"], x["high"], x["low"], x["close"], int(x["vol"])])
        with gzip.open(self._path(code), "wt") as f:
            f.write(buf.getvalue())
        (DIR / "_empty.json").write_text(json.dumps(self.empty))

    def ensure(self, code, dates):
        """dates 의 5분봉이 없으면 키움에서 받아 저장. 반환: 자료가 있는 날짜 집합"""
        have = self.load(code)
        emp = set(self.empty.get(code, []))
        miss = sorted(d for d in dates if d not in have and d not in emp)
        if miss and self.src:
            days = dict(have)
            base = miss[-1].replace("-", "")
            got = {}
            for _ in range(4):                                   # 900봉 × 최대 4번 ≈ 40거래일
                try:
                    pages = self.src._call("/api/dostk/chart", "ka10080",
                                           {"stk_cd": code, "tic_scope": "5", "upd_stkpc_tp": "1", "base_dt": base}, pages=1)
                except Exception as e:
                    print(f"[분봉 건너뜀] {code} {base}: {e}")
                    break
                self.calls += 1
                time.sleep(PAUSE)
                rows = pages[0].get("stk_min_pole_chart_qry") or []
                for r in rows:
                    ts = str(r.get("cntr_tm", ""))
                    if len(ts) < 12:
                        continue
                    d = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                    got.setdefault(d, {})[ts[8:12]] = {"t": ts[8:12], "open": _num(r.get("open_pric")), "high": _num(r.get("high_pric")),
                                                       "low": _num(r.get("low_pric")), "close": _num(r.get("cur_prc")), "vol": _num(r.get("trde_qty"))}
                if not rows:
                    break
                oldest = min(str(r.get("cntr_tm", "99999999"))[:8] for r in rows)
                if oldest <= miss[0].replace("-", "") or len(rows) < 900:
                    break
                base = oldest                                      # 더 과거로 (같은 날 겹침은 시각 키로 합침)
            for d, m in got.items():
                days[d] = sorted(m.values(), key=lambda x: x["t"])
            for d in miss:
                if d not in days:
                    self.empty.setdefault(code, []).append(d)
            self.cache[code] = days
            self._save(code, days)
            have = days
        return {d for d in dates if d in have}

    def size_mb(self):
        return round(sum(p.stat().st_size for p in DIR.glob("*.csv.gz")) / 1e6, 1)
