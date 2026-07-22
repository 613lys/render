#!/usr/bin/env python3
import argparse
import difflib
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml


CATEGORY_RULES = [
    ("spring.datasource", "Database"), ("spring.sql", "Database"), ("database", "Database"),
    ("spring.security", "Security & Auth"), ("oidc", "Security & Auth"),
    ("client-ssl", "Security & Auth"), ("swiftconfirmation", "Security & Auth"),
    ("spring.kafka", "Kafka"), ("tedra", "Kafka"), ("carnel.component.kafka", "Kafka"),
    ("email", "Email & Notification"), ("msg", "Email & Notification"),
    ("logging", "Logging"), ("storage", "Storage"),
    ("route", "Business Routes"), ("flow", "Business Flow"),
    ("application", "Application Config"), ("app", "Application Config"), ("dm", "Application Config"),
    ("datamart", "Database"), ("cors", "Security & Auth"), ("carnel", "Camel"),
    ("server", "Server"), ("ccd", "External Services"), ("comstar", "External Services"),
    ("deriv-dash-url", "External Services"), ("gateway", "External Services"),
    ("sirius", "External Services"), ("traderepositoryservice", "External Services"),
    ("features", "External Services"), ("optimus", "External Services"),
    ("rice", "External Services"), ("upstream", "External Services"),
]
CATEGORY_RULES.sort(key=lambda item: len(item[0]), reverse=True)
ENV_ORDER = {"dev": 0, "qa": 1, "prod": 2, "pp": 3, "pr": 4, "bcp": 5}
CLUSTER_ORDER = {"shg": 0, "bjv": 1}
ENV_NAMES = r"dev|qa|prod|pp|pr|bcp"
AUTOMATION_ENV_PATTERN = r"(?:panda-automation-(?:dev|qa)|ops-automation-prod)"
SIMPLE_ENV_TOKEN_PATTERN = re.compile(
    rf"(?:c?shg|c?bjv)(?:-ms)?[-_]?(?:{ENV_NAMES})|(?<![A-Za-z])(?:{ENV_NAMES})(?![A-Za-z])",
    re.I,
)
ENV_TOKEN_PATTERN = re.compile(
    rf"{AUTOMATION_ENV_PATTERN}|{SIMPLE_ENV_TOKEN_PATTERN.pattern}",
    re.I,
)


def normalize_environment_tokens(value, replacement="<env>"):
    text = re.sub(AUTOMATION_ENV_PATTERN, f"automation{replacement}", str(value), flags=re.I)
    return SIMPLE_ENV_TOKEN_PATTERN.sub(replacement, text)


def environment_logical_value(value):
    """Remove optional environment suffixes/aliases for logical matching."""
    text = normalize_environment_tokens(value)
    text = re.sub(r"^<env>[-_]?", "", text, flags=re.I)
    text = re.sub(r"[-_]?<env>(?=$|[-_.])", "", text, flags=re.I)
    return text


class PrettyDumper(yaml.SafeDumper):
    """Keep embedded ConfigMap/config text readable instead of escaping newlines."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _represent_string(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


PrettyDumper.add_representer(str, _represent_string)


def split_stem(path: Path):
    stem = path.stem
    return stem.rsplit("__", 1) if "__" in stem else (stem, "unknown")


def filename_without_version(item):
    base_name, _ = split_stem(item["path"])
    return f"{base_name}{item['path'].suffix}"


def env_info(label: str):
    low = label.lower()
    env = next((x for x in ENV_ORDER if re.search(rf"(?:^|[-_]){x}(?:$|[-_])", low)), "other")
    cluster = "shg" if "shg" in low else ("bjv" if "bjv" in low else "other")
    return env, cluster


def env_sort(label: str):
    env, cluster = env_info(label)
    return (CLUSTER_ORDER.get(cluster, 9), ENV_ORDER.get(env, 9), label)


def env_display(label: str):
    env, cluster = env_info(label)
    location = "Shanghai" if cluster == "shg" else ("BJV" if cluster == "bjv" else cluster.upper())
    return f"{location} · {env.upper()}"


def normalize_name(value: str) -> str:
    value = str(value)
    suffix = Path(value).suffix
    stem = value[:-len(suffix)] if suffix else value
    # Remove Git/SHA-256 suffixes before environment normalization, then repeat
    # after it so both name-env-sha and name-sha-env conventions are supported.
    stem = re.sub(r"(?:-sha(?:1|256)?-?|-)(?=[0-9a-f]{7,64}$)[0-9a-f]{7,64}$", "", stem, flags=re.I)
    stem = re.sub(rf"-(?:{AUTOMATION_ENV_PATTERN})$", "", stem, flags=re.I)
    stem = re.sub(r"-(?:c?shg|c?bjv)(?:-ms)?-(?:dev|qa|prod|pp|pr|bcp)$", "", stem, flags=re.I)
    stem = re.sub(r"-(?:(?:c?shg|c?bjv)[-_]?)?(?:dev|qa|prod|pp|pr|bcp)[-_]\d+$", "", stem, flags=re.I)
    stem = re.sub(r"-(?:prod-bcp|prod-bj|dev-bcp|qa-bcp|dev|qa|prod|pp|pr|bcp)(?=-|$)", "", stem,
                  flags=re.I)
    stem = re.sub(r"-\d+(?:\.\d+){1,}(?:-(?:sha(?:1|256)-?)?[0-9a-f]{7,64})?$", "", stem, flags=re.I)
    stem = re.sub(r"(?:-sha(?:1|256)?-?|-)(?=[0-9a-f]{7,64}$)[0-9a-f]{7,64}$", "", stem, flags=re.I)
    return stem + suffix


def yaml_docs(text: str):
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def raw_resource_docs(text: str):
    """Split rendered multi-document YAML while retaining each workload's source text."""
    resources = []
    for part in re.split(r"(?m)^\s*---\s*$", text):
        raw = part.strip()
        if not raw:
            continue
        docs = yaml_docs(raw)
        if docs:
            resources.append((resource_key(normalize_configmap(docs[0])), raw + "\n"))
    return resources


def resource_key(doc: dict):
    meta = doc.get("metadata") or {}
    return f"{doc.get('kind', 'Unknown')}/{normalize_name(meta.get('name', 'unnamed'))}"


def normalize_configmap(doc):
    if doc.get("kind") == "ConfigMap" and isinstance(doc.get("data"), dict):
        doc = json.loads(json.dumps(doc))
        doc["data"] = {normalize_name(k): v for k, v in doc["data"].items()}
    return doc


def flatten(value, prefix=""):
    out = {}
    if isinstance(value, dict):
        for key, child in value.items():
            p = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten(child, p))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            out.update(flatten(child, f"{prefix}[{i}]"))
    else:
        out[prefix] = value
    return out


def semantic_fields_from_text(text):
    fields = {}
    docs = yaml_docs(text)
    for doc_index, doc in enumerate(docs):
        prefix = f"doc[{doc_index}]." if len(docs) > 1 else ""
        for path, value in flatten(doc).items():
            full_path = prefix + path
            if isinstance(value, str) and "\n" in value:
                try:
                    embedded = yaml.safe_load(value)
                except yaml.YAMLError:
                    embedded = None
                if isinstance(embedded, (dict, list)):
                    for nested_path, nested_value in flatten(embedded, full_path).items():
                        fields[nested_path] = nested_value
                    continue
            fields[full_path] = value
    if not docs:
        fields["raw"] = text
    return fields


def semantic_text(text):
    return "\n".join(f"{path}: {scalar_text(value)}"
                     for path, value in sorted(semantic_fields_from_text(text).items()))


def expand_embedded_yaml(node):
    if isinstance(node, dict):
        return {key: expand_embedded_yaml(value) for key, value in node.items()}
    if isinstance(node, list):
        return [expand_embedded_yaml(value) for value in node]
    if isinstance(node, str) and "\n" in node:
        try:
            parsed = yaml.safe_load(node)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return expand_embedded_yaml(parsed)
    return node


def list_item_identity(item):
    """Return the unique name path/value used to align unordered YAML list items."""
    if not isinstance(item, dict):
        return None
    if "name" in item and not isinstance(item["name"], (dict, list)):
        return (("name",), str(item["name"]))
    candidates = []

    def collect(node, path=()):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            child_path = path + (str(key),)
            if key == "name" and not isinstance(value, (dict, list)):
                candidates.append((child_path, str(value)))
            elif isinstance(value, dict):
                collect(value, child_path)

    collect(item)
    return candidates[0] if len(candidates) == 1 else None


def identity_aligned_list(left, right):
    """Align two lists by a unique name identity, or return None for index fallback."""
    combined = list(left) + list(right)
    identities = [list_item_identity(item) for item in combined]
    if not combined or any(identity is None for identity in identities):
        return None
    left_ids = [list_item_identity(item) for item in left]
    right_ids = [list_item_identity(item) for item in right]
    if len(set(left_ids)) != len(left_ids) or len(set(right_ids)) != len(right_ids):
        return None
    left_map = dict(zip(left_ids, left))
    right_map = dict(zip(right_ids, right))
    order = left_ids + [identity for identity in right_ids if identity not in left_map]
    return [(identity, left_map.get(identity, _OMIT), right_map.get(identity, _OMIT))
            for identity in order]


def without_identity(item, identity_path):
    """Copy a list item without its identity leaf so it can be shown as context once."""
    if not isinstance(item, dict):
        return item
    result = json.loads(json.dumps(item))
    node = result
    parents = []
    for key in identity_path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return result
        parents.append((node, key))
        node = node[key]
    if isinstance(node, dict):
        node.pop(identity_path[-1], None)
    for parent, key in reversed(parents):
        if parent.get(key) == {}:
            parent.pop(key)
    return result


