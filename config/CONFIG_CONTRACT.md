# 설정 자료 계약 D2

이 문서는 strict 설정 DTO의 필드·타입·교차 검증 계약이다. 아래 예시는 연결 실행에 필요한 실제 값이 채워진 설정이 아니다. 모든 객체는 알 수 없는 필드를 거부하고 JSON 중복 key도 오류다. 문자열은 명시적으로 빈 값이 허용된 경우 외에는 비어 있을 수 없다. 설정끼리 임의 deep merge하지 않는다.

연결/repository schema 2와 service schema 1을 strict 검증 함수와 immutable dataclass로 구현했다. 배포/운영 평가 정책은 아직 설계 계약이다. 로컬 평가의 plan/suite는 별도 schema 1이며 운영 평가 정책을 실행한 것으로 취급하지 않는다. [README.md](../README.md)와 [transport 구현 계약](../TRANSPORT_IMPLEMENTATION.md)에 범위를 기록한다.

D2는 여러 LLM 등록·목적별 선택을 추가한다. 연결/repository 설정은 schema_version=2, 변경되지 않은 service/배포 설정은 1이다. 모델 세부 계약은 [MULTI_MODEL_SPEC.md](../MULTI_MODEL_SPEC.md)가 기준이다. 사내 API 확인은 지금 수행하지 않으며 미구성 항목을 유지한 오프라인 형식 검사를 허용한다.

## 1. 연결 설정

[production.example.json](production.example.json), [test-external.example.json](test-external.example.json)을 기준으로 한다.

| 필드 | 타입·제약 |
| --- | --- |
| schema_version | integer, 2 |
| profile | production / test |
| network.external_access | boolean; production은 false |
| network.internal_hosts / external_hosts | 중복 없는 hostname 배열, wildcard·scheme·path 불가; external_hosts는 명시적 test에서만 |
| network.follow_redirects | D1은 false만 지원 |
| llm.providers | 고유 provider ID별 boundary/adapter/base_url/auth/max_concurrent_requests. production은 internal만 |
| llm.models | 고유 model ID별 provider/model_env/capabilities/context_window_tokens/max_output_tokens |
| llm.routing | default_model + 목적별 by_purpose. 등록된 model만 참조 |
| llm.failure_policy | stop. 자동 모델/환경 fallback 없음 |
| maven.repository_url | HTTPS URL, 목적지 확인 |
| maven.mirror_of | D1은 `*` |
| maven.username_env / password_env | 선택, 사용 시 함께 지정 |
| maven.local_cache_dir / workspace.root | 경로, 운영/테스트 분리 |
| storage.state_dir / log_dir | 경로, workspace와 분리, 실행 코드 접근 불가 |

provider 인증은 unconfigured/none/bearer/header로 분리하고 복합 인증은 설치된 custom adapter의 인증 schema로 확장한다. `unconfigured`와 capability의 `unknown`, 한도의 null은 미확인 상태이며 실제 호출 준비 완료를 의미하지 않는다. 내부 OpenAI 호환 API는 production에서도 해당 protocol adapter를 사용할 수 있다. 외부 여부는 adapter 이름이 아니라 boundary와 실제 목적지 정책으로 판정한다.

## 2. 서비스 설정

[service.example.json](service.example.json)의 필수 객체는 schema_version, connection_profile_file, github, web, execution, transport, limits, repository_profile_files다.

| 객체 | 필드·제약 |
| --- | --- |
| github | instance_id, web_base_url, api_base_url, app_id_env, private_key_file, webhook_secret_env 모두 필수. api_version은 선택적인 `YYYY-MM-DD` REST version. GHES URLs는 연결 프로파일의 내부 host에 속해야 함 |
| web | bind_host, port(1..65535), public_url(HTTPS), tls_mode(reverse_proxy/direct), trusted_proxy_cidrs, identity_adapter, client_id_env, client_secret_env, session_absolute_seconds, session_idle_seconds, permission_cache_seconds, sse_heartbeat_seconds, poll_interval_seconds |
| execution | runner_pool_profile_file, toolchains_profile_file, egress_profile_file, artifact_root, global_concurrency, repository_concurrency, model_concurrency |
| transport | tls(system/custom_ca), proxy(mode=none), dns_mode=system, host/port/address CIDR routes, connect/read timeout, 응답 크기, 읽기 시도 횟수 |
| limits | 예시의 모든 필드 필수, 양의 integer; retry 횟수의 0 허용 여부는 별도 필드에서 명시 |
| repository_profile_files | 경로 배열, 정규화 후 중복 금지 |

추가 제약:

- D1의 identity_adapter는 ghes_user다. 환경 확인 후 다른 인증 경로를 채택하면 별도 타입/명시적 설정으로 추가한다.
- tls_mode=direct이면 `tls_certificate_file`, `tls_private_key_file`이 필수이며 reverse_proxy에서는 이 두 필드를 사용하지 않는다. reverse_proxy 기본 bind는 loopback이고 trusted proxy 명시가 필요하다.
- session idle은 absolute 이하, permission cache는 1..60초, 나머지 timeout은 양수. model_concurrency와 run concurrency는 별도 자원이다.
- `connection_profile_file`은 정확히 하나다. 시작한 프로세스에서 다른 profile로 전환하지 않는다.
- external test 서비스 설정은 테스트용 artifact_root·GitHub test repo/자격 증명·runtime 경로를 별도로 지정한다. 운영 service 예시의 연결 파일 한 줄만 바꾸면 충분하다고 간주하지 않는다.
- 서비스 예시의 secrets/runtime 참조 파일은 설치 환경에서 공급한다. 누락되면 readiness 실패이며 도구 실행은 하지 않는다.
- route는 connection allowlist 안의 정확한 host/port와 0/0이 아닌 IP CIDR을 사용한다. 빈 address_ranges는 형식상 유효하지만 readiness 미완료이며 DNS/socket 연결을 만들지 않는다.
- custom_ca는 CA bundle 파일이 필수다. proxy는 현재 명시적 none만 지원하며 환경 proxy를 상속하지 않는다. 고정 proxy가 필요한 환경은 별도 계약 구현 전 ready가 아니다.
- GET/HEAD만 제한적으로 재시도한다. write 전송 후 응답 유실은 `EFFECT_UNKNOWN`으로 남기며 자동 재시도·endpoint fallback하지 않는다.

## 3. repository profile

[repository.example.json](repository.example.json)의 schema_version=2, enabled, github_instance_id, repository_id, display_name, intake, model_routing, workflow, roles, project, knowledge, deployment는 필수다.

- repository_id는 양의 integer, instance ID는 service와 일치. display_name은 표시용이며 API 접근의 식별 기준이 아님.
- intake.mode는 D1에서 all_human_issues. 처리하려면 App 설치·profile enabled가 모두 충족되어야 한다.
- model_routing은 allowed_models(등록된 model ID 배열), default_model(해당 배열의 model ID 또는 null), by_purpose(허용된 목적과 model ID의 map)로 구성한다. null/빈 map은 전역 route 상속이다. repo purpose → repo default → global purpose → global default 순으로 선택한 뒤 allowed_models를 검사한다. 외부 테스트 repo profile은 테스트 registry의 model ID를 사용하며 운영 model 목록을 자동 병합하지 않는다.
- workflow의 enum·기본값은 [작업 계약](../WORKFLOW_SPEC.md) §1을 따른다. requirement_approval_required는 true만 지원. required_human_reviews는 0 이상. allowed_issue_overrides는 start_policy/design_mode 값의 부분집합만 허용한다.
- roles의 다섯 필드는 `minimum_repository_permission=write`, `actor_ids=양의 정수 배열` 형태. 실제 실행에는 GHES 권한과 ID allowlist를 모두 확인한다. 읽기/일반 기여 역할은 작업 계약의 기본 정책을 따른다.
- project.adapter는 java_maven / command, rules_source는 repository. command는 개발 언어와 무관하게 저장소별 명령을 표현한다. toolchain_id는 설치 profile의 ID. command_overrides는 command ID를 key로 하는 객체이며 비어 있으면 repository discovery가 근거와 확인 필요 사유를 포함한 계획을 제안한다. 설정 DTO는 rules_source와 knowledge 경로를 보존한다. 발견 결과는 운영자 toolchain의 executable/network 상한을 넓히지 않으며 실제 운영 runner 실행은 후속 범위다.
- 각 command override는 `executable_id`, `argv` 문자열 배열, `cwd` workspace 상대 경로, `timeout_seconds`, `report_patterns`로 정의한다. executable_id는 등록된 toolchain 내 ID다. shell 문자열·secret 삽입·제어 파일 경로는 허용하지 않는다. 문자열 인자의 `${...}` 등은 자동 치환하지 않으며 동적 인자는 별도 typed API로 전달한다.
- junit_report_patterns는 작업 공간 내 상대 glob 배열. java_maven에서는 하나 이상 필요하고 command에서는 비어 있어도 된다. 실제 plugin/profile의 보고서 경로와 맞추고 필요한 보고서 누락을 성공 처리하지 않는다. 다른 검증 도구는 별도 결과 해석을 연결한다.
- knowledge 경로는 workspace 상대 경로, `.git` 및 제어 영역 제외. 기존 ADR/KB 위치를 먼저 탐색하고 없을 때만 설정 경로를 새 Git 변경 제안 위치로 사용한다. 설계·리뷰 문맥 항목은 commit/path/digest를 보존한다.
- deployment.required_by_default는 boolean, environment_profile_files는 파일 배열. required=true인데 환경이 없으면 배포 미구성으로 표시하며 운영 준비 실패다.

enabled=false인 예시는 실제 repo ID·승인 담당자·배포 환경을 아직 채우지 않았음을 의미한다. reader/approver에게 자동 권한을 부여하는 예시가 아니다. Java build command를 예제 하나로 고정하지 않도록 command_overrides는 비워 두었다.

## 4. 환경별 배포 profile

