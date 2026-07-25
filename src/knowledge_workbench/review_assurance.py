from __future__ import annotations

from .errors import KnowledgeWorkbenchError
from .utils import sha256_text


INDEPENDENT_REVIEW_MODE = "independent"
SOLO_ATTESTED_REVIEW_MODE = "solo_attested"
SOLO_ATTESTATION_PHRASE = (
    "我确认本次由同一责任人完成二次核对，非独立复核"
)
REVIEW_MODES = frozenset(
    {INDEPENDENT_REVIEW_MODE, SOLO_ATTESTED_REVIEW_MODE}
)


def required_review_mode(value: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeWorkbenchError("复核模式无效")
    mode = value.strip().replace("-", "_")
    if mode not in REVIEW_MODES:
        raise KnowledgeWorkbenchError(
            "复核模式必须是 independent 或 solo_attested"
        )
    return mode


def review_mode_from_metadata(
    metadata: dict[str, str],
) -> str:
    return required_review_mode(
        metadata.get("review_mode", INDEPENDENT_REVIEW_MODE)
    )


def validate_review_actor_policy(
    *,
    submitter: str,
    reviewer: str,
    review_mode: str,
) -> None:
    review_mode = required_review_mode(review_mode)
    if (
        review_mode == INDEPENDENT_REVIEW_MODE
        and submitter == reviewer
    ):
        raise KnowledgeWorkbenchError("标注人与复核人必须不同")
    if (
        review_mode == SOLO_ATTESTED_REVIEW_MODE
        and submitter != reviewer
    ):
        raise KnowledgeWorkbenchError(
            "单人确认只能复核当前 actor 自己提交的内容"
        )


def review_actor_policy_valid(
    *,
    submitter: str,
    reviewer: str,
    review_mode: str,
) -> bool:
    try:
        validate_review_actor_policy(
            submitter=submitter,
            reviewer=reviewer,
            review_mode=review_mode,
        )
    except KnowledgeWorkbenchError:
        return False
    return True


def validate_review_attestation(
    review_mode: str,
    solo_attestation: str | None,
) -> str | None:
    review_mode = required_review_mode(review_mode)
    if review_mode == INDEPENDENT_REVIEW_MODE:
        if solo_attestation:
            raise KnowledgeWorkbenchError(
                "独立复核模式不接受单人确认声明"
            )
        return None
    if not isinstance(solo_attestation, str):
        raise KnowledgeWorkbenchError(
            "单人确认模式必须提供明确确认声明"
        )
    attestation = solo_attestation.strip()
    if attestation != SOLO_ATTESTATION_PHRASE:
        raise KnowledgeWorkbenchError(
            "单人确认声明与要求的确认短语不一致"
        )
    return sha256_text(attestation)


def review_audit_context(
    review_mode: str,
    attestation_sha256: str | None,
) -> dict[str, str | bool]:
    review_mode = required_review_mode(review_mode)
    context: dict[str, str | bool] = {
        "review_mode": review_mode,
        "independent_review": (
            review_mode == INDEPENDENT_REVIEW_MODE
        ),
    }
    if attestation_sha256 is not None:
        context["solo_attestation_sha256"] = attestation_sha256
    return context
