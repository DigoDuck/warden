"""Print the OpenAPI schema of the API app as JSON, for the frontend's typed client.

Owned by the frontend track (see frontend/README.md, `npm run gen:api`): it builds the same
`create_app` production uses, but with an ephemeral in-memory RSA `KeyPair` (never
`identity.load_keys()`, which reads a key file from disk) and a session factory bound to no
engine at all. Neither is a real request handled by this call: `app.openapi()` only walks the
route definitions and their Pydantic schemas to build the spec, it never opens a DB session or
touches the network. This mirrors how tests/test_api.py builds `keys` and `app` without a key
file or a live socket.
"""

import json
import sys

from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden.api.app import create_app
from warden.identity.jwt import KeyPair, _kid_for


def _ephemeral_keys() -> KeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=_kid_for(public_key))


def main() -> int:
    # bind=None: nothing here ever calls session_factory() to open a session, so there is
    # nothing to connect to. A real engine would need a running Postgres for no reason.
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=None, expire_on_commit=False
    )
    app = create_app(session_factory=session_factory, keys=_ephemeral_keys())
    json.dump(app.openapi(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
