from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1] / "outputs" / "recon-data"


def write(rel: str, text: str) -> None:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def helm(env: str, version: str, replicas: int, image: str, port: int, memory: str,
         feature: bool, suffix: str, baseline: bool = False) -> str:
    comment = "# Source: payment/templates/deployment.yaml"
    old = '        legacyFlag: "true"\n' if baseline else ""
    optional = "        FEATURE_FAST_PAY: \"true\"\n" if feature else ""
    return f"""
{comment}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-api-{suffix}
  labels:
    app: payment-api
    environment: {env}
spec:
  replicas: {replicas}
  selector:
    matchLabels:
      app: payment-api
  template:
    metadata:
      labels:
        app: payment-api
    spec:
      containers:
        - name: payment-api
          image: registry.example/payment-api:{image}
          ports:
            - name: http
              containerPort: {port}
          env:
            - name: LOG_LEVEL
              value: INFO
          resources:
            requests:
              cpu: 250m
              memory: {memory}
---
# Source: payment/templates/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: payment-api-{suffix}
spec:
  selector:
    app: payment-api
  ports:
    - name: http
      port: 80
      targetPort: {port}
---
# Rendered ConfigMap; key names intentionally contain environment suffixes
apiVersion: v1
kind: ConfigMap
metadata:
  name: payment-config-{suffix}
data:
  application-{env}.yaml: |
    server:
      port: {port}
    spring:
      application:
        name: payment-api
{optional}{old}"""


def app_config(env: str, db: str, pool: int, kafka: str, timeout: int,
               feature: bool, server_port: int = 8080, baseline: bool = False) -> str:
    removed = "  legacy-mode: true\n" if baseline else ""
    feature_line = f"  fast-pay: {str(feature).lower()}\n"
    shared_mode = "legacy" if baseline else "modern"
    previous_env = {"dev": "qa", "qa": "prod", "prod": "dev"}.get(env, "qa")
    environment_alias = f"service-{previous_env}" if baseline else f"service-{env}"
    review_threshold = 10 if baseline else {"dev": 12, "qa": 15, "prod": 20}.get(env, 12)
    return f"""
spring:
  application:
    name: payment-api
  datasource:
    url: jdbc:postgresql://{db}:5432/payment
    username: payment
    hikari:
      maximum-pool-size: {pool}
  kafka:
    bootstrap-servers: {kafka}:9092
server:
  port: {server_port}
logging:
  level:
    root: INFO
features:
{feature_line}{removed}partner:
  endpoint: https://partner-{env}.example/api
  timeout-ms: {timeout}
custom-unmapped:
  owner: platform
  note: "must remain visible"
release-demo:
  shared-mode: {shared_mode}
  environment-alias: {environment_alias}
  review-threshold: {review_threshold}
"""


def routing_config(env: str, baseline: bool = False) -> str:
    weight = 80 if env == "prod" else 20
    if baseline:
        weight -= 5
    return f"""
route:
  payment:
    upstream: payment-core-{env}
    timeout: 3s
flow:
  fraud-check:
    enabled: true
    weight: {weight}
gateway:
  base-url: https://gateway-{env}.example
"""


def module_helm(module: str, *args, **kwargs) -> str:
    return (helm(*args, **kwargs)
            .replace("payment-api", f"{module}-api")
            .replace("payment-config", f"{module}-config")
            .replace("payment/templates", f"{module}/templates")
            .replace("registry.example/payment", f"registry.example/{module}"))


def remove_workload(text: str, kind: str) -> str:
    """Remove one rendered YAML document to mock an environment-specific absence."""
    documents = text.split("\n---\n")
    kept = [doc for doc in documents if f"\nkind: {kind}\n" not in f"\n{doc}\n"]
    return "\n---\n".join(kept)


def apply_name_scenario(text: str, module: str, env: str, suffix: str) -> str:
    """Exercise identical and environment-only resource names."""
    if module == "inventory":
        replacement = ""
    elif module == "notification":
        replacement = f"-{env}"
    elif module == "risk-engine":
        replacement = f"-{env}"
    else:
        return text
    return (text.replace(f"{module}-api-{suffix}", f"{module}-api{replacement}")
            .replace(f"{module}-config-{suffix}", f"{module}-config{replacement}"))


