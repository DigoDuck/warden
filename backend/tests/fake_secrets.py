"""Credential-shaped strings for tests, assembled at runtime.

They match the detectors in `identity/broker.py::SECRET_PATTERNS` on purpose, which is the whole
point of the tests that use them. Written out in one piece in a source file they would also match
every secret scanner that reads this repository (gitleaks in CI), so each is built from parts that
are not credential-shaped on their own.
"""

import base64
import json


def github_pat() -> str:
    return "gh" + "p_" + "Zx9Qw3Er7Ty1Ui5Op2As4Df6Gh8Jk0Lm3Nb7"


def github_fine_grained() -> str:
    return "github" + "_pat_" + "11AAAAAAA0abcdefghijkl_" + "mnopqrstuvwxyz0123456789ABCDEFGHIJ"


def anthropic_key() -> str:
    return "sk" + "-ant-" + "api03-" + "Qz8Lm2Vn5Bx7Cr4Dt1Fy6Gu9Hi3Jo0Kp"


def aws_access_key() -> str:
    return "AK" + "IA" + "IOSFODNN7EXAMPLE"


def jwt() -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return ".".join([part({"alg": "RS256", "typ": "JWT"}), part({"sub": "agent:1"}), "c2lnbmF0dXJl"])


def pem_private_key() -> str:
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 3
    return "-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END " + "PRIVATE KEY-----"