[deployment.example.json](deployment.example.json)은 필드 형식만 보여주는 disabled 예시다. 경로·정상 기준·관측 시간은 실제 앱에서 확정해야 하며 예시 수치를 제품별 기본 성공 조건으로 사용하지 않는다.

필수 필드: schema_version=1, enabled, environment_id, target_lock_key, adapter=command, command_profile_id, allowed_endpoints, credential_env, prerequisites, apply_timeout_seconds, inspect_interval_seconds, verify_checks, stabilization_window_seconds, rollback_mode, rollback_profile_id, migration_policy, observation.

| 항목 | 계약 |
| --- | --- |
| allowed_endpoints | 상위 운영 egress profile의 허용 범위 안에 있는 URL 배열; repo가 이를 추가했다고 실제 연결 권한이 생기지 않음 |
| prerequisites | release_verified, artifact_digest_matches, environment_generation_matches 필수 포함 |
| verify_checks | 1개 이상, 고유 ID. http_json 또는 command 형식 |
| http_json check | url, expected_status, health_field, healthy_value, release_id_field, timeout_seconds, consecutive_successes 필수; field는 최상위 JSON key, 임의 식 실행 없음 |
| command check | command_profile_id, timeout_seconds, consecutive_successes; 검증된 JSON 결과 계약 사용 |
| rollback_mode | manual / automatic; 둘 모두 검증된 rollback profile 필요 |
| migration_policy | none / backward_compatible / manual_recovery_required; 마지막 값은 자동 rollback 금지 |
| observation | interval_seconds, incident_failure_threshold 양수, create_followup_issue boolean |

verify의 기대 release ID는 사용자가 댓글에 적은 임의 값이 아니라 승인된 manifest에서 공급한다. 실패 응답·미일치·관측 불가는 모두 정상 판정에 포함하지 않는다.

## 5. 설치자가 제공할 실행 profile

이 값들은 OS·toolchain·배포 도구를 실제 확인한 후 생성한다. checkout 안에서 공급하지 않는다.

| profile | 필수 구성 |
| --- | --- |
| runner-pool | schema_version, profile_id, OS/CPU identity, 고정 launcher 경로/digest, UID/GID pool, workspace roots, resource limits, uid별 egress profile ID |
| toolchains | schema_version, 각 toolchain ID의 java_home/java_version, maven executable/version, Git executable/version, CA/truststore 참조, 허용 executable IDs |
| egress | schema_version, profile ID별 주체(control/model/publisher/runner/deployer), 허용 scheme/host/address/port, 내부 DNS·proxy route, OS enforcement 근거 |
| command profiles | schema_version, 각 ID의 root/관리자 소유 executable 경로/digest, 고정 argv, typed stdin schema, credential/egress scope, timeout, stdout schema, 지원 apply/inspect/verify/rollback 동작 |

예시의 미제공 profile 참조는 사용자에게 API 규격을 추측하라는 요구가 아니다. 설치 담당자가 내부 자료·실행 도구를 확인하고 채울 연결점이다. 공통 schema와 동작 코드는 먼저 구현 가능하다.

## 6. 검증 단계

1. 오프라인 형식 검사: JSON, key/type/enum, 중복, 상대 경로 정규화, profile 제약. 네트워크 없음.
2. 로컬 참조 검사: 권한·secret 존재·profile ID·실행 파일/digest·경로 격리·OS enforcement 확인. 네트워크 없음.
3. 명시적 내부 연결 검사: 실제 GHES/Gemma/Maven/SSO/배포 inspect 계약. 승인된 주소만 사용.
4. 활성화 검사: 권한 담당자·배포/복구/정상 판정·toolchain·OS 검증을 충족한 repo만 운영 준비로 표시.

문법이 유효하다는 것과 연결·권한·운영 준비가 완료되었다는 것을 각각 표시한다. 이 문서를 작성하면서 수행한 검증은 로컬 문서/JSON 확인이며 실제 연결이나 설정 로더 실행이 아니다.

## 7. 평가 정책 E1

[evaluation.example.json](evaluation.example.json)은 서비스 실행 설정과 분리된 평가 정책이다. schema_version=1이고 필드 의미·산식·관문은 [EVALUATION_SPEC.md](../EVALUATION_SPEC.md)를 따른다. policy_status는 proposed/adopted이며 정식 판정에는 adopted와 고정 policy digest가 필요하다.

사례/반복/최소 분모는 양의 정수, rate/신뢰수준은 0..1 범위(신뢰수준은 양 끝 제외), violation 허용 수는 0이다. confidence_method는 wilson, confidence_unit은 independent_task다. 평가 반복을 독립 사례 수로 대체하지 않는다. model·scope·suite 참조와 version을 평가 manifest에 고정한다.

resource_and_latency_limits_file=null은 미설정 상태이므로 형식 검사는 가능하지만 운영 적합 판정은 미평가다. suite/scope 경로는 이후 채우는 참조이며 현재 예시를 실행 준비 완료로 보지 않는다. 누락·미측정 관문을 pass로 처리하지 않고, 판정 결과가 자동 배포를 유발하지 않게 한다.
