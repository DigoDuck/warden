# The image the hardened sandbox (warden/sandbox/docker.py) starts containers from.
#
# The sandbox has no network (ADR-004), so pip install inside it is impossible: whatever a
# tool needs at run time has to already be in the image. That is a real limitation, not a
# convenience. This image serves repositories whose dependencies are the ones baked in below;
# installing the dependencies of an arbitrary target repo from outside the sandbox is
# post-MVP (briefing [EVO]).
#
# git is here for commands a future apply_patch / commit tool may run, not for network
# access: with network_mode=none, git fetch/clone/push simply fail like everything else.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Covers the allowlisted commands in policies/default.yaml (pytest, ruff, mypy) and the
# dependencies of examples/target-repo (fastapi, httpx, pydantic), so the demo target
# repository's own test suite can run inside the sandbox with no network.
#
# Pinned, not floating: an unpinned `pip install` here makes `make sandbox-image` and the CI
# build non-reproducible, since a new pytest/ruff release upstream can turn a green branch
# red with no commit of ours to point at. Versions below are what was actually verified
# working (`pip freeze` inside the built image); bump them deliberately, not by surprise.
RUN pip install --no-cache-dir \
    pytest==9.1.1 \
    ruff==0.16.8 \
    mypy==2.3.1 \
    fastapi==0.141.1 \
    httpx==0.28.1 \
    pydantic==2.13.5
