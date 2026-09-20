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
RUN pip install --no-cache-dir \
    pytest \
    ruff \
    mypy \
    fastapi \
    httpx \
    pydantic
