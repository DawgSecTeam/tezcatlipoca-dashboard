"""The dynamic node catalog: masking, token retention, and the engine-facing node list."""

import json
import stat

import pytest

from dashboard import config, nodes


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    return tmp_path


def _node(name="node-a", **kw):
    base = {"name": name, "endpoint": "https://node-a.example:8006", "node": "pve1",
            "datastore": "store-a", "engine_base_vmid": 901,
            "token": "terraform@pam!comp-automation=secret"}
    base.update(kw)
    return base


def test_saved_catalog_is_0600_and_masks_tokens():
    nodes.save([_node()], {})
    assert stat.S_IMODE(nodes.catalog_path().stat().st_mode) == 0o600
    view = nodes.view(nodes.load())
    assert view["nodes"][0]["has_token"] is True
    assert "token" not in view["nodes"][0]


def test_resaving_a_masked_catalog_keeps_the_stored_token():
    nodes.save([_node()], {})
    masked = [{**n, "token": ""} for n in nodes.view(nodes.load())["nodes"]]
    nodes.save(masked, {})
    assert nodes.load()["nodes"][0]["token"].endswith("secret")


def test_run_list_carries_token_env_names_never_values():
    nodes.save([_node()], {"strategy": "capacity-fill"})
    run = nodes.run_list(nodes.load(), ["node-a"])
    assert run["nodes"][0]["token_env"] == "TEZ_NODE_TOKEN_NODE_A"
    assert "token" not in run["nodes"][0]
    assert "secret" not in json.dumps(run)
    assert run["balancing"] == {"strategy": "capacity-fill"}


def test_run_list_refuses_unknown_and_tokenless_nodes():
    nodes.save([_node()], {})
    with pytest.raises(ValueError, match="unknown node"):
        nodes.run_list(nodes.load(), ["nope"])
    nodes.save([_node("bare", token="")], {})
    with pytest.raises(ValueError, match="without an API token"):
        nodes.run_list(nodes.load(), ["bare"])


def test_token_env_hands_every_catalog_token_to_a_run():
    nodes.save([_node(), _node("node-b", endpoint="https://node-b.example:8006",
                               datastore="store-b", token="other")], {})
    env = nodes.token_env(nodes.load())
    assert env == {"TEZ_NODE_TOKEN_NODE_A": "terraform@pam!comp-automation=secret",
                   "TEZ_NODE_TOKEN_NODE_B": "other"}


def test_save_refuses_incomplete_or_duplicate_nodes():
    with pytest.raises(ValueError, match="missing 'endpoint'"):
        nodes.save([_node(endpoint="")], {})
    with pytest.raises(ValueError, match="duplicate node name"):
        nodes.save([_node(), _node()], {})
