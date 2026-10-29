#!/usr/bin/env python3
"""newsdigest.py : 주요 뉴스를 모아 분류·묶음·중요도 순으로 정리해 data/news/날짜.json 에 저장합니다.

  python newsdigest.py --edition am      # 매일 07:00 최초 : 전날 20:30 ~ 07:00 (밤사이·해외)
  python newsdigest.py --edition pm      # 매일 20:30 최종 : 07:00 ~ 20:30 (국내 장중·장후)
  python newsdigest.py --coverage        # 커버리지 점검: 검색어별 기사 수·언론사 수만 출력(저장 없음)
  python newsdigest.py --edition pm --mock   # 네트워크 없이 화면 점검용 가짜 뉴스

출처: 구글 뉴스 RSS (키 필요 없음)
  - 국내: 한국판(hl=ko, gl=KR) 검색 6개 분야
  - 해외: 미국판(hl=en-US, gl=US) 비즈니스 헤드라인 + 시장 검색어 → 제목을 한국어로 번역해 함께 표시
    번역은 ANTHROPIC_API_KEY 가 있을 때만(Claude Haiku, 제목 수십 개라 하루 비용 매우 적음). 없으면 영어 원문 그대로.
  (네이버 검색 API는 2026-07-31 개발자센터 신규 발급 종료·약관상 저장 제한 문제로 쓰지 않습니다)

정리 규칙(코드만)
  1) 분류: 아래 CATEGORIES 의 검색어로 분야별 수집 → 여러 분야에 걸리면 처음 걸린 분야에 둠
  2) 묶음: 한글은 글자 2개 조각, 영어는 단어 겹침률 0.45 이상이면 같은 사건으로 묶음
  3) 중요도: 묶음 안 '서로 다른 언론사 수'(많이 보도될수록 중요) + 최근일수록 가산
  4) 제외: 포토·영상·운세·광고성·[게시판] 등 잡음 단어, 같은 링크 중복
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))

CATEGORIES = [
    ("매크로·금리·환율", ["기준금리", "국채금리", "원달러 환율", "소비자물가", "경기 침체", "한국은행"]),
    ("글로벌", ["미국 증시", "연준 FOMC", "중국 경제", "관세 무역", "국제유가", "엔화 엔캐리"]),
    ("정책·규제", ["금융위원회 증시", "정부 정책 발표 산업", "밸류업 공시", "세법 개정 주식", "공매도 제도"]),
    ("산업·섹터", ["반도체 업황", "HBM", "2차전지", "조선 수주", "방산 수출", "바이오 임상", "자동차 수출", "AI 데이터센터", "원전"]),
    ("기업", ["실적 발표 영업이익", "단일판매 공급계약 체결", "유상증자 결정", "자사주 소각", "인수합병 M&A", "상장 IPO"]),
    ("시장·수급", ["코스피 마감", "코스닥 마감", "외국인 순매수", "신용융자 잔고", "증시 전망"]),
]
# 해외(영문): "@BUSINESS" 는 미국판 비즈니스 헤드라인 피드
OVERSEAS = ("해외 뉴스 (번역)", ["@BUSINESS", "stock market", "Federal Reserve", "Treasury yields", "Nasdaq S&P 500",
                               "oil prices", "China economy", "tariffs trade", "Nvidia", "semiconductor stocks", "earnings outlook"])
NOISE = ("[포토]", "[사진]", "[영상]", "[게시판]", "[부고]", "[인사]", "운세", "날씨", "[광고]", "[알림]", "[카드뉴스]", "[화보]")
DOMAIN = {"yna.co.kr": "연합뉴스", "hankyung.com": "한국경제", "mk.co.kr": "매일경제", "sedaily.com": "서울경제",
          "edaily.co.kr": "이데일리", "mt.co.kr": "머니투데이", "fnnews.com": "파이낸셜뉴스", "asiae.co.kr": "아시아경제",
          "newspim.com": "뉴스핌", "news1.kr": "뉴스1", "newsis.com": "뉴시스", "chosun.com": "조선일보", "joongang.co.kr": "중앙일보",
          "donga.com": "동아일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문", "heraldcorp.com": "헤럴드경제",
          "etnews.com": "전자신문", "thelec.kr": "디일렉", "bloter.net": "블로터", "einfomax.co.kr": "연합인포맥스",
          "infostock.co.kr": "인포스탁", "biz.chosun.com": "조선비즈", "ytn.co.kr": "YTN", "sbs.co.kr": "SBS", "kbs.co.kr": "KBS",
          "mbn.co.kr": "MBN", "hankookilbo.com": "한국일보", "seoul.co.kr": "서울신문", "inews24.com": "아이뉴스24",
          "zdnet.co.kr": "지디넷", "dt.co.kr": "디지털타임스", "ajunews.com": "아주경제", "nocutnews.co.kr": "노컷뉴스",
          "businesspost.co.kr": "비즈니스포스트", "thebell.co.kr": "더벨", "investchosun.com": "인베스트조선"}


# ───────────────────────── 수집 ─────────────────────────
def _rss(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        print(f"[경고] RSS 실패: {url[:90]} ({type(e).__name__})", file=sys.stderr)
        return []
    out = []
    for item in root.iter("item"):
        t, src = (item.findtext("title") or "").strip(), (item.findtext("source") or "").strip()
        if src and t.endswith(" - " + src):
            t = t[: -len(src) - 3]
        try:
            when = parsedate_to_datetime(item.findtext("pubDate"))
        except Exception:
            continue
        out.append({"t": html.unescape(t), "u": item.findtext("link") or "", "s": src, "desc": "", "when": when})
    return out


EDITION = {"ko": "hl=ko&gl=KR&ceid=KR:ko", "en": "hl=en-US&gl=US&ceid=US:en"}


def google_search(query, days=2, lang="ko"):
    if query.startswith("@"):  # 분야 헤드라인 피드(BUSINESS 등)
        return _rss(f"https://news.google.com/rss/headlines/section/topic/{query[1:]}?{EDITION[lang]}")
    return _rss("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{days}d") + "&" + EDITION[lang])


def search(query, days=2, lang="ko"):
    return google_search(query, days, lang)


def translate(items):
    """영문 제목 → 한국어(items 의 'ko' 칸). ANTHROPIC_API_KEY 없으면 아무것도 안 함."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not items:
        return False
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        src = [it["t"] for it in items]
        prompt = ("다음 영어 경제·증시 뉴스 제목을 한국 투자자가 읽기 쉬운 자연스러운 한국어 제목으로 번역하세요. "
                  "회사·지수 이름은 한국에서 쓰는 표기로, 숫자·고유명사는 정확히. 해석이나 의견은 덧붙이지 마세요. "
                  "반드시 JSON 배열(입력과 같은 순서, 같은 개수)만 출력:\n" + json.dumps(src, ensure_ascii=False))
        resp = client.messages.create(model=os.environ.get("TRANSLATE_MODEL", "claude-haiku-5-5"), max_tokens=4000,
                                      messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
        ko = json.loads(text[text.index("["): text.rindex("]") + 1])
        if len(ko) != len(items):
            raise ValueError("번역 개수 불일치")
        for it, k in zip(items, ko):
            it["ko"] = str(k).strip()
        return True
    except Exception as e:
        print(f"[경고] 제목 번역 실패 → 영어 원문으로 표시: {e}", file=sys.stderr)
        return False


# ───────────────────────── 정리 ─────────────────────────
STOP = {"the", "and", "for", "with", "from", "that", "this", "after", "amid", "over", "into", "says", "will", "are", "its"}


def _grams(t):
    if len(re.findall(r"[A-Za-z]", t)) > len(t) * 0.5:  # 영어 제목: 단어 집합
        return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) >= 3 and w not in STOP}
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a, b):
    if not a or not b:
        return 0.0
    if any(len(x) > 2 for x in a):  # 영어 단어 집합은 자카드(합집합 대비) — 흔한 단어 몇 개로 묶이지 않게
        return len(a & b) / len(a | b) * 1.6
    return len(a & b) / min(len(a), len(b))


