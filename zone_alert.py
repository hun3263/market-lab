"""zone_alert.py : 최+신+일 A·B 후보가 '매수 자리(차선)'에 닿으면 휴대폰 알림을 보냅니다. (조회 전용 · 주문 기능 없음)

흐름  20:00 filter_choisinil.py → data/choisinil/날짜.json (후보·2/3/4차선·10/20일선)
      장중 10분마다(flow_tick 예약과 같이) 이 스크립트 → 현재가(ka10001) 확인 → 차선 ±0.5% 안이면 ntfy 알림
알림  NTFY_TOPIC (서버 ~/.market-lab.env, run.sh 실패 알림과 같은 채널). 같은 종목·같은 선은 하루 한 번만.
주의  알림은 '확인할 때가 됐다'는 신호입니다. 매수 여부는 체크리스트(지수 구간·재료·공포 신호)를 사람이 확인해 정합니다.
"""
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

import collect

KST = dt.timezone(dt.timedelta(hours=9))
CACHE = Path(os.environ.get("MLAB_CACHE", Path.home() / ".market-lab-cache")) / "alerts"
TOL = 0.005                 # [추정] 선 위 0.5% 이내 접근이면 '도달'로 봄
REGIME_NOTE = {"U": "상승기: 상단(2·3차선) 위주", "D1": "1단 하락: 최신일 상단·중단만, 하단 금지 (DR5강 p67)",
               "D2": "2단 하락: 추가매수 간격 확대 (DR5강 p76)", "D3": "3단 하락: 중단·하단 분할, 레버리지 금지 (DR5강 p82)"}


def notify(title, body):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    print(f"[알림] {title} | {body}")
    if topic:
        import urllib.parse                                   # 한글 제목은 헤더 대신 주소(title=)로 보냄
        req = urllib.request.Request(f"https://ntfy.sh/{topic}?" + urllib.parse.urlencode({"title": title, "tags": "chart_with_downwards_trend"}),
                                     data=body.encode("utf-8"))
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[경고] 알림 전송 실패: {e}", file=sys.stderr)


def main(data="data"):
    now = dt.datetime.now(KST)
    if now.weekday() >= 5 or not ("0900" <= now.strftime("%H%M") <= "1530"):
        return
    files = sorted(Path(data, "choisinil").glob("20*.json"))
    if not files:
        return
    res = json.loads(files[-1].read_text(encoding="utf-8"))
    rows = [r for r in res["rows"] if r["score"] >= 2 and r.get("zone") and not r.get("flags")]
    if not rows:
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    sent_path = CACHE / f"{now.date().isoformat()}.json"
    sent = json.loads(sent_path.read_text()) if sent_path.exists() else []
    os.environ.setdefault("KIWOOM_EXCHANGE", "KRX")
    src = collect.KiwoomSource("close")
    reg = " / ".join(f"{k} {v['regime']}" for k, v in res.get("regime", {}).items())
    rank = {"U": 0, "D1": 1, "D2": 2, "D3": 3}
    worst = max((v["regime"] for v in res.get("regime", {}).values()), key=lambda x: rank.get(x, 0), default="U")
    for r in rows:
        try:
            px = abs(float(str(src._call("/api/dostk/stkinfo", "ka10001", {"stk_cd": r["code"]})[0]["cur_prc"]).replace(",", "").replace("+", "")))
        except Exception as e:
            print(f"[건너뜀] {r['name']}: {e}", file=sys.stderr)
            continue
        z = r["zone"]
        for n, line in sorted(((int(k), v) for k, v in z["lines"].items()), reverse=True):   # 깊은 선부터
            key = f"{r['code']}-{n}"
            if px <= line * (1 + TOL) and key not in sent:
                over = [o for o in z.get("overlap", []) if o.startswith(f"{n}차선")]
                body = (f"{r['name']} {px:,.0f}원 → {n}차선 {line:,}원 도달{' · 중첩 ' + ','.join(over) if over else ''}\n"
                        f"등급 {r['grade']} · 지수 {reg} → {REGIME_NOTE.get(worst, '')}\n"
                        f"체크: ①재료 살아있나(소멸·철회 공시?) ②5분봉 장대음봉·저점이탈 공포 신호 ③MAX 원금10%·1:1:1\n"
                        f"   ④데드라인 = 3차 매수가 -5%(상단)/-7%(중단) ⑤기간손절 = 1차 후 5거래일")
                notify(f"[{r['grade']}] {r['name']} {n}차선", body)
                sent.append(key)
                break
    sent_path.write_text(json.dumps(sent), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[경고] 타점 알림 실패: {e}", file=sys.stderr)