def flatten_diff(value, prefix=""):
    """Flatten YAML using stable list identities so classification matches rendering."""
    out = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten_diff(child, path))
    elif isinstance(value, list):
        identities = [list_item_identity(item) for item in value]
        use_identities = (bool(value) and all(identity is not None for identity in identities)
                          and len(set(identities)) == len(identities))
        for index, child in enumerate(value):
            if use_identities:
                identity_path, identity_value = identities[index]
                token = ".".join(identity_path) + "=" + identity_value
                child_path = f"{prefix}[{token}]"
            else:
                child_path = f"{prefix}[{index}]"
            out.update(flatten_diff(child, child_path))
    else:
        out[prefix] = value
    return out


def prune_yaml(node, changed_paths, path=""):
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            child = prune_yaml(value, changed_paths, child_path)
            if child is not _OMIT:
                result[key] = child
        return result if result else _OMIT
    if isinstance(node, list):
        result = []
        for index, value in enumerate(node):
            child_path = f"{path}[{index}]"
            child = prune_yaml(value, changed_paths, child_path)
            if child is not _OMIT:
                if isinstance(value, dict) and isinstance(child, dict) and "name" in value:
                    child = {"name": value["name"], **child}
                result.append(child)
        return result if result else _OMIT
    return node if path in changed_paths else _OMIT


_OMIT = object()


def hierarchical_diff_texts(old, new):
    old_docs = yaml_docs(old)
    new_docs = yaml_docs(new)
    old_value = expand_embedded_yaml(old_docs[0]) if old_docs else {}
    new_value = expand_embedded_yaml(new_docs[0]) if new_docs else {}
    old_flat, new_flat = flatten(old_value), flatten(new_value)
    changed = {path for path in set(old_flat) | set(new_flat)
               if old_flat.get(path, _OMIT) != new_flat.get(path, _OMIT)}
    old_changed = prune_yaml(old_value, changed)
    new_changed = prune_yaml(new_value, changed)
    dump = lambda value: yaml.dump({} if value is _OMIT else value, Dumper=PrettyDumper,
                                   sort_keys=False, allow_unicode=True, width=120).rstrip()
    return dump(old_changed), dump(new_changed), dump(old_value), dump(new_value)


def hierarchical_diff_html(old, new, categories=None, show_all=False):
    old_docs, new_docs = yaml_docs(old), yaml_docs(new)
    left = expand_embedded_yaml(old_docs[0]) if old_docs else _OMIT
    right = expand_embedded_yaml(new_docs[0]) if new_docs else _OMIT
    rows = []

    categories = categories or {}

    def expected_line_html(text):
        pieces, cursor = [], 0
        for match in ENV_TOKEN_PATTERN.finditer(text):
            pieces.append(f'<span class="expected-common">{html.escape(text[cursor:match.start()])}</span>')
            pieces.append(f'<span class="expected-env-token">{html.escape(match.group(0))}</span>')
            cursor = match.end()
        pieces.append(f'<span class="expected-common">{html.escape(text[cursor:])}</span>')
        return "".join(pieces)

    def line(css, text, path=None):
        category = categories.get(path)
        if category is None and path:
            descendant_categories = {
                value for child_path, value in categories.items()
                if child_path.startswith(path + ".") or child_path.startswith(path + "[")
            }
            if len(descendant_categories) == 1:
                category = next(iter(descendant_categories))
        category = category or "diff-actual"
        category_class = f" diff-fragment {category}" if css in ("add", "del") else ""
        if css in ("add", "del"):
            marker = "+" if css == "add" else "-"
            marker_match = re.match(r"^(\s*)[+-]\s?(.*)$", text)
            yaml_text = (f"{marker_match.group(1)}{marker_match.group(2)}"
                         if marker_match else text)
            content = (expected_line_html(yaml_text)
                       if ENV_TOKEN_PATTERN.search(yaml_text) else html.escape(yaml_text))
            logical_path = environment_logical_value(path or "__root__")
            raw_signature = marker + yaml_text
            normalized_signature = environment_logical_value(raw_signature)
            rows.append(
                f'<span class="{css}{category_class}"'
                f' data-diff-path="{html.escape(path or "__root__", quote=True)}"'
                f' data-diff-key="{html.escape(logical_path, quote=True)}"'
                f' data-raw-signature="{html.escape(raw_signature, quote=True)}"'
                f' data-normalized-signature="{html.escape(normalized_signature, quote=True)}"'
                f' data-env-derived="{str(raw_signature != normalized_signature).lower()}">'
                f'<span class="diff-marker">{marker}</span>'
                f'<span class="diff-yaml">{content}</span></span>'
            )
        else:
            rows.append(f'<span class="{css}">{html.escape(text)}</span>')

    def scalar_line(prefix, value):
        return f"{prefix}{scalar_text(value)}"

    def emit_multiline_diff(a, b, indent, key, path):
        pad = " " * indent
        if key is not None:
            line("ctx", f"{pad}{key}: |-")
            indent += 2
        old_lines = [] if a is _OMIT else str(a).splitlines()
        new_lines = [] if b is _OMIT else str(b).splitlines()
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                if show_all:
                    for raw_line in old_lines[old_start:old_end]:
                        line("ctx", f"{' ' * indent}{raw_line}")
            if tag in ("delete", "replace"):
                for raw_line in old_lines[old_start:old_end]:
                    line("del", f"{' ' * indent}- {raw_line}", path)
            if tag in ("insert", "replace"):
                for raw_line in new_lines[new_start:new_end]:
                    line("add", f"{' ' * indent}+ {raw_line}", path)

    def emit_context(node, indent=0, key=None):
        pad = " " * indent
        if key is not None and isinstance(node, (dict, list)):
            line("ctx", f"{pad}{key}:")
            indent += 2
            pad = " " * indent
        if isinstance(node, dict):
            for child_key, child in node.items():
                if isinstance(child, (dict, list)):
                    emit_context(child, indent, child_key)
                else:
                    line("ctx", f"{' ' * indent}{child_key}: {scalar_text(child)}")
        elif isinstance(node, list):
            for child in node:
                if isinstance(child, dict) and "name" in child:
                    line("ctx", f"{pad}- name: {scalar_text(child['name'])}")
                    emit_context({k: v for k, v in child.items() if k != "name"}, indent + 2)
                elif isinstance(child, dict) and child:
                    items = list(child.items())
                    first_key, first_value = items[0]
                    if isinstance(first_value, (dict, list)):
                        line("ctx", f"{pad}- {first_key}:")
                        emit_context(first_value, indent + 4)
                    else:
                        line("ctx", f"{pad}- {first_key}: {scalar_text(first_value)}")
                    emit_context(dict(items[1:]), indent + 2)
                elif isinstance(child, (dict, list)):
                    line("ctx", f"{pad}-")
                    emit_context(child, indent + 2)
                else:
                    line("ctx", f"{pad}- {scalar_text(child)}")
        elif key is not None:
            line("ctx", f"{pad}{key}: {scalar_text(node)}")

    def emit_one_sided(node, css, indent=0, key=None, path=""):
        """Render an added/removed YAML subtree with +/- on every real YAML line."""
        marker = "+" if css == "add" else "-"
        pad = " " * indent
        if key is not None and isinstance(node, (dict, list)):
            line(css, f"{pad}{marker} {key}:", path)
            indent += 2
            pad = " " * indent
        if isinstance(node, dict):
            for child_key, child in node.items():
                child_path = f"{path}.{child_key}" if path else str(child_key)
                if isinstance(child, (dict, list)):
                    emit_one_sided(child, css, indent, child_key, child_path)
                else:
                    line(css, f"{' ' * indent}{marker} {child_key}: {scalar_text(child)}", child_path)
        elif isinstance(node, list):
            identities = [list_item_identity(child) for child in node]
            use_identities = (bool(node) and all(identity is not None for identity in identities)
                              and len(set(identities)) == len(identities))
            for index, child in enumerate(node):
                if use_identities:
                    identity_path, identity_value = identities[index]
                    token = ".".join(identity_path) + "=" + identity_value
                    child_path = f"{path}[{token}]"
                else:
                    child_path = f"{path}[{index}]"
                emit_list_item_one_sided(child, css, indent, child_path)
        elif key is not None:
            line(css, f"{pad}{marker} {key}: {scalar_text(node)}", path)

    def emit_list_identity(identity, indent):
        identity_path, identity_value = identity
        pad = " " * indent
        if len(identity_path) == 1:
            line("ctx", f"{pad}- {identity_path[0]}: {identity_value}")
            return
        line("ctx", f"{pad}- {identity_path[0]}:")
        for depth, key_part in enumerate(identity_path[1:-1], start=1):
            line("ctx", f"{' ' * (indent + depth * 2)}{key_part}:")
        line("ctx", f"{' ' * (indent + len(identity_path) * 2)}{identity_path[-1]}: {identity_value}")

    def emit_list_item_one_sided(item, css, indent, path):
        marker = "+" if css == "add" else "-"
        pad = " " * indent
        if isinstance(item, dict):
            items = list(item.items())
            if not items:
                line(css, f"{pad}{marker} - {{}}", path)
                return
            first_key, first_child = items[0]
            first_path = f"{path}.{first_key}"
            if isinstance(first_child, (dict, list)):
                line(css, f"{pad}{marker} - {first_key}:", first_path)
                emit_one_sided(first_child, css, indent + 4, path=first_path)
            else:
                line(css, f"{pad}{marker} - {first_key}: {scalar_text(first_child)}", first_path)
            for child_key, child in items[1:]:
                child_path = f"{path}.{child_key}"
                if isinstance(child, (dict, list)):
                    emit_one_sided(child, css, indent + 2, child_key, child_path)
                else:
                    line(css, f"{' ' * (indent + 2)}{marker} {child_key}: {scalar_text(child)}", child_path)
        else:
            line(css, f"{pad}{marker} - {scalar_text(item)}", path)

    def walk(a, b, indent=0, key=None, path=""):
        if a is not _OMIT and b is not _OMIT and a == b:
            if show_all:
                emit_context(a, indent, key)
            return
        if a is _OMIT and isinstance(b, (dict, list)):
            emit_one_sided(b, "add", indent, key, path)
            return
        if b is _OMIT and isinstance(a, (dict, list)):
            emit_one_sided(a, "del", indent, key, path)
            return
        pad = " " * indent
        a_container = isinstance(a, (dict, list))
        b_container = isinstance(b, (dict, list))
        if key is not None and (a_container or b_container):
            line("ctx", f"{pad}{key}:")
            indent += 2
            pad = " " * indent
        if isinstance(a, dict) or isinstance(b, dict):
            left_dict = a if isinstance(a, dict) else {}
            right_dict = b if isinstance(b, dict) else {}
            keys = list(left_dict)
            keys.extend(k for k in right_dict if k not in left_dict)
            for child_key in keys:
                child_path = f"{path}.{child_key}" if path else str(child_key)
                av = left_dict.get(child_key, _OMIT)
                bv = right_dict.get(child_key, _OMIT)
                if av is not _OMIT and bv is not _OMIT and av == bv:
                    if show_all:
                        emit_context(av, indent, child_key)
                    continue
                if isinstance(av, (dict, list)) or isinstance(bv, (dict, list)):
                    walk(av, bv, indent, child_key, child_path)
                elif ((isinstance(av, str) and "\n" in av)
                      or (isinstance(bv, str) and "\n" in bv)):
                    emit_multiline_diff(av, bv, indent, child_key, child_path)
                else:
                    if av is not _OMIT:
                        line("del", scalar_line(f"{' ' * indent}- {child_key}: ", av), child_path)
                    if bv is not _OMIT:
                        line("add", scalar_line(f"{' ' * indent}+ {child_key}: ", bv), child_path)
            return
        if isinstance(a, list) or isinstance(b, list):
            left_list = a if isinstance(a, list) else []
            right_list = b if isinstance(b, list) else []
            aligned = identity_aligned_list(left_list, right_list)
            if aligned is not None:
                for identity, av, bv in aligned:
                    identity_path, identity_value = identity
                    identity_token = ".".join(identity_path) + "=" + identity_value
                    child_path = f"{path}[{identity_token}]"
                    if av is _OMIT:
                        emit_list_item_one_sided(bv, "add", indent, child_path)
                    elif bv is _OMIT:
                        emit_list_item_one_sided(av, "del", indent, child_path)
                    elif av == bv:
                        if show_all:
                            emit_context(av, indent)
                    else:
                        emit_list_identity(identity, indent)
                        walk(without_identity(av, identity_path),
                             without_identity(bv, identity_path),
                             indent + 2, path=child_path)
                return
            for index in range(max(len(left_list), len(right_list))):
                child_path = f"{path}[{index}]"
                av = left_list[index] if index < len(left_list) else _OMIT
                bv = right_list[index] if index < len(right_list) else _OMIT
                if av is not _OMIT and bv is not _OMIT and av == bv:
                    if show_all:
                        if isinstance(av, dict) and "name" in av:
                            line("ctx", f"{' ' * indent}- name: {scalar_text(av['name'])}")
                            emit_context({k: v for k, v in av.items() if k != "name"}, indent + 2)
                        elif isinstance(av, dict):
                            emit_context([av], indent)
                        elif isinstance(av, (dict, list)):
                            line("ctx", f"{' ' * indent}-")
                            emit_context(av, indent + 2)
                        else:
                            line("ctx", f"{' ' * indent}- {scalar_text(av)}")
                    continue
                left_name = av.get("name") if isinstance(av, dict) else None
                right_name = bv.get("name") if isinstance(bv, dict) else None
                if left_name is not None and left_name == right_name:
                    line("ctx", f"{pad}- name: {left_name}")
                    walk({k: v for k, v in av.items() if k != "name"},
                         {k: v for k, v in bv.items() if k != "name"}, indent + 2,
                         path=child_path)
                elif isinstance(av, dict) and isinstance(bv, dict):
                    shared_first = next(
                        (candidate for candidate in av
                         if candidate in bv and av[candidate] == bv[candidate]),
                        None,
                    )
                    if shared_first is not None:
                        shared_value = av[shared_first]
                        if isinstance(shared_value, (dict, list)):
                            line("ctx", f"{pad}- {shared_first}:")
                            emit_context(shared_value, indent + 4)
                        else:
                            line("ctx", f"{pad}- {shared_first}: {scalar_text(shared_value)}")
                        walk({k: v for k, v in av.items() if k != shared_first},
                             {k: v for k, v in bv.items() if k != shared_first},
                             indent + 2, path=child_path)
                    else:
                        line("ctx", f"{pad}-")
                        walk(av, bv, indent + 2, path=child_path)
                else:
                    line("ctx", f"{pad}-")
                    walk(av, bv, indent + 2, path=child_path)
            return
        if ((isinstance(a, str) and "\n" in a)
                or (isinstance(b, str) and "\n" in b)):
            emit_multiline_diff(a, b, indent, key, path)
            return
        prefix = f"{pad}{key}: " if key is not None else pad
        if a is not _OMIT:
            line("del", scalar_line(prefix.replace(pad, pad + "- ", 1), a), path)
        if b is not _OMIT:
            line("add", scalar_line(prefix.replace(pad, pad + "+ ", 1), b), path)

    walk(left, right)
    return "<br>".join(rows) or '<span class="yaml-same">No release changes</span>'


