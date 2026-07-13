#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

import yaml


REPOSITORIES = [
    {
        "name": "artifactory-local",
        "url": "https://artifactory.example.com/artifactory/helm/",
    }
]


def option(args: list[str], name: str) -> str:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        raise SystemExit(f"missing required option {name}")


def options(args: list[str], name: str) -> list[str]:
    return [args[index + 1] for index, value in enumerate(args[:-1]) if value == name]


def merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def emit(document: dict) -> str:
    return yaml.safe_dump(document, sort_keys=False).rstrip()


def main() -> int:
    args = sys.argv[1:]
    if args == ["repo", "list", "-o", "json"]:
        print(json.dumps(REPOSITORIES))
        return 0
    if not args or args[0] != "template":
        print(f"unsupported fake helm command: {args}", file=sys.stderr)
        return 2

    if len(args) < 3:
        print("template requires release and chart", file=sys.stderr)
        return 2
    release_name = args[1]
    chart = args[2]
    namespace = option(args, "--namespace")
    version = option(args, "--version")
    values = {}
    for values_path in options(args, "--values"):
        loaded = yaml.safe_load(Path(values_path).read_text(encoding="utf-8")) or {}
        merge(values, loaded)

    if chart != "artifactory-local/demo-app":
        print(f"unexpected chart: {chart}", file=sys.stderr)
        return 4
    if version != "1.2.3":
        print(f"unexpected version: {version}", file=sys.stderr)
        return 5

    log_path = os.environ.get("FAKE_HELM_LOG")
    if log_path:
        Path(log_path).write_text(json.dumps(args), encoding="utf-8")

    app_config = values.get("appConfig") or {}
    documents = []
    if "--include-crds" in args:
        documents.append(
            {
                "apiVersion": "apiextensions.k8s.io/v1",
                "kind": "CustomResourceDefinition",
                "metadata": {"name": "widgets.example.test"},
                "spec": {},
            }
        )
    documents.extend(
        [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": f"{release_name}-config", "namespace": namespace},
                "data": {
                    "values.yaml": yaml.safe_dump(values, sort_keys=False),
                    "environment": str(app_config.get("environment", "")),
                    "logLevel": str(app_config.get("logLevel", "")),
                    "endpoint": str(app_config.get("endpoint", "")),
                    "shared": str(app_config.get("shared", "")),
                    "inlineOnly": str(app_config.get("inlineOnly", "")),
                    "token": str(app_config.get("token", "")),
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": release_name, "namespace": namespace},
                "spec": {
                    "replicas": values.get("replicaCount"),
                    "selector": {"matchLabels": {"app": release_name}},
                    "template": {
                        "metadata": {"labels": {"app": release_name}},
                        "spec": {"containers": [{"name": "app", "image": "example.test/demo:1.2.3"}]},
                    },
                },
            },
            {
                "apiVersion": "secrets.hashicorp.com/v1beta1",
                "kind": "VaultStaticSecret",
                "metadata": {"name": f"{release_name}-vss", "namespace": namespace},
                "spec": {
                    "path": "kv/default/demo",
                    "destination": {"name": f"{release_name}-secret", "create": True},
                },
            },
        ]
    )
    print("\n---\n".join(emit(document) for document in documents))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
