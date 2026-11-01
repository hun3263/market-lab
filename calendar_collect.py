"""calendar_collect.py : 'events.csv 후보'를 자동으로 모읍니다. (조회 전용 · 사람이 확인한 줄만 events.csv 로 옮김)

왜 필요한가
  events.csv(최+신+일의 '일')는 사람이 손으로 채우는 일정표입니다. 2026-11 초판은 웹 검색으로
  공식 출처(한국은행·MSCI·NVIDIA IR·조직위원회 등)를 하나씩 확인해 만들었습니다.
  이 스크립트는 그 방법을 3갈래로 복제·자동화하고, 사람이 '확인만' 하면 되게 후보 목록을 만듭니다.

수집 갈래
  ① 기본 달력 calendar_base.csv  : 1년에 한 번 공식 일정(금통위·FOMC·MSCI·수능 등)을 붙여 넣는 파일 → kind=M
     + 계산 가능한 일정은 자동 생성: 코스피200 옵션 만기(매월 둘째 목요일)
  ② DART 공시 (OpenDART list.json): 최근 N일 공시 제목을 규칙으로 분류
     IR 개최·임상·품목허가·공개매수 → '+' / 유상증자·전환사채·추가상장 → '-'
     IR 은 공시 원문에서 '개최일시' 날짜를 찾아 채움(예산 내)
  ③ 뉴스 RSS (구글 뉴스, collect.py 와 같은 방식): 일정형 검색어 → 제목에서 날짜·종목·테마 추출
  (선택) ④ AI 정리: ANTHROPIC_API_KEY 가 있으면 뉴스 제목 묶음에서 {날짜, 테마, 일정} 을 JSON 으로 추출해 정확도 보강

출력  events_candidates.csv  (열: date,code,theme,event,kind,source,url,confidence,origin)
      confidence: high = 공식 달력·DART 원문에서 날짜 확인 / mid = 공시 제목·뉴스 제목에 날짜 있음 / low = 날짜 추정
사용  python calendar_collect.py --days 7 --ahead 30            (DART 키: 환경변수 DART_API_KEY, 서버 ~/.market-lab.env)
      python calendar_collect.py --selftest                      (네트워크 없이 규칙만 점검)
"""
import argparse
import csv
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
import xml.etree.ElementTree as ET
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
FIELDS = ["date", "code", "theme", "event", "kind", "source", "url", "confidence", "origin"]

# ── ② DART 공시 제목 규칙: (제목에 들어간 말, kind, 일정 이름) ─────────────────────────────
DART_RULES = [
    ("기업설명회(IR)개최", "+", "IR 개최"),
    ("임상시험", "+", "임상 관련 공시"),
    ("품목허가", "+", "품목허가 관련 공시"),
    ("공개매수", "+", "공개매수"),
    ("유상증자결정", "-", "유상증자 (신주 상장일 확인)"),
    ("전환사채권발행결정", "-", "전환사채 발행 (전환 가능일 확인)"),
    ("신주인수권부사채권발행결정", "-", "BW 발행 (행사 가능일 확인)"),
    ("추가상장", "-", "추가상장 (물량 출회)"),
    ("주식등의대량보유", None, None),        # 일정 아님 → 건너뜀
]
DART_TYPES = ("I", "B")                     # I 거래소공시(IR·임상 등), B 주요사항보고서(증자·사채)

# ── ③ 뉴스: 일정형 검색어와 '일정 단어 → 테마 일부' 사전 (테마는 키움 테마 이름 부분 일치용) ──────
NEWS_QUERIES = [
    "개최 예정 박람회", "전시회 개막 앞두고", "정상회담 앞두고", "정부 대책 발표 예정",
    "임상 결과 발표 예정 학회", "의무보유 해제 예정", "신규 상장 예정 수요예측", "수주 결과 발표 예정",
]
THEME_HINTS = {  # [추정] 초기값. 결과를 보며 계속 늘립니다
    "지스타": "게임", "게임": "게임", "원전": "원자력", "SMR": "원자력", "조선": "조선", "마스가": "조선",
    "방산": "방위", "무기": "방위", "반도체": "반도체", "HBM": "반도체", "로봇": "로봇", "우주": "우주",
    "바이오": "바이오", "임상": "바이오", "제약": "제약", "2차전지": "2차전지", "배터리": "2차전지",
    "재건": "재건", "AI": "AI", "인공지능": "AI", "보안": "보안", "해킹": "보안", "전력": "전력", "변압기": "전력",
}
NEG_WORDS = ("의무보유 해제", "보호예수 해제", "유상증자", "전환사채", "오버행", "철회", "연기", "무산")

