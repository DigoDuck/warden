"""github.open_pr: publish files from the task's sandbox as a pull request (ADR-025).

Everything reaches GitHub through its REST Git Data API over plain HTTP, from the control
plane's own process. Never `git` (no CLI, no on-disk checkout of the target repo, nothing
that would need the token as an environment variable or a command-line argument a process
list could show), and never the sandbox: the container has no network at all (ADR-004),
which is the whole point of a gateway tool existing in the first place.

The credential is brokered per call (`identity.broker.get_credential`), scoped to
`github:pr:open`, and is only ever read once, into the `Authorization` header of this
module's own `httpx.AsyncClient` calls. It is never logged, never put in an exception
message this module raises itself, and if GitHub's own error body ever echoed it back
somehow, `core/events.py`'s `broker.redact()` choke point is what would still keep it out of
the event log.
"""

from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from warden.config import Settings
from warden.identity import broker
from warden.sandbox.docker import Sandbox
from warden.tools.registry import ToolContext, ToolError
from warden.tools.sandboxed import ReadFileArgs, read_file

# refs/heads/warden/<task_id[:8]>-<slug>: short enough to read in a branch list, and the
# `warden/` prefix is this control plane's own namespace, never touched by anything else, so
# force-updating a ref under it is always safe (see `_upsert_ref`).
_BRANCH_PREFIX = "warden"


class OpenPrArgs(BaseModel):
    title: str = Field(description="The pull request's title.")
    body: str = Field(description="A short summary of what changed and why.")
    branch_slug: str = Field(
        pattern=r"^[a-z0-9]([a-z0-9-]{0,40})$",
        description="Lowercase, hyphenated identifier for the branch, e.g. 'fix-timeout'.",
    )
    paths: list[str] = Field(
        min_length=1,
        description="Workspace-relative files to publish, read from the task's sandbox.",
    )
    base: str = Field(default="main", description="Branch to open the pull request against.")


async def open_pr_paths(args: OpenPrArgs) -> list[str]:
    """The inspector (ADR-017's own pattern, reused): every path this call would publish is
    judged by the policy engine exactly like `apply_patch`'s paths are, so a deny on any one
    of them blocks the whole call before a single HTTP request is made (`core/loop.py`'s
    `_decide`/`combine`)."""
    return list(args.paths)


def _branch_name(task_id: UUID, slug: str) -> str:
    return f"{_BRANCH_PREFIX}/{str(task_id)[:8]}-{slug}"


def _pr_report(task_id: UUID | None, paths: list[str], summary: str) -> str:
    files = "\n".join(f"- `{path}`" for path in paths)
    return f"**Task:** `{task_id}`\n\n**Files published:**\n{files}\n\n---\n\n{summary.strip()}\n"


async def _request(
    client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str], **kwargs: Any
) -> Any:
    """One HTTP call, GitHub's error body surfaced as a `ToolError` the model can react to
    instead of an `httpx` exception taking the whole task down. Never includes the token:
    `headers` is never part of `httpx.HTTPStatusError`'s own message, only the method, URL and
    status, and GitHub's JSON error body is a "not authorized"/"not found" sentence, never an
    echo of the credential that failed.
    """
    try:
        response = await client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"github.open_pr: {method} {url} failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"github.open_pr: could not reach GitHub: {exc}") from exc
    return response.json() if response.content else None


async def _upsert_ref(
    client: httpx.AsyncClient, headers: dict[str, str], ref_url: str, sha: str
) -> None:
    """Create the branch, or move it to `sha` if it already exists.

    `force: true` on the update is safe only because `_BRANCH_PREFIX` is this control plane's
    own namespace: nothing else ever pushes to a `warden/*` branch, so there is no history on
    it a force-update could ever discard that this same tool did not itself just supersede.
    """
    existing = await client.get(ref_url, headers=headers)
    if existing.status_code == 404:
        base, _, ref_path = ref_url.rpartition("/git/ref/")
        await _request(
            client,
            "POST",
            f"{base}/git/refs",
            headers,
            json={"ref": f"refs/{ref_path}", "sha": sha},
        )
        return
    existing.raise_for_status()
    await _request(client, "PATCH", ref_url, headers, json={"sha": sha, "force": True})


async def open_pr(
    sandbox: Sandbox,
    settings: Settings,
    args: OpenPrArgs,
    context: ToolContext,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """The executor. `context` (ADR-025's identity-context flag) carries the claims the
    gateway already verified and the loop's own session, so `broker.get_credential` spends
    exactly the credential this one call was authorised for, never a cached or ambient one.
    """
    task_id = context.claims.task_id
    if task_id is None:
        # Cannot happen through the gateway (it only ever hands a task-bound token to a
        # tool), but the branch name is task-derived, so failing loudly here beats a branch
        # named `warden/None-<slug>` if this is ever called some other way.
        raise ToolError("github.open_pr requires a task-bound token")

    try:
        credential = await broker.get_credential(
            context.session, context.claims, "github:pr:open", settings=settings
        )
    except (broker.CredentialDenied, broker.UnknownScope, broker.SecretNotConfigured) as exc:
        raise ToolError(f"github.open_pr: credential denied: {exc}") from exc

    owner, _, repo = settings.github_repo.partition("/")
    branch = _branch_name(task_id, args.branch_slug)
    headers = {
        "Authorization": f"Bearer {credential.reveal()}",
        "Accept": "application/vnd.github+json",
    }
    repo_url = f"{settings.github_api_url}/repos/{owner}/{repo}"

    async def _run(http: httpx.AsyncClient) -> str:
        base_ref = await _request(http, "GET", f"{repo_url}/git/ref/heads/{args.base}", headers)
        base_sha = base_ref["object"]["sha"]
        base_commit = await _request(http, "GET", f"{repo_url}/git/commits/{base_sha}", headers)
        base_tree_sha = base_commit["tree"]["sha"]

        tree_entries = []
        for path in args.paths:
            content = await read_file(sandbox, ReadFileArgs(path=path))
            blob = await _request(
                http,
                "POST",
                f"{repo_url}/git/blobs",
                headers,
                json={"content": content, "encoding": "utf-8"},
            )
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        tree = await _request(
            http,
            "POST",
            f"{repo_url}/git/trees",
            headers,
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        )
        commit = await _request(
            http,
            "POST",
            f"{repo_url}/git/commits",
            headers,
            json={"message": f"warden: {args.title}", "tree": tree["sha"], "parents": [base_sha]},
        )
        await _upsert_ref(http, headers, f"{repo_url}/git/ref/heads/{branch}", commit["sha"])

        # Idempotency (ADR-025): same task -> same branch -> the same open PR is returned
        # instead of a second one being opened. `head` is qualified with the owner, GitHub's
        # own convention for a same-repository branch in this query.
        existing = await _request(
            http,
            "GET",
            f"{repo_url}/pulls",
            headers,
            params={"head": f"{owner}:{branch}", "state": "open"},
        )
        existing = []  # TEMP: idempotency check disabled to prove the RED test for it
        if existing:
            pr = existing[0]
        else:
            pr = await _request(
                http,
                "POST",
                f"{repo_url}/pulls",
                headers,
                json={
                    "title": args.title,
                    "head": branch,
                    "base": args.base,
                    "body": _pr_report(task_id, args.paths, args.body),
                },
            )
        return f"opened PR #{pr['number']}: {pr['html_url']}"

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient() as owned_client:
        return await _run(owned_client)
