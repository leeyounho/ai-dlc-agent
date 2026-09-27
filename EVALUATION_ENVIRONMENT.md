# 평가 보고서의 실행 환경

관련 실행 Issue: #28. 부모 인수 Issue: #11.

새 local_contract 보고서는 schema_version=2를 사용한다. `runtime_environment`에는
아래 다섯 필드만 있고, 관측할 수 없는 값은 JSON null이다.

| 필드 | 의미와 수집 방법 |
| --- | --- |
| python_implementation | sys.implementation.name |
| python_version | sys.version_info의 major/minor/micro와 alpha/beta/rc serial을 포함한 정확한 버전 |
| os_family | sys.platform에서 식별한 Windows/Linux/Darwin 계열, 식별 불가는 null |
| os_release | Windows의 sys.getwindowsversion major/minor/build, Linux의 kernel osrelease |
| machine_architecture | Windows GetNativeSystemInfo의 architecture, Linux AT_PLATFORM의 알려진 아키텍처 label |

Linux의 AT_PLATFORM이 CPU 모델이거나 알려지지 않은 값이면 아키텍처를 추정하지 않고 null이다.
Darwin/미지원 플랫폼의 release와 architecture도 현재 null로 둔다. Windows/Linux 수집 로직은
fixture와 Windows 실 실행으로 확인했으며 실제 RHEL/다른 OS 검증은 별도다.

hostname·사용자 이름·환경 변수·credential·실행 파일 전체 경로를 수집하지 않는다.
`platform.uname`, `platform.machine`, `platform.release`, `os.uname`은 hostname까지
조회할 수 있으므로 호출하지 않는다. 전체 sys.version/compiler/build 문자열도 수집하지 않는다.
JSON에 추가 필드·부적절한 자료형·공백/control/path 구분자·과도한 길이를 가진 값이 있으면
거부한다. 저장하기 전에도 환경 schema를 검증한다.

## 구버전과 비교 정책

schema 1은 그대로 읽고 다시 쓰거나 자동 마이그레이션하지 않는다. 환경 값은 모두 unknown으로
표시한다. 현재 머신에서 환경을 채우지 않으며 schema 1에 환경 필드를 덧붙인 혼합 문서도 거부한다.
schema 2에는 다섯 필드를 모두 요구하며, 다른 schema와 bool/문자열 version은 거부한다.

Markdown은 실행 당시 값을 표시하고 CLI `eval compare`의 JSON은 `environment`를 추가한다.

- same: 다섯 필드가 모두 알려져 있고 값이 같다.
- different: 양쪽에서 알려진 필드 중 하나 이상이 다르다. differences에 두 값을 기록한다.
- unknown: 알려진 차이는 없으나 하나 이상이 미확인이다.

unknown_fields는 different일 때도 미확인 필드를 그대로 보여준다. 구버전끼리 비교해도 same으로
판정하지 않는다. 환경 값의 일치는 전체 실행 조건의 동일성이나 성능 우열을 증명하지 않는다.
performance_conclusion은 not_evaluated다.

같은 suite/fixture/expectation의 기존 사례별 pass/fail·regressions·improvements 비교와 CLI
종료 코드는 유지한다. suite가 다르면 계속 비교를 거부한다. eligible_for_release=false는
환경이 같거나 모든 사례가 통과해도 바뀌지 않는다. CLI의 기존 보고서 위치 안내는 유지한다.

## 개발 실행과 인수 경계

고정 기준 A는 #10 머지 commit `d2196d41a907b70d4eeeb9ac036d587751b1c5ab`다.
후보 B는 별도 Git worktree의 `issue-11-evaluation-environment` 브랜치에서 수정한다.
A의 기존 평가/CLI 테스트와 suite/fixture를 변경하지 않고 B의 모듈에 적용한다.
B의 새 fixture는 same/different/unknown, schema 오류, 금지 정보 조회 차단과 구버전 보존을 검사한다.

이 실행은 Codex를 통한 assisted 개발이다. 설치된 Agent 서비스가 GitHub 승인부터 모델 변경,
운영 runner, App PR 게시까지 자율 수행했다는 증거는 아니다. 사람 승인 기록이나 모델 실행
근거를 대신 만들어내지 않는다. 중복 webhook/재시작은 기존 로컬 계약 시험만 있으며 라이브
자기 적용 인수로 계산하지 않는다. 기능 PR은 #28을 닫고 부모 #11은 미완료 인수 조건을 유지한다.
실제 App/모델/runner 설정을 통한 인수는 별도 실행해야 한다.