DATE_FULL = re.compile(r"(20\d\d)\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*일?")
DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def parse_dates(text, today):
    """문장 속 날짜 → [date]. 연도 없는 'M월 D일'은 오늘 기준 가까운 미래로 해석"""
    out = []
    for y, m, d in DATE_FULL.findall(text):
        try:
            out.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    if not out:
        for m, d in DATE_MD.findall(text):
            try:
                x = dt.date(today.year, int(m), int(d))
            except ValueError:
                continue
            if (today - x).days > 30:          # 이미 한참 지났으면 내년 일정으로
                x = x.replace(year=today.year + 1)
            out.append(x)
    return out


def classify_dart(report_nm):
    for key, kind, ev in DART_RULES:
        if key in report_nm.replace(" ", ""):
            return (kind, ev) if kind else None
    return None


def theme_of(text):
    for k, t in THEME_HINTS.items():
        if k in text:
            return t
    return ""


def option_expiry(year, month):
    """코스피200 옵션 만기: 매월 둘째 목요일 [팩트: 거래소 상품 규정, 휴일이면 앞당겨짐 → 휴일 확인 필요]"""
    d = dt.date(year, month, 1)
    first_thu = d + dt.timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + dt.timedelta(days=7)


# ── 통신 공통 ─────────────────────────────────────────────────────────────────────
def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 market-lab"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── ① 기본 달력 ──────────────────────────────────────────────────────────────────
def from_base(path, today, ahead):
    rows = []
    if path and Path(path).exists():
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    x = dt.date.fromisoformat(r["date"].strip())
                except Exception:
                    continue
                if 0 <= (x - today).days <= ahead:
                    rows.append(dict({k: r.get(k, "") for k in FIELDS}, confidence="high", origin="base",
                                     kind=r.get("kind") or "M"))
    for k in range(0, ahead // 28 + 2):
        y, m = today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1
        x = option_expiry(y, m)
        if 0 <= (x - today).days <= ahead:
            rows.append({"date": x.isoformat(), "code": "", "theme": "", "event": "코스피200 옵션 만기(둘째 목요일, 휴일 확인)",
                         "kind": "M", "source": "계산", "url": "", "confidence": "mid", "origin": "calc"})
    return rows


# ── ② DART ───────────────────────────────────────────────────────────────────────
def dart_text(key, rcept_no, limit=6000):
    """공시 원문(zip 안 XML/HTML) → 글자만. zip 이 아니면 그대로 디코딩"""
    raw = get("https://opendart.fss.or.kr/api/document.xml?" + urllib.parse.urlencode({"crtfc_key": key, "rcept_no": rcept_no}), 30)
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        raw = b"".join(z.read(n) for n in z.namelist())
    except zipfile.BadZipFile:
        pass
    t = raw.decode("utf-8", "ignore")
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t)[:limit]


