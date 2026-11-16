"""部署文件的静态安全契约，不构建镜像或访问外网。"""
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def requirements(path):
    # 依赖清单只提取声明，注释和 -r 引用不是一个额外软件包。
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-r"))}


def test_production_and_dev_requirements_match_project_metadata_with_upper_bounds():
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = requirements(BACKEND / "requirements.txt")
    dev = requirements(BACKEND / "requirements-dev.txt")
    assert runtime == set(project["dependencies"])
    assert dev == set(project["optional-dependencies"]["dev"])
    assert all(">=" in value and ",<" in value for value in runtime | dev)
    assert not any(value.lower().startswith(("pytest", "respx")) for value in runtime)
    assert "-r requirements.txt" in (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")


def test_compose_loopback_port_and_trusted_proxy_override_are_preserved():
    # 明确空字符串也表示不信任代理，不能被 Compose 默认值覆盖。
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${MQ_PORT:-8000}:8000"' in compose
    assert "MQ_TRUSTED_PROXY_CIDRS=${MQ_TRUSTED_PROXY_CIDRS-" in compose
    assert "MQ_TRUSTED_PROXY_CIDRS=${MQ_TRUSTED_PROXY_CIDRS:-" not in compose
    assert compose.count("stop_grace_period:") == compose.count("max-size:") == compose.count('max-file: "3"') == 2


def test_image_uses_nonroot_and_excludes_secrets_data_tests_and_dependency_caches():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert "USER 10001:10001" in dockerfile
    assert {".git", ".codegraph", ".env", "**/.venv", "**/data", "**/tests", "**/node_modules"} <= ignored


def test_nginx_auth_websocket_and_security_headers_cover_both_route_prefixes():
    # 路由前缀保持严格一致，WS 在发送到后端前剥离 /mq。
    config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    assert "location ~ ^/(mq/)?api/auth/" in config
    assert "location ~ ^/(mq/)?ws/" in config
    assert "rewrite ^/mq/(.*)$ /$1 break;" in config
    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert "proxy_set_header Connection $mq_connection_upgrade;" in config
    assert all(f"add_header {header} " in config for header in (
        "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Permissions-Policy",
    ))
    assert "#     listen 443 ssl;" in config
    # TLS 示例可直接承载相同安全边界，不依赖运维者自行拼接登录/WS 路由。
    tls = config[config.index("# server {"):]
    assert "#     location ~ ^/(mq/)?api/auth/" in tls
    assert "#     location ~ ^/(mq/)?ws/" in tls
    assert "#         proxy_set_header Connection $mq_connection_upgrade;" in tls
    assert "#         limit_req zone=mq_auth_limit burst=5 nodelay;" in tls
