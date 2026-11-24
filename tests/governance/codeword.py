from __future__ import annotations

import hashlib

CODEWORD_NAMESPACE = "stagewhisper-governance-demo"


def derive_codeword(label: str, index: int) -> str:
    digest = hashlib.sha256(
        f"{CODEWORD_NAMESPACE}:{index}:{label.strip().lower()}".encode("utf-8")
    ).hexdigest()
    return f"CODEWORD-{digest[:16].upper()}"