def from_dart(key, today, days, ahead, doc_budget=20):
    rows, docs = [], 0
    bgn = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    for ty in DART_TYPES:
        for page in range(1, 6):
            data = json.loads(get("https://opendart.fss.or.kr/api/list.json?" + urllib.parse.urlencode({
                "crtfc_key": key, "bgn_de": bgn, "end_de": today.strftime("%Y%m%d"), "pblntf_ty": ty,
                "page_no": page, "page_count": 100})).decode("utf-8"))
            if data.get("status") != "000":
                break
            for it in data.get("list", []):
                if not it.get("stock_code"):                # 비상장 제외
                    continue
                c = classify_dart(it["report_nm"])
                if not c:
                    continue
                kind, ev = c
                url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"
                when, conf = None, "low"
                ds = parse_dates(it["report_nm"], today)
                if ds:
                    when, conf = ds[0], "mid"
                elif "IR" in ev and docs < doc_budget:        # IR 은 원문에서 '개최일시' 근처 날짜
                    try:
                        txt = dart_text(key, it["rcept_no"])
                        docs += 1
                        m = re.search(r"(개최\s*일시|일\s*시|개최\s*일자)(.{0,40})", txt)
                        ds = parse_dates(m.group(2) if m else txt[:800], today)
                        if ds:
                            when, conf = ds[0], "high"
                    except Exception:
                        pass
                    time.sleep(0.2)
                if when is None:
                    when = dt.date(int(it["rcept_dt"][:4]), int(it["rcept_dt"][4:6]), int(it["rcept_dt"][6:]))
                    ev += " (일정일 미확인 · 공시일 기준)"
                if -1 <= (when - today).days <= ahead:
                    rows.append({"date": when.isoformat(), "code": it["stock_code"], "theme": "",
                                 "event": f"{it['corp_name']} {ev}", "kind": kind, "source": "DART " + it["report_nm"].strip(),
                                 "url": url, "confidence": conf, "origin": "dart"})
            if int(data.get("total_page", 1)) <= page:
                break
            time.sleep(0.2)
    return rows


# ── ③ 뉴스 ───────────────────────────────────────────────────────────────────────
def load_names(data_dir):
    """종목명 → 코드 (market-lab 수집 자료에서). 제목 속 종목명을 코드로 바꾸는 데 씀"""
    names = {}
    d = Path(data_dir)
    for f in (d / "stocks").glob("*.json"):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            names[j["name"]] = j["code"]
        except Exception:
            pass
    heat = sorted((d / "heat").glob("20*.json"))
    if heat:
        h = json.loads(heat[-1].read_text(encoding="utf-8"))
        names.update({r[1]: r[0] for r in h["rows"]})
    return {k: v for k, v in names.items() if len(k) >= 2}


def news_rows_from_titles(items, today, ahead, names):
    rows = []
    for it in items:
        t = it["t"]
        ds = [x for x in parse_dates(t, today) if 0 <= (x - today).days <= ahead]
        if not ds:
            continue
        code = next((c for n, c in sorted(names.items(), key=lambda kv: -len(kv[0])) if n in t), "")
        kind = "-" if any(w in t for w in NEG_WORDS) else "+"
        rows.append({"date": ds[0].isoformat(), "code": code, "theme": "" if code else theme_of(t),
                     "event": t[:70], "kind": kind, "source": it.get("s", "뉴스"), "url": it.get("u", ""),
                     "confidence": "mid" if code or theme_of(t) else "low", "origin": "news"})
    return rows


def fetch_news(query, days=7):
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{days}d") + "&hl=ko&gl=KR&ceid=KR:ko"
    root = ET.fromstring(get(url, 10))
    out = []
    for item in root.iter("item"):
        t, s = (item.findtext("title") or "").strip(), (item.findtext("source") or "").strip()
        if s and t.endswith(" - " + s):
            t = t[: -len(s) - 3]
        out.append({"t": t, "u": (item.findtext("link") or "").strip(), "s": s})
    return out


# ── ④ (선택) AI 정리 ─────────────────────────────────────────────────────────────
def ai_extract(items, today, api_key):
    """뉴스 제목 묶음 → [{date, theme, event, kind}] (제목에 근거한 일정만, 추측 금지)"""
    try:
        import anthropic
        prompt = ("다음은 한국 증시 관련 뉴스 제목입니다. 오늘은 " + today.isoformat() + "입니다.\n"
                  "제목에 '날짜가 명시된 앞으로의 일정'만 골라 JSON 배열로 출력하세요. 날짜가 없으면 넣지 마세요.\n"
                  '형식: [{"date":"YYYY-MM-DD","theme":"테마 한 단어","event":"30자 이내","kind":"+ 또는 -","idx":제목번호}]\n\n'
                  + "\n".join(f"{i}. {x['t']}" for i, x in enumerate(items)))
        r = anthropic.Anthropic(api_key=api_key).messages.create(
            model="claude-sonnet-5-5", max_tokens=3000, messages=[{"role": "user", "content": prompt}])
        txt = r.content[0].text
        arr = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        rows = []
        for a in arr:
            src = items[int(a.get("idx", 0))] if str(a.get("idx", "")).isdigit() and int(a["idx"]) < len(items) else {}
            rows.append({"date": a["date"], "code": "", "theme": a.get("theme", ""), "event": a.get("event", ""),
                         "kind": a.get("kind", "+"), "source": "AI 추출: " + src.get("s", ""), "url": src.get("u", ""),
                         "confidence": "mid", "origin": "ai"})
        return rows
    except Exception as e:
        print(f"[경고] AI 정리 실패(건너뜀): {e}", file=sys.stderr)
        return []