def normalized_diff_signature(old, new):
    old_sem, new_sem = semantic_text(old), semantic_text(new)
    changed = list(difflib.unified_diff(old_sem.splitlines(), new_sem.splitlines(), n=0, lineterm=""))
    signature = "\n".join(line for line in changed if not line.startswith(("---", "+++", "@@")))
    signature = normalize_environment_tokens(signature)
    signature = re.sub(r"(?:sha(?:1|256)-?)?[0-9a-f]{7,64}", "<sha>", signature, flags=re.I)
    return signature


def environment_only_diff(old, new):
    left, right = semantic_fields_from_text(old), semantic_fields_from_text(new)
    differing = {path for path in set(left) | set(right) if left.get(path) != right.get(path)}
    if not differing:
        return False
    for path in differing:
        if path not in left or path not in right:
            return False
        a, b = str(left[path]), str(right[path])
        if normalize_name(a) != normalize_name(b) and environment_logical_value(a) != environment_logical_value(b):
            return False
    return True


def diff_events(old, new):
    old_docs, new_docs = yaml_docs(old), yaml_docs(new)
    left = flatten_diff(expand_embedded_yaml(old_docs[0])) if old_docs else {}
    right = flatten_diff(expand_embedded_yaml(new_docs[0])) if new_docs else {}
    return {path: (left.get(path, _OMIT), right.get(path, _OMIT))
            for path in set(left) | set(right)
            if left.get(path, _OMIT) != right.get(path, _OMIT)}


def container_paths_from_text(text):
    docs = yaml_docs(text)
    root = expand_embedded_yaml(docs[0]) if docs else {}
    paths = set()

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(child, (dict, list)):
                    paths.add(child_path)
                    walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                child_path = f"{path}[{index}]"
                if isinstance(child, (dict, list)):
                    paths.add(child_path)
                    walk(child, child_path)

    walk(root)
    return paths


def shared_one_sided_parent_categories(text_pairs, required):
    """Mark a parent green when all namespaces add or remove that YAML subtree."""
    counts = Counter()
    for old, new in text_pairs:
        old_paths = container_paths_from_text(old)
        new_paths = container_paths_from_text(new)
        for path in old_paths - new_paths:
            counts[(path, "removed")] += 1
        for path in new_paths - old_paths:
            counts[(path, "added")] += 1
    result = {}
    for (path, _direction), count in counts.items():
        if count == required:
            result[path] = "diff-all-namespaces"
    return result


def normalize_event_text(value):
    if value is _OMIT:
        return "<missing>"
    text = str(value)
    text = environment_logical_value(text)
    return re.sub(r"(?:sha(?:1|256)-?)?[0-9a-f]{7,64}", "<sha>", text, flags=re.I)


def event_signature(path, old_value, new_value):
    return (normalize_event_text(path), normalize_event_text(old_value),
            normalize_event_text(new_value))


def event_is_environment_only(old_value, new_value):
    if old_value is _OMIT or new_value is _OMIT:
        return False
    a, b = str(old_value), str(new_value)
    # Expected Diff currently handles environment-only names. SHA-bearing names
    # stay as regular diffs until their matching semantics are defined.
    sha_pattern = r"(?:sha(?:1|256)-?)?[0-9a-f]{7,64}"
    if re.search(sha_pattern, a, re.I) or re.search(sha_pattern, b, re.I):
        return False
    return a != b and environment_logical_value(a) == environment_logical_value(b)