def config_name_scenario(module: str, base: str, env: str) -> str:
    if module == "inventory":
        return base
    if module == "risk-engine":
        return f"{base}-{env}"
    return f"{base}-{env}"


def module_config(module: str, *args, **kwargs) -> str:
    return (app_config(*args, **kwargs)
            .replace("payment-api", f"{module}-api")
            .replace("/payment", f"/{module}")
            .replace("username: payment", f"username: {module}"))


def module_routing(module: str, *args, **kwargs) -> str:
    return (routing_config(*args, **kwargs)
            .replace("payment:", f"{module}:")
            .replace("payment-core", f"{module}-core"))


def namespace(env_label: str, version: str, airflow: bool, quota: str,
              baseline: bool = False) -> str:
    namespace_name = ("airflow-ms-" if airflow else "payment-") + env_label
    extra = "\n  annotations:\n    owner: data-platform" if airflow else ""
    limit = "12" if baseline else quota
    vss_doc = "" if (not baseline and env_label == "cshg-dev") else f"""
---
apiVersion: platform.example.io/v1
kind: VSS
metadata:
  name: shared-secret-store-{env_label}
  namespace: {namespace_name}
spec:
  provider: vault
  refreshInterval: {"10m" if baseline else "5m"}
  secretPath: teams/{namespace_name}
"""
    regional_doc = f"""
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: bjv-production-operator-{env_label}
  namespace: {namespace_name}
subjects:
  - kind: ServiceAccount
    name: runtime-sa-{env_label}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: runtime-reader-{env_label}
""" if env_label == "cbjv-prod" else ""
    return f"""
# Kustomize build output for {env_label}
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace_name}{extra}
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: compute-quota-{env_label}
  namespace: {namespace_name}
spec:
  hard:
    requests.cpu: "{quota}"
    limits.cpu: "{limit}"
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: runtime-sa-{env_label}
  namespace: {namespace_name}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: namespace-settings-{env_label}
  namespace: {namespace_name}
data:
  runtime.yaml: |
    environment: {env_label}
    audit-enabled: true
    reconciliation-version: {version}
{vss_doc}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: runtime-reader-{env_label}
  namespace: {namespace_name}
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets"]
    verbs: ["get", "list"]
{regional_doc}
"""


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)

    envs = [
        ("cshg-dev", "dev", "1.3.0", 2, "1.3.0", 8080, "256Mi", True, "dev-12256"),
        ("cshg-qa", "qa", "1.3.0-rc2", 2, "1.3.0-rc2", 8081, "384Mi", False, "qa-44218"),
        ("cshg-prod", "prod", "1.2.9", 4, "1.2.9", 8080, "512Mi", False, "prod-31882"),
        ("cbjv-dev", "dev", "current-code", 2, "current-code", 8080, "256Mi", True, "dev-90871"),
        ("cbjv-qa", "qa", "1.3.0-rc3", 2, "1.3.0-rc3", 8081, "384Mi", True, "qa-55291"),
        ("cbjv-prod", "prod", "release-2026.07", 4, "1.2.9", 8080, "512Mi", False, "prod-77102"),
    ]
    modules = [
        ("payment", 0, "2.4.0", "2.3.1", "docker-2026.07.20", "docker-2026.06.10"),
        ("inventory", 10, "current-code", "1.8.4", "current-code", "helm-1.8.4"),
        ("notification", 20, "notification-snapshot", "notification-stable", "helm-3.2.1", "docker-3.2.0"),
        ("risk-engine", 30, "risk-current-code", "risk-release-2026.06", "docker-4.7.2", "current-code"),
    ]
    for module, port_offset, current_version, baseline_version, app_current_version, app_baseline_version in modules:
        for label, env, version, replicas, image, port, memory, feature, suffix in envs:
            module_version = current_version
            module_image = current_version
            current_helm = module_helm(module, env, module_version, replicas, module_image,
                                       port + port_offset, memory, feature, suffix)
            current_helm = apply_name_scenario(current_helm, module, env, suffix)
            # Payment Service and ConfigMap are deliberately absent only from
            # Shanghai QA current. Baseline retains them, exercising whole-resource
            # removal including +/- markers on YAML parent lines.
            if module == "payment" and label == "cshg-qa":
                current_helm = remove_workload(current_helm, "Service")
                current_helm = remove_workload(current_helm, "ConfigMap")
            write(f"current/helm/{label}/{module}__{module_version}.yaml", current_helm)
            old_version = baseline_version
            baseline_helm = (current_helm if module == "notification" else
                             module_helm(module, env, old_version,
                                         1 if env != "prod" else 3, old_version,
                                         8080 + port_offset, "256Mi", False, suffix,
                                         baseline=True))
            if module != "notification":
                baseline_helm = apply_name_scenario(baseline_helm, module, env, suffix)
            # Exercise a true Helm Expected Diff in all six namespaces: only the
            # environment suffix changes between baseline and current.
            if module == "risk-engine":
                previous_env = {"dev": "qa", "qa": "prod", "prod": "dev"}[env]
                baseline_helm = (baseline_helm
                                 .replace(f"{module}-api-{env}", f"{module}-api-{previous_env}")
                                 .replace(f"{module}-config-{env}", f"{module}-config-{previous_env}")
                                 .replace(f"application-{env}.yaml", f"application-{previous_env}.yaml"))
            write(f"baseline/helm/{label}/{module}__{old_version}.yaml", baseline_helm)

            current_app = module_config(module, env, f"{module}-db-{env}",
                                        10 if env == "prod" else 5, f"kafka-{env}",
                                        2500 if env == "qa" else 2000, feature,
                                        server_port=port + port_offset)
            baseline_app = (current_app if module == "notification" else
                            module_config(module, env, f"{module}-db-{env}-old", 4,
                                          f"kafka-{env}", 3000, False,
                                          server_port=8080 + port_offset, baseline=True))
            if module == "risk-engine":
                current_app += ("\nenvironment-transition-demo:\n"
                                "  environment-to-fixed: service-common\n"
                                f"  fixed-to-environment: service-{env}\n"
                                "one-sided-demo:\n"
                                "  added-fixed: enabled\n"
                                f"  added-environment: service-{env}\n")
                baseline_app += ("\nenvironment-transition-demo:\n"
                                 f"  environment-to-fixed: service-{env}\n"
                                 "  fixed-to-environment: service-common\n"
                                 "one-sided-demo:\n"
                                 "  removed-fixed: legacy\n"
                                 f"  removed-environment: service-{env}\n")
            application_name = config_name_scenario(module, "application", env)
            routes_name = config_name_scenario(module, "routes", env)
            write(f"current/app_config/{label}/{module}/database/{application_name}__{app_current_version}.yaml",
                  current_app)
            write(f"baseline/app_config/{label}/{module}/database/{application_name}__{app_baseline_version}.yaml",
                  baseline_app)
            current_routes = module_routing(module, env)
            baseline_routes = current_routes if module == "notification" else module_routing(module, env, baseline=True)
            write(f"current/app_config/{label}/{module}/routing/{routes_name}__{app_current_version}.yaml",
                  current_routes)
            write(f"baseline/app_config/{label}/{module}/routing/{routes_name}__{app_baseline_version}.yaml",
                  baseline_routes)

    ns_envs = [
        ("cshg-dev", "dev", "ns-4.2.0", "8"),
        ("cshg-qa", "qa", "ns-4.2.0", "10"),
        ("cshg-prod", "prod", "ns-4.2.0", "16"),
        ("cbjv-dev", "dev", "ns-4.2.0", "8"),
        ("cbjv-qa", "qa", "ns-4.2.0", "10"),
        ("cbjv-prod", "prod", "ns-4.2.0", "16"),
    ]
    for label, env, version, quota in ns_envs:
        write(f"current/ns/{label}/{label}__{version}.yaml",
              namespace(label, version, False, quota))
        write(f"baseline/ns/{label}/{label}__ns-4.1.0.yaml",
              namespace(label, "ns-4.1.0", False, quota, baseline=True))

        airflow_label = label.replace("-", "-ms-")
        write(f"current/ns/{airflow_label}/{airflow_label}__{version}.yaml",
              namespace(airflow_label, version, True, quota))
        write(f"baseline/ns/{airflow_label}/{airflow_label}__ns-4.1.0.yaml",
              namespace(airflow_label, "ns-4.1.0", True, quota, baseline=True))

    print(f"Mock data created at {ROOT}")


if __name__ == "__main__":
    main()