def cluster(items, thr=0.45):
    groups = []
    for it in sorted(items, key=lambda x: x["when"]):
        g = _grams(it["t"])
        best = None
        for grp in groups:
            if _sim(g, grp["g"]) >= thr:
                best = grp
                break
        if best:
            best["items"].append(it)
            best["g"] |= g if len(best["g"]) < 60 else set()
        else:
            groups.append({"g": g, "items": [it]})
    out = []
    for grp in groups:
        its = sorted(grp["items"], key=lambda x: x["when"])
        srcs = []
        for x in its:
            if x["s"] not in srcs:
                srcs.append(x["s"])
        lead = its[0]  # 가장 먼저 보도한 기사를 대표로
        # n = 서로 다른 언론사 수, k = 기사 수(같은 언론사가 여러 번 쓰면 k 가 더 큼). 화면의 '같은 사건 기사'는 앞쪽 몇 건만 보여 줌
        out.append({"t": lead["t"], "u": lead["u"], "s": lead["s"], "desc": lead.get("desc", ""),
                    "time": lead["when"].astimezone(KST).strftime("%m/%d %H:%M"), "n": len(srcs), "k": len(its),
                    "sources": srcs[:40], "more": [{"t": x["t"], "u": x["u"], "s": x["s"]} for x in its[1:6]],
                    "_last": max(x["when"] for x in its)})
    return out


