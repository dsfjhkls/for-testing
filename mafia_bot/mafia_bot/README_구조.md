# 마피아 GM 봇 — 파일 구조 (13,481줄 단일 파일 → 35개 모듈)

실행은 예전과 똑같이 `python bot.py` 입니다. (Render 시작 명령 변경 불필요)

```
bot.py            진입점: 모듈을 불러오면 슬래시 명령어가 자동 등록되고, 마지막에 봇을 실행
requirements.txt  의존성 (버전 범위 지정)
assets/           font.ttf, paper_background.jpg, 보스/네임드 그림
mafia/
  core.py           Flask keep-alive, 수입 배율, 명령어 잠금, Supabase 연결, bot 객체, 오류 보고
  status.py         감옥·수배 저장/복구, get_user_data 등 유저 데이터 접근
  alignment.py      성향(선/악) · 전직
  achievements.py   업적 · 일퀘 · 주간 · 칭호
  admin.py          /관리 · 점검 모드 · 공통 체크
  transfer.py       /송금
  police_arrest.py  /설정 /검거 /밀수          police_ops.py   /작전 /심문
  battle_data.py    NPC·보스·네임드 데이터      battle.py       /전투 /보스 /네임드
  civilian.py       /아르바이트 /봉사 /기부 /소매치기   corrupt_police.py  /갈취
  gamble.py         /도박        duel.py  /결투       join_leave.py  가입·탈퇴
  mission.py        /임무        blackmarket.py 암시장   profile.py  /프로필
  police_shop.py    경찰상점     info.py  /정보
  family.py /조직   raid.py /레이드   territory.py /구역   ranking.py /랭킹
  jailbreak.py /탈옥  crafting.py /제작  stats.py /능력치  auction.py /경매  help.py /도움말
  police_unit.py /수사팀   salary.py /월급   confirm_upgrade.py   dept_store.py /백화점
  biz_stock.py      /사업 /주식
  events.py         on_ready
```

의존 방향은 위에서 아래(번호가 낮은 모듈 → 높은 모듈)입니다. 아래 모듈의 함수를 위 모듈이 호출해야 할 때는
함수 안에서 `from .xxx import ...` 로 호출 시점에 가져옵니다 (순환 import 방지). 새 명령어는 해당 모듈에 추가하고,
새 모듈을 만들었다면 `bot.py` 의 import 목록에만 넣으면 됩니다.

## 이번에 고친 버그
1. **get_user_data (심각)**: DB 읽기에 한 번이라도 실패하면 '잔고 1000짜리 가짜 데이터'를 돌려줬고, 그 값으로 계산한
   결과가 저장되면서 실제 잔고·인벤토리가 덮어써질 수 있었음 → 이제 `DBReadError` 로 알리고 저장하지 않음.
   (처음 접속 유저가 동시에 두 번 생성 요청을 보낸 경우에도 다시 읽도록 보강)
2. **백그라운드 작업 소실 위험**: 레이드·구역전·수사작전·주식 시세 루프 등 `create_task` 결과를 변수에 안 담아
   실행 도중 사라질 수 있었음 → `keep_task()` 로 끝날 때까지 붙잡아 둠 (7곳).
3. **재연결 때마다 명령어 재동기화**: on_ready 가 다시 불릴 때마다 `tree.sync()` 를 호출 → 프로세스당 1회로 제한.
4. 결투 시간초과 처리의 `except:` → `except Exception:` (취소 신호를 삼키지 않도록).
5. Flask 포트를 `PORT` 환경변수 우선으로 (없으면 기존 8080).
6. 분리 과정에서 전역 변수(점검 모드·오류 로그 채널·이벤트 루프)를 모듈 간에 안전하게 공유하도록 정리.
   `/칭호` 안의 불필요한 `global npc_cooldowns` 삭제.

## 점검했지만 손대지 않은 것 (권장 사항)
- 비동기 함수 안에서 DB 호출을 직접 하는 곳이 있음 (전투 종료 `end_battle`, 무기상점·암시장 구매, `finalize_mission` 등 8곳).
  DB가 느리면 그동안 봇 전체가 멈춤 → 점차 `await asyncio.to_thread(...)` 로 옮기는 것을 권장.
- 돈 갱신이 "읽기 → 계산 → 덮어쓰기" 방식이라, 스레드에서 도는 작업(송금·경매·레이드 정산)과 겹치면 드물게 덮어써질 수 있음.
  근본 해결은 Supabase SQL 함수(rpc)로 `money = money + x` 를 한 번에 처리하는 것.
- `update_user_stats` 는 잔고가 모자라도 음수로 저장함 (호출하는 쪽에서 미리 검사하는 구조라 현재는 문제 없음).
- 도박 기대수익은 슬롯 약 89.7%, 동전 96%로 정상 범위입니다.

## 검증 방법과 한계
- discord.py 를 설치할 수 없는 환경이라, 가짜(스텁) discord 로 원본과 분리본을 각각 import 해서 비교했습니다:
  슬래시 명령어 95개 등록 동일, 최상위 이름·함수 동일, 정의 913개 중 의도한 수정 7개만 다름, 정의 안 된 이름 0개.
- 실제 디스코드 서버에서의 동작은 직접 확인하지 못했습니다. 배포 전에 테스트 서버에서 주요 명령어
  (/프로필, /전투, /송금, /레이드, /주식)를 한 번씩 눌러 보세요.
- 버그 점검은 전체 정독이 아니라 정적 분석 + 돈이 오가는 핵심 로직(유저 데이터, 송금, 도박, 주식) 위주였습니다.
