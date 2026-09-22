"""Trusted integration boundary; webhook/model assertions are not observations.

A GHES adapter must reread the original Issue/comment and the actor's current
repository permission. The core intentionally supplies no real network adapter.
"""

from dataclasses import asdict, dataclass
import hashlib
from typing import Protocol

from ..storage import TaskKey


@dataclass(frozen=True)
class IssueObservation:
    task: TaskKey
    author_id: int
    title: str
    body: str
    version: str
    open: bool = True
    author_type: str = "User"

    def document(self) -> dict:
        return {**asdict(self), "body_digest": hashlib.sha256(self.body.encode("utf-8")).hexdigest()}


@dataclass(frozen=True)
class CommentObservation:
    task: TaskKey
    comment_id: int
    actor_id: int
    actor_type: str
    body: str
    created_at: str
    updated_at: str

    def document(self) -> dict:
        return {**asdict(self), "body_digest": hashlib.sha256(self.body.encode("utf-8")).hexdigest()}


class ObservationGateway(Protocol):
    def issue(self, task: TaskKey) -> IssueObservation: ...

    def comment(self, task: TaskKey, comment_id: int) -> CommentObservation | None: ...

    def permission(self, task: TaskKey, actor_id: int) -> str: ...
