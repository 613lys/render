import unittest
from collections import Counter

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

    def test_release_diff_keeps_yaml_parent_hierarchy(self):
        old = {"spec": {"containers": [{"name": "api", "ports": [
            {"name": "http", "containerPort": 8080}
        ]}]}}
        new = {"spec": {"containers": [{"name": "api", "ports": [
            {"name": "http", "containerPort": 8081}
        ]}]}}
        old_text = report.yaml.dump(old, sort_keys=False)
        new_text = report.yaml.dump(new, sort_keys=False)
        before, after, _, _ = report.hierarchical_diff_texts(old_text, new_text)
        self.assertIn("spec:", before)
        self.assertIn("    - name: api", before)
        self.assertIn("          containerPort: 8080", before)
        self.assertIn("          containerPort: 8081", after)
        self.assertNotIn("spec.containers[0]", before)

        colored = report.hierarchical_diff_html(old_text, new_text)
        self.assertIn('<span class="ctx">spec:</span>', colored)
        self.assertIn('<span class="ctx">  containers:</span>', colored)
        self.assertIn('<span class="del diff-fragment diff-actual">          - containerPort: 8080</span>', colored)
        self.assertIn('<span class="add diff-fragment diff-actual">          + containerPort: 8081</span>', colored)
        self.assertNotIn('<span class="add">+ spec:</span>', colored)

    def test_removed_resource_marks_parent_and_leaf_yaml_lines(self):
        old = """apiVersion: v1
kind: ConfigMap
metadata:
  name: payment-config
data:
  application.yaml:
    server:
      port: 8080
"""
        rendered = report.hierarchical_diff_html(old, "")
        self.assertIn('class="del diff-fragment diff-actual">- metadata:</span>', rendered)
        self.assertIn('class="del diff-fragment diff-actual">  - name: payment-config</span>', rendered)
        self.assertIn('class="del diff-fragment diff-actual">- data:</span>', rendered)
        self.assertIn('class="del diff-fragment diff-actual">    - server:</span>', rendered)
        self.assertIn('class="del diff-fragment diff-actual">      - port: 8080</span>', rendered)
        self.assertNotIn('<span class="ctx">metadata:</span>', rendered)

    def test_added_resource_marks_parent_and_leaf_yaml_lines(self):
        new = """apiVersion: v1
kind: ConfigMap
metadata:
  name: payment-config
data:
  application.yaml:
    enabled: true
"""
        rendered = report.hierarchical_diff_html("", new)
        self.assertIn('class="add diff-fragment diff-actual">+ metadata:</span>', rendered)
        self.assertIn('class="add diff-fragment diff-actual">+ data:</span>', rendered)
        self.assertIn('class="add diff-fragment diff-actual">    + enabled: true</span>', rendered)

    def test_only_environment_suffix_changes_are_expected(self):
        old = "metadata:\n  name: payment-dev\nserver:\n  port: 8080\n"
        new = "metadata:\n  name: payment-qa\nserver:\n  port: 8081\n"
        events = report.diff_events(old, new)
        signatures = Counter()
        for path, values in events.items():
            signatures[report.event_signature(path, *values)] = 6 if path == "metadata.name" else 1
        classified = report.classify_diff_events(old, new, signatures, shared_required=6)
        self.assertEqual(classified["metadata.name"], "diff-expected-env")
        self.assertEqual(classified["server.port"], "diff-actual")

    def test_environment_suffix_key_rename_is_expected(self):
        old = "data:\n  application-dev.yaml: enabled\n"
        new = "data:\n  application-prod.yaml: enabled\n"
        signatures = Counter(report.event_signature(path, *values) for path, values in report.diff_events(old, new).items())
        signatures = Counter({signature: 6 for signature in signatures})
        classified = report.classify_diff_events(old, new, signatures, shared_required=6)
        self.assertEqual(set(classified.values()), {"diff-expected-env"})
        rendered = report.hierarchical_diff_html(old, new, classified)
        self.assertIn('<span class="expected-common">  - application-</span>', rendered)
        self.assertIn('<span class="expected-env-token">dev</span>', rendered)
        self.assertIn('<span class="expected-env-token">prod</span>', rendered)

    def test_expected_metadata_name_splits_stable_prefix_from_environment(self):
        old = "metadata:\n  name: payment-qa\n"
        new = "metadata:\n  name: payment-dev\n"
        path, values = next(iter(report.diff_events(old, new).items()))
        signatures = Counter({report.event_signature(path, *values): 6})
        classified = report.classify_diff_events(old, new, signatures, shared_required=6)
        rendered = report.hierarchical_diff_html(old, new, classified)
        self.assertEqual(classified[path], "diff-expected-env")
        self.assertIn('class="del diff-fragment diff-expected-env"', rendered)
        self.assertIn('<span class="expected-common">  - name: payment-</span>', rendered)
        self.assertIn('<span class="expected-env-token">qa</span>', rendered)
        self.assertIn('<span class="expected-env-token">dev</span>', rendered)

    def test_sha_names_are_not_expected_but_follow_normal_shared_rule(self):
        old = "metadata:\n  name: payment-qa-sha256-a1b2c3d4\n"
        new = "metadata:\n  name: payment-dev-sha256-deadbeef\n"
        path, values = next(iter(report.diff_events(old, new).items()))
        signatures = Counter({report.event_signature(path, *values): 6})
        classified = report.classify_diff_events(old, new, signatures, shared_required=6)
        rendered = report.hierarchical_diff_html(old, new, classified)
        self.assertEqual(classified[path], "diff-all-namespaces")
        self.assertIn('class="del diff-fragment diff-all-namespaces"', rendered)

    def test_metadata_name_uses_normal_shared_diff_rule(self):
        old = "metadata:\n  name: old-service\n"
        new = "metadata:\n  name: new-service\n"
        path, values = next(iter(report.diff_events(old, new).items()))
        signatures = Counter({report.event_signature(path, *values): 6})
        classified = report.classify_diff_events(old, new, signatures, shared_required=6)
        self.assertEqual(classified[path], "diff-all-namespaces")

    def test_environment_suffix_in_only_one_namespace_is_regular_diff(self):
        old = "metadata:\n  name: payment-dev\n"
        new = "metadata:\n  name: payment-qa\n"
        path, values = next(iter(report.diff_events(old, new).items()))
        signature = report.event_signature(path, *values)
        classified = report.classify_diff_events(
            old, new, Counter({signature: 1}), shared_required=6
        )
        self.assertEqual(classified[path], "diff-actual")

    def test_environment_variants_on_baseline_side_are_expected(self):
        pairs = [
            (f"target: service-{env}\n", "target: service-common\n")
            for env in ("dev", "qa", "prod", "dev", "qa", "prod")
        ]
        expected_paths = report.aggregate_environment_paths(pairs, 6)
        signatures = Counter()
        for old, new in pairs:
            for path, values in report.diff_events(old, new).items():
                signatures[report.event_signature(path, *values)] += 1
        classified = report.classify_diff_events(
            *pairs[0], signatures, shared_required=6,
            aggregate_expected_paths=expected_paths,
        )
        self.assertEqual(expected_paths, {"target"})
        self.assertEqual(classified["target"], "diff-expected-env")

    def test_environment_variants_on_current_side_are_expected(self):
        pairs = [
            ("target: service-common\n", f"target: service-{env}\n")
            for env in ("dev", "qa", "prod", "dev", "qa", "prod")
        ]
        expected_paths = report.aggregate_environment_paths(pairs, 6)
        signatures = Counter()
        for old, new in pairs:
            for path, values in report.diff_events(old, new).items():
                signatures[report.event_signature(path, *values)] += 1
        classified = report.classify_diff_events(
            *pairs[0], signatures, shared_required=6,
            aggregate_expected_paths=expected_paths,
        )
        self.assertEqual(expected_paths, {"target"})
        self.assertEqual(classified["target"], "diff-expected-env")

    def test_fixed_one_sided_field_is_shared_in_all_namespaces(self):
        pairs = [("{}\n", "added: enabled\n") for _ in range(6)]
        signatures = Counter()
        for old, new in pairs:
            for path, values in report.diff_events(old, new).items():
                signatures[report.event_signature(path, *values)] += 1
        classified = report.classify_diff_events(
            *pairs[0], signatures, shared_required=6,
            aggregate_expected_paths=report.aggregate_environment_paths(pairs, 6),
        )
        self.assertEqual(classified["added"], "diff-all-namespaces")

    def test_environment_one_sided_field_is_expected(self):
        pairs = [
            ("{}\n", f"added: service-{env}\n")
            for env in ("dev", "qa", "prod", "dev", "qa", "prod")
        ]
        signatures = Counter()
        for old, new in pairs:
            for path, values in report.diff_events(old, new).items():
                signatures[report.event_signature(path, *values)] += 1
        expected_paths = report.aggregate_environment_paths(pairs, 6)
        classified = report.classify_diff_events(
            *pairs[0], signatures, shared_required=6,
            aggregate_expected_paths=expected_paths,
        )
        self.assertEqual(expected_paths, {"added"})
        self.assertEqual(classified["added"], "diff-expected-env")

    def test_same_diff_in_every_namespace_has_own_class(self):
        old = "server:\n  port: 8080\n"
        new = "server:\n  port: 8081\n"
        path, values = next(iter(report.diff_events(old, new).items()))
        signature = report.event_signature(path, *values)
        complete = report.classify_diff_events(
            old, new, Counter({signature: 6}), shared_required=6
        )
        partial = report.classify_diff_events(
            old, new, Counter({signature: 5}), shared_required=6
        )
        self.assertEqual(complete[path], "diff-all-namespaces")
        self.assertEqual(partial[path], "diff-actual")


if __name__ == "__main__":
    unittest.main()
