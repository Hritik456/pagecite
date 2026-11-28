"""docker-compose.prod.yml keeps the P5B constraints: fits 2 GB, restarts itself, rotates logs, exposes
only Caddy, gives secrets only to the services that need them."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
LONG_RUNNING = {"caddy", "api", "relay", "worker", "mysql", "redis"}


def mib(limit: str) -> int:
    m = re.fullmatch(r"(\d+)([mg])", str(limit).lower())
    assert m, limit
    return int(m.group(1)) * (1024 if m.group(2) == "g" else 1)


def test_services_are_exactly_the_prod_set():
    assert set(SERVICES) == LONG_RUNNING | {"flyway"}  # no MinIO, no flyway-test
    assert SERVICES["flyway"]["profiles"] == ["migrate"] and SERVICES["flyway"]["restart"] == "no"


@pytest.mark.parametrize("name", sorted(LONG_RUNNING))
def test_every_service_restarts_has_a_memory_limit_and_rotated_logs(name):
    svc = SERVICES[name]
    assert svc["restart"] == "unless-stopped"
    assert mib(svc["mem_limit"]) > 0
    assert svc["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}


def test_memory_limits_fit_the_host():
    # The six long-running services: 2,046 MiB of limits on a 2,048 MiB host (+2 GiB swap); steady use is
    # ~0.8 GB. Flyway (320 MiB) runs only for a few seconds during a deploy, on top of that, into swap.
    long_running = sum(mib(SERVICES[n]["mem_limit"]) for n in LONG_RUNNING)
    assert long_running <= 2048
    assert long_running + mib(SERVICES["flyway"]["mem_limit"]) <= 2048 + 1024  # never more than half the swap
    assert mib(SERVICES["redis"]["mem_limit"]) >= 2 * 128  # AOF-rewrite fork headroom over maxmemory


def test_only_caddy_publishes_ports():
    assert [n for n, s in SERVICES.items() if s.get("ports")] == ["caddy"]
    assert SERVICES["caddy"]["ports"] == ["80:80", "443:443", "443:443/udp"]


def test_secrets_only_where_needed():
    with_env_file = sorted(n for n, s in SERVICES.items() if "env_file" in s)
    assert with_env_file == ["api", "relay", "worker"]
    assert set(SERVICES["caddy"]["environment"]) == {"SITE_ADDRESS", "DEMO_ASK_KEY"}


def test_worker_consumes_all_stage_queues_with_one_process():
    cmd = SERVICES["worker"]["command"]
    assert cmd[cmd.index("-Q") + 1] == "parse,chunk,embed,index"
    assert cmd[cmd.index("-c") + 1] == "1"
    assert "--max-tasks-per-child" in cmd and "--max-memory-per-child" in cmd


def test_small_mysql_and_bounded_redis_and_one_gunicorn_worker():
    mysql = SERVICES["mysql"]["command"]
    assert "--performance-schema=OFF" in mysql and "--innodb-buffer-pool-size=128M" in mysql
    redis = SERVICES["redis"]["command"]
    assert redis[redis.index("--maxmemory") + 1] == "128mb"
    assert redis[redis.index("--maxmemory-policy") + 1] == "noeviction"
    assert SERVICES["api"]["environment"]["WEB_CONCURRENCY"] == "1"


def test_images_are_pinned_to_the_deployed_commit():
    for name in ("caddy", "api", "relay", "worker", "flyway"):
        assert SERVICES[name]["image"].startswith("ghcr.io/hritik456/pagecite-")
        assert ":${IMAGE_TAG:?" in SERVICES[name]["image"]
