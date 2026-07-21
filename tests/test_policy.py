import unittest

from knowledge_workbench.errors import PolicyDeniedError
from knowledge_workbench.models import Classification
from knowledge_workbench.policy import (
    ModelRequestPolicy,
    ProviderLocation,
    enforce_model_policy,
    enforce_model_selection,
)


class PolicyTests(unittest.TestCase):
    def test_public_can_use_cloud(self):
        enforce_model_policy(
            ModelRequestPolicy(Classification.PUBLIC, ProviderLocation.CLOUD)
        )

    def test_internal_cloud_requires_single_use_authorization(self):
        with self.assertRaises(PolicyDeniedError):
            enforce_model_policy(
                ModelRequestPolicy(Classification.INTERNAL, ProviderLocation.CLOUD)
            )
        enforce_model_policy(
            ModelRequestPolicy(
                Classification.INTERNAL,
                ProviderLocation.CLOUD,
                allow_internal_cloud_once=True,
            )
        )

    def test_confidential_cloud_is_always_denied(self):
        with self.assertRaises(PolicyDeniedError):
            enforce_model_policy(
                ModelRequestPolicy(
                    Classification.CONFIDENTIAL,
                    ProviderLocation.CLOUD,
                    allow_internal_cloud_once=True,
                )
            )

    def test_qwen_model_is_explicitly_blocked(self):
        with self.assertRaises(PolicyDeniedError):
            enforce_model_selection("qwen2.5:7b-instruct")

    def test_bge_m3_remains_allowed(self):
        enforce_model_selection("bge-m3")


if __name__ == "__main__":
    unittest.main()
