"""Source-linked status projections and conservative, durable comment publication."""

import html
import uuid

from ..errors import AgentError
from ..models.types import require
from ..validation import canonical_digest


def marker(key):
    return "<!-- aidlc:status task=" + canonical_digest(key.as_dict()) + " -->"


def _safe(text):
    value = html.escape(str(text))
    for character in "@`|[]*_#\\":
        value = value.replace(character, f"&#{ord(character)};")
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def render_status(store, key):
    state = store.read(key)
    agent = state.get("agent") or {}
    req, design = state["requirements"], state["design"]
    if state["cancelled"] or state.get("cancel_requested"):
        action = "취소된 작업입니다. 미확정 실행 효과를 확인하세요."
    elif state["paused"]:
        action = "운영자: /aidlc resume"
    elif agent.get("status") == "blocked":
        action = "운영자: 실패 근거·현재 diff·미완료 효과를 확인하세요. 자동 예산 초기화/재실행은 하지 않습니다."
    elif req is None:
        action = "Agent: 원문과 변경 요청을 보존하고 요구사항을 정리합니다."
    elif req["has_open_questions"]:
        action = "기여자: 열린 질문에 답하고 /aidlc amend " + req["revision"] + " 명령으로 변경 요청을 남기세요."
    elif state["requirement_approval"] is None:
        action = "요구사항 승인자: /aidlc approve requirements " + req["revision"]
    elif not state["started"]:
        action = "시작 담당자: /aidlc start " + req["revision"]
    elif design is None:
        action = "Agent: 승인된 요구사항의 설계안을 작성합니다."
    elif design["has_open_questions"]:
        action = "사람 협의: 설계의 열린 질문을 해소하고 새 설계 revision을 검토하세요."
    elif state["design_mode"] == "collaborative" and state["design_approval"] is None:
        action = "설계 승인자: /aidlc approve design " + design["revision"]
    elif agent.get("status") == "ready_for_pr":
        action = "게시 담당: 검증한 source digest로 PR을 준비하세요. AI 리뷰는 사람 승인이 아닙니다."
    else:
        action = "Agent: 승인 범위 안에서 구현·검증을 진행합니다."
    lines = [marker(key), "", "| 항목 | 현재 내용 |", "| --- | --- |",
             f"| 단계·상태 | {_safe(agent.get('stage', state['phase']))} · {_safe(agent.get('status', state['status']))} |",
             f"| 대기·중단 이유 | {_safe(agent.get('reason') or '없음')} |",
             f"| 다음 행동·담당 | {_safe(action)} |",
             f"| 요구사항·설계 | {_safe(req['revision'] if req else '미작성')} · {_safe(design['revision'] if design else '미작성')} |",
             f"| 원문 근거 | Issue #{key.issue_number} · source digest {_safe(state['source_digest'])} |",
             f"| 원문 보존 | original digest {_safe(state['original_source_digest'])} |",
             f"| 코드 근거 | {_safe((agent.get('diff') or {}).get('workspace_digest', '미확인'))} |", ""]
    for label, document in (("요구사항", req), ("설계", design)):
        if document:
            content = store.blob(key, document["digest"])
            lines.append(f"{label} {_safe(document['revision'])}: {_safe(content['summary'])}")
            for field in ("scope", "acceptance_criteria", "changes", "validation_plan", "open_questions", "split_proposals"):
                for item in content.get(field, []):
                    lines.append(f"- {_safe(field)}: {_safe(item)}")
            lines.append("")
    for field in ("requirement_approval", "start_approval", "design_approval"):
        if state[field]:
            comment_id = state[field]["comment_id"]
            lines.append(f"- {_safe(field)}: [댓글 {comment_id}](#issuecomment-{comment_id})")
    for digest in state["amendments"][-20:]:
        observation = store.blob(key, digest)
        comment_id = observation['comment_id']
        lines.append(f"- 변경 요청: [댓글 {comment_id}](#issuecomment-{comment_id}) · digest {digest}")
    for check in agent.get("checks", [])[-10:]:
        evidence = check.get("verification") or {}
        lines.append(f"- 검증 {_safe(check['command_id'])}: {_safe(check['status'])} / {_safe(check['reason'])} "
                     f"· source {_safe(check['source_digest'])} · plan {_safe(check['plan_digest'])} "
                     f"· tests {_safe(evidence.get('executed', '미집계'))}")
    review = agent.get("review")
    if review:
        lines.append("- AI 리뷰(참고 의견, 사람 승인 아님): " + _safe(review["summary"]))
        lines.extend("- 리뷰 의견: " + _safe(f) for f in review["findings"])
    if len(state["amendments"]) > 20 or len(agent.get("checks", [])) > 10:
        lines.append("오래된 근거는 원본 댓글과 task journal에 보존되어 있습니다.")
    body = "\n".join(lines)
    require(len(body.encode("utf-8")) <= 60000, "STATUS_CONTENT_LIMIT")
    return body


class StatusPublisher:
    """Gateway owns bot identity and repository scope. No blind PATCH/retry."""
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    def _record(self, key, document):
        history = self.store.history(key)
        def reduce(state):
            state["status_comment"] = document
            state["state_revision"] = len(history) + 1
            return state
        result = self.store.commit(key, expected_revision=len(history), event_id="status-" + uuid.uuid4().hex,
            event={"kind": "status_comment_checkpoint", "document": document}, reduce=reduce)
        self.store.assert_healthy()
        return result

    def publish(self, key):
        with self.store.locked(key):
            desired = render_status(self.store, key)
            record = self.store.read(key).get("status_comment")
            comments = self.gateway.find(key, marker(key))
            require(len(comments) <= 1, "STATUS_COMMENT_CONFLICT")
            current = comments[0] if comments else None
            if record and record["status"] == "pending":
                require(current is not None and current["body"] == record["body"], "STATUS_EFFECT_UNKNOWN")
                record = {**record, "status": "confirmed", "comment_id": current["id"]}
                self._record(key, record)
            if record:
                require(current is not None and current["id"] == record["comment_id"]
                        and current["body"] == record["body"], "STATUS_COMMENT_EDITED")
                if desired == record["body"]:
                    return {"status": "unchanged", "comment_id": record["comment_id"]}
            elif current:
                # A journal-less remote marker is not proof of ownership by this
                # publication attempt; do not overwrite a restored/manual copy.
                raise AgentError("STATUS_COMMENT_CONFLICT", "Existing status requires explicit reconciliation.")
            intent = {"status": "pending", "comment_id": current["id"] if current else None,
                      "body": desired, "previous_digest": canonical_digest(current["body"]) if current else None}
            self._record(key, intent)
            if current:
                self.gateway.update(key, current["id"], desired, expected_body=current["body"])
            else:
                self.gateway.create(key, desired)
            observed = self.gateway.find(key, marker(key))
            require(len(observed) == 1 and observed[0]["body"] == desired, "STATUS_EFFECT_UNKNOWN")
            self._record(key, {**intent, "status": "confirmed", "comment_id": observed[0]["id"]})
            return {"status": "confirmed", "comment_id": observed[0]["id"]}
