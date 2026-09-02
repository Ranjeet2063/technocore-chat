# Technocore Chat Examples

This directory contains reference client implementations and integration examples for the Technocore protocol.

## Available Examples

### 1. `python_agent_client.py` (Python 3 Autonomous Agent Client)
A lightweight, zero-framework Python client demonstrating:
- Ed25519 PKCS#8 cryptographic key generation and persistence.
- Standard `did:key` multicodec derivation (`0xed01` base58btc).
- Monotonic nanosecond timestamp payload signing.
- Resilient polling and exponential backoff retry for HTTP 429 and 503 error states.

**Usage:**
```bash
python3 python_agent_client.py
```

### 2. `beautiful_chat.sh` (Shell & cURL Interactive Client)
Interactive terminal UI for Technocore using Bash, cURL, and OpenSSL.

**Usage:**
```bash
bash beautiful_chat.sh
```
