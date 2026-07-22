from pathlib import Path
import re
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
    legacy_secret = ("            - secretRef:\n"
                     "                name: legacy-payment-secret\n") if baseline else ""
    secret_key = "legacy-password" if baseline else "password"
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
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: payment-database-secret
                  key: {secret_key}
          envFrom:
{legacy_secret}            - secretRef:
                name: payment-primary-secret
            - configMapRef:
                name: payment-runtime-config
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
    notification_channels = ["email", "sms"] + (["pager"] if env == "prod" else [])
    channels_yaml = "\n".join(f"    - {channel}" for channel in notification_channels)
    clients = [
        (f"acl-{env}-reader", "group", "VIEWER"),
        (f"acl-{env}-writer", "group", "EDITOR"),
    ] + ([(f"acl-{env}-ops", "user", "ADMIN")] if env == "prod" else [])
    clients_yaml = "\n".join(
        f"    - username: {username}\n"
        f"      type: {client_type}\n"
        f"      authorities:\n"
        f"        - {authority}"
        for username, client_type, authority in clients
    )
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
notifications:
  channels:
{channels_yaml}
application:
  clients:
{clients_yaml}
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
            .replace("registry.example/payment", f"registry.example/{module}")
            .replace("payment-", f"{module}-"))


def remove_workload(text: str, kind: str) -> str:
    """Remove one rendered YAML document to mock an environment-specific absence."""
    documents = text.split("\n---\n")
    kept = [doc for doc in documents if f"\nkind: {kind}\n" not in f"\n{doc}\n"]
    return "\n---\n".join(kept)


def add_pki_deployment_scenario(text: str, env: str, baseline: bool) -> str:
    """Mock the PKI/list diff shape captured from a rendered Deployment."""
    image = ("edge-prod.ai.ms.com.cn/devops-docker-release/ops-automation/"
             f"china-ops-central:{'2026.06.18-1' if baseline else '2026.07.08-6'}")
    text = re.sub(r"(?m)^          image: .+$", f"          image: {image}", text, count=1)
    script = ("/base-image/scripts/af_token_update_to_vault.sh "
              f"{'-c' if baseline else '-n msbic/core -c'} /pki/proid-ops-auto "
              f"-i 24969-{env}-ops_auto -k password_or_token-shg "
              f"-p secret/24969/{env}/k8s/artifactory -a /pki/ca-trust/cacerts.pem "
              f"-f https://edge-{env}-mtls.ai.ms.com.cn")
    text = text.replace(
        f"          image: {image}\n",
        f"          image: {image}\n"
        "          command:\n"
        "            - /bin/sh\n"
        "            - -c\n"
        "            - |\n"
        f"              {script}\n",
        1,
    )
    if baseline:
        text = text.replace(
            "          env:\n",
            "          volumeMounts:\n"
            "            - name: pki-ca-trust\n"
            "              mountPath: /pki/ca-trust\n"
            "          env:\n",
            1,
        )
    text = text.replace("legacy-risk-engine-secret", "pki-proid-ops-auto-password")
    secret_suffix = "vps" if baseline else env
    trust_volume = (
        "        - name: pki-ca-trust\n"
        "          secret:\n"
        "            secretName: pki-ca-trust\n"
    ) if baseline else ""
    volumes = (
        "      volumes:\n"
        f"{trust_volume}"
        "        - name: pki-ssl\n"
        "          secret:\n"
        f"            secretName: pki-ssl-china-ops-central-{secret_suffix}\n"
    )
    text = text.replace("---\n# Source: risk-engine/templates/service.yaml",
                        volumes + "---\n# Source: risk-engine/templates/service.yaml", 1)
    namespace_alias = {
        "dev": "panda-automation-dev",
        "qa": "panda-automation-qa",
        "prod": "ops-automation-prod",
    }[env]
    current_secret = f"pki-ssl-china-ops-central-{env}"
    baseline_secret = "pki-ssl-china-ops-central-vps"
    ingress_secret = baseline_secret if baseline else current_secret
    ingress = f"""
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: china-ops-central
  namespace: {namespace_alias}
  annotations: null
spec:
  tls:
    - hosts:
        - china-ops-central-{env}.srv.ms.com.cn
      secretName: {ingress_secret}
  rules:
    - host: china-ops-central-{env}.srv.ms.com.cn
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: risk-engine-api
                port:
                  number: 80
"""
    return text.rstrip() + ingress


def add_vault_pki_secret(text: str, env: str) -> str:
    namespace_alias = {
        "dev": "panda-automation-dev",
        "qa": "panda-automation-qa",
        "prod": "ops-automation-prod",
    }[env]
    dns_name = ("msbic-ops-data-service.srv.ms.com.cn" if env == "prod"
                else f"msbic-ops-data-service-{env}.srv.ms.com.cn")
    resource = f"""
---
apiVersion: secrets.msbic.io/v1
kind: VaultPKISecret
metadata:
  namespace: {namespace_alias}
  name: pki-ssl-data-service-{env}
spec:
  mount: pki
  role: server
  commonName: {dns_name}
  altNames:
    - {dns_name}
  format: pem
  privateKeyFormat: pkcs8
  expiryOffset: 360h
  namespace: msbic/core
  destination:
    create: true
    name: pki-ssl-data-service-{env}
    type: kubernetes.io/tls
"""
    return text.rstrip() + resource


