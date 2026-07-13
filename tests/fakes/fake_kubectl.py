#!/usr/bin/env python3
import sys
import os
from pathlib import Path

import yaml


def load_documents(path: Path) -> list[dict]:
    return [item for item in yaml.safe_load_all(path.read_text(encoding="utf-8")) if item]


def matches(document: dict, target: dict) -> bool:
    api_version = str(document.get("apiVersion") or "")
    group, _, version = api_version.rpartition("/")
    if not group:
        version = api_version
    metadata = document.get("metadata") or {}
    checks = {
        "group": group,
        "version": version,
        "kind": str(document.get("kind") or ""),
        "name": str(metadata.get("name") or ""),
        "namespace": str(metadata.get("namespace") or ""),
    }
    return all(not target.get(key) or str(target[key]) == value for key, value in checks.items())


def apply_json_patch(document: dict, operations: list[dict]) -> None:
    for operation in operations:
        if operation.get("op") not in {"add", "replace"}:
            raise SystemExit(f"unsupported fake patch operation: {operation.get('op')}")
        tokens = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].split("/")[1:]]
        current = document
        for token in tokens[:-1]:
            current = current[token]
        current[tokens[-1]] = operation.get("value")


def render_post_kustomization(directory: Path) -> str:
    log_path = os.environ.get("FAKE_KUBECTL_POST_RENDER_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as stream:
            stream.write("post-render\n")

    config = yaml.safe_load((directory / "kustomization.yaml").read_text(encoding="utf-8")) or {}
    resources = [str(item).replace("\\", "/").removeprefix("./") for item in config.get("resources") or []]
    if "helm-rendered.yaml" not in resources:
        raise SystemExit("post-render kustomization is missing helm-rendered.yaml")

    documents = load_documents(directory / "helm-rendered.yaml")
    for entry in config.get("patches") or []:
        patch = yaml.safe_load((directory / entry["path"]).read_text(encoding="utf-8"))
        for document in documents:
            if matches(document, entry.get("target") or {}):
                apply_json_patch(document, patch)
    return "\n---\n".join(yaml.safe_dump(item, sort_keys=False).rstrip() for item in documents) + "\n"


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "kustomize":
        print("expected: fake_kubectl.py kustomize DIR", file=sys.stderr)
        return 2
    rendered = Path(sys.argv[2]) / "rendered.yaml"
    directory = Path(sys.argv[2])
    if (directory / "helm-rendered.yaml").is_file():
        sys.stdout.write(render_post_kustomization(directory))
        return 0

    if rendered.is_file():
        sys.stdout.write(rendered.read_text(encoding="utf-8"))
        return 0

    print(f"missing fixture: {rendered}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
