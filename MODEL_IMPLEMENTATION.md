# 모델 어댑터·세션 구현 계약

관련 Issue: #8. Chat Completions/Responses 프로토콜, 설치 코드의 custom adapter 등록,
파일 저널 기반 세션·호출·도구 claim을 구현했다. 합성 응답과 로컬 TLS 서버에서 검증했으며
실제 사내 Gemma4/gpt-oss/DeepSeek/Gauss 또는 외부 모델에 호출하지 않았다.
승인·파일·runner 도구 연결은 #9의 [AgentLoop](AGENT_IMPLEMENTATION.md)에 구현했다.

## 호출 경로

`build_model_components(bundle, store)`는 네트워크 없이 Router, AdapterRegistry,
공유 ModelConcurrency, ModelSessions를 구성한다. 서비스당 이 구성 한 개를 공유한다.
`serve`도 이 구성을 생성하지만 설치된 task workspace/runner/driver가 없는 기본 composition은 readiness에
`MODEL_WORKFLOW_UNCONNECTED`를 유지한다. adapter 설치를 업무 수행 준비 완료로 해석하지 않는다.

호출자는 기존 task의 FileJournal에서 `create_run` → `open_session` → `generate`를 사용한다.
run ID는 task 실행 엔진이 관리하며 모델에 새 run/예산 생성 권한을 주지 않는다.
repository ID와 instance, purpose route, allowlist, 활성 상태, capability, 인증 참조,
model 이름·한도를 매번 확인한다. 모델 계열 이름으로 API를 선택하지 않는다.

모든 HTTP 호출은 공통 HttpTransport의 HTTPS/host/port/CIDR/CA/redirect/응답 크기 정책을 따른다.
base URL에는 지정된 adapter 경로(`/chat/completions` 또는 `/responses`)만 붙이며
인증값은 adapter에서 header에 적용한다. 다른 endpoint 탐색이나 모델 fallback은 없다.
기존 연결 설정의 port 443 제한을 유지한다. 테스트의 임시 localhost port는 wire fixture 전용이다.

## 프로토콜과 도구

- `ModelRequest`, `ModelResponse`, `Message`, `ToolDefinition`, `ToolCall`, `Usage`는 immutable DTO다.
  중첩 인자·schema는 검증한 canonical JSON 문자열로 고정한다.
- 비 streaming 텍스트·함수 도구만 지원한다. Chat Completions는 `max_completion_tokens`,
  Responses는 `max_output_tokens`, `store=false`를 사용한다. 구형 게이트웨이의 별도 필드나
  다른 규격은 설치자가 custom adapter 코드로 구현하고 계약 시험해야 한다.
- Responses의 reasoning item/encrypted content는 같은 session에서만 재전송하고 공개 요약이나
  다른 모델로 넘기지 않는다. 별도 provider 대화 저장·opaque response ID 의존성을 만들지 않는다.
- tool 이름·ID·중복 ID, arguments JSON, 실제 제공한 schema를 검증한다. 중복 JSON key,
  NaN/Infinity, 잘못된 자료형, 추가 인자, 미등록 tool, 부분 응답, refusal은 오류다.
- JSON Schema는 object/array/string/integer/number/boolean/null, description, enum, required,
  additionalProperties=false, items 및 길이·수치 bounds만 지원한다. `$ref`, union 등의 미지원
  조건은 정의 시 거부한다. 서버에는 `strict=false`를 보내고 로컬 검증은 항상 수행한다.
- 모델 요청은 실행 권한이 아니다. `claim_tool`은 실제 도구 실행 전 durable claim만 제공한다.
  이후 승인·권한·경로·runner 정책 재검사 및 실행은 #9의 책임이다.
  `finish_tool`은 실행기/복구기가 제공한 실제 결과와 evidence reference를 기록한다.

## 세션, 복구와 예산

session에 model/provider/purpose/config/model-name/tool-schema/adapter-version/prompt/checkpoint
digest를 고정한다. 설정이나 실제 model 환경변수 값이 바뀌면 기존 session을 재사용하지 않는다.
모델 교체는 미완료 요청·도구가 없는 checkpoint에서 새 session ID로 수행한다.
새 문맥에는 호출자가 선정한 requirements/design/diff/verification_summary 네 필드만 허용한다.
이 자료 자체는 승인이 아니며 workflow 승인 원본을 대신하지 않는다.

