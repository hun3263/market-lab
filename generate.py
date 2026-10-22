# generate.py : data 폴더의 하루치 JSON 파일들을 모아 index.html 을 만듭니다.
import json
from pathlib import Path

rows = []
for f in sorted(Path("data").glob("*.json")):
    try:
        rows.append(json.loads(f.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[오류] {f} 파일의 문법이 틀렸습니다: {e}")

rows.sort(key=lambda r: r["date"], reverse=True)  # 최신 날짜가 위로

html = Path("template.html").read_text(encoding="utf-8")
marker = "/*__DATA__*/[]"
if marker not in html:
    raise SystemExit("[오류] template.html 에서 데이터 자리표시자를 찾지 못했습니다.")

data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")  # 화면 깨짐 방지
Path("index.html").write_text(html.replace(marker, data), encoding="utf-8")
print(f"index.html 생성 완료: {len(rows)}일치 데이터")
