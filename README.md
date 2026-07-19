# Flux HelmRelease local renderer

This script reproduces the local rendering pipeline without applying anything
to a cluster:

```text
namespace Kustomization
  -> HelmRelease valuesFrom + spec.values
  -> helm template
  -> the same namespace Kustomization patches
```

## Install

```bash
python -m pip install -r requirements.txt
```

The referenced `HelmRepository` must already be configured locally with
`helm repo add`. Its local alias is matched by URL.

## Render one HelmRelease

```bash
python render_flux_helmrelease.py ./namespaces/app-dev/kustomization.yaml \
  --name my-release > app-dev-my-release.yaml
```

## Render all HelmReleases in the namespace entry

```bash
python render_flux_helmrelease.py ./namespaces/app-dev --all > app-dev.yaml
python render_flux_helmrelease.py ./namespaces/app-prod --all > app-prod.yaml
git diff --no-index app-dev.yaml app-prod.yaml
```

The first argument can be the namespace directory or its Kustomization file.
There is no `--namespace` option: the entry already defines the resource scope,
and each HelmRelease's `metadata.namespace` is read from the Kustomize output.
The only selection options are `--name` and `--all`.

The script uses `kubectl` and `helm`, writes the final YAML to stdout, and does
not contact or apply to a Kubernetes cluster.

## Test

```bash
python -m unittest discover -s tests -v
```

## Generate a reconciliation report

`generate_recon_report.py` creates a standalone light-theme HTML report from a
`baseline`/`current` reconciliation directory:

```bash
python generate_recon_report.py \
  --input-dir ./recon-data \
  --output ./recon_report.html
```

The report includes:

- release diffs aligned by Kubernetes `kind` and normalized `metadata.name`;
- environment diffs for Helm workloads and GitOps namespace resources;
- semantic, order-independent App Config field comparison;
- numeric and descriptive versions such as `2.4.0` and `current-code`;
- environment-, version-, numeric-, short-SHA-, and SHA-256-aware name matching.

To build a complete demonstration dataset before generating the report:

```bash
python create_mock_data.py
python generate_recon_report.py \
  --input-dir outputs/recon-data \
  --output outputs/recon_report.html
```

Generated mock data and reports are local artifacts and should not be committed.