model_calls/tool_calls/active_seconds 한도는 run 생성 시 고정하고 모든 session에서 합산한다.
동일 run 재등록으로 예산을 올릴 수 없다. model call은 HTTP 전에 횟수와 timeout 전체를 예약한다.
정상 반환·오류는 실제 관측 시간을 반영하며, process 중단 시 예약을 그대로 유지한다.
active_seconds는 이 계층의 모델 호출 시간이며 전체 workflow/runner 시간은 #9에서 별도 제한해야 한다.
사용량 미제공은 null이고, 서로 다른 모델의 token 수를 비용처럼 합산하지 않는다.
문맥 입장 검사는 canonical request UTF-8 바이트 수와 1024 token 여유를 사용하는 보수적 추정이다.
정확한 tokenizer 측정이 아니므로 provider 계약 시험에서 실제 context 한도를 추가 확인해야 한다.

요청 ID는 run 안에서 고유하다. 완료된 요청의 재전달은 저장된 응답을 반환하고 HTTP를 다시 보내지 않는다.
pending 요청을 가진 상태로 재시작하면 `MODEL_EFFECT_UNKNOWN`으로 중단한다.
`abandon_request`는 운영자가 근거를 남긴 명시적 응답 폐기이며 원격 취소 성공을 의미하지 않는다.
pending/claimed 도구가 있으면 새 session으로 이동할 수 없다. 도구 claim은 한 번만 허용하고
claimed 상태에서 재시작한 도구는 실제 효과를 확인한 뒤 `finish_tool`로 조정한다.
동일 도구 결과의 재전달은 idempotent이고 다른 결과로 덮어쓰기는 거부한다.

파일 저널의 단일 writer·task lock·CAS·hash chain·immutable record를 재사용한다.
응답 영속화 전에 저장이 실패하면 도구를 외부로 반환하지 않으며 pending intent가 복구를 요구한다.
transcript는 제어 프로세스 소유 state에만 저장하고 일반 상태 댓글/웹에 그대로 공개하지 않는다.
현재 저널의 2 MiB record 한도에 도달하면 저장을 차단한다. 무제한 transcript를 잘라 재시도하지 않는다.

## 중단, 오류와 재시도

서비스 전체 및 provider 동시성 한도를 동시에 적용한다. 대기 슬롯은 `MODEL_BUSY`를 반환하며
호출 예산을 소비하지 않는다. 호출자가 scheduler에 다시 예약해야 한다.
인증 오류, protocol 오류, 부분 응답, 결과가 불명확한 POST는 자동 재시도하지 않는다.
연결 전 실패와 명확한 HTTP 429에만 같은 요청/모델/endpoint에서 총 1~3회 시도를 허용한다.
429의 정수 Retry-After가 0~60초 범위일 때만 재시도 시점을 기록한다. 긴 값이나 HTTP-date는
자동 재시도하지 않는다. sleep loop 없이 scheduler가 같은 요청 ID를 다시 전달한다.
각 시도는 호출 예산에 포함되며 재시작으로 한도를 초기화하지 않는다.

취소/마감 시간은 transport 진입, DNS 후, 응답 수신 후 검사하고, 연결된 TLS socket에는
watcher를 두어 handshake·header·body 대기도 shutdown으로 중단한다. OS DNS와 TCP connect는
동기 시스템 호출이므로 즉시 취소를 보장하지 않으며 TCP는 설정 timeout으로 제한된다.
늦거나 취소된 결과의 tool call은 반환하지 않는다. 이는 로컬 I/O 중단이며 원격 추론 취소의 증거가 아니다.
custom adapter는 설치자 소유의 신뢰 코드이고 같은 취소·시간 계약을 구현해야 한다.

공통 ModelError는 code/retryable/effect_state와 값 없는 메시지를 반환한다.
HTTP 오류 body, 원본 예외, 토큰/header는 진단에 넣지 않는다.

## 검증 및 미검증

`python -m unittest tests.test_model_adapters_and_sessions -v`로 wire 변환, 실제 localhost TLS,
부분 body 중단/시간 초과, 오류·도구 인자·usage, 동시성, 예산, checkpoint 교체,
process 중단과 재시작·중복 전달을 검증한다. 기존 transport 및 전체 회귀도 함께 실행한다.

프로토콜 근거: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling),
[Chat Completions 생성](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
이 문서는 호환 프로토콜 구현 근거이며 사내 provider 호환성이나 실제 모델 업무 품질의 증거가 아니다.
실제 API/GHES/RHEL/Java/Maven·업무 평가 및 운영 배포는 수행하지 않았다.

승인·모델·파일 도구·실제 검증을 연결한 호출자는 [AgentLoop](AGENT_IMPLEMENTATION.md)다.
모델 journal mutation도 workflow state_revision을 갱신하여 같은 task의 사람 승인과
실행 CAS가 이어진다. 임의 도구/승인/명령은 제공하지 않으며 purpose 변경에는 최신
요구사항·설계·diff·실제 검증 checkpoint만 전달한다.
