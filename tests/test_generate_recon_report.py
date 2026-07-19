import unittest

import generate_recon_report as report


class ReconciliationReportTests(unittest.TestCase):
    def test_normalizes_environment_and_sha_variants(self):
        sha256 = "0123456789abcdef" * 4
        variants = [
            "payment-api",
            "payment-api-dev",
            "payment-api-cshg-qa",
            "payment-api-prod-a1b2c3d4",
            f"payment-api-qa-sha256-{sha256}",
            "payment-api-a1b2c3d4-prod",
        ]
        self.assertEqual(
            {report.normalize_name(value) for value in variants},
            {"payment-api"},
        )

    def test_recursive_yaml_renderer_marks_list_leaf_value(self):
        values = {
            "cshg-dev": {"spec": {"ports": [{"containerPort": 8080}]}},
            "cshg-qa": {"spec": {"ports": [{"containerPort": 8081}]}},
        }
        changed = report.differences(values)
        rendered = report.render_yaml(values["cshg-qa"], changed)
        self.assertIn("spec.ports[0].containerPort", changed)
        self.assertIn(
            '<span class="yaml-diff">    - containerPort: 8081</span>',
            rendered,
        )

    def test_resource_key_uses_normalized_metadata_name(self):
        document = {
            "kind": "Deployment",
            "metadata": {"name": "payment-api-dev-a1b2c3d4"},
        }
        self.assertEqual(report.resource_key(document), "Deployment/payment-api")


if __name__ == "__main__":
    unittest.main()
