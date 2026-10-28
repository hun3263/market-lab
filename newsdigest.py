#!/usr/bin/env python3
"""newsdigest.py : 주요 뉴스를 모아 분류·묶음·중요도 순으로 정리해 data/news/날짜.json 에 저장합니다.

  python newsdigest.py --edition pm      # 매일 20:00 : 직전 20:00 이후 24시간 (휴장일 포함 매일)
  python newsdigest.py --edition am      # 월요일 08:00 : 직전 거래일(금) 20:00 이후 주말·휴일 전체
  python newsdigest.py --edition pm --mock   # 네트워크 없이 화면 점검용 가짜 뉴스

출처: 네이버 뉴스 검색 API (환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET, 무료, 하루 25,000회)
      키가 없으면 구글 뉴스 RSS 로 대신합니다(국내 기사 커버리지는 다소 좁음).

정리 규칙(코드만, AI 없음)
  1) 분류: 아래 CATEGORIES 의 검색어로 분야별 수집 → 기사가 여러 분야에 걸리면 처음 걸린 분야에 둠
  2) 묶음: 제목의 글자 2개 조각(bigram) 겹침률 0.45 이상이면 같은 사건으로 묶음
  3) 중요도: 묶음 안 '서로 다른 언론사 수' (많이 보도될수록 중요) + 최근일수록 가산
  4) 제외: 포토·영상·운세·광고성·[게시판] 등 잡음 단어, 같은 언론사 중복
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
def _keys():
    return os.environ.get("NAVER_CLIENT_ID", "").strip(), os.environ.get("NAVER_CLIENT_SECRET", "").strip()


def _src_name(url):
    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    for dom, name in DOMAIN.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host.split(":")[0]


def _clean(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t or "")).strip()


def naver_search(query, display=50, sort="date"):
    """네이버 뉴스 검색 → [{t, u, s, when(datetime)}]. 키가 없거나 실패하면 None."""
    cid, sec = _keys()
    if not (cid and sec):
        return None
    url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
        {"query": query, "display": display, "sort": sort})
    req = urllib.request.Request(url, headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[경고] 네이버 검색 실패({query}): {e}", file=sys.stderr)
        return None
    out = []
    for it in data.get("items", []):
        link = it.get("originallink") or it.get("link")
        try:
            when = parsedate_to_datetime(it.get("pubDate"))
        except Exception:
            continue
        out.append({"t": _clean(it.get("title")), "u": link, "naver": it.get("link"), "s": _src_name(link),
                    "desc": _clean(it.get("description"))[:120], "when": when})
    return out


def google_search(query, days=2):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{days}d")
           + "&hl=ko&gl=KR&ceid=KR:ko")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            root = ET.fromstring(r.read())
    except Exception:
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
        out.append({"t": t, "u": item.findtext("link") or "", "s": src, "desc": "", "when": when})
    return out


def search(query, days=2):
    r = naver_search(query)
    return r if r is not None else google_search(query, days)


# ───────────────────────── 정리 ─────────────────────────
def _grams(t):
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", t)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a, b):
    if not a or not b:
        return 0.0
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
        out.append({"t": lead["t"], "u": lead["u"], "s": lead["s"], "desc": lead.get("desc", ""),
                    "time": lead["when"].astimezone(KST).strftime("%m/%d %H:%M"), "n": len(srcs),
                    "sources": srcs[:6], "more": [{"t": x["t"], "u": x["u"], "s": x["s"]} for x in its[1:4]],
                    "_last": max(x["when"] for x in its)})
    return out


def digest(start, end, mock=False, per_cat=6):
    seen_urls, cats = set(), []
    for name, queries in CATEGORIES:
        pool = []
        for q in queries:
            items = mock_items(name, q, start, end) if mock else search(q, days=max(2, (end - start).days + 1))
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
        top = [{k: v for k, v in g.items() if not k.startswith("_") and k != "score"} for g in groups[:per_cat]]
        cats.append({"name": name, "items": top, "pool": len(pool)})
    return cats


def mock_items(cat, q, start, end):
    import random
    r = random.Random(cat + q)
    base = [f"{q} 관련 정부 발표에 시장 주목", f"{q} 이슈로 업종 전반 변동성 확대", f"외국인, {q} 영향 속 순매수 지속",
            f"{q} 둘러싼 전망 엇갈려…전문가 분석"]
    out = []
    for i, t in enumerate(base):
        for j in range(r.randint(1, 5)):
            out.append({"t": t + ("" if j == 0 else f" ({j})"), "u": f"https://example.com/{abs(hash(t))}/{j}",
                        "s": r.choice(list(DOMAIN.values())), "desc": "",
                        "when": start + (end - start) * r.random()})
    return out


def window(edition, now):
    """pm: 직전 20:00~지금(24시간) / am(월): 직전 금요일 20:00~지금"""
    if edition == "am":
        x = now.date() - dt.timedelta(days=1)
        while x.weekday() >= 5:
            x -= dt.timedelta(days=1)
        start = dt.datetime.combine(x, dt.time(20, 0), KST)
    else:
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
    src = "모의" if mock else ("네이버 뉴스 검색" if all(_keys()) else "구글 뉴스 RSS")
    day["editions"][edition] = {
        "label": "월요일 08:00 · 주말·휴일 주요 뉴스" if edition == "am" else "20:00 · 오늘의 주요 뉴스",
        "from": start.strftime("%m/%d %H:%M"), "to": end.strftime("%m/%d %H:%M"), "source": src,
        "asof": now.strftime("%Y-%m-%d %H:%M"), "categories": cats,
        "rule": "분야별 검색 → 비슷한 제목 묶음 → 보도한 언론사 수가 많은 순(최근 보도 가산)"}
    top = sorted((dict(it, cat=c["name"]) for c in cats for it in c["items"]), key=lambda x: -x["n"])[:1]
    day["headline"] = top[0]["t"] if top else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(day, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"뉴스 저장: {path} · {edition} · {src} · {total}건(분야 {len(cats)})")
    return day


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=["am", "pm"], default="pm")
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--now", help="시험용 시각 YYYY-MM-DDTHH:MM")
    a = ap.parse_args()
    now = dt.datetime.fromisoformat(a.now).replace(tzinfo=KST) if a.now else None
    run(a.edition, a.out_dir, a.mock, now)