def digest(start, end, mock=False, per_cat=6):
    seen_urls, cats = set(), []
    for name, queries in CATEGORIES + [OVERSEAS]:
        lang = "en" if (name, queries) == OVERSEAS else "ko"
        pool = []
        for q in queries:
            items = (mock_items(name, q, start, end, lang) if mock
                     else search(q, days=max(2, (end - start).days + 1), lang=lang))
            for it in items:
                if not (start <= it["when"] <= end) or any(w in it["t"] for w in NOISE) or len(it["t"]) < 12:
                    continue
                if it["u"] in seen_urls:
                    continue
                seen_urls.add(it["u"])
                pool.append(it)
            if not mock:
                time.sleep(0.12)
        groups = cluster(pool)
        hours = max(1.0, (end - start).total_seconds() / 3600)
        for g in groups:  # 언론사 수 + 최신성(창의 마지막 25% 안이면 +0.5)
            age = (end - g["_last"]).total_seconds() / 3600
            g["score"] = g["n"] + (0.5 if age < hours / 4 else 0)
        groups.sort(key=lambda g: (-g["score"], -g["_last"].timestamp()))
        n_keep = per_cat + 2 if lang == "en" else per_cat
        top = [{k: v for k, v in g.items() if not k.startswith("_") and k != "score"} for g in groups[:n_keep]]
        if lang == "en" and not mock:
            translate(top)
        elif lang == "en":
            for it in top:
                it["ko"] = "[모의 번역] " + it["t"]
        cats.append({"name": name, "items": top, "pool": len(pool), "lang": lang})
    return cats


def mock_items(cat, q, start, end, lang="ko"):
    import random
    r = random.Random(cat + q)
    base = ([f"{q} 관련 정부 발표에 시장 주목", f"{q} 이슈로 업종 전반 변동성 확대", f"외국인, {q} 영향 속 순매수 지속",
             f"{q} 둘러싼 전망 엇갈려…전문가 분석"] if lang == "ko" else
            [f"Investors weigh {q} as markets wait for data", f"{q}: analysts see volatility ahead"])
    out = []
    for i, t in enumerate(base):
        for j in range(r.randint(1, 5)):
            out.append({"t": t + ("" if j == 0 else f" ({j})"), "u": f"https://example.com/{abs(hash(t))}/{j}",
                        "s": r.choice(list(DOMAIN.values())), "desc": "",
                        "when": start + (end - start) * r.random()})
    return out


