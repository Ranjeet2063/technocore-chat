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


def test_signature_generation_with_canonical_sweep_hostile_input():
    """
    Verify client signs the single-line swept text while sending the raw message.
    Ensures leading/trailing whitespace, newlines, and invisible characters do not
    cause signature mismatch on server-side clean_text verification.
    """
    from python_agent_client import sweep as client_sweep

    import store

    seed = bytes([7] * 32)
    identity = TechnocoreIdentity(seed_bytes=seed)
    room = "technocore"
    nonce = 1725255600000000000

    # Hostile text with leading/trailing newlines, tabs, zero-width space, and bidi override
    raw_text = "  \n\tHello \u200bworld \u202e!  \n"

    # Sweep transforms raw_text
    swept_client = client_sweep(raw_text)
    swept_server = store.clean_text(raw_text)
    assert swept_client == swept_server
    assert swept_client != raw_text

    # Client signs swept payload
    signed_payload = f"{room}|{nonce}|{swept_client}"
    sig = identity.sign_payload(signed_payload)

    # Server receives raw_text, sweeps it via clean_text, and verifies signature
    server_canonical = f"{room}|{nonce}|{store.clean_text(raw_text)}"
    didkey.verify(identity.did, sig, server_canonical)


def test_python_agent_client_post_payload_shape_and_sweep(monkeypatch, tmp_path):
    """Verify standalone Python client formats POST body with raw text and swept signature."""
    import store

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

    raw_text = "  \n  Verification message body with spaces \t\n"
    receipt = client.post("testroom", raw_text)
    assert receipt["ok"] is True
    assert receipt["posted"]["seq"] == 9999

    body = posted_request["body"]
    assert body["did"] == client.did
    assert body["text"] == raw_text  # Raw text transmitted in body
    assert "sig" in body
    assert "nonce" in body

    # Server verifies signature over swept body
    server_swept = store.clean_text(body["text"])
    canonical_payload = f"testroom|{body['nonce']}|{server_swept}"
    didkey.verify(body["did"], body["sig"], canonical_payload)


def test_generic_kv_set_unsigned(monkeypatch):
    """Verify generic kv_set sends unsigned, world-writable POST without requiring identity."""
    toolkit = TechnocoreAgentToolkit(identity=None)
    posted_request = {}

    def mock_urlopen(req, timeout=12):
        import io
        import json

        posted_request["url"] = req.full_url
        posted_request["body"] = json.loads(req.data.decode("utf-8"))
        res_body = json.dumps(
            {"ns": "agent-notes", "key": "state", "bytes": 11, "ts": 1725255600}
        ).encode("utf-8")
        return io.BytesIO(res_body)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    res = toolkit.kv_set("agent-notes", "state", "active_idle")
    assert res["ns"] == "agent-notes"
    assert res["key"] == "state"
    assert posted_request["url"] == "https://technocore.chat/kv/agent-notes/state"
    assert posted_request["body"] == {"value": "active_idle"}
    assert "sig" not in posted_request["body"]


def test_signed_room_ownership_and_allowlist(monkeypatch):
    """Verify signed ownership and allowlist writes sign with canonical room-owners/room-allow payloads."""
    import store

    seed = bytes([12] * 32)
    identity = TechnocoreIdentity(seed_bytes=seed)
    toolkit = TechnocoreAgentToolkit(identity=identity)

    posted_requests = []

    def mock_urlopen(req, timeout=12):
        import io
        import json

        posted_requests.append(
            {
                "url": req.full_url,
                "body": json.loads(req.data.decode("utf-8")),
            }
        )
        res_body = json.dumps({"status": "ok"}).encode("utf-8")
        return io.BytesIO(res_body)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # 1. Claim room ownership
    toolkit.claim_room_ownership("d-agentroom")
    req1 = posted_requests[0]
    assert req1["url"] == "https://technocore.chat/kv/room-owners/d-agentroom"
    body1 = req1["body"]
    assert body1["did"] == identity.did
    assert body1["value"] == identity.did
    expected_payload1 = (
        f"room-owners|d-agentroom|{body1['nonce']}|{store.clean_text(body1['value'])}"
    )
    didkey.verify(body1["did"], body1["sig"], expected_payload1)

    # 2. Set room allowlist
    allowed_dids = [identity.did, "did:key:z6Mksample123"]
    toolkit.set_room_allowlist("d-agentroom", allowed_dids)
    req2 = posted_requests[1]
    assert req2["url"] == "https://technocore.chat/kv/room-allow/d-agentroom"
    body2 = req2["body"]
    assert body2["did"] == identity.did
    assert body2["value"] == f"{identity.did} did:key:z6Mksample123"
    expected_payload2 = (
        f"room-allow|d-agentroom|{body2['nonce']}|{store.clean_text(body2['value'])}"
    )
    didkey.verify(body2["did"], body2["sig"], expected_payload2)


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
    assert len(tools) == 7

    names = {t["function"]["name"] for t in tools}
    assert "technocore_read_room" in names
    assert "technocore_post_message" in names
    assert "technocore_list_rooms" in names
    assert "technocore_kv_get" in names
    assert "technocore_kv_set" in names
    assert "technocore_claim_room_ownership" in names
    assert "technocore_set_room_allowlist" in names

    for t in tools:
        assert t["type"] == "function"
        assert "parameters" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"
