#!/usr/bin/env python3

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import tempfile

import yaml


def load_yaml(text):
    resources = []
    for item in yaml.safe_load_all(text):
        if item and item.get("kind") == "List":
            resources += item["items"]
        elif item:
            resources.append(item)
    return resources


def metadata(resource, key):
    return resource.get("metadata", {}).get(key, "")


def find(resources, kind, name, namespace):
    for resource in resources:
        if (
            resource.get("kind") == kind
            and metadata(resource, "name") == name
            and (metadata(resource, "namespace") or "default") == namespace
        ):
            return resource


def render_release(release, resources, repositories, values_directory):
    spec = release["spec"]
    chart = spec["chart"]["spec"]
    reference = chart["sourceRef"]
    release_namespace = metadata(release, "namespace") or "default"
    source_namespace = reference.get("namespace", release_namespace)
    source = find(resources, "HelmRepository", reference["name"], source_namespace)

    repo_name = reference["name"]
    if source:
        source_url = source["spec"]["url"].rstrip("/")
        for repo in repositories:
            if repo["url"].rstrip("/") == source_url:
                repo_name = repo["name"]

    values_directory.mkdir()
    values_options = ""
    for index, reference in enumerate(spec.get("valuesFrom", [])):
        kind = reference.get("kind", "ConfigMap")
        source = find(resources, kind, reference["name"], release_namespace)
        if source is None and reference.get("optional"):
            continue

        key = reference.get("valuesKey", "values.yaml")
        if kind == "Secret":
            value = source.get("stringData", {}).get(key)
            if value is None:
                value = base64.b64decode(source["data"][key]).decode()
        else:
            value = source["data"][key]

        if reference.get("targetPath"):
            value = yaml.safe_load(value)
            for part in reversed(reference["targetPath"].split(".")):
                value = {part: value}
            value = yaml.safe_dump(value)

        values_file = values_directory / f"values-{index}.yaml"
        values_file.write_text(str(value), encoding="utf-8")
        values_options += f' --values "{values_file}"'

    if spec.get("values"):
        values_file = values_directory / "values-inline.yaml"
        values_file.write_text(
            yaml.safe_dump(spec["values"], sort_keys=False), encoding="utf-8"
        )
        values_options += f' --values "{values_file}"'

    release_name = spec.get("releaseName", metadata(release, "name"))
    target_namespace = spec.get("targetNamespace", release_namespace)
    version = f' --version "{chart["version"]}"' if chart.get("version") else ""
    command = (
        f'helm template "{release_name}" "{repo_name}/{chart["chart"]}"'
        f' --namespace "{target_namespace}" --include-crds{values_options}{version}'
    )
    return os.popen(command).read().strip()


def post_render(manifest, directory):
    names = ("kustomization.yaml", "kustomization.yml", "Kustomization")
    source_file = next(directory / name for name in names if (directory / name).is_file())

    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        work = Path(temporary)
        shutil.copytree(directory, work, dirs_exist_ok=True)
        config_file = work / source_file.name
        config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        config["resources"] = ["helm-rendered.yaml"]
        config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        (work / "helm-rendered.yaml").write_text(manifest + "\n", encoding="utf-8")
        return os.popen(f'kubectl kustomize "{work}"').read()


parser = argparse.ArgumentParser()
parser.add_argument("kustomization", type=Path)
selection = parser.add_mutually_exclusive_group(required=True)
selection.add_argument("--name")
selection.add_argument("--all", action="store_true")
args = parser.parse_args()

directory = args.kustomization.parent if args.kustomization.is_file() else args.kustomization
resources = load_yaml(os.popen(f'kubectl kustomize "{directory}"').read())
releases = [resource for resource in resources if resource.get("kind") == "HelmRelease"]
if args.name:
    releases = [release for release in releases if metadata(release, "name") == args.name]

repositories = json.loads(os.popen("helm repo list -o json").read())
with tempfile.TemporaryDirectory() as temporary:
    rendered = [
        render_release(release, resources, repositories, Path(temporary) / str(index))
        for index, release in enumerate(releases)
    ]

print(post_render("\n---\n".join(rendered), directory), end="")
