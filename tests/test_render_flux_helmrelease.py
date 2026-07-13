from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "render_flux_helmrelease.py"
FAKE_KUBECTL = ROOT / "tests" / "fakes" / "fake_kubectl.py"
FAKE_HELM = ROOT / "tests" / "fakes" / "fake_helm.py"
FIXTURES = ROOT / "tests" / "fixtures"


def add_fake_command(directory: Path, name: str, script: Path) -> None:
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(
            f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8"
        )
    else:
        path = directory / name
        path.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)


def documents(content: str) -> list[dict]:
    return [document for document in yaml.safe_load_all(content) if document]


def find_document(items: list[dict], kind: str, name: str) -> dict:
    return next(
        item
        for item in items
        if item.get("kind") == kind and item["metadata"]["name"] == name
    )


class RenderFluxHelmReleaseTest(unittest.TestCase):
    def render(
        self,
        entry: Path,
        *selection: str,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(extra_environment or {})
        with tempfile.TemporaryDirectory() as temporary:
            command_directory = Path(temporary)
            add_fake_command(command_directory, "kubectl", FAKE_KUBECTL)
            add_fake_command(command_directory, "helm", FAKE_HELM)
            environment["PATH"] = str(command_directory) + os.pathsep + environment["PATH"]
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(entry), *selection],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_name_merges_values_and_applies_namespace_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            helm_log = Path(temporary) / "helm.json"
            result = self.render(
                FIXTURES / "dev" / "kustomization.yaml",
                "--name",
                "demo",
                extra_environment={"FAKE_HELM_LOG": str(helm_log)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            rendered = documents(result.stdout)
            config = find_document(rendered, "ConfigMap", "demo-dev-config")
            values = yaml.safe_load(config["data"]["values.yaml"])
            self.assertEqual(values["replicaCount"], 4)
            self.assertEqual(values["appConfig"]["environment"], "dev")
            self.assertEqual(values["appConfig"]["shared"], "from-common")
            self.assertEqual(values["appConfig"]["token"], "dev-token")
            self.assertEqual(values["appConfig"]["logLevel"], "debug")

            deployment = find_document(rendered, "Deployment", "demo-dev")
            self.assertEqual(deployment["spec"]["replicas"], 4)
            vss = find_document(rendered, "VaultStaticSecret", "demo-dev-vss")
            self.assertEqual(vss["spec"]["path"], "kv/dev/demo")
            self.assertEqual(vss["spec"]["destination"]["name"], "demo-dev-secret")

            helm_arguments = json.loads(helm_log.read_text(encoding="utf-8"))
            self.assertEqual(helm_arguments[2], "artifactory-local/demo-app")
            self.assertIn("--include-crds", helm_arguments)
            self.assertEqual(
                helm_arguments[helm_arguments.index("--version") + 1], "1.2.3"
            )

    def test_all_renders_releases_then_runs_namespace_kustomize_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            post_render_log = Path(temporary) / "post-render.log"
            result = self.render(
                FIXTURES / "namespace",
                "--all",
                extra_environment={
                    "FAKE_KUBECTL_POST_RENDER_LOG": str(post_render_log)
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                post_render_log.read_text(encoding="utf-8").splitlines(),
                ["post-render"],
            )

            rendered = documents(result.stdout)
            alpha = find_document(rendered, "VaultStaticSecret", "alpha-vss")
            beta = find_document(rendered, "VaultStaticSecret", "beta-vss")
            self.assertEqual(alpha["spec"]["path"], "kv/dev/namespace")
            self.assertEqual(beta["spec"]["path"], "kv/dev/namespace")

    def test_name_selects_only_one_release(self) -> None:
        result = self.render(FIXTURES / "namespace", "--name", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {
            document.get("metadata", {}).get("name")
            for document in documents(result.stdout)
        }
        self.assertIn("alpha-vss", names)
        self.assertNotIn("beta-vss", names)


if __name__ == "__main__":
    unittest.main()
