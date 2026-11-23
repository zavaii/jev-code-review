# Jev Code Review

Jev를 코드 리뷰의 **초벌 판정 도구**로 사용하는 작은 Python CLI입니다. Git 커밋에서 코드를 읽어 JSON 요청을 준비하고, 사용자가 명시적으로 실행할 때만 TypeSafe API를 호출합니다.

각 검사 항목은 `violation`, `no_violation`, `insufficient` 중 하나와 확률을 반환합니다. 이 결과는 검토 후보이며 **확인된 버그나 안전하다는 증명이 아닙니다.** 관련 호출부와 재현 검사는 사람이 또는 주재 에이전트가 따로 확인해야 합니다.

## 필요한 것

- Python 3.10 이상과 Git
- macOS 또는 Linux. Windows에서는 WSL을 사용합니다(`fcntl` 잠금 사용).
- 실제 호출 시 본인의 [TypeSafe API 키](https://console.typesafe.ai/keys)와 크레딧

추가 Python 패키지, ChatGPT 계정, SDK 설치는 필요하지 않습니다.

## 시작하기

```bash
git clone https://github.com/zavaii/jev-code-review.git
cd jev-code-review
cp .env.example .env
chmod 600 .env
```

로컬 `.env`의 `TYPESAFE_API_KEY=` 뒤에 **본인의 키**를 넣습니다. `.env`는 Git에서 제외되며, 이 저장소에는 키나 기존 실행 결과가 들어 있지 않습니다. 환경변수로 같은 값을 전달해도 됩니다. 비어 있지 않은 프로세스 환경변수가 `.env`보다 우선합니다.

검토할 프로젝트는 이 도구와 별도의 Git 디렉토리에 둡니다.

```bash
python3 review.py prepare --repo ../your-project --ref HEAD
```

이 단계는 오프라인입니다. 출력된 `preparation` 경로의 `manifest.json`, `source/`, `requests/`를 먼저 확인합니다. 출력 디렉토리는 검토 대상 레포 밖이어야 하며, 자기 자신을 검토할 때는 `--out ../jev-self-review`처럼 따로 지정합니다.

```bash
# 아래 경로를 prepare가 출력한 실제 경로로 바꿉니다.
python3 review.py run runs/<preparation>
```

기본 `run`도 오프라인입니다. **`--live`를 넣어야 코드 조각이 TypeSafe 서버로 전송되고 요금이 발생합니다.** `.env` 저장만으로 실행되지는 않습니다.

```bash
# 첫 시험: 최대 5건, 이 준비본의 누적 예산 상한 0.05 USD
python3 review.py run runs/<preparation> \
  --live --budget-usd 0.05 --max-requests 5
```

`--budget-usd`와 `--max-requests`를 생략하면 `.env`의 `JEV_REVIEW_BUDGET_USD`, `JEV_REVIEW_MAX_REQUESTS`를 사용합니다. `--id <request-id>`를 반복하면 선택한 요청만 보낼 수 있습니다.

## 검토 범위

- **커밋된 파일만** 읽습니다. 미커밋 변경과 미추적 파일은 포함하지 않습니다.
- `src`, `app`, `backend`, `packages` 등 특정 프로젝트 폴더명에 제한하지 않습니다. Python, JS/TS, Go, Rust, Java, C/C++, Ruby, shell, SQL 및 일반 설정 파일 등의 허용 확장자를 탐색합니다.
- 숨김 경로, 의존성·빌드·런타임 산출물, 심볼릭 링크, 1 MB 초과 파일, 명백한 자격 증명 형태가 있는 파일은 제외합니다. 일부 누락·큰 요청은 `excluded`와 `uncovered`에 기록합니다.
- 테스트 파일은 기본적으로 참고 문맥입니다. 테스트 자체를 검사하려면 `--with-tests`를 사용합니다.
- `--path 'src/*'`를 반복해 주 검토 범위를 좁힐 수 있습니다. 숨김·산출물 제외 규칙을 해제하는 옵션은 아닙니다.
- 관련 import 문맥 자동 수집은 JS/TS의 상대 import에 대한 제한된 힌트입니다. 다른 언어와 복잡한 호출 관계는 주재자가 직접 확인해야 합니다.

비밀 정보 검사는 패턴 기반이며 모든 개인정보·비밀을 찾아내지 못합니다. 실제 호출 전에 요청 내용을 확인하세요. Git에 기록한 비밀은 로컬 작업 파일에서 지웠더라도 커밋 기준 검사에 남을 수 있습니다.

## 검사 기준 바꾸기

`rubric.json`에는 입력 검증, 경로 경계, 명령 주입, 비밀 노출, 중복 부작용, 실패 은폐, 상태 경쟁, 값 흐름 오류의 일반 검사 항목 8개가 있습니다.

```bash
python3 review.py prepare --repo ../your-project --rubric ./rubric.json
```

각 항목의 `instruction`은 **하나의 좁은 판단**을 설명하고, `violation`, `no_violation`, `insufficient`는 구별 가능한 조건으로 작성합니다. 필요한 호출부가 없을 때 위반이라고 추측하지 않도록 합니다.

## 예산과 재실행

- 모델은 `jev-1.14.0`으로 고정합니다. 다른 버전의 응답은 자동으로 수락하지 않습니다.
- 현재 계산 요금은 입력 100만 토큰당 $0.042, 출력 무료입니다(2026-11-23 공식 문서 확인). 최신 [모델·요금·문맥 한도](https://docs.typesafe.ai/models)를 확인하고 요금이 바뀌면 상수를 먼저 갱신하세요.
- 예산은 **준비본 하나의 누적 기준**입니다. 여러 준비본의 전역 합산 상한은 아닙니다.
- 다음 요청을 보내기 전에 공개된 기본 문맥 한도에 근거해 최대 65,536 입력 토큰 비용을 보수적으로 예약합니다. 완료된 요청은 실제 입력 토큰 사용량으로 정산합니다. 공급자 요금 변경까지 막는 결제 계정의 하드 캡은 아닙니다.
- 같은 준비본에서 완료한 요청은 다시 보내지 않습니다. 새 준비본은 별도의 실행 기록을 가집니다. 저장 직전 중단된 응답은 다음 실행이 회복합니다.
- 접수 불명·타임아웃·잘못된 응답·HTTP 오류를 자동으로 다시 보내지 않습니다. 오류가 나면 원인을 확인한 뒤 판단해야 합니다.
- 인증 키는 고정된 `https://api.typesafe.ai/v1/systemone`에만 전송하고 HTTP 리다이렉트는 거절합니다.

## 결과 파일

```text
runs/<preparation>/
  manifest.json      # 고정 커밋, 파일·줄 범위, 요청 ID, 누락 범위
  source/            # 검토 대상으로 복사한 커밋의 소스
  requests/          # 전송할 JSON 본문 (인증 헤더 없음)
  responses/         # 받은 응답과 형식 검증을 통과한 응답
  attempts/          # 전송 전 기록한 요청 상태와 토큰 사용량
  REPORT.md          # 미확인 후보와 후속 검증 목록
```

이 폴더에는 검토 대상의 코드와 응답이 있으므로 `.env`와 함께 Git에서 제외됩니다. 기존 프로젝트의 소스 사본·로그·보고서는 이 저장소에 포함하지 않았습니다.

```bash
python3 review.py report runs/<preparation>
python3 -m unittest discover -s tests -v
```

시험은 임시 Git 레포와 가짜 응답만 사용하며 실제 API를 호출하지 않습니다. CI도 키 없이 실행합니다. 정적 판정은 전체 실행 시험·보안 감사·수동 코드 리뷰를 대체하지 않습니다.

## 공식 API

- [API 규격](https://docs.typesafe.ai/api/system-one)
- [선택형 판정](https://docs.typesafe.ai/primitives/choice)
- [모델의 알려진 한계](https://docs.typesafe.ai/model-jaggedness)

이 프로젝트는 TypeSafe의 공식 제품이나 SDK가 아닌 독립적인 도구입니다.

## 라이선스

MIT. 자세한 내용은 [LICENSE](LICENSE)를 참고하세요.
