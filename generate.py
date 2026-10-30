# generate.py : index.html 을 만듭니다.
# 날짜별 자료(data/날짜.json)는 화면이 필요할 때 days/날짜.json 으로 불러오므로, index.html 에는 '날짜 목록'만 넣습니다.
# (예전처럼 모든 날짜를 한 파일에 넣으면 1년 뒤 수십 MB가 되어 느려집니다)
import json
from pathlib import Path

days = {}
for f in sorted(Path("data").glob("20*.json")):  # 날짜 파일만(data/status.json 같은 운영 파일 제외)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[오류] {f} 파일의 문법이 틀렸습니다: {e}")
    days[d["date"]] = {"date": d["date"], "headline": d.get("headline", ""), "market": True}
for f in sorted(Path("data/news").glob("*.json")):
    try:
        n = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        continue
    x = days.setdefault(n["date"], {"date": n["date"], "headline": "📰 " + n.get("headline", "주요 뉴스"), "market": False})
    x["news"] = sorted(n.get("editions", {}))

rows = sorted(days.values(), key=lambda r: r["date"], reverse=True)  # 최신 날짜가 위로
html = Path("template.html").read_text(encoding="utf-8")
marker = "/*__DATA__*/[]"
if marker not in html:
    raise SystemExit("[오류] template.html 에서 데이터 자리표시자를 찾지 못했습니다.")
data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")  # 화면 깨짐 방지
Path("index.html").write_text(html.replace(marker, data), encoding="utf-8")
print(f"index.html 생성 완료: 날짜 {len(rows)}개 (시황 {sum(r['market'] for r in rows)}, 뉴스 {sum('news' in r for r in rows)})")
