# 서비스 설정·HTTP transport 구현 계약

현재 구현은 service schema 1을 connection/repository 설정과 함께 로드하고, GitHub 및 LLM adapter가 공통으로 사용할 HTTPS transport 경계를 제공한다. GitHub App/API adapter는 이 경계를 사용하도록 연결했지만 실제 GHES 계약 시험은 아직 하지 않았고, 모델 protocol adapter도 연결하지 않았다. 이 문서는 실제 GHES·LLM 통합 성공을 주장하지 않는다.

## 1. 서비스 설정과 readiness

`load_service`는 service → connection → repository 참조를 선택한 설정 파일 기준으로 해석한다. 알 수 없는 필드, 잘못된 타입, 원격 파일 경로, repository/GitHub instance 불일치, 중복 repository ID, 관리 디렉터리 중첩을 거부한다. 파일이나 환경 변수의 실제 값은 설정 DTO에 복사하지 않는다.

구조적으로 유효하지만 아직 설치자가 공급하지 않은 항목은 오류로 위장하지 않고 `configuration_pending`으로 표시한다. 대상에는 GitHub App 키, webhook/OAuth 및 provider 자격 증명, runner/toolchain/egress profile, TLS CA, 주소 route, 실제 model adapter, 활성 repository가 포함된다. readiness 결과에는 값·환경 변수 이름·파일 경로를 넣지 않는다.

`assert_service_isolation`은 운영/테스트 service의 workspace, state, log, Maven cache, artifact 경로가 겹치지 않는지와 credential 환경 변수·파일 참조를 공유하지 않는지 검사한다. 연결 설정 파일 한 줄만 바꾼 service는 별도 테스트 환경으로 인정하지 않는다.

```text
python -m ai_dlc validate-service --service config/service.example.json
python -m ai_dlc validate-service --service <test-service.json> --compare-service <production-service.json>
```

예시 설정은 의도적으로 placeholder endpoint, 빈 address range, 누락된 설치 profile/secret을 가지므로 `valid=true`이면서 readiness는 `configuration_pending`이다. `--require-ready`는 이 상태에서 종료 코드 3을 반환한다.

## 2. 애플리케이션 HTTP 경계

`HttpTransport`는 요청마다 다음 순서로 검사한다.

1. 허용 method, credential 없는 HTTPS URL, 정확한 hostname/port, header/body 형식을 검증한다.
2. connection의 internal/external 경계와 service의 `(host, port)` route를 확인한다.
3. custom CA 파일을 포함한 TLS trust 설정을 socket/DNS 전에 준비한다.
4. 명시된 system DNS resolver 결과 전체가 route의 IP CIDR 안에 있는지 확인한다. 하나라도 밖이면 연결하지 않는다.
5. 검증한 IP 하나에 직접 연결하되 TLS SNI와 hostname 검증은 원래 host로 수행한다.
6. 응답 크기를 `Content-Length`와 실제 읽기 양쪽에서 제한하고 redirect는 반환 대상과 관계없이 따라가지 않는다.

현재 proxy 정책은 `mode=none`만 지원한다. 이는 `HTTP_PROXY`/`HTTPS_PROXY` 같은 프로세스 환경 설정을 상속하지 않는 명시적 direct 정책이다. 설치 환경에 고정 proxy가 필요하면 proxy 인증·DNS 책임·route와 TLS 중첩 방식을 별도 계약으로 구현하기 전 readiness를 완료할 수 없다. 자동 proxy 탐색이나 direct fallback은 없다.

## 3. 재시도와 오류 분류

- GET/HEAD만 `read_retry_attempts` 안에서 일시적 DNS/connect/timeout/응답 유실과 제한된 HTTP 상태를 같은 endpoint로 재시도한다.
- POST/PUT/PATCH/DELETE는 transport가 자동 재시도하지 않는다.
- write bytes 전송 후 응답을 확인하지 못하면 `EFFECT_UNKNOWN`이다. 실패로 확정하거나 다른 endpoint로 보내지 않고 원격 조회 adapter가 실제 효과를 재조정해야 한다.
- 인증서 오류는 `TLS_ERROR`, 응답 한도 초과는 `RESPONSE_LIMIT`, redirect는 `NETWORK_REDIRECT`, 읽기 일시 오류는 `TRANSIENT_READ`로 분류한다.
- backend 예외, URL, header 값, body, credential은 공개 오류에 포함하지 않는다.

## 4. 경계 밖 통신

| 통신 주체 | 현재 애플리케이션 경계 | 추가로 필요한 통제 |
| --- | --- | --- |
| GitHub/LLM Python adapter | GitHub adapter는 `HttpTransport`를 사용, LLM adapter는 미연결 | 실제 GHES/provider 계약 시험과 LLM adapter 연결 |
| Git publisher | 해당 없음 | 고정 Git 실행 파일, credential helper 제거, publisher UID egress |
| Maven/JVM/repository command | 해당 없음 | runner UID별 OS egress, 내부 settings/cache/truststore |
| 배포 command | 해당 없음 | deployer profile과 대상별 OS egress |

host/CIDR 설정은 Git, JVM, shell 및 자식 프로세스의 방화벽이 아니다. 이 프로세스들이 `HttpTransport`를 우회할 수 없다는 운영 근거는 RHEL runner/egress 작업에서 별도로 확보한다.

## 5. 검증 범위와 남은 확인

단위 시험은 설정 strictness, 운영 외부 목적지 사전 차단, DNS route 이탈, redirect, timeout/읽기 재시도, 쓰기 응답 유실, 응답 크기, 오류 sanitization을 가짜 backend로 확인한다. loopback TLS fixture는 지정 CA와 hostname 검증 성공 및 system CA 거부를 실제 socket으로 확인한다. fixture의 private key는 테스트 전용이며 운영 credential이 아니다.

아직 검증하지 않은 범위는 실제 GHES/LLM API, 사내 DNS/CA/proxy, IPv6 운영 route, HTTP streaming, RHEL 7/8/9 TLS/socket 동작, Git/JVM/자식 프로세스 egress다. 실제 API adapter와 OS 통제가 연결되기 전에는 service readiness나 운영 적합성을 완료로 표시하지 않는다.
