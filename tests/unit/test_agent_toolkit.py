"""
Unit tests for Technocore AI Agent Toolkit
Ensures deterministic DID derivations, signing consistency, and tool schema validity.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add examples directory to path for import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "examples"))

import pytest
from technocore_agent_toolkit import (
    AgentMessage,
    TechnocoreAgentToolkit,
    TechnocoreIdentity,
    _base58btc_encode,
)


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


def test_signature_generation():
    """Verify cryptographic signatures are non-empty and URL-safe."""
    seed = bytes([7] * 32)
    identity = TechnocoreIdentity(seed_bytes=seed)
    payload = "technocore\n1000\nHello decentralized world"
    sig = identity.sign_payload(payload)

    assert isinstance(sig, str)
    assert len(sig) > 0
    assert not sig.endswith("=")  # URL-safe stripped padding


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
