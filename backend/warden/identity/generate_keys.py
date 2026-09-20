"""Generate the RSA key pair this control plane signs JWTs with. Run via `make keys`.

Not `openssl -genrsa`: CLAUDE.md's environment notes say it is not guaranteed to be on
Windows, and `cryptography` is already a dependency of `pyjwt[crypto]`, so this needs nothing
that is not installed already.
"""

import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from warden.config import get_settings
from warden.identity.jwt import _resolve_key_path

# 2048 bits: the RSA size PyJWT's own docs use for RS256 and still comfortably above the
# 2030-era NIST minimum recommendation. This is a dev-machine signing key, not a certificate
# meant to stay valid for a decade, 2048 is not a corner cut here.
KEY_SIZE = 2048


def main() -> int:
    path = _resolve_key_path(get_settings().jwt_private_key_path)
    if path.exists():
        print(f"refusing to overwrite existing key at {path}", file=sys.stderr)
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
