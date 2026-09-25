# GitHub App·webhook·실시간 관측 구현 계약

이 구현은 GitHub App 서버 인증과 서명된 webhook을 기존 workflow 엔진에 연결한다. App JWT, repository 하나로 제한한 installation token, 현재 Issue·댓글·권한 재조회, 원본 webhook의 영속 inbox와 중복 처리를 포함한다. 실제 GHES 인스턴스와 내장 HTTP 서버의 route wiring은 아직 검증하지 않았으므로 운영 준비 완료를 뜻하지 않는다.

## 1. 신뢰 경계와 처리 순서

```text
POST /hooks/github의 원본 bytes
  → X-Hub-Signature-256 검증
  → 크기·delivery ID·event/action·객체 ID 검증
  → 원본 bytes와 digest를 inbox에 원자적으로 확정
  → 접수 응답
  → 비동기 processor가 installation token으로 현재 API 사실 재조회
  → workflow 저널 commit
  → delivery 처리 결과 확정
```

webhook payload에서 신뢰하는 값은 재조회 대상을 찾기 위한 installation/repository/Issue/comment ID뿐이다. payload의 본문, 사용자 종류, 작성자, 권한과 revision은 승인 근거가 아니다. `GitHubApiClient`는 매 관측마다 repository ID·이름·활성 상태를 확인한 뒤 현재 Issue, 원본 댓글 및 계산된 현재 collaborator 권한을 다시 읽는다.

처리 중 종료되어도 inbox가 확정되지 않은 delivery는 GitHub가 재전송할 수 있고, 확정된 delivery는 같은 ID·내용으로 중복 접수된다. source/command/reconcile event ID도 결정적이므로 workflow commit 뒤 처리 표식 전에 종료된 경우 재실행해도 중복 효과를 만들지 않는다. 같은 delivery ID의 다른 bytes나 처리 결과 변경은 충돌로 거부한다.

## 2. App 등록

현재 read 경로의 최소 권한은 다음과 같다.

| 범위 | 권한 | 사용처 |
| --- | --- | --- |
| Repository | Metadata: read | repository identity·상태, collaborator의 계산된 현재 권한 |
| Repository | Issues: read | Issue 원문과 issue comment 원본 |

현재 adapter는 GitHub에 댓글을 쓰거나 PR·contents를 변경하지 않으므로 이 단계에서 Issues write, Contents write, Administration write를 요구하지 않는다. 이후 게시·Git·PR 기능을 추가할 때 해당 기능의 권한을 별도 증분하고 설치 관리자의 재승인을 받아야 한다. GitHub는 App 권한에 따라 사용할 수 있는 REST endpoint와 구독 event를 제한하므로 대상 GHES 버전의 App 등록 화면과 REST 문서에서 다시 확인한다.

구독 event/action:

- `issues`: `opened`, `edited`, `reopened`, `closed`
- `issue_comment`: `created`, `edited`, `deleted`
- `repository`: `archived`, `unarchived`, `edited`, `renamed`, `deleted`, `transferred`
- App에 기본 전달되는 `installation`: `created`, `deleted`, `suspend`, `unsuspend`, `new_permissions_accepted`
- App에 기본 전달되는 `installation_repositories`: `added`, `removed`
- 등록 확인용 `ping`

직접 collaborator나 team/organization membership 변경 event는 GHES 버전과 Members 조직 권한 범위를 확인한 뒤 추가한다. 현재도 모든 명령과 구현 gate에서 permission endpoint를 다시 호출하므로 권한을 읽지 못하거나 write 미만이면 진행하지 않는다. Issue/comment 갱신 event에서도 저장된 승인을 재조정한다.

