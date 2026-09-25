# RHEL installed runner 구현 계약

Issue #7의 결과는 설치자 소유 runtime profile을 검증하고 제한된 native helper에 실행을 위임하는 `InstalledRunner` 제어 adapter다. repository나 모델이 OS 격리 여부를 선언할 수 없고, 미구성 helper·불일치 host·미검증 RHEL/egress에서는 실행하지 않는다. 제어 계정의 shell이나 일반 `subprocess`로 대체하는 경로는 없다.

## 1. 신뢰 경계

설치자는 서비스 checkout 밖의 세 JSON을 공급한다.

| profile | 고정하는 항목 |
| --- | --- |
| runner pool | RHEL major/CPU/cgroup/network 방식, helper 절대 경로·digest, UID/GID/home slot, workspace/protected roots, CPU/memory/process/file/output/disk 한도 |
| toolchains | platform·egress 연결, root 소유 executable 경로·digest·version, command ID별 정확한 argv/cwd/timeout/검증·생성 경로, Maven settings/truststore/per-run cache/credential profile ID |
| egress | production/test, runner 주체, 기본 deny, DNS/proxy 방식, 내부 또는 명시적 test 목적지의 scheme/host/port/CIDR/purpose, OS enforcement evidence |

알 수 없는 필드, RHEL 7/8/9 이외 OS, workspace/protected 경로 중첩, 중복 UID/GID/home, `0.0.0.0/0`·`::/0`, production의 test_external route, 참조되지 않는 platform/resource/egress는 거부한다. verified 상태에는 timestamp와 placeholder가 아닌 evidence digest가 필요하다.

실행 전 helper·executable·Maven settings/truststore를 다시 연다. 최종 symlink, hardlink, root 이외 소유, group/world writable 파일, writable/unowned parent directory, digest 변경을 거부한다. 실행 파일은 executable mode도 확인한다. 이 검증과 profile/evidence/workspace digest를 runtime digest로 묶고 helper가 같은 digest를 attestation해야 예약할 수 있다.

## 2. helper 프로토콜

`NativeHelperClient`는 profile의 정확한 helper에 고정 인자 `request-v1` 하나만 전달한다. `shell=False`, cwd `/`, 환경은 `LANG=C`, `LC_ALL=C`뿐이며 PATH, HOME, proxy, Git/JVM/Maven 옵션, App/LLM/deploy secret을 상속하지 않는다. stdin/stdout은 1 MiB 이하 strict JSON protocol v1이고 stderr 내용은 오류에 노출하지 않는다.

helper는 같은 배포 패키지에서 설치된 root 소유 native artifact여야 한다. 다음을 profile ID와 exact request에 따라 독립적으로 재검증한다.

- preflight: platform/profile/runtime digest와 slot/resource/egress 가용성
- start: 빈 supplementary groups, 전용 UID/GID/home/workspace, 고정 executable+argv, clean env, rlimit/cgroup/disk/output 한도, 사전 설치된 default-deny egress scope로 시작
- inspect: run token, PID와 start ticks, cgroup, UID slot, workspace/runtime digest가 모두 같은 실행만 관측
- cancel: process group/cgroup 전체 종료를 요청하고 요청 성공을 종료 성공으로 바꾸지 않음

helper의 설치·권한·OS 정책은 repository 변경으로 생성하지 않는다. packaging 단계는 이 protocol을 구현한 검토된 native artifact와 RHEL별 설치 정책을 같은 패키지에 포함해야 한다. 이 저장소에는 권한 상승을 흉내 내는 Python helper나 범용 sudo 규칙을 넣지 않았다.

## 3. 종료·복구

identity에는 run/token/platform/slot/UID/PID/start ticks/cgroup/runtime/workspace/resource/egress가 모두 들어간다. 재시작 후 `inspect`는 전체 identity가 일치할 때만 결과를 받으며 모르는 실행을 재시작하지 않는다. start 응답 유실, identity 변경, helper timeout/오류는 coordinator에서 uncertain으로 남는다.

완료 결과는 exit/termination, stdout·stderr digest/byte 수 외에 process tree 종료, 잔류 process 수, UID 재사용 가능 여부를 포함한다. 잔류 process가 있거나 UID를 격리해야 하면 `process_tree_stopped=false`로 전달되어 성공 확정이 차단된다. `resource_limit` 종료를 별도 결과로 보존한다. 기존 coordinator의 source 변화, stale JUnit, 보고서 없음, 0 tests, all skipped 검증도 그대로 적용된다.

## 4. RHEL별 상태와 실제 검증 범위

[runner pool 예시](runtime/runner-pool.example.json)는 RHEL 7 cgroup v1/iptables owner, RHEL 8 cgroup v1/iptables owner, RHEL 9 cgroup v2/nftables cgroup 계약을 분리한다. 부팅 옵션으로 다른 cgroup mode인 RHEL 8, aarch64 등 정확히 일치하지 않는 host는 지원 대상으로 추측하지 않는다.

세 예시 profile은 모두 `unverified`이고 digest도 placeholder다. 따라서 그대로는 service readiness와 `InstalledRunner` preflight를 통과하지 않는다. 현재 확인한 것은 strict loader, exact argv/timeout/workspace, 최소 egress 연결, helper invocation 환경, start/inspect/cancel 및 재시작 identity, 잔류 process·UID quarantine·resource limit의 로컬 protocol fixture다.

현재 개발 환경은 Windows/Python 3.12.6이며 전체 Python unittest 명령 자체는 이 환경에서 실행한다. 설치된 Java는 11.0.2이고 Maven은 없으므로 Java 8/Maven 검증 근거로 사용하지 않았다. 실제 native helper binary, 별도 OS 계정에서의 Python unittest/평가, JDK 8/Maven/JUnit, child process kill, cgroup/rlimit/disk quota, filesystem secret 차단, IPv4/IPv6/DNS/proxy 우회 차단은 RHEL 7/8/9 설치 환경에서 아직 실행하지 않았다. 해당 evidence가 생성되기 전 어떤 platform도 지원 완료나 운영 준비로 표시하지 않는다.