def aggregate_environment_paths(text_pairs, required):
    """Find fields whose baseline or current side varies only by environment."""
    values_by_path = defaultdict(list)
    sha_pattern = re.compile(r"(?:sha(?:1|256)-?)?[0-9a-f]{7,64}", re.I)
    for old, new in text_pairs:
        for path, values in diff_events(old, new).items():
            values_by_path[path].append(values)

    def side_varies_as_environment(values):
        if len(values) != required or any(value is _OMIT for value in values):
            return False
        raw = [str(value) for value in values]
        return (len(set(raw)) > 1
                and any(ENV_TOKEN_PATTERN.search(value) for value in raw)
                and not any(sha_pattern.search(value) for value in raw)
                and len({environment_logical_value(value) for value in raw}) == 1)

    return {path for path, values in values_by_path.items()
            if side_varies_as_environment([old for old, _ in values])
            or side_varies_as_environment([new for _, new in values])}


def classify_diff_events(old, new, signature_counts=None, shared_required=0, unmatched=False,
                         aggregate_expected_paths=None):
    signature_counts = signature_counts or Counter()
    aggregate_expected_paths = aggregate_expected_paths or set()
    events = diff_events(old, new)
    expected_renames = set()
    if not unmatched:
        removed = [(path, values[0]) for path, values in events.items()
                   if values[0] is not _OMIT and values[1] is _OMIT]
        added = [(path, values[1]) for path, values in events.items()
                 if values[0] is _OMIT and values[1] is not _OMIT]
        for old_path, old_value in removed:
            for new_path, new_value in added:
                if (environment_logical_value(old_path) == environment_logical_value(new_path)
                        and environment_logical_value(old_value) == environment_logical_value(new_value)):
                    expected_renames.update((old_path, new_path))
    result = {}
    for path, (old_value, new_value) in events.items():
        signature = event_signature(path, old_value, new_value)
        environment_candidate = (path in expected_renames
                                 or path in aggregate_expected_paths
                                 or event_is_environment_only(old_value, new_value))
        if (environment_candidate and shared_required > 0
                and signature_counts[signature] == shared_required):
            result[path] = "diff-expected-env"
        elif (shared_required > 0
              and signature_counts[signature] == shared_required):
            result[path] = "diff-all-namespaces"
        else:
            result[path] = "diff-actual"
    return result


def differences(values_by_env):
    paths = set()
    flattened = {}
    for env, value in values_by_env.items():
        flattened[env] = flatten(value)
        paths.update(flattened[env])
    changed = set()
    for path in paths:
        vals = [flattened[e].get(path, object()) for e in values_by_env]
        if any(v != vals[0] for v in vals[1:]):
            changed.add(path)
    return changed


def category(path: str):
    low = path.lower()
    for prefix, name in CATEGORY_RULES:
        if low == prefix or low.startswith(prefix + "."):
            return name
    return "General"


def categories_in(value):
    names = {category(path) for path in flatten(value)}
    return sorted(names, key=lambda x: (x == "General", x))