GitHub의 공식 계약은 [App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app), [webhook 서명 검증](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries), [App 권한 선택](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app), [webhook event](https://docs.github.com/en/webhooks/webhook-events-and-payloads), [collaborator 권한 조회](https://docs.github.com/en/rest/collaborators/collaborators)를 기준으로 한다. 실제 설치에서는 연결 대상 GHES 버전으로 문서를 고정한다.

## 3. endpoint와 내부 배치

- App의 Webhook URL은 외부 reverse proxy가 노출하는 `https://<public-host>/hooks/github`로 설정한다.
- proxy는 body를 decode·재인코딩·압축 해제하지 않고 원본 bytes를 단일 worker에 전달해야 한다. 서명은 JSON parsing 전에 원본 bytes로 검증한다.
- webhook route는 브라우저 session이나 SSO 로그인을 요구하지 않고 App webhook secret만 검증한다. 그 밖의 웹 route와 cookie를 공유하지 않는다.
- 최대 body는 1 MiB다. 접수 성공은 inbox 파일과 상위 directory의 영속화가 끝난 뒤에만 반환한다. 긴 workflow/model/build 작업은 요청 안에서 실행하지 않는다.
- reverse proxy와 Agent 사이도 내부 TLS·호스트 방화벽·요청 크기 제한을 적용한다. `trusted_proxy_cidrs`는 client identity를 만드는 용도가 아니라 명시된 proxy 경계를 제한하는 설정이다.
- 현재 저장소는 `GitHubWebhookEndpoint`로 정확한 `POST /hooks/github`와 접수 후 202, 요청 오류 4xx, 영속 저장 불가 503 계약을 제공한다. 후속 내장 웹 서버는 method/path, raw bytes와 세 header를 변형 없이 이 경계에 연결해야 한다.

## 4. 자격 증명과 API 버전

`app_id_env`에는 App ID, `private_key_file`에는 2048-bit 이상 RSA private key, `webhook_secret_env`에는 고엔트로피 webhook secret을 공급한다. private key는 checkout 밖의 관리자 관리 파일이고 installation token은 메모리에만 캐시한다. JWT는 RS256, `iat=현재-60초`, 10분 미만 expiry를 사용한다. installation token 요청은 `repository_ids=[현재 repository_id]`로 더 좁힌다.

`github.api_version`은 선택 필드다. GitHub.com 개발 환경에서는 그 환경에서 검증한 REST version date를 명시한다. GHES에서 version header 지원 여부나 값이 확인되지 않았으면 추측해서 넣지 않는다. 409/415/422 응답은 `GITHUB_CAPABILITY_UNAVAILABLE`로 구분해 설정한 버전에서 계약이 지원되지 않음을 표시한다. 401/403, 404, 일시 장애는 각각 인증/범위, 미존재, 가용성 오류로 분리하며 응답 본문이나 token을 오류에 넣지 않는다.

## 5. GHES 운영과 GitHub.com 개발 분리

운영 GHES와 GitHub.com 개발은 서로 다른 connection/service/repository profile, App ID·private key·webhook secret·사용자 OAuth credential, state/log/workspace/artifact 경로를 사용한다. GitHub.com 개발 profile만 `github.com`과 `api.github.com`을 external allowlist와 transport route에 명시할 수 있다. 운영 profile에서 외부 host로 자동 fallback하지 않는다.

`validate-service --compare-service`로 관리 경로와 credential 참조가 겹치지 않는지 확인한다. 이 검사는 OS 계정, 방화벽, App 설치 범위까지 증명하지 않으므로 설치 검증에서 별도로 확인한다.

## 6. App 서버 인증과 사람 SSO

App JWT와 installation token은 Agent가 GitHub API를 호출하는 server-to-server 자격 증명이다. 댓글 작성자가 현재 로그인한 사람인지 확인하는 근거는 API가 반환한 actor type/ID와 현재 repository permission이며, App 설치 권한을 사람의 웹 조회 권한으로 사용하지 않는다.

대시보드의 사람 로그인은 별도의 `identity_adapter=ghes_user`와 user-to-server/OAuth 또는 승인된 사내 SSO 연동이다. webhook endpoint에는 이 session이 없고, 브라우저 session에는 App private key나 installation token이 노출되지 않는다. 실제 GHES에서 지원되는 사용자 인증 흐름은 웹 구현 전에 별도 계약 시험이 필요하다.

## 7. 검증과 남은 범위

로컬 시험은 독립 RSA 검증을 포함한 JWT, repository-scoped token cache, API version header·기능 미지원 분류, 현재 Issue/comment/permission 재조회, 교차 repository comment 거부, 서명 변조·크기·event/action, durable inbox, 중복 delivery, inbox commit 후 응답 유실, bot/편집 댓글, 승인 삭제·권한 회수, 이벤트 순서 역전을 다룬다. 실제 secret 대신 테스트 전용 RSA/TLS fixture와 API 대역을 사용한다.

아직 검증하지 않은 항목은 실제 GHES App 등록·설치 token/API 응답, GHES별 event/action과 REST version, 내부 CA/DNS/reverse proxy, webhook 재전송 UI/API, 사용자 SSO, endpoint를 실제 socket에 연결하는 내장 HTTP server, organization/team 권한 변경 event다. installation/repository 범위 event의 `scope_reconciler`는 delivery 기반 event ID로 멱등 처리해야 하며, repository 전체 task 열거·실행 중단은 scheduler 통합 단계에서 연결한다. 이 연결 전에도 API scope/권한 조회 실패는 gate를 닫는다.
