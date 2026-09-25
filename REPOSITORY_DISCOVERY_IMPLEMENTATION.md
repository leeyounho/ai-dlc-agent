# Repository discovery 구현 계약

이 구현은 고정된 Git commit의 저장소 규칙과 지식을 읽어 실행 계획 및 설계·리뷰 문맥을 제안한다. 발견 단계는 repository 코드를 실행하지 않으며, 제안된 명령은 별도의 승인과 기존 execution coordinator 검사를 통과해야 한다.

## 수집 근거와 우선순위

각 근거는 `kind`, source path/API, commit, SHA-256 digest, priority, 요약을 갖는다. 낮은 우선순위부터 README(10), build 파일(20), workflow(30), AGENTS.md(40), CODEOWNERS(50), GitHub branch protection/ruleset(60), repository profile(80), 운영자 toolchain(100) 순이다. 서로 다른 명시적 build/test command, 누락 module, JDK 불일치처럼 안전하게 결정할 수 없는 경우 `confirmation_required`와 출처 목록을 반환한다.

우선순위는 권한 순서이기도 하다. repository 파일과 GitHub 정책은 근거일 뿐 다음 항목을 추가하거나 완화할 수 없다.

- 등록 executable ID, JDK 및 timeout
- Maven settings/cache 참조
- runner network profile과 서비스 egress
- 요구사항·설계·실행·merge·deployment 승인

GitHub App adapter는 현재 default branch의 protection과 repository ruleset을 읽기만 한다. CODEOWNERS도 파일 근거로만 기록하며, 실제 reviewer/merge 판정은 최신 GitHub 관측과 workflow 정책이 담당한다.

## 실행 프로필

`java_maven` adapter는 DTD/entity가 없는 bounded `pom.xml`을 파싱한다. root와 선언된 modules, Maven profiles, compiler source/target/release, Surefire/Failsafe 기본 JUnit 경로와 고정 `reportsDirectory`를 수집한다. 명시적 command override가 없을 때만 운영자 profile에 등록된 Maven executable로 `-B test` 후보를 만든다. profile을 임의 활성화하거나 `${...}` 경로를 추측하지 않는다. Maven settings와 cache는 shell 인자가 아니라 운영자 소유 ID로 유지한다.

공통 `command` adapter는 repository profile의 argv 배열을 그대로 사용한다. 명령이 없으면 README 예시나 workflow shell을 실행하지 않고 확인 대기로 둔다. 이 저장소의 Python fixture도 같은 adapter를 사용한다. 언어별 Agent는 만들지 않는다.

C# 등은 `ProjectProfile`/`CommandProfile`과 공통 execution 경계를 재사용할 수 있지만, 이번 범위에는 SDK 탐색·결과 parser·RHEL/Windows runner 검증이 없다. 따라서 C# 실행 완료를 주장하지 않는다.

## ADR·KB 문맥

기존 `docs/adr`, `adr`, `docs/decisions`와 `docs/knowledge`, `knowledge`, `docs/kb`를 우선 사용하고, 없을 때 repository profile의 fallback 경로를 제안 위치로 정한다. Markdown 항목은 commit/path/digest/title/status/supersedes를 보존한다. 같은 제목의 활성 ADR이 명시적으로 대체되지 않으면 conflict로 표시한다.

`KnowledgeIndex.context`는 파일명·제목·본문 keyword로 필요한 항목만 bounded하게 골라 design/review 목적에 제공한다. 별도 vector DB나 전체 문서 무조건 전송은 없다. 중요한 새 결정이나 운영 지식은 `KnowledgeChangeProposal`로 Markdown 파일 내용을 만들 뿐 직접 기록하지 않으며, 이후 제한된 patch 도구와 PR review를 거치는 Git 변경이다.

## 검증 범위와 남은 경계

단위 fixture는 Actions 없는 저장소, single/multi-module Maven, Maven profiles/JUnit 경로/settings/cache, 상충 규칙, 미설정·미승인 명령, Python command profile, ADR 충돌, GitHub branch protection/ruleset을 포함한다. 실제 GHES 버전별 ruleset 계약, pagination, 사내 Maven/JDK, 실제 build 실행, C# adapter는 운영 환경에서 별도 검증해야 한다.
