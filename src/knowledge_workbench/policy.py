from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import PolicyDeniedError
from .models import Classification


BLOCKED_MODELS = frozenset({"qwen2.5:7b-instruct"})


class ProviderLocation(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True, slots=True)
class ModelRequestPolicy:
    classification: Classification
    provider_location: ProviderLocation
    allow_internal_cloud_once: bool = False


def enforce_model_policy(policy: ModelRequestPolicy) -> None:
    if policy.provider_location is ProviderLocation.LOCAL:
        return
    if policy.classification is Classification.PUBLIC:
        return
    if (
        policy.classification is Classification.INTERNAL
        and policy.allow_internal_cloud_once
    ):
        return
    raise PolicyDeniedError(
        f"密级 {policy.classification.value} 不允许本次云模型调用"
    )


def enforce_model_selection(model_name: str) -> None:
    normalized = model_name.strip().lower()
    if normalized in BLOCKED_MODELS:
        raise PolicyDeniedError(f"项目配置禁止调用模型 {model_name}")


def most_restrictive(
    first: Classification, second: Classification
) -> Classification:
    order = {
        Classification.PUBLIC: 0,
        Classification.INTERNAL: 1,
        Classification.CONFIDENTIAL: 2,
        Classification.RESTRICTED: 3,
    }
    return max((first, second), key=order.__getitem__)
