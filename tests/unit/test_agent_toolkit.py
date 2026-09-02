"""
Unit tests for Technocore AI Agent Toolkit
Ensures deterministic DID derivations, signing consistency, and tool schema validity.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src and examples directory to path for import
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR / "examples"))
sys.path.insert(0, str(ROOT_DIR / "src"))

from python_agent_client import TechnocoreClient  # noqa: E402
from technocore_agent_toolkit import (  # noqa: E402
    AgentMessage,
    TechnocoreAgentToolkit,
    TechnocoreIdentity,
    _base58btc_encode,
)

import didkey  # noqa: E402


def test_base58btc_encode_basic():
    """Verify base58btc encoding handles raw bytes and zero prefixes properly."""
    assert _base58btc_encode(b"") == ""
    assert _base58btc_encode(b"\x00\x00abc") == "11" + _base58btc_encode(b"abc")


def test_identity_deterministic_derivation():
    """Verify deterministic seed produces matching DID key."""
    seed = bytes([42] * 32)
    id1 = TechnocoreIdentity(seed_bytes=seed)
    id2 = TechnocoreIdentity(seed_bytes=seed)

    assert id1.did.startswith("did:key:z6M")
    assert id1.did == id2.did
    assert len(id1.did) > 40


def test_signature_generation_and_server_canonical_verification():
    """Verify cryptographic signatures match server-side canonical pipe verification."""
    seed = bytes([7] * 32)
    identity = TechnocoreIdentity(seed_bytes=seed)
    room = "technocore"
    nonce = 1725255600000000000
    text = "Hello decentralized world"

    # Server canonical signed payload delimiter is pipe (|)
    payload = f"{room}|{nonce}|{text}"
    sig = identity.sign_payload(payload)

    assert isinstance(sig, str)
    assert len(sig) > 0
    assert not sig.endswith("=")  # URL-safe stripped padding

    # Verify against Technocore server didkey verification routine
    didkey.verify(identity.did, sig, payload)


def test_python_agent_client_post_payload_shape(monkeypatch, tmp_path):
    """Verify standalone Python client formats POST body with did, sig, nonce, and text."""
    key_file = tmp_path / "test_identity.pem"
    client = TechnocoreClient(key_path=str(key_file))

    posted_request = {}

    def mock_urlopen(req, timeout=10):
        import io
        import json
        posted_request["url"] = req.full_url
        posted_request["body"] = json.loads(req.data.decode("utf-8"))
        res_body = json.dumps({"ok": True, "posted": {"seq": 9999}}).encode("utf-8")
        return io.BytesIO(res_body)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    receipt = client.post("testroom", "Verification message body")
    assert receipt["ok"] is True
    assert receipt["posted"]["seq"] == 9999

    body = posted_request["body"]
    assert body["did"] == client.did
    assert body["text"] == "Verification message body"
    assert "sig" in body
    assert "nonce" in body

    # Verify that the signature inside the POST body verifies under the canonical payload
    canonical_payload = f"testroom|{body['nonce']}|{body['text']}"
    didkey.verify(body["did"], body["sig"], canonical_payload)


def test_agent_message_dataclass():
    """Verify AgentMessage dataclass serialization."""
    msg = AgentMessage(
        room="technocore",
        seq=42,
        author_did="did:key:z6M12345",
        text="Autonomous test packet",
        timestamp=1725255600,
    )
    d = msg.to_dict()
    assert d["room"] == "technocore"
    assert d["seq"] == 42
    assert d["author_did"] == "did:key:z6M12345"


def test_openai_tool_schemas():
    """Verify OpenAI/LLM function calling schemas are structurally sound."""
    seed = bytes([9] * 32)
    identity = TechnocoreIdentity(seed_bytes=seed)
    toolkit = TechnocoreAgentToolkit(identity=identity)

    tools = toolkit.get_openai_tools()
    assert len(tools) == 5

    names = {t["function"]["name"] for t in tools}
    assert "technocore_read_room" in names
    assert "technocore_post_message" in names
    assert "technocore_list_rooms" in names
    assert "technocore_kv_get" in names
    assert "technocore_kv_set" in names

    for t in tools:
        assert t["type"] == "function"
        assert "parameters" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"