def render_yaml(value, changed, categories=False):
    rows = []

    def is_changed(path, include_children=False):
        if path in changed:
            return True
        return include_children and any(p.startswith(path + ".") or p.startswith(path + "[")
                                        for p in changed)

    def scalar(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return str(value).lower()
        dumped = yaml.safe_dump(value, default_flow_style=True,
                                allow_unicode=True, width=120).strip()
        return re.sub(r"\n\.\.\.$", "", dumped)

    def add(line, path, include_children=False):
        cls = "yaml-diff" if is_changed(path, include_children) else "yaml-same"
        rows.append(f'<span class="{cls}" data-yaml-path="{html.escape(path, quote=True)}">{html.escape(line)}</span>')

    def walk(node, path="", indent=0):
        pad = " " * indent
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(child, (dict, list)):
                    add(f"{pad}{key}:", child_path, include_children=True)
                    walk(child, child_path, indent + 2)
                elif isinstance(child, str) and "\n" in child:
                    add(f"{pad}{key}: |", child_path)
                    for text_line in child.rstrip("\n").splitlines():
                        add(f"{pad}  {text_line}", child_path)
                else:
                    add(f"{pad}{key}: {scalar(child)}", child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                child_path = f"{path}[{index}]"
                if isinstance(child, dict):
                    items = list(child.items())
                    if not items:
                        add(f"{pad}- {{}}", child_path)
                        continue
                    first_key, first_value = items[0]
                    first_path = f"{child_path}.{first_key}"
                    if isinstance(first_value, (dict, list)):
                        add(f"{pad}- {first_key}:", first_path, include_children=True)
                        walk(first_value, first_path, indent + 4)
                    elif isinstance(first_value, str) and "\n" in first_value:
                        add(f"{pad}- {first_key}: |", first_path)
                        for text_line in first_value.rstrip("\n").splitlines():
                            add(f"{pad}    {text_line}", first_path)
                    else:
                        add(f"{pad}- {first_key}: {scalar(first_value)}", first_path)
                    if len(items) > 1:
                        walk(dict(items[1:]), child_path, indent + 2)
                elif isinstance(child, list):
                    add(f"{pad}-", child_path, include_children=True)
                    walk(child, child_path, indent + 2)
                else:
                    add(f"{pad}- {scalar(child)}", child_path)
        else:
            add(f"{pad}{scalar(node)}", path)

    walk(value)
    return "<br>".join(rows)


def config_logical_key(item):
    return f'{item["config_dir"] or "root"}/{normalize_name(item["name"])}{item["path"].suffix}'


def scalar_text(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return yaml.dump(value, Dumper=PrettyDumper, sort_keys=False,
                         allow_unicode=True, width=80).strip()
    return str(value)


def app_semantic_matrix(items, title):
    envs = sorted({x["env"] for x in items}, key=env_sort)
    parsed = {}
    for item in items:
        docs = yaml_docs(item["text"])
        value = docs[0] if docs else {"raw": item["text"]}
        parsed[item["env"]] = (item, value, flatten(value))
    paths = sorted({p for _, _, fields in parsed.values() for p in fields},
                   key=lambda p: (category(p), p))
    head = []
    for env in envs:
        item = parsed[env][0]
        head.append(f'<th data-env="{html.escape(env, quote=True)}"><b>{html.escape(env)}</b>'
                    f'<small class="file-head">{html.escape(filename_without_version(item))}</small></th>')
    rows, current_category = [], None
    for path in paths:
        group = category(path)
        if group != current_category:
            rows.append(f'<tr class="category-row"><th colspan="{len(envs)+1}">{html.escape(group)}</th></tr>')
            current_category = group
        marker = object()
        values = [parsed[e][2].get(path, marker) for e in envs]
        changed = any(v != values[0] for v in values[1:])
        cells = []
        for env, value in zip(envs, values):
            if value is marker:
                body = '<span class="missing">MISSING</span>'
            else:
                cls = "field-diff" if changed else "field-same"
                body = f'<span class="{cls}">{html.escape(scalar_text(value))}</span>'
            cells.append(f'<td class="field-value" data-env="{html.escape(env, quote=True)}"><div class="collapse-box field-collapse">'
                         f'<div class="collapsible-content">{body}</div></div></td>')
        rows.append(f'<tr class="{"diff-row" if changed else "same-row"}" data-field-path="{html.escape(path, quote=True)}"><th class="field-path">{html.escape(path)}</th>{"".join(cells)}</tr>')
    raw_cells = []
    for env in envs:
        item, value, _ = parsed[env]
        raw_cells.append(f'<td data-env="{html.escape(env, quote=True)}"><details><summary>View original YAML</summary>'
                         f'<div class="orig-file">{html.escape(item["path"].name)}</div>'
                         f'<div class="collapse-box"><pre class="collapsible-content">{render_yaml(value, set())}</pre>'
                         f'</div></details></td>')
    rows.append(f'<tr class="raw-yaml-row"><th class="field-path">Original YAML</th>{"".join(raw_cells)}</tr>')
    return (f'<div class="table-wrap">'
            f'<table class="matrix field-matrix env-matrix"><thead><tr><th>Resource</th>{"".join(head)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def app_env_config_tabs(items):
    grouped = defaultdict(list)
    for item in items:
        grouped[config_logical_key(item)].append(item)
    sections = [(key, app_semantic_matrix(group, key)) for key, group in sorted(grouped.items())]
    return tabs(sections, extra_class="config-tabs")


def app_release_config_tabs(current, baseline):
    grouped_current, grouped_baseline = defaultdict(list), defaultdict(list)
    for item in current:
        grouped_current[config_logical_key(item)].append(item)
    for item in baseline:
        grouped_baseline[config_logical_key(item)].append(item)
    keys = sorted(set(grouped_current) | set(grouped_baseline))
    sections = [(key, release_matrix(grouped_current.get(key, []),
                                     grouped_baseline.get(key, []), key,
                                     kind="app_config"))
                for key in keys]
    return tabs(sections, extra_class="config-tabs")


def scan(root: Path, version_dir: str, kind: str):
    base = root / version_dir / kind
    items = []
    if not base.exists():
        return items
    for path in sorted(base.rglob("*.yaml")):
        rel = path.relative_to(base)
        parts = rel.parts
        env_label = parts[0]
        name, version = split_stem(path)
        module = parts[1] if kind == "app_config" and len(parts) > 2 else name
        config_dir = "/".join(parts[2:-1]) if kind == "app_config" else ""
        items.append({"path": path, "env": env_label, "name": name, "version": version,
                      "module": module, "config_dir": config_dir,
                      "text": path.read_text(encoding="utf-8")})
    return items


def env_matrix(items, kind, title):
    envs = sorted({x["env"] for x in items}, key=env_sort)
    if not envs:
        return ""
    filenames = {}
    if kind in ("helm", "ns"):
        matrix = defaultdict(dict)
        for item in items:
            for doc in yaml_docs(item["text"]):
                doc = normalize_configmap(doc)
                matrix[resource_key(doc)][item["env"]] = doc
    else:
        matrix = defaultdict(dict)
        for item in items:
            key = f'{item["config_dir"] or "root"}/{normalize_name(item["name"])}'
            docs = yaml_docs(item["text"])
            matrix[key][item["env"]] = docs[0] if docs else {"raw": item["text"]}
            filenames[(key, item["env"])] = item["path"].name
    head = "".join(f'<th data-env="{html.escape(e, quote=True)}"><b>{html.escape(e)}</b></th>' for e in envs)
    rows = []
    for key, values in sorted(matrix.items()):
        changed = differences({e: values.get(e, {"__missing__": True}) for e in envs})
        cells = []
        for env in envs:
            if env not in values:
                body = '<span class="missing">MISSING IN THIS ENVIRONMENT</span>'
            else:
                body = render_yaml(values[env], changed, categories=False)
            file_label = ""
            if kind == "app_config" and (key, env) in filenames:
                file_label = f'<div class="orig-file">Config file · {html.escape(filenames[(key, env)])}</div>'
            cells.append(f'<td data-env="{html.escape(env, quote=True)}">{file_label}<div class="collapse-box">'
                         f'<pre class="collapsible-content env-resource-yaml">{body}</pre></div></td>')
        row_definition = html.escape(key)
        if kind == "app_config":
            all_categories = set()
            for value in values.values():
                all_categories.update(categories_in(value))
            badges = "".join(f'<span class="row-category">{html.escape(name)}</span>'
                             for name in sorted(all_categories, key=lambda x: (x == "General", x)))
            row_definition += f'<div class="config-definition"><b>Application-level categories</b>{badges}</div>'
        rows.append(f'<tr class="{"diff-row" if changed else "same-row"}"><th class="row-title">{row_definition}</th>{"".join(cells)}</tr>')
    first_column = "Resource"
    return f"""
<div class="table-wrap"><table class="matrix env-matrix"><thead><tr><th>{html.escape(first_column)}</th>{head}</tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def unified(old, new, old_name, new_name, show_all=False):
    if show_all and old == new:
        content = "<br>".join(f'<span class="ctx">{html.escape(line)}</span>'
                              for line in new.splitlines())
        return content
    context = max(len(old.splitlines()), len(new.splitlines())) if show_all else 0
    # Zero context keeps release reconciliation focused strictly on changed lines.
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=old_name,
                                 tofile=new_name, lineterm="", n=context)
    out = []
    for line in lines:
        cls = "ctx"
        if line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        elif line.startswith("@@"):
            cls = "hunk"
        out.append(f'<span class="{cls}">{html.escape(line)}</span>')
    return "<br>".join(out) or '<span class="yaml-same">No release changes</span>'


def release_matrix(current, baseline, title, kind="generic"):
    envs = sorted({x["env"] for x in current + baseline}, key=env_sort)
    def group(items):
        out = defaultdict(list)
        for x in items:
            if kind == "ns":
                logical = "gitops-customization.yaml"
            elif kind == "app_config":
                logical = config_logical_key(x)
            else:
                logical = f'{normalize_name(x["name"])}{x["path"].suffix}'
            out[(x["env"], logical)].append(x)
        return out
    cg, bg = group(current), group(baseline)
    logicals = sorted({k[1] for k in cg} | {k[1] for k in bg})
    if kind == "app_config":
        head_cells = []
        for env in envs:
            item = next((x for x in current if x["env"] == env), None)
            item = item or next((x for x in baseline if x["env"] == env), None)
            file_name = filename_without_version(item) if item else "missing"
            head_cells.append(f'<th data-env="{html.escape(env, quote=True)}"><b>{html.escape(env)}</b>'
                              f'<small class="file-head">{html.escape(file_name)}</small></th>')
        head = "".join(head_cells)
    else:
        head = "".join(f'<th data-env="{html.escape(e, quote=True)}">{html.escape(e)}</th>' for e in envs)
    rows = []
    for logical in logicals:
        signatures = Counter()
        text_pairs = []
        for env in envs:
            c = cg.get((env, logical), [])
            b = bg.get((env, logical), [])
            if c or b:
                old_text = b[0]["text"] if b else ""
                new_text = c[0]["text"] if c else ""
                text_pairs.append((old_text, new_text))
                for path, (old_value, new_value) in diff_events(old_text, new_text).items():
                    signatures[event_signature(path, old_value, new_value)] += 1
        aggregate_expected = aggregate_environment_paths(text_pairs, len(envs))
        parent_categories = shared_one_sided_parent_categories(text_pairs, len(envs))
        cells = []
        for env in envs:
            c = cg.get((env, logical), [])
            b = bg.get((env, logical), [])
            cur = c[0] if c else None
            base = b[0] if b else None
            old_text, new_text = base["text"] if base else "", cur["text"] if cur else ""
            old_compare, new_compare, old_full, new_full = hierarchical_diff_texts(old_text, new_text)
            old_name = f'baseline/{base["path"].name if base else "missing"}'
            new_name = f'current/{cur["path"].name if cur else "missing"}'
            categories = classify_diff_events(old_text, new_text, signatures, len(envs),
                                              unmatched=(base is None or cur is None),
                                              aggregate_expected_paths=aggregate_expected)
            categories.update(parent_categories)
            compact_diff = hierarchical_diff_html(old_text, new_text, categories)
            full_diff = hierarchical_diff_html(old_text, new_text, categories, show_all=True)
            cells.append(f'<td data-env="{html.escape(env, quote=True)}"><pre class="diff-compact">{compact_diff}</pre>'
                         f'<pre class="diff-full hidden">{full_diff}</pre></td>')
        rows.append(f'<tr><th class="row-title">{html.escape(logical)}</th>{"".join(cells)}</tr>')
    return (f'<div class="toolbar release-toolbar"><button class="release-view-toggle" '
            f'data-show-all="false">Show all lines</button>'
            f'<span>Only changed lines are currently shown</span></div>'
            f'{diff_legend()}'
            f'<div class="table-wrap"><table class="matrix release"><thead><tr><th>Resource</th>'
            f'{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def resource_release_matrix(current, baseline, title, raw_env_headers=False):
    """Release comparison aligned by Kubernetes workload rather than whole files."""
    envs = sorted({x["env"] for x in current + baseline}, key=env_sort)

    def collect(items):
        by_env = defaultdict(dict)
        for item in items:
            for key, raw in raw_resource_docs(item["text"]):
                by_env[item["env"]][key] = raw
        return by_env

    cg, bg = collect(current), collect(baseline)
    workloads = sorted({key for resources in cg.values() for key in resources} |
                       {key for resources in bg.values() for key in resources})
    head = "".join(f'<th data-env="{html.escape(env, quote=True)}"><b>{html.escape(env)}</b></th>' for env in envs)
    rows = []
    for workload in workloads:
        signatures = Counter()
        text_pairs = []
        for env in envs:
            old = bg.get(env, {}).get(workload)
            new = cg.get(env, {}).get(workload)
            if old is not None or new is not None:
                old_text, new_text = old or "", new or ""
                text_pairs.append((old_text, new_text))
                for path, (old_value, new_value) in diff_events(old_text, new_text).items():
                    signatures[event_signature(path, old_value, new_value)] += 1
        aggregate_expected = aggregate_environment_paths(text_pairs, len(envs))
        parent_categories = shared_one_sided_parent_categories(text_pairs, len(envs))
        cells = []
        for env in envs:
            old = bg.get(env, {}).get(workload)
            new = cg.get(env, {}).get(workload)
            if old is None and new is None:
                cells.append(f'<td class="absent-workload" data-env="{html.escape(env, quote=True)}"></td>')
                continue
            if old is None:
                status = '<div class="workload-status added-status">Added in current</div>'
            elif new is None:
                status = '<div class="workload-status removed-status">Removed from current</div>'
            else:
                status = ""
            old_compare, new_compare, old_full, new_full = hierarchical_diff_texts(old or "", new or "")
            categories = classify_diff_events(old or "", new or "", signatures, len(envs),
                                              unmatched=(old is None or new is None),
                                              aggregate_expected_paths=aggregate_expected)
            categories.update(parent_categories)
            compact = hierarchical_diff_html(old or "", new or "", categories)
            full = hierarchical_diff_html(old or "", new or "", categories, show_all=True)
            cells.append(f'<td data-env="{html.escape(env, quote=True)}">{status}'
                         f'<div class="collapse-box diff-compact"><pre class="collapsible-content">{compact}</pre></div>'
                         f'<div class="collapse-box diff-full hidden"><pre class="collapsible-content">{full}</pre></div></td>')
        rows.append(f'<tr><th class="row-title workload-title">{html.escape(workload)}</th>'
                    f'{"".join(cells)}</tr>')
    return (f'<div class="toolbar release-toolbar"><button class="release-view-toggle" '
            f'data-show-all="false">Show all lines</button>'
            f'<span>Workloads are matched by kind + normalized metadata.name</span></div>'
            f'{diff_legend()}'
            f'<div class="table-wrap"><table class="matrix release workload-release">'
            f'<thead><tr><th>Resource</th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def diff_legend():
    return ('<div class="diff-legend">'
            '<span class="legend-all">Diff in all namespaces</span>'
            '<span class="legend-env">Expected Diff · environment suffix only</span>'
            '<span class="legend-actual">Diff</span>'
            '</div>')


def tabs(sections, extra_class=""):
    buttons, bodies = [], []
    for i, section in enumerate(sections):
        label, body = section[0], section[1]
        status = section[2] if len(section) > 2 else None
        release_attr = ' data-release="true"' if status is not None else ""
        active = " active" if i == 0 else ""
        badge = ""
        if status is not None:
            label_text, cls = release_status_meta(status)
            badge = f'<span class="tab-diff-badge {cls}">{label_text}</span>'
        buttons.append(f'<button class="tab{active}" data-index="{i}"{release_attr}>'
                       f'{html.escape(label)}{badge}</button>')
        bodies.append(f'<div class="tab-pane{active}" data-index="{i}"{release_attr}>{body}</div>')
    return f'<div class="tab-group {extra_class}"><div class="tabs">{"".join(buttons)}</div>{"".join(bodies)}</div>'


def version_bar(current_items, baseline_items):
    current_versions = sorted({x["version"] for x in current_items}) or ["missing"]
    baseline_versions = sorted({x["version"] for x in baseline_items}) or ["missing"]
    return (f'<div class="release-version-bar">'
            f'<div><span>Prod Version</span><b>{html.escape(", ".join(baseline_versions))}</b></div>'
            f'<div class="version-arrow">→</div>'
            f'<div><span>Current version</span><b>{html.escape(", ".join(current_versions))}</b></div>'
            f'</div>')


def release_changed(current_items, baseline_items, kind):
    def snapshot(items):
        result = {}
        for item in items:
            if kind in ("helm", "ns"):
                for resource, raw in raw_resource_docs(item["text"]):
                    result[(item["env"], resource)] = raw
            else:
                result[(item["env"], config_logical_key(item))] = item["text"]
        return result
    return snapshot(current_items) != snapshot(baseline_items)


def release_status(changed, rendered_release):
    if not changed:
        return "none"
    if "diff-actual" in rendered_release:
        return "diff"
    return "expected"


def release_status_meta(status):
    return {
        "none": ("No Diff", "no-diff"),
        "expected": ("Expected", "expected-diff"),
        "diff": ("Diff", "has-diff"),
    }[status]


def combined_release_status(*statuses):
    if "diff" in statuses:
        return "diff"
    if "expected" in statuses:
        return "expected"
    return "none"


def nav_item(cid, label, status):
    status_text, cls = release_status_meta(status)
    return (f'<a href="#{cid}" data-target="{cid}"><span>{html.escape(label)}</span>'
            f'<span class="diff-badge {cls}">{status_text}</span></a>')


def build(input_dir: Path):
    current = {k: scan(input_dir, "current", k) for k in ("helm", "app_config", "ns")}
    baseline = {k: scan(input_dir, "baseline", k) for k in ("helm", "app_config", "ns")}
    modules = sorted({x["module"] for x in (current["app_config"] + baseline["app_config"]
                                             + current["helm"] + baseline["helm"])})
    cards, nav = [], ['<div class="nav-heading">Modules</div>']
    for module in modules:
        cid = "module-" + re.sub(r"\W+", "-", module)
        cc = [x for x in current["app_config"] if x["module"] == module]
        bb = [x for x in baseline["app_config"] if x["module"] == module]
        # Mock convention: Helm chart and module share their name.
        ch = [x for x in current["helm"] if x["name"] == module]
        bh = [x for x in baseline["helm"] if x["name"] == module]
        release_sections, env_sections, statuses = [], [], []
        if ch or bh:
            helm_release = resource_release_matrix(ch, bh, "Workload (kind / metadata.name)")
            helm_status = release_status(release_changed(ch, bh, "helm"), helm_release)
            statuses.append(helm_status)
            release_sections.append(
                ("RELEASE-DIFF · Helm", version_bar(ch, bh) + helm_release, helm_status)
            )
            env_sections.append(
                ("ENV-DIFF · Helm", version_bar(ch, bh) + env_matrix(ch, "helm", "Workload / resource"))
            )
        if cc or bb:
            app_release = app_release_config_tabs(cc, bb)
            app_status = release_status(release_changed(cc, bb, "app_config"), app_release)
            statuses.append(app_status)
            release_sections.append(
                ("RELEASE-DIFF · App Config", version_bar(cc, bb) + app_release, app_status)
            )
            env_sections.append(
                ("ENV-DIFF · App Config", version_bar(cc, bb) + app_env_config_tabs(cc))
            )
        sections = release_sections + env_sections
        nav.append(nav_item(cid, module, combined_release_status(*statuses)))
        cards.append(f'<section id="{cid}"><h2>Application · {html.escape(module)}</h2>'
                     f'{tabs(sections)}</section>')

    nav.append('<div class="nav-heading">CKS NAMESPACE CONFIG</div>')
    for is_msms, label in ((False, "MSBIC CKS Namespace Config"),
                           (True, "MSMS CKS Namespace Config")):
        cn = [x for x in current["ns"] if ("-ms-" in x["env"].lower()) == is_msms]
        bn = [x for x in baseline["ns"] if ("-ms-" in x["env"].lower()) == is_msms]
        cid = "msms-cks-ns" if is_msms else "msbic-cks-ns"
        ns_release = resource_release_matrix(cn, bn, "GitOps workload (kind / metadata.name)", raw_env_headers=True)
        ns_status = release_status(release_changed(cn, bn, "ns"), ns_release)
        nav.append(nav_item(cid, label, ns_status))
        cards.append(f'<section id="{cid}"><h2>{label}</h2>{version_bar(cn, bn)}'
                     f'{tabs([("RELEASE-DIFF", ns_release, ns_status), ("ENV-DIFF", env_matrix(cn, "ns", "Namespace resource"))])}</section>')

    envs = sorted({x["env"] for kind in current for x in current[kind]} |
                  {x["env"] for kind in baseline for x in baseline[kind]}, key=env_sort)
    def filter_group(title, group_envs):
        options = "".join(
            f'<label><input type="checkbox" class="namespace-filter" value="{html.escape(env, quote=True)}"'
            f'{"" if (env.startswith("cshg") and env.endswith("dev")) else " checked"}><span>{html.escape(env)}</span></label>'
            for env in group_envs
        )
        return (f'<div class="namespace-filter-group"><div class="namespace-filter-heading">'
                f'{html.escape(title)}</div>{options}</div>')

    msbic_envs = [env for env in envs if "-ms-" not in env.lower()]
    msms_envs = [env for env in envs if "-ms-" in env.lower()]
    filters = (filter_group("MSBIC CKS NAMESPACE", msbic_envs) +
               filter_group("MSMS CKS NAMESPACE", msms_envs))
    data = {"nav": "".join(nav), "content": "".join(cards), "filters": filters}
    return (TEMPLATE.replace("__NAV__", data["nav"])
            .replace("__CONTENT__", data["content"])
            .replace("__FILTER__", data["filters"]))


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reconciliation Report</title><style>
:root{--bg:#f4f7fb;--panel:#fff;--line:#d9e2ef;--text:#1f2a3d;--muted:#66758c;--cyan:#087f8c;--red:#c9364f;--green:#177245;--yellow:#9b6300}
*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Segoe UI,sans-serif}
header{position:fixed;inset:0 0 auto 0;height:56px;z-index:5;background:#fffffff2;border-bottom:1px solid var(--line);padding:9px 18px;box-shadow:0 2px 10px #25385810;display:flex;align-items:center;gap:20px}
h1{margin:0;font-size:18px;white-space:nowrap}.namespace-picker{position:relative;margin-left:auto}.namespace-picker-toggle{display:flex;align-items:center;gap:7px;min-width:210px;padding:8px 12px;border:1px solid #cbd9e8;border-radius:7px;background:#edf4fb;color:#2b5873;cursor:pointer;font-size:12px}.namespace-picker-toggle:hover{background:#e4f0f8}.namespace-picker-icon{color:#07869a;font-size:13px}.namespace-picker-count{margin-left:auto;padding:1px 8px;border-radius:10px;background:#087f8c;color:#fff;font-weight:800}.namespace-picker-caret{margin-left:2px;color:#71849b;font-size:9px}.namespace-picker-menu{position:absolute;top:calc(100% + 6px);right:0;z-index:20;width:350px;max-height:calc(100vh - 78px);overflow:auto;padding:10px;background:#fff;border:1px solid #cad8e8;border-radius:8px;box-shadow:0 8px 22px #263b5228}.namespace-picker-actions{display:flex;gap:10px;padding-bottom:9px;border-bottom:1px solid #dce5ef}.namespace-picker-actions button{padding:5px 10px;border:1px solid #d1ddeb;border-radius:5px;background:#edf3f9;color:#536a82;cursor:pointer}.namespace-picker-actions button:hover{background:#e2edf6}.namespace-filter-group{padding-top:9px}.namespace-filter-heading{margin-bottom:4px;padding:0 8px 4px;border-bottom:1px solid #e2e9f1;color:#34465d;font-size:10px;font-weight:900;letter-spacing:.04em}.namespace-filter-group label{display:flex;align-items:center;gap:8px;padding:4px 9px;color:#34445d;font:11px/1.25 Consolas,"Cascadia Mono",monospace;cursor:pointer}.namespace-filter-group label:hover{background:#edf6fb;border-radius:4px}.namespace-filter-group input{margin:0;accent-color:#087f8c}.namespace-filter-group span{overflow-wrap:anywhere}aside{position:fixed;top:56px;bottom:0;width:210px;padding:14px;border-right:1px solid var(--line);background:#fff;overflow:auto}
aside a{display:flex;align-items:center;justify-content:space-between;gap:7px;color:var(--muted);padding:8px 9px;border-radius:6px;text-decoration:none;border-left:3px solid transparent;font-size:12px}aside a:hover{background:#eaf3fb;color:#174a68}aside a.active{background:#dceef7;color:#0b5f7b;border-left-color:#087f8c;font-weight:700}.diff-badge{flex:none;padding:2px 5px;border-radius:9px;font-size:8px;font-weight:800;letter-spacing:.03em}.diff-badge.has-diff{color:#a62239;background:#ffe1e6}.diff-badge.expected-diff{color:#086b9c;background:#d9effa}.diff-badge.no-diff{color:#17633d;background:#dcf5e6}
.nav-heading{margin:14px 6px 6px;padding-bottom:6px;border-bottom:1px solid #dce5ef;color:#32445d;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.nav-heading:first-child{margin-top:0}
main{position:fixed;top:56px;right:0;bottom:0;left:210px;padding:14px;overflow:hidden}main>section{display:none;height:100%;min-height:0;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}main>section.active-view{display:flex;flex-direction:column}
h2{flex:none;font-size:15px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line)}
.tab-group{display:flex;flex:1;min-height:0;flex-direction:column}.tabs{display:flex;flex:none;gap:3px;padding:7px 9px 0;overflow-x:auto}.tab{border:1px solid #dce5ef;border-bottom:0;background:#edf2f8;color:var(--muted);padding:7px 9px;border-radius:6px 6px 0 0;cursor:pointer;white-space:nowrap;font-size:11px}.tab.active{background:#fff;color:#174a68;border-top:2px solid var(--cyan)}
.tab-diff-badge{display:inline-block;margin-left:6px;padding:1px 4px;border-radius:8px;font-size:8px;font-weight:800}.tab-diff-badge.has-diff{color:#a62239;background:#ffe1e6}.tab-diff-badge.expected-diff{color:#086b9c;background:#d9effa}.tab-diff-badge.no-diff{color:#17633d;background:#dcf5e6}
.tab-pane{display:none;padding:8px}.tab-pane.active{display:flex;flex:1;min-height:0;flex-direction:column;overflow:hidden}.toolbar{flex:none;padding:5px;color:var(--muted)}.table-wrap{flex:1;min-height:0;overflow:auto;max-height:none;border:1px solid var(--line);border-radius:7px}
.config-tabs{border:1px solid #dce5ef;border-radius:8px;background:#f8fafc}.config-tabs>.tabs{padding-top:8px}.config-tabs>.tabs>.tab{font-family:Consolas,monospace;padding:8px 11px}.config-tabs>.tab-pane{padding:8px}
table{border-collapse:separate;border-spacing:0;min-width:100%;table-layout:fixed}th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line);vertical-align:top}
thead th{position:sticky;top:0;background:#eaf1f8;z-index:3;padding:10px;min-width:350px}.matrix thead th:first-child,.row-title{min-width:190px;width:190px;position:sticky;left:0;z-index:2;background:#f1f5f9}
th small{display:block;color:var(--yellow);margin-top:5px;font-weight:400}td{background:#fff}td pre{margin:4px 6px;padding:6px 8px;white-space:pre-wrap;word-break:break-word;font:11px/1.28 Consolas,"Cascadia Mono",monospace;background:#f8fafc;border:1px solid #e5ebf2;border-radius:6px;tab-size:2}
.row-title{padding:9px;text-align:left;color:var(--cyan)}.yaml-diff{display:inline;color:#c12640;font-weight:700}.yaml-same{display:inline;color:#34445d}
.missing{color:var(--red);font-weight:bold}.category{display:inline-block;color:#08111f;background:#87c7ff;border-radius:3px;padding:0 4px;font:10px Segoe UI,sans-serif}
.config-definition{margin-top:10px;padding-top:8px;border-top:1px solid #d6e0eb;color:#52637b;font-size:11px}.config-definition b{display:block;margin-bottom:5px}.row-category{display:inline-block;margin:2px 3px 2px 0;padding:2px 5px;border-radius:10px;background:#dceef5;color:#24566b;font-weight:500}
.field-matrix .field-path{position:sticky;left:0;z-index:2;background:#f6f9fc;padding:7px 9px;text-align:left;color:#35546d;font:11px/1.28 Consolas,monospace}.field-value{padding:7px 9px;font:11px/1.28 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.field-diff{color:#c12640;font-weight:700}.field-same{color:#34445d}.category-row th{position:sticky;left:0;background:#dfeef5;color:#175c73;text-align:left;padding:7px 10px;font-size:11px;letter-spacing:.02em}.file-head{color:#526d83!important;overflow-wrap:anywhere}.raw-yaml-row details{margin:6px}.raw-yaml-row summary{cursor:pointer;color:#176a8d;font-weight:600;padding:5px}.raw-yaml-row pre{max-height:420px;overflow:auto}
.env-matrix td pre{margin:4px 6px;padding:6px 8px;font-size:11px;line-height:1.28}.env-matrix .row-title{padding:6px 8px}.field-matrix .field-path{padding:4px 7px;line-height:1.28}.field-matrix .field-value{padding:4px 7px;line-height:1.28}.field-matrix .category-row th{padding:4px 8px;font-size:11px}.field-matrix details{margin:3px}.field-matrix details summary{padding:3px}
.version{padding:6px 9px;background:#f4f0df;color:var(--yellow);font-size:12px}.orig-file{margin:6px 7px 0;padding:5px 8px;border-left:3px solid #4a90b8;background:#edf6fb;color:#28546d;font:12px/1.35 Consolas,monospace;overflow-wrap:anywhere}.add{display:block;color:#08733f;font-weight:600}.del{display:block;color:#c12640;font-weight:600}.hunk{display:block;color:#236a9d}.diff-file{display:block;color:var(--yellow)}.ctx{display:block;color:#40516b}
.release td pre{margin:4px 6px;padding:6px 8px;font-size:11px;line-height:1.28}.release .add,.release .del,.release .hunk,.release .diff-file,.release .ctx{display:inline;margin:0;padding:0;line-height:1.28}
.release-toolbar{display:flex;align-items:center;gap:10px}.release-toolbar span{font-size:12px}.release-view-toggle{border:1px solid #2c7699;background:#edf7fb;color:#1d607f;border-radius:6px;padding:7px 11px;cursor:pointer}.release-view-toggle:hover{background:#dceff7}.hidden{display:none!important}
.workload-title{font-weight:700}.workload-status{margin:6px 7px 0;padding:4px 8px;font-size:11px;font-weight:700;border-left:3px solid}.added-status{color:#08733f;border-color:#08733f}.removed-status{color:#c12640;border-color:#c12640}.absent-workload{min-height:36px;background:#fafbfd}
.diff-fragment{display:inline-grid!important;vertical-align:top;grid-template-columns:12px minmax(0,1fr);width:100%;background:transparent!important}.diff-marker{grid-column:1;text-align:center;font-weight:800;border-radius:2px 0 0 2px}.diff-yaml{grid-column:2;white-space:pre-wrap;min-width:0;border-radius:0 2px 2px 0}.diff-compact .ctx,.diff-full .ctx{display:inline;padding-left:12px}.diff-fragment.diff-all-namespaces>.diff-marker,.diff-fragment.diff-all-namespaces>.diff-yaml{background:#e4f6ea}.diff-fragment.diff-expected-env>.diff-marker,.diff-fragment.diff-expected-env>.diff-yaml{background:#e4f6ea}.diff-fragment.diff-actual>.diff-marker,.diff-fragment.diff-actual>.diff-yaml{background:#ffe7eb}.diff-fragment.diff-expected-env .expected-common{background:transparent}.diff-fragment.diff-expected-env .expected-env-token{background:#bfe3f7;border-radius:2px;font-weight:800}.diff-legend{display:flex;flex:none;gap:7px;padding:4px 7px;font-size:9px;color:#52637b}.diff-legend span{padding:2px 6px;border-radius:8px}.legend-all{background:#dff3e6}.legend-env{background:#bfe3f7;color:#086b9c}.legend-actual{background:#ffe0e5}
.collapse-box{position:relative}.collapsible-content{max-height:280px;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable}.field-collapse .collapsible-content{max-height:92px;white-space:pre-wrap}.collapsible-content::-webkit-scrollbar{width:7px}.collapsible-content::-webkit-scrollbar-thumb{background:#b8c8d5;border-radius:6px}.collapsible-content::-webkit-scrollbar-track{background:#edf2f6}
.release-version-bar{display:flex;flex:none;align-items:center;gap:10px;padding:7px 12px;background:#f7fafc;border-bottom:1px solid var(--line)}.release-version-bar>div:not(.version-arrow){display:flex;align-items:center;gap:6px}.release-version-bar span{color:var(--muted);font-size:10px}.release-version-bar b{color:#174f6b;font:600 11px Consolas,monospace}.version-arrow{color:#6f8198;font-size:14px}
.namespace-hidden{display:none!important}.only-differences .same-row{display:none}@media(max-width:800px){aside{display:none}main{top:56px;right:0;bottom:0;left:0;margin:0;padding:8px}.namespace-picker-toggle{min-width:auto}.namespace-picker-label{display:none}.namespace-picker-menu{right:0;width:min(350px,calc(100vw - 16px))}}
.only-differences .env-resource-yaml .yaml-same{display:none}
</style></head><body><header><h1>Release & Environment Reconciliation</h1><div class="namespace-picker"><button type="button" class="namespace-picker-toggle" aria-expanded="false"><span class="namespace-picker-icon">◇</span><span class="namespace-picker-label">Namespaces</span><span class="namespace-picker-count">0/0</span><span class="namespace-picker-caret">▼</span></button><div class="namespace-picker-menu hidden"><div class="namespace-picker-actions"><button type="button" data-filter-action="all">Select All</button><button type="button" data-filter-action="none">Deselect All</button></div>__FILTER__</div></div></header>
<aside>__NAV__</aside><main>__CONTENT__</main>
<script>
document.querySelectorAll('.tab-group').forEach(group=>{
 const directTabs=group.querySelectorAll(':scope > .tabs > .tab');
 directTabs.forEach(btn=>btn.onclick=()=>{
   directTabs.forEach(x=>x.classList.remove('active'));
   group.querySelectorAll(':scope > .tab-pane').forEach(x=>x.classList.remove('active'));
   btn.classList.add('active');
   group.querySelector(`:scope > .tab-pane[data-index="${btn.dataset.index}"]`).classList.add('active');
 });
});
document.querySelectorAll('.release-view-toggle').forEach(btn=>btn.onclick=()=>{
 const pane=btn.closest('.tab-pane'),showAll=btn.dataset.showAll!=='true';
 pane.querySelectorAll('.diff-compact').forEach(x=>x.classList.toggle('hidden',showAll));
 pane.querySelectorAll('.diff-full').forEach(x=>x.classList.toggle('hidden',!showAll));
 btn.dataset.showAll=String(showAll);btn.textContent=showAll?'Show changed lines only':'Show all lines';
 const hint=btn.nextElementSibling;if(hint)hint.textContent=showAll?'All unchanged and changed lines are shown':'Only changed lines are currently shown';
});
const filterInputs=[...document.querySelectorAll('.namespace-filter')];
const namespacePicker=document.querySelector('.namespace-picker');
const namespaceToggle=document.querySelector('.namespace-picker-toggle');
const namespaceMenu=document.querySelector('.namespace-picker-menu');
namespaceToggle?.addEventListener('click',event=>{
 event.stopPropagation();
 const opening=namespaceMenu.classList.contains('hidden');
 namespaceMenu.classList.toggle('hidden',!opening);
 namespaceToggle.setAttribute('aria-expanded',String(opening));
});
namespaceMenu?.addEventListener('click',event=>event.stopPropagation());
document.addEventListener('click',()=>{
 namespaceMenu?.classList.add('hidden');namespaceToggle?.setAttribute('aria-expanded','false');
});
document.querySelectorAll('[data-filter-action]').forEach(button=>button.addEventListener('click',()=>{
 const checked=button.dataset.filterAction==='all';
 filterInputs.forEach(input=>input.checked=checked);
 applyNamespaceFilter();
}));
function setDiffClass(fragment,classification){
 fragment.classList.remove('diff-all-namespaces','diff-expected-env','diff-actual');
 fragment.classList.add(classification);
}
function reclassifyReleaseTable(table,selected){
 const rows=[...table.tBodies].flatMap(body=>[...body.rows]);
 rows.forEach(row=>{
   const cells=[...row.querySelectorAll(':scope > td[data-env]')]
     .filter(cell=>selected.has(cell.dataset.env));
   const paths=new Set(cells.flatMap(cell=>
     [...cell.querySelectorAll('.diff-compact .diff-fragment[data-diff-path]')]
       .map(fragment=>fragment.dataset.diffKey)));
   paths.forEach(path=>{
     const matchingByCell=cells.map(cell=>[...cell.querySelectorAll('.diff-compact .diff-fragment[data-diff-key]')]
       .filter(fragment=>fragment.dataset.diffKey===path));
     const signatures=matchingByCell.map(fragments=>fragments
       .map(fragment=>fragment.dataset.rawSignature)
       .join('\\n'));
     const normalizedSignatures=matchingByCell.map(fragments=>fragments
       .map(fragment=>fragment.dataset.normalizedSignature)
       .join('\\n'));
     const complete=signatures.length>0&&signatures.every(Boolean);
     const exact=complete&&new Set(signatures).size===1;
     const normalized=complete&&new Set(normalizedSignatures).size===1;
     const hasEnv=matchingByCell.some(fragments=>
       fragments.some(fragment=>fragment.dataset.envDerived==='true'));
     const classification=exact?'diff-all-namespaces':
       (normalized&&hasEnv?'diff-expected-env':'diff-actual');
     cells.forEach(cell=>cell.querySelectorAll('.diff-fragment[data-diff-path]').forEach(fragment=>{
       if(fragment.dataset.diffKey===path)setDiffClass(fragment,classification);
     }));
   });
 });
}
function setBadge(badge,status){
 if(!badge)return;
 badge.classList.remove('no-diff','expected-diff','has-diff');
 const meta={none:['No Diff','no-diff'],expected:['Expected','expected-diff'],diff:['Diff','has-diff']}[status];
 badge.textContent=meta[0];badge.classList.add(meta[1]);
}
function refreshReleaseStatus(selected){
 document.querySelectorAll('.tab-pane[data-release="true"]').forEach(pane=>{
   pane.querySelectorAll('table.release').forEach(table=>reclassifyReleaseTable(table,selected));
   const visibleFragments=[...pane.querySelectorAll('.diff-compact .diff-fragment')]
     .filter(fragment=>selected.has(fragment.closest('td[data-env]')?.dataset.env));
   const status=!visibleFragments.length?'none':
     (visibleFragments.some(fragment=>fragment.classList.contains('diff-actual'))?'diff':'expected');
   pane.dataset.releaseStatus=status;
   const group=pane.parentElement;
   const button=group.querySelector(`:scope > .tabs > .tab[data-index="${pane.dataset.index}"]`);
   setBadge(button?.querySelector('.tab-diff-badge'),status);
 });
 document.querySelectorAll('main > section').forEach(section=>{
   const statuses=[...section.querySelectorAll('.tab-pane[data-release="true"]')]
     .map(pane=>pane.dataset.releaseStatus);
   const status=statuses.includes('diff')?'diff':(statuses.includes('expected')?'expected':'none');
   const link=document.querySelector(`aside a[data-target="${section.id}"]`);
   setBadge(link?.querySelector('.diff-badge'),status);
 });
}
function refreshEnvironmentDiff(selected){
 document.querySelectorAll('table.env-matrix').forEach(table=>{
   table.querySelectorAll('tbody tr[data-field-path]').forEach(row=>{
     const cells=[...row.querySelectorAll(':scope > td[data-env]')]
       .filter(cell=>selected.has(cell.dataset.env));
     const values=cells.map(cell=>cell.textContent.trim());
     const changed=values.length>1&&new Set(values).size>1;
     row.classList.toggle('diff-row',changed);row.classList.toggle('same-row',!changed);
     cells.forEach(cell=>cell.querySelectorAll('.field-diff,.field-same').forEach(value=>{
       value.classList.toggle('field-diff',changed);value.classList.toggle('field-same',!changed);
     }));
   });
   table.querySelectorAll('tbody tr:not([data-field-path])').forEach(row=>{
     const cells=[...row.querySelectorAll(':scope > td[data-env]')]
       .filter(cell=>selected.has(cell.dataset.env));
     if(!row.querySelector('td[data-env]'))return;
     const paths=new Set(cells.flatMap(cell=>[...cell.querySelectorAll('[data-yaml-path]')]
       .map(line=>line.dataset.yamlPath)));
     const directlyChanged=new Set();
     paths.forEach(path=>{
       const values=cells.map(cell=>[...cell.querySelectorAll('[data-yaml-path]')]
         .filter(line=>line.dataset.yamlPath===path).map(line=>line.textContent).join('\\n')||'<missing>');
       if(values.length>1&&new Set(values).size>1)directlyChanged.add(path);
     });
     const changedPaths=new Set([...paths].filter(path=>[...directlyChanged]
       .some(changed=>changed===path||changed.startsWith(path+'.')||changed.startsWith(path+'['))));
     cells.forEach(cell=>cell.querySelectorAll('[data-yaml-path]').forEach(line=>{
       const changed=changedPaths.has(line.dataset.yamlPath);
       line.classList.toggle('yaml-diff',changed);line.classList.toggle('yaml-same',!changed);
     }));
     row.classList.toggle('diff-row',changedPaths.size>0);
     row.classList.toggle('same-row',changedPaths.size===0);
   });
 });
}
function applyNamespaceFilter(){
 const selected=new Set(filterInputs.filter(input=>input.checked).map(input=>input.value));
 const count=document.querySelector('.namespace-picker-count');
 if(count)count.textContent=`${selected.size}/${filterInputs.length}`;
 document.querySelectorAll('[data-env]').forEach(element=>{
   if(element.classList.contains('namespace-filter'))return;
   element.classList.toggle('namespace-hidden',!selected.has(element.dataset.env));
 });
 refreshReleaseStatus(selected);
 refreshEnvironmentDiff(selected);
}
filterInputs.forEach(input=>input.addEventListener('change',applyNamespaceFilter));
const navLinks=[...document.querySelectorAll('aside a[data-target]')];
const views=[...document.querySelectorAll('main > section')];
function selectView(target,updateHash=true){
 if(!document.getElementById(target))target=views[0]?.id;
 views.forEach(view=>view.classList.toggle('active-view',view.id===target));
 navLinks.forEach(link=>link.classList.toggle('active',link.dataset.target===target));
 if(updateHash&&target)history.replaceState(null,'','#'+target);
}
navLinks.forEach(link=>link.addEventListener('click',event=>{
 event.preventDefault();selectView(link.dataset.target);
}));
window.addEventListener('hashchange',()=>selectView(location.hash.slice(1),false));
selectView(location.hash.slice(1)||navLinks[0]?.dataset.target,false);
applyNamespaceFilter();
</script></body></html>"""


def main():
    p = argparse.ArgumentParser(description="Generate a standalone release/environment reconciliation HTML report.")
    p.add_argument("--input-dir", required=True, type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    output = args.output or args.input_dir.parent / "recon_report.html"
    output.write_text(build(args.input_dir.resolve()), encoding="utf-8")
    print(f"Report generated: {output.resolve()}")


if __name__ == "__main__":
    main()
