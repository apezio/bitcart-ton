"""Compose generator inputs: the rule only appends our mount; the component has the expected shape."""

import copy
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MOUNT = "./plugins/docker/ton/backend/forkedton:/app/modules/forkedton:ro"


def load_rule():
    # same way the generator loads plugin rules: a module from a file path, then rule(services, settings)
    spec = importlib.util.spec_from_file_location("plugins.ton.rules.90_ton_modules", ROOT / "rules/90_ton_modules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.rule


def test_rule_appends_only():
    existing = [
        "bitcart_datadir:/datadir",
        "./plugins/docker:/plugins/docker:ro",
        "./plugins/docker/solana/backend/forked:/app/modules/forked:ro",
    ]
    services = {
        "backend": {"image": "bitcart/bitcart:0.10.3.0", "volumes": list(existing)},
        "worker": {"image": "bitcart/bitcart:0.10.3.0", "volumes": list(existing)},
        "database": {"image": "postgres", "volumes": ["dbdata:/var/lib/postgresql/data"]},
    }
    before = copy.deepcopy(services)
    load_rule()(services, None)
    for name in ("backend", "worker"):
        assert services[name]["volumes"] == [*existing, MOUNT]
        assert {k: v for k, v in services[name].items() if k != "volumes"} == {"image": "bitcart/bitcart:0.10.3.0"}
    assert services["database"] == before["database"]


def test_rule_without_volumes_or_services():
    services = {"backend": {}}
    load_rule()(services, None)
    assert services == {"backend": {"volumes": [MOUNT]}}


def test_component():
    data = yaml.safe_load((ROOT / "components/ton.yml").read_text())
    ton = data["services"]["ton"]
    assert ton["image"] == "forked/bitcart-ton:0.10.3.0"
    assert ton["pull_policy"] == "build"
    assert ton["build"]["context"] == "./plugins/docker/ton/daemon"
    assert ton["environment"] == {
        "TON_NETWORK": "${TON_NETWORK:-mainnet}",
        "TON_SERVER": "${TON_SERVER:-https://toncenter.com/api/v3}",
        "TON_DEBUG": "${TON_DEBUG:-false}",
    }
    assert ton["expose"] == ["5013"]
    assert ton["volumes"] == ["ton_datadir:/data"]
    for name in ("backend", "worker"):
        assert data["services"][name]["environment"] == {"TON_HOST": "ton", "TON_NETWORK": "${TON_NETWORK:-mainnet}"}
    assert data["services"]["backend"]["depends_on"] == ["ton"]
    assert data["services"]["backend"]["links"] == ["ton"]
    assert "ton_datadir" in data["volumes"]