def add_krb5_config(text: str, env: str, baseline: bool) -> str:
    namespace_alias = {
        "dev": "panda-automation-dev",
        "qa": "panda-automation-qa",
        "prod": "ops-automation-prod",
    }[env]
    if baseline:
        body = """[logging]
default = FILE:/var/log/krb5libs.log
kdc = FILE:/var/log/krb5kdc.log
admin_server = FILE:/var/log/kadmind.log

[libdefaults]
rdns = false
dns_lookup_realm = false
dns_lookup_kdc = true
kdc_timeout = 5000
max_retries = 1"""
        routing_body = f"""[service]
endpoint = auth-{env}.srv.ms.com.cn
mode = legacy"""
        review_body = """[limits]
request_timeout = 30
retry_count = 3"""
    else:
        body = """[logging]
default = FILE:/var/log/krb5libs.log
kdc = FILE:/var/log/krb5kdc.log
admin_server = FILE:/var/log/kadmind.log

[libdefaults]
default_realm = COD.MS.COM.CN
rdns = false
dns_lookup_realm = false
dns_lookup_kdc = false
kdc_timeout = 5000
max_retries = 1

[realms]
COD.MS.COM.CN = {
  kdc = cod.ms.com.cn
  admin_server = cod.ms.com.cn
}"""
        routing_body = f"""[service]
endpoint = auth-{env}-v2.srv.ms.com.cn
mode = legacy"""
        review_timeout = 45 if env == "qa" else 30
        review_body = f"""[limits]
request_timeout = {review_timeout}
retry_count = 3"""
    indented = "\n".join(f"    {line}" for line in body.splitlines())
    routing_indented = "\n".join(f"    {line}" for line in routing_body.splitlines())
    review_indented = "\n".join(f"    {line}" for line in review_body.splitlines())
    resource = f"""
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: krb5-config
  namespace: {namespace_alias}
data:
  EMPTY_VALUE:
  krb5.conf: |-
{indented}
  routing.conf: |-
{routing_indented}
  review.conf: |-
{review_indented}
"""
    return text.rstrip() + resource


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
            if module == "risk-engine":
                current_helm = add_pki_deployment_scenario(current_helm, env, baseline=False)
                current_helm = add_vault_pki_secret(current_helm, env)
                current_helm = add_krb5_config(current_helm, env, baseline=False)
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
            if module == "notification":
                previous_env = {"dev": "qa", "qa": "prod", "prod": "dev"}[env]
                baseline_helm = (baseline_helm
                                 .replace(f"notification-api-{env}", f"notification-api-{previous_env}")
                                 .replace(f"notification-config-{env}", f"notification-config-{previous_env}"))
            if module != "notification":
                baseline_helm = apply_name_scenario(baseline_helm, module, env, suffix)
            if module == "risk-engine":
                baseline_helm = add_pki_deployment_scenario(baseline_helm, env, baseline=True)
                baseline_helm = add_krb5_config(baseline_helm, env, baseline=True)
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
                automation_alias = {
                    "dev": "panda-automation-dev",
                    "qa": "panda-automation-qa",
                    "prod": "ops-automation-prod",
                }[env]
                current_app += ("\nenvironment-transition-demo:\n"
                                "  environment-to-fixed: service-common\n"
                                f"  fixed-to-environment: service-{env}\n"
                                "  optional-environment-suffix: reconciliation\n"
                                f"  automation-alias: {automation_alias}\n"
                                "one-sided-demo:\n"
                                "  added-fixed: enabled\n"
                                f"  added-environment: service-{env}\n")
                baseline_app += ("\nenvironment-transition-demo:\n"
                                 f"  environment-to-fixed: service-{env}\n"
                                 "  fixed-to-environment: service-common\n"
                                 f"  optional-environment-suffix: reconciliation-{env}\n"
                                 "  automation-alias: automation\n"
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

    # A Helm-only application proves the report does not require App Config
    # files in order to expose a module and its two Helm comparison tabs.
    for label, env, _version, replicas, _image, port, memory, feature, suffix in envs:
        module = "worker"
        current_version = "1.1.0"
        baseline_version = "1.0.0"
        current_helm = module_helm(module, env, current_version, replicas, current_version,
                                   port + 40, memory, feature, suffix)
        baseline_helm = module_helm(module, env, baseline_version, replicas, baseline_version,
                                    port + 40, memory, feature, suffix, baseline=True)
        write(f"current/helm/{label}/{module}__{current_version}.yaml", current_helm)
        write(f"baseline/helm/{label}/{module}__{baseline_version}.yaml", baseline_helm)

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