def window(edition, now):
    """am(07:00 최초): 전날 20:30 ~ 지금 — 밤사이 해외 시장·뉴스 / pm(20:30 최종): 오늘 07:00 ~ 지금 — 국내 장중·장후.
    휴장일·주말도 매일 두 번 돌아서 빈틈 없이 이어집니다."""
    if edition == "am":
        start = dt.datetime.combine(now.date() - dt.timedelta(days=1), dt.time(20, 30), KST)
    else:
        start = dt.datetime.combine(now.date(), dt.time(7, 0), KST)
        if now - start < dt.timedelta(hours=3):  # 시험 실행 등으로 아침에 pm 을 돌리면 24시간
            start = now - dt.timedelta(hours=24)
    return start, now


def run(edition, out_dir="data", mock=False, now=None):
    now = now or dt.datetime.now(KST)
    start, end = window(edition, now)
    cats = digest(start, end, mock)
    d = now.date().isoformat()
    path = Path(out_dir) / "news" / f"{d}.json"
    try:
        day = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        day = {"date": d, "editions": {}}
    total = sum(len(c["items"]) for c in cats)
    tr = "번역 Claude" if os.environ.get("ANTHROPIC_API_KEY") else "번역 없음(원문)"
    src = "모의" if mock else f"구글 뉴스 RSS(국내 한국판 · 해외 미국판, {tr})"
    day["editions"][edition] = {
        "label": "07:00 최초 · 밤사이 주요 뉴스" if edition == "am" else "20:30 최종 · 오늘의 주요 뉴스",
        "from": start.strftime("%m/%d %H:%M"), "to": end.strftime("%m/%d %H:%M"), "source": src,
        "asof": now.strftime("%Y-%m-%d %H:%M"), "categories": cats,
        "rule": "분야별 검색 → 비슷한 제목 묶음 → 보도한 언론사 수가 많은 순(최근 보도 가산)"}
    top = sorted((dict(it, cat=c["name"]) for c in cats for it in c["items"]), key=lambda x: -x["n"])[:1]
    day["headline"] = top[0]["t"] if top else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(day, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"뉴스 저장: {path} · {edition} · {src} · {total}건(분야 {len(cats)})")
    return day


def coverage(hours=24):
    """검색어별로 최근 hours 시간 기사 수·언론사 수를 출력(저장 없음) → 국내/해외 커버리지 판단용"""
    end = dt.datetime.now(KST)
    start = end - dt.timedelta(hours=hours)
    tot = {}
    for name, queries in CATEGORIES + [OVERSEAS]:
        lang = "en" if (name, queries) == OVERSEAS else "ko"
        for q in queries:
            items = [x for x in search(q, 2, lang) if start <= x["when"] <= end]
            srcs = {x["s"] for x in items}
            t = tot.setdefault(lang, [0, set()])
            t[0] += len(items)
            t[1] |= srcs
            print(f"[{lang}] {name:<10} {q:<22} 기사 {len(items):>3} · 언론사 {len(srcs):>2}  예: {sorted(srcs)[:4]}")
            time.sleep(0.15)
    for lang, (n, ss) in tot.items():
        print(f"== {lang} 합계: 기사 {n}건 · 서로 다른 언론사 {len(ss)}곳")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--edition", choices=["am", "pm"], default="pm")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--now", help="시험용 시각 YYYY-MM-DDTHH:MM")
    a = ap.parse_args()
    if a.coverage:
        coverage()
        raise SystemExit
    now = dt.datetime.fromisoformat(a.now).replace(tzinfo=KST) if a.now else None
    run(a.edition, a.out_dir, a.mock, now)
