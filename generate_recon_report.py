#!/usr/bin/env python3
import argparse
import difflib
import html
import json
import re
from collections import defaultdict
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


class PrettyDumper(yaml.SafeDumper):
    """Keep embedded ConfigMap/config text readable instead of escaping newlines."""


def _represent_string(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


PrettyDumper.add_representer(str, _represent_string)


def split_stem(path: Path):
    stem = path.stem
    return stem.rsplit("__", 1) if "__" in stem else (stem, "unknown")


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
        rows.append(f'<span class="{cls}">{html.escape(line)}</span>')

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
    return "\n".join(rows)


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
        head.append(f'<th><b>{html.escape(env)}</b>'
                    f'<small class="file-head">{html.escape(item["path"].name)}</small></th>')
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
        for value in values:
            if value is marker:
                body = '<span class="missing">MISSING</span>'
            else:
                cls = "field-diff" if changed else "field-same"
                body = f'<span class="{cls}">{html.escape(scalar_text(value))}</span>'
            cells.append(f'<td class="field-value"><div class="collapse-box field-collapse">'
                         f'<div class="collapsible-content">{body}</div></div></td>')
        rows.append(f'<tr class="{"diff-row" if changed else "same-row"}"><th class="field-path">{html.escape(path)}</th>{"".join(cells)}</tr>')
    raw_cells = []
    for env in envs:
        item, value, _ = parsed[env]
        raw_cells.append(f'<td><details><summary>View original YAML</summary>'
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
    head = "".join(f'<th><b>{html.escape(e)}</b></th>' for e in envs)
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
            cells.append(f'<td>{file_label}<div class="collapse-box">'
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
        content = "\n".join(f'<span class="ctx">{html.escape(line)}</span>'
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
    return "\n".join(out) or '<span class="yaml-same">No release changes</span>'


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
    head = "".join(f"<th>{html.escape(e)}</th>" for e in envs)
    rows = []
    for logical in logicals:
        cells = []
        for env in envs:
            c = cg.get((env, logical), [])
            b = bg.get((env, logical), [])
            cur = c[0] if c else None
            base = b[0] if b else None
            old_text, new_text = base["text"] if base else "", cur["text"] if cur else ""
            old_name = f'baseline/{base["path"].name if base else "missing"}'
            new_name = f'current/{cur["path"].name if cur else "missing"}'
            compact_diff = unified(old_text, new_text, old_name, new_name)
            full_diff = unified(old_text, new_text, old_name, new_name, show_all=True)
            cells.append(f'<td><pre class="diff-compact">{compact_diff}</pre>'
                         f'<pre class="diff-full hidden">{full_diff}</pre></td>')
        rows.append(f'<tr><th class="row-title">{html.escape(logical)}</th>{"".join(cells)}</tr>')
    return (f'<div class="toolbar release-toolbar"><button class="release-view-toggle" '
            f'data-show-all="false">Show all lines</button>'
            f'<span>Only changed lines are currently shown</span></div>'
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
    head = "".join(f'<th><b>{html.escape(env)}</b></th>' for env in envs)
    rows = []
    for workload in workloads:
        cells = []
        for env in envs:
            old = bg.get(env, {}).get(workload)
            new = cg.get(env, {}).get(workload)
            if old is None and new is None:
                cells.append('<td class="absent-workload"></td>')
                continue
            if old is None:
                status = '<div class="workload-status added-status">Added in current</div>'
            elif new is None:
                status = '<div class="workload-status removed-status">Removed from current</div>'
            else:
                status = ""
            compact = unified(old or "", new or "", "baseline", "current")
            full = unified(old or "", new or "", "baseline", "current", show_all=True)
            cells.append(f'<td>{status}'
                         f'<div class="collapse-box diff-compact"><pre class="collapsible-content">{compact}</pre></div>'
                         f'<div class="collapse-box diff-full hidden"><pre class="collapsible-content">{full}</pre></div></td>')
        rows.append(f'<tr><th class="row-title workload-title">{html.escape(workload)}</th>'
                    f'{"".join(cells)}</tr>')
    return (f'<div class="toolbar release-toolbar"><button class="release-view-toggle" '
            f'data-show-all="false">Show all lines</button>'
            f'<span>Workloads are matched by kind + normalized metadata.name</span></div>'
            f'<div class="table-wrap"><table class="matrix release workload-release">'
            f'<thead><tr><th>Resource</th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def tabs(sections, extra_class=""):
    buttons, bodies = [], []
    for i, section in enumerate(sections):
        label, body = section[0], section[1]
        changed = section[2] if len(section) > 2 else None
        active = " active" if i == 0 else ""
        badge = ""
        if changed is not None:
            status = "DIFF" if changed else "NO DIFF"
            cls = "has-diff" if changed else "no-diff"
            badge = f'<span class="tab-diff-badge {cls}">{status}</span>'
        buttons.append(f'<button class="tab{active}" data-index="{i}">'
                       f'{html.escape(label)}{badge}</button>')
        bodies.append(f'<div class="tab-pane{active}" data-index="{i}">{body}</div>')
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


def nav_item(cid, label, changed):
    status = "DIFF" if changed else "NO DIFF"
    cls = "has-diff" if changed else "no-diff"
    return (f'<a href="#{cid}" data-target="{cid}"><span>{html.escape(label)}</span>'
            f'<span class="diff-badge {cls}">{status}</span></a>')


def build(input_dir: Path):
    current = {k: scan(input_dir, "current", k) for k in ("helm", "app_config", "ns")}
    baseline = {k: scan(input_dir, "baseline", k) for k in ("helm", "app_config", "ns")}
    modules = sorted({x["module"] for x in current["app_config"] + baseline["app_config"]})
    cards, nav = [], ['<div class="nav-heading">Modules</div>']
    for module in modules:
        cid = "module-" + re.sub(r"\W+", "-", module)
        cc = [x for x in current["app_config"] if x["module"] == module]
        bb = [x for x in baseline["app_config"] if x["module"] == module]
        # Mock convention: Helm chart and module share their name.
        ch = [x for x in current["helm"] if x["name"] == module]
        bh = [x for x in baseline["helm"] if x["name"] == module]
        helm_changed = release_changed(ch, bh, "helm")
        app_changed = release_changed(cc, bb, "app_config")
        nav.append(nav_item(cid, module, helm_changed or app_changed))
        sections = [
            ("RELEASE-DIFF · Helm", version_bar(ch, bh) + resource_release_matrix(ch, bh, "Workload (kind / metadata.name)"), helm_changed),
            ("RELEASE-DIFF · App Config", version_bar(cc, bb) + app_release_config_tabs(cc, bb), app_changed),
            ("ENV-DIFF · Helm", version_bar(ch, bh) + env_matrix(ch, "helm", "Workload / resource")),
            ("ENV-DIFF · App Config", version_bar(cc, bb) + app_env_config_tabs(cc)),
        ]
        cards.append(f'<section id="{cid}"><h2>Application · {html.escape(module)}</h2>'
                     f'{tabs(sections)}</section>')

    nav.append('<div class="nav-heading">Namespaces</div>')
    for airflow, label in ((False, "Service Namespaces"), (True, "Airflow Namespaces")):
        cn = [x for x in current["ns"] if ("ms" in x["env"].lower()) == airflow]
        bn = [x for x in baseline["ns"] if ("ms" in x["env"].lower()) == airflow]
        cid = "airflow-ns" if airflow else "service-ns"
        ns_changed = release_changed(cn, bn, "ns")
        nav.append(nav_item(cid, label, ns_changed))
        cards.append(f'<section id="{cid}"><h2>{label}</h2>{version_bar(cn, bn)}'
                     f'{tabs([("RELEASE-DIFF", resource_release_matrix(cn, bn, "GitOps workload (kind / metadata.name)", raw_env_headers=True), ns_changed), ("ENV-DIFF", env_matrix(cn, "ns", "Namespace resource"))])}</section>')

    data = {"nav": "".join(nav), "content": "".join(cards)}
    return TEMPLATE.replace("__NAV__", data["nav"]).replace("__CONTENT__", data["content"])


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reconciliation Report</title><style>
:root{--bg:#f4f7fb;--panel:#fff;--line:#d9e2ef;--text:#1f2a3d;--muted:#66758c;--cyan:#087f8c;--red:#c9364f;--green:#177245;--yellow:#9b6300}
*{box-sizing:border-box}html,body{height:100%;overflow:hidden}body{margin:0;background:var(--bg);color:var(--text);font:13px Inter,Segoe UI,sans-serif}
header{position:fixed;inset:0 0 auto 0;height:56px;z-index:5;background:#fffffff2;border-bottom:1px solid var(--line);padding:14px 18px;box-shadow:0 2px 10px #25385810}
h1{margin:0;font-size:18px}aside{position:fixed;top:56px;bottom:0;width:210px;padding:14px;border-right:1px solid var(--line);background:#fff;overflow:auto}
aside a{display:flex;align-items:center;justify-content:space-between;gap:7px;color:var(--muted);padding:8px 9px;border-radius:6px;text-decoration:none;border-left:3px solid transparent;font-size:12px}aside a:hover{background:#eaf3fb;color:#174a68}aside a.active{background:#dceef7;color:#0b5f7b;border-left-color:#087f8c;font-weight:700}.diff-badge{flex:none;padding:2px 5px;border-radius:9px;font-size:8px;font-weight:800;letter-spacing:.03em}.diff-badge.has-diff{color:#a62239;background:#ffe1e6}.diff-badge.no-diff{color:#17633d;background:#dcf5e6}
.nav-heading{margin:14px 6px 6px;padding-bottom:6px;border-bottom:1px solid #dce5ef;color:#32445d;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.nav-heading:first-child{margin-top:0}
main{position:fixed;top:56px;right:0;bottom:0;left:210px;padding:14px;overflow:hidden}main>section{display:none;height:100%;min-height:0;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}main>section.active-view{display:flex;flex-direction:column}
h2{flex:none;font-size:15px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line)}
.tab-group{display:flex;flex:1;min-height:0;flex-direction:column}.tabs{display:flex;flex:none;gap:3px;padding:7px 9px 0;overflow-x:auto}.tab{border:1px solid #dce5ef;border-bottom:0;background:#edf2f8;color:var(--muted);padding:7px 9px;border-radius:6px 6px 0 0;cursor:pointer;white-space:nowrap;font-size:11px}.tab.active{background:#fff;color:#174a68;border-top:2px solid var(--cyan)}
.tab-diff-badge{display:inline-block;margin-left:6px;padding:1px 4px;border-radius:8px;font-size:8px;font-weight:800}.tab-diff-badge.has-diff{color:#a62239;background:#ffe1e6}.tab-diff-badge.no-diff{color:#17633d;background:#dcf5e6}
.tab-pane{display:none;padding:8px}.tab-pane.active{display:flex;flex:1;min-height:0;flex-direction:column;overflow:hidden}.toolbar{flex:none;padding:5px;color:var(--muted)}.table-wrap{flex:1;min-height:0;overflow:auto;max-height:none;border:1px solid var(--line);border-radius:7px}
.config-tabs{border:1px solid #dce5ef;border-radius:8px;background:#f8fafc}.config-tabs>.tabs{padding-top:8px}.config-tabs>.tabs>.tab{font-family:Consolas,monospace;padding:8px 11px}.config-tabs>.tab-pane{padding:8px}
table{border-collapse:separate;border-spacing:0;min-width:100%;table-layout:fixed}th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line);vertical-align:top}
thead th{position:sticky;top:0;background:#eaf1f8;z-index:3;padding:10px;min-width:350px}.matrix thead th:first-child,.row-title{min-width:190px;width:190px;position:sticky;left:0;z-index:2;background:#f1f5f9}
th small{display:block;color:var(--yellow);margin-top:5px;font-weight:400}td{background:#fff}td pre{margin:5px 7px;padding:8px 10px;white-space:pre-wrap;word-break:break-word;font:12px/1.32 Consolas,"Cascadia Mono",monospace;background:#f8fafc;border:1px solid #e5ebf2;border-radius:6px;tab-size:2}
.row-title{padding:9px;text-align:left;color:var(--cyan)}.yaml-diff{display:block;color:#c12640;font-weight:700}.yaml-same{display:block;color:#34445d}
.missing{color:var(--red);font-weight:bold}.category{display:inline-block;color:#08111f;background:#87c7ff;border-radius:3px;padding:0 4px;font:10px Segoe UI,sans-serif}
.config-definition{margin-top:10px;padding-top:8px;border-top:1px solid #d6e0eb;color:#52637b;font-size:11px}.config-definition b{display:block;margin-bottom:5px}.row-category{display:inline-block;margin:2px 3px 2px 0;padding:2px 5px;border-radius:10px;background:#dceef5;color:#24566b;font-weight:500}
.field-matrix .field-path{position:sticky;left:0;z-index:2;background:#f6f9fc;padding:7px 9px;text-align:left;color:#35546d;font:12px/1.3 Consolas,monospace}.field-value{padding:7px 9px;font:12px/1.35 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.field-diff{color:#c12640;font-weight:700}.field-same{color:#34445d}.category-row th{position:sticky;left:0;background:#dfeef5;color:#175c73;text-align:left;padding:7px 10px;font-size:12px;letter-spacing:.02em}.file-head{color:#526d83!important;overflow-wrap:anywhere}.raw-yaml-row details{margin:6px}.raw-yaml-row summary{cursor:pointer;color:#176a8d;font-weight:600;padding:5px}.raw-yaml-row pre{max-height:420px;overflow:auto}
.env-matrix td pre{margin:3px 5px;padding:5px 7px;font-size:11px;line-height:1.08}.env-matrix .row-title{padding:6px 8px}.field-matrix .field-path{padding:4px 7px;line-height:1.15}.field-matrix .field-value{padding:4px 7px;line-height:1.15}.field-matrix .category-row th{padding:4px 8px;font-size:11px}.field-matrix details{margin:3px}.field-matrix details summary{padding:3px}
.version{padding:6px 9px;background:#f4f0df;color:var(--yellow);font-size:12px}.orig-file{margin:6px 7px 0;padding:5px 8px;border-left:3px solid #4a90b8;background:#edf6fb;color:#28546d;font:12px/1.35 Consolas,monospace;overflow-wrap:anywhere}.add{display:block;color:#08733f;font-weight:600}.del{display:block;color:#c12640;font-weight:600}.hunk{display:block;color:#236a9d}.diff-file{display:block;color:var(--yellow)}.ctx{display:block;color:#40516b}
.release td pre{margin:3px 5px;padding:5px 7px;font-size:11px;line-height:1.06}.release .add,.release .del,.release .hunk,.release .diff-file,.release .ctx{line-height:1.06}
.release-toolbar{display:flex;align-items:center;gap:10px}.release-toolbar span{font-size:12px}.release-view-toggle{border:1px solid #2c7699;background:#edf7fb;color:#1d607f;border-radius:6px;padding:7px 11px;cursor:pointer}.release-view-toggle:hover{background:#dceff7}.hidden{display:none!important}
.workload-title{font-weight:700}.workload-status{margin:6px 7px 0;padding:4px 8px;font-size:11px;font-weight:700;border-left:3px solid}.added-status{color:#08733f;border-color:#08733f}.removed-status{color:#c12640;border-color:#c12640}.absent-workload{min-height:36px;background:#fafbfd}
.collapse-box{position:relative}.collapsible-content{max-height:280px;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable}.field-collapse .collapsible-content{max-height:92px;white-space:pre-wrap}.collapsible-content::-webkit-scrollbar{width:7px}.collapsible-content::-webkit-scrollbar-thumb{background:#b8c8d5;border-radius:6px}.collapsible-content::-webkit-scrollbar-track{background:#edf2f6}
.release-version-bar{display:flex;flex:none;align-items:center;gap:10px;padding:7px 12px;background:#f7fafc;border-bottom:1px solid var(--line)}.release-version-bar>div:not(.version-arrow){display:flex;align-items:center;gap:6px}.release-version-bar span{color:var(--muted);font-size:10px}.release-version-bar b{color:#174f6b;font:600 11px Consolas,monospace}.version-arrow{color:#6f8198;font-size:14px}
.only-differences .same-row{display:none}@media(max-width:800px){aside{display:none}main{top:56px;right:0;bottom:0;left:0;margin:0;padding:8px}}
.only-differences .env-resource-yaml .yaml-same{display:none}
</style></head><body><header><h1>Release & Environment Reconciliation</h1></header>
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
