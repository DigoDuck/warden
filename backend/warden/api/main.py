"""Production entry point: `uvicorn warden.api.main:app` (the `make api` target).

Wires the two things `create_app` takes as arguments in tests: the real signing key from
disk (`identity.load_keys()`, refuses to start without one, run `make keys`) and a session
factory over the configured database.
"""

from warden.api.app import create_app
from warden.config import get_settings
from warden.db import make_engine, make_session_factory
from warden.identity import load_keys

_settings = get_settings()

app = create_app(
    session_factory=make_session_factory(make_engine(_settings.database_url)),
    keys=load_keys(_settings),
)
