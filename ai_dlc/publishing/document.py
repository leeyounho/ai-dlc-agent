"""A complete PR document assembled from final, source-linked task evidence."""

from ..agents.status import _safe
from ..models.types import require
from ..validation import canonical_digest


def marker(key):
    return "<!-- aidlc:pull task=" + canonical_digest(key.as_dict()) + " -->"


def render_pull(store, key, candidate, notes):
    # Decisions/deployment limitations are supplied by the trusted caller after
    # reviewing final requirements, not invented from a successful test exit.
    fields = {"decisions", "limitations", "deployment", "recovery", "knowledge", "questions"}
    require(type(notes) is dict and set(notes) == fields
            and all(type(value) is str and value.strip() for value in notes.values()), "PUBLISH_DOCUMENT")
    state = store.read(key)
    req, design = state["requirements"], state["design"]
    requirements, architecture = store.blob(key, req["digest"]), store.blob(key, design["digest"])
    agent = state["agent"]
    title = "Issue #" + str(key.issue_number) + ": " + requirements["summary"].replace("\n", " ")[:180]
    lines = [marker(key), "", "# " + _safe(title), "", f"Refs #{key.issue_number}", "",
        f"문서 revision: {candidate['operation_id']} · 요구사항: {req['revision']} · 설계: {design['revision']} · 검증 head: {candidate['commit']}",
        f"Source digest: {candidate['source_digest']} · Git tree: {candidate['tree']} · Base: {candidate['parent']}",
        "", "## 목적과 범위", "", _safe(requirements["summary"]), ""]
    lines.extend("- " + _safe(item) for item in requirements["scope"])
    lines += ["", "## 인수 조건과 충족 근거", "", "| 조건 | 구현 결과 | 확인 근거 |", "| --- | --- | --- |"]
    for item in requirements["acceptance_criteria"]:
        lines.append(f"| {_safe(item)} | 아래 변경·검증에 대해 사람 검토 필요 | req blob {req['digest']} |")
    lines += ["", "## 최종 설계", "", _safe(architecture["summary"]), ""]
    lines.extend("- " + _safe(item) for item in architecture["changes"])
    lines += ["", _safe(notes["decisions"]), "", "## 실제 변경", ""]
    for kind in ("added", "modified", "deleted"):
        for item in agent["diff"][kind]:
            path = item if type(item) is str else item["path"]
            lines.append(f"- {kind}: {_safe(path)}")
    lines += ["", "## 검증 결과", "", "| 검사·명령 | 환경·commit | 실제 결과 | 근거 |", "| --- | --- | --- | --- |"]
    for check in agent["checks"]:
        lines.append(f"| {_safe(check['command_id'])} | source {check['source_digest']} | {_safe(check['status'])} / "
                     f"{_safe(check['reason'] or 'command completed')} | plan {check['plan_digest']} · run {_safe(check['run_id'])} |")
    lines += ["", "AI 리뷰(참고 의견, 사람 승인 아님): " + _safe(agent["review"]["summary"]),
        "", _safe(notes["limitations"]), "", "## 배포와 복구 계획", "", _safe(notes["deployment"]),
        "", _safe(notes["recovery"]), "", "## 관련 지식과 작업", "", _safe(notes["knowledge"]), "", _safe(notes["questions"])]
    body = "\n".join(lines)
    require(len(body.encode()) <= 60000, "PUBLISH_DOCUMENT_LIMIT")
    return {"title": title, "body": body, "branch": candidate["branch"], "base": candidate["base"]}
