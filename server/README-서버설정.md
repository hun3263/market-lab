# 서버 설정 가이드

목표: 내 컴퓨터가 꺼져 있어도 평일 12:05 / 17:30 / 20:10에 서버가 키움에서 시황을 받아 GitHub에 올리고,
GitHub가 사이트를 자동으로 다시 만든다.

```
[서버 cron] → collect.py(키움 조회) → data/날짜.json → git push → [GitHub Actions] → 사이트 갱신
```

이미 끝난 일: Lightsail 서버 생성(서울, Ubuntu 24.04, 월 5달러), 고정 IP 연결, 키움 IP 등록.
이 문서는 **그 다음부터** 순서대로 진행합니다.

---

## 1. GitHub 저장소(market-lab)에 파일 올리기
1. zip을 컴퓨터에서 풀어 둡니다.
2. GitHub market-lab → **Add file → Upload files** → 풀어 둔 폴더 안의 파일과 폴더(`collect.py`, `generate.py`, `template.html`, `journal.html`, `requirements.txt`, `.gitignore`, `server`, `data`)를 끌어다 놓고 **Commit changes**.
3. 숨김 폴더 `.github`는 위 방법으로 올라가지 않을 수 있습니다. **Add file → Create new file**을 누르고 이름 칸에 `.github/workflows/daily.yml`을 입력(`/`를 치면 폴더가 자동 생성됨), 내용 칸에 `daily.yml` 파일의 내용을 전부 붙여 넣고 **Commit changes**.
4. **Settings → Pages → Build and deployment → Source**를 **GitHub Actions**로 바꿉니다.

## 2. 서버에 접속
Lightsail → market-lab-server → **Connect 탭 → Connect using SSH**. 검은 창이 열립니다.
(상태 검사가 Initializing이면 1~2분 기다린 뒤 접속)

## 3. 메모리 보강 (서버 메모리가 512MB라서 필요)
```
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. GitHub에 쓸 수 있는 열쇠 만들기 (Deploy key)
```
ssh-keygen -t ed25519 -f ~/.ssh/id_market_lab -N ""
cat ~/.ssh/id_market_lab.pub
```
출력된 한 줄(`ssh-ed25519 ...`)을 복사 → GitHub **market-lab → Settings → Deploy keys → Add deploy key**
→ 붙여 넣고 **Allow write access 체크** → 저장. (이 열쇠는 market-lab 저장소 하나에만 쓸 수 있습니다.)
```
printf 'Host github.com\n  IdentityFile ~/.ssh/id_market_lab\n  IdentitiesOnly yes\n' >> ~/.ssh/config
ssh -T git@github.com
```
`yes`를 입력하고 "successfully authenticated" 문구가 나오면 성공입니다.

## 5. 코드 내려받고 설치
```
git clone git@github.com:hun3263/market-lab.git
cd market-lab
bash server/setup.sh
```
(`hun3263`은 GitHub 계정 이름입니다. 다르면 바꾸세요.)

## 6. 키움 키 입력
```
nano ~/.market-lab.env
```
`KIWOOM_APPKEY=`와 `KIWOOM_SECRETKEY=` 뒤에 값을 붙여 넣고 **Ctrl+O → Enter → Ctrl+X**로 저장합니다.
키는 채팅·GitHub에 올리지 마세요. 실전용 키는 주문 권한이 있는 키이므로 서버 밖으로 나가지 않게 합니다.

## 7. 점검 → 시험 실행
**먼저 연결 점검(저장 없음):**
```
set -a; . ~/.market-lab.env; set +a; ./.venv/bin/python collect.py --check
```
정상이면 이렇게 나옵니다.
```
1) 토큰 발급: 성공 (앱키·시크릿·허용 IP 모두 통과)
2) 지수 조회: 성공 → 코스피 ..., 코스닥 ...
3) 오늘 개장 여부: 개장 (또는 휴장 또는 개장 전)
```
실패 문구는 아래 "문제가 생기면" 표를 보세요.

**그다음 실제 1회 수집·업로드:**
```
bash server/run.sh close
tail -30 ~/market-lab.log
```
- `저장 완료`와 `[완료] GitHub 에 올림`이 보이면 성공입니다. GitHub **Actions** 탭에서 deploy-site가 돌고, 몇 분 뒤 사이트에 오늘 날짜가 나타납니다.
- 평일 장중·장 마감 후에만 데이터가 저장됩니다. 주말·휴장일에는 `변경 없음`이 정상입니다.

## 8. 컴퓨터를 끈 상태로 자동 실행 확인 (1회 시험)
평일이고 휴장일이 아닌 날, **자정 전에** 시험합니다.

1. 서버에서 예약 도구를 설치합니다(처음 한 번).
   ```
   sudo apt-get install -y at
   ```
2. 시험 시각을 정해 예약합니다. 예: 오늘 밤 23:30.
   ```
   echo "$HOME/market-lab/server/run.sh close" | at 23:30
   atq
   ```
   `atq`에 예약 한 줄이 보이면 등록된 것입니다.
3. **PC 전원을 끕니다.** 서버 터미널 창도 닫아도 됩니다.
4. 시험 시각 5분 뒤 **휴대폰**으로 GitHub market-lab의 **Commits**를 엽니다.
   `data: 2026-MM-DD close 자동 수집` 커밋이 있으면 성공입니다. Actions의 deploy-site가 초록색으로 끝나고 사이트에 날짜가 보이면 전체 연결이 확인된 것입니다.
5. 안 올라왔으면 PC를 켜고 서버 터미널에서 `tail -50 ~/market-lab.log`의 마지막 줄을 확인합니다.

통과하면 평일 12:05 / 17:30 / 20:10 자동 실행도 같은 방식(cron)으로 동작합니다.
첫 주에는 12:10 / 17:35 / 20:15 이후 Commits에 `noon`, `close`, `nxt` 3개가 올라오는지 한 번 더 보세요.

## 문제가 생기면
| 문구 | 원인 | 조치 |
|---|---|---|
| `8030 ... 투자구분` | 키 종류(실전/모의)와 접속 주소 불일치 | 실전 키면 기본 주소, 모의 키면 `~/.market-lab.env`에 `KIWOOM_BASE=https://mockapi.kiwoom.com` 추가 |
| 토큰 발급 실패 / 인증 오류 | 서버 IP 미등록, 키 오타 | 키움 IP 등록 화면에 고정 IP가 있는지, 키 다시 입력 |
| `연결 실패` | 서버 인터넷 접속 문제 | 잠시 뒤 다시 시도, 계속되면 로그 공유 |
| `Permission denied (publickey)` | Deploy key 쓰기 권한 누락 | Deploy keys에서 Allow write access 체크 확인 |
| `변경 없음` | 휴장일이거나 개장 전 | 정상. 장중에 다시 시험 |
| 커밋이 안 올라옴 | cron 미등록 | `crontab -l`에 market-lab-job 3줄이 있는지 확인 |

## 비용·유지
- Lightsail 월 5달러(새 계정은 크레딧으로 시작, 가입 후 약 6개월 뒤 유료 플랜 업그레이드 필요).
- 키움 앱키는 만료일이 있습니다(발급 화면의 만료일 확인). 만료 전에 갱신하고 `~/.market-lab.env`의 키를 교체하세요.
- 고정 IP는 서버에 연결된 상태로 두세요. 서버를 지울 때는 고정 IP도 같이 지웁니다.
