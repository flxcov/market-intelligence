"""Explanations of stored records. AI-written when configured, always checked."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict

from apps.marketpilot.api.dependencies import require_session_factory
from apps.marketpilot.api.security import require_write_access
from apps.marketpilot.services.explanations import ExplanationService, SubjectKind
from packages.ai.explanations import Explanation

router = APIRouter(prefix="/explanations", tags=["explanations"])
SubjectId = Annotated[int, Path(ge=1)]


class ExplanationAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    provider: str | None
    model: str | None
    request_id: str | None
    facts_hash: str
    issues: list[str]
    error_type: str | None
    created_at: datetime


def _service(request: Request) -> ExplanationService:
    return ExplanationService(require_session_factory(request), request.app.state.explanation_model)


@router.post("/{kind}/{subject_id}", dependencies=[Depends(require_write_access)])
async def explain(request: Request, kind: SubjectKind, subject_id: SubjectId) -> Explanation:
    """Explain a stored order, signal or backtest.

    With ANTHROPIC_API_KEY set, the text is written by the AI model from a fact sheet and
    rejected unless every number appears in the facts and every point cites them; the
    deterministic explanation is returned otherwise. Requires the bearer token because an
    AI call costs money. Every attempt is recorded.
    """
    return await _service(request).explain(kind, subject_id)


@router.get("/{kind}/{subject_id}/attempts")
async def attempts(
    request: Request, kind: SubjectKind, subject_id: SubjectId
) -> list[ExplanationAttempt]:
    """The audit trail of explanation attempts for a record, newest first."""
    rows = await _service(request).history(kind, subject_id)
    return [ExplanationAttempt.model_validate(r) for r in rows]
