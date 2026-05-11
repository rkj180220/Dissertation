"""Feedback endpoint — capture user ratings after RFP generation.

POST /feedback  — accepts a 1-5 star rating + optional comment and logs a
LangFuse score event so ratings are visible in the observability dashboard.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from langfuse import Langfuse
from pydantic import BaseModel, Field

logger = structlog.get_logger()
router = APIRouter(tags=["feedback"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """User feedback payload."""

    request_id: str = Field(
        ..., description="The request_id from the pipeline run being rated"
    )
    rating: int = Field(
        ..., ge=1, le=5, description="1–5 star rating (5 = excellent)"
    )
    comment: str | None = Field(
        default=None, max_length=2000, description="Optional free-text comment"
    )


class FeedbackResponse(BaseModel):
    """Acknowledgement envelope."""

    status: str
    message: str


# ---------------------------------------------------------------------------
# POST /feedback
# ---------------------------------------------------------------------------


@router.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(body: FeedbackRequest) -> FeedbackResponse:
    """Record a user rating against the LangFuse trace for the pipeline run.

    Args:
        body: FeedbackRequest with request_id, rating (1-5), optional comment.

    Returns:
        FeedbackResponse acknowledging the submission.
    """
    log = logger.bind(component="feedback", request_id=body.request_id)
    log.info(
        "feedback_received",
        rating=body.rating,
        has_comment=body.comment is not None,
    )

    try:
        lf = Langfuse()
        lf.score(
            trace_id=body.request_id,
            name="user_satisfaction",
            value=body.rating,
            comment=body.comment or "",
            data_type="NUMERIC",
        )
        lf.flush()
        log.info("feedback_logged_to_langfuse", rating=body.rating)
    except Exception:
        # Never fail the user request because of observability issues
        log.warning("langfuse_score_failed", exc_info=True)

    return FeedbackResponse(
        status="ok",
        message="Thank you for your feedback!",
    )