# ── 합치기 ───────────────────────────────────────────────────────────────────────
def dedupe(rows):
    best = {}
    rank = {"high": 3, "mid": 2, "low": 1}
    for r in rows:
        key = (r["date"], r["code"] or r["theme"], re.sub(r"\W", "", r["event"])[:12])
        if key not in best or rank[r["confidence"]] > rank[best[key]["confidence"]]:
            best[key] = r
    return sorted(best.values(), key=lambda r: (r["date"], -rank[r["confidence"]]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--base", default="calendar_base.csv")
    ap.add_argument("--out", default="events_candidates.csv")
    ap.add_argument("--days", type=int, default=7, help="최근 며칠 공시·뉴스를 볼지")
    ap.add_argument("--ahead", type=int, default=30, help="앞으로 며칠 안의 일정만 남길지")
    ap.add_argument("--no-dart", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    today = dt.datetime.now(KST).date()
    if a.selftest:
        return selftest()

    rows = from_base(a.base, today, a.ahead)
    key = os.environ.get("DART_API_KEY")
    if key and not a.no_dart:
        try:
            rows += from_dart(key, today, a.days, a.ahead)
        except Exception as e:
            print(f"[경고] DART 수집 실패: {e}", file=sys.stderr)
    if not a.no_news:
        items = []
        for q in NEWS_QUERIES:
            try:
                items += fetch_news(q, a.days)
            except Exception as e:
                print(f"[경고] 뉴스 '{q}' 실패: {e}", file=sys.stderr)
            time.sleep(0.5)
        rows += news_rows_from_titles(items, today, a.ahead, load_names(a.data))
        api = os.environ.get("ANTHROPIC_API_KEY")
        if api and items:
            rows += ai_extract(items[:80], today, api)
    rows = dedupe(rows)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    cnt = {k: sum(r["confidence"] == k for r in rows) for k in ("high", "mid", "low")}
    print(f"후보 {len(rows)}건 저장 → {a.out}  (high {cnt['high']} · mid {cnt['mid']} · low {cnt['low']})")


def selftest():
    today = dt.date(2026, 11, 2)
    assert parse_dates("지스타 2026, 11월 19일 개막", today) == [dt.date(2026, 11, 19)]
    assert parse_dates("2026년 12월 1일 적용", today) == [dt.date(2026, 12, 1)]
    assert parse_dates("1월 8일 CES 개막", today) == [dt.date(2027, 1, 8)]
    assert classify_dart("기업설명회(IR)개최(안내공시)") == ("+", "IR 개최")
    assert classify_dart("주요사항보고서(유상증자결정)") == ("-", "유상증자 (신주 상장일 확인)")
    assert classify_dart("단일판매ㆍ공급계약체결") is None
    assert option_expiry(2026, 11) == dt.date(2026, 11, 12)
    names = {"안랩": "053800", "크래프톤": "259960"}
    r = news_rows_from_titles([{"t": "크래프톤, 11월 19일 지스타서 신작 공개", "u": "u", "s": "s"},
                               {"t": "A사 11월 20일 의무보유 해제 예정", "u": "u", "s": "s"},
                               {"t": "날짜 없는 기사", "u": "u", "s": "s"}], today, 30, names)
    assert r[0]["code"] == "259960" and r[0]["kind"] == "+"
    assert r[1]["kind"] == "-" and len(r) == 2
    print("selftest OK:", len(r), "건 뉴스 후보,", option_expiry(2026, 11), "옵션 만기")


if __name__ == "__main__":
    main()
