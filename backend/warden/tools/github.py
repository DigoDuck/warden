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
# `warden/` prefix marks it as this control plane's. Updates are fast-forward only (see
# `open_pr`), so even a branch someone else pushed under this prefix is never rewritten.
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
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    missing_ok: bool = False,
    **kwargs: Any,
) -> Any:
    """One HTTP call, GitHub's error body surfaced as a `ToolError` the model can react to
    instead of an `httpx` exception taking the whole task down. Never includes the token:
    `headers` is never part of `httpx.HTTPStatusError`'s own message, only the method, URL and
    status, and GitHub's JSON error body is a "not authorized"/"not found" sentence, never an
    echo of the credential that failed.
    """
    try:
        response = await client.request(method, url, headers=headers, **kwargs)
        if missing_ok and response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"github.open_pr: {method} {url} failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"github.open_pr: could not reach GitHub: {exc}") from exc
    return response.json() if response.content else None


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
    # Commit the `credential.granted` row now, before any sandbox read or HTTP call:
    # `audit.append` holds the audit chain's advisory lock until this transaction ends, and
    # every other task's audit write would otherwise queue behind GitHub's latency. A refusal
    # above needs no commit here: its row is flushed and the loop's checkpoint (d) commits it.
    await context.checkpoint()

    owner, _, repo = settings.github_repo.partition("/")
    branch = _branch_name(task_id, args.branch_slug)
    headers = {
        "Authorization": f"Bearer {credential.reveal()}",
        "Accept": "application/vnd.github+json",
    }
    repo_url = f"{settings.github_api_url}/repos/{owner}/{repo}"

    async def _run(http: httpx.AsyncClient) -> str:
        # Build on the task's branch when it already exists, on the base branch otherwise.
        # Never a force-update: rebuilding on the base and force-moving the branch (what an
        # earlier version did) silently dropped every file an earlier call of this same task
        # had published from a PR that may already be under review. A fast-forward from the
        # branch's own head keeps them, and the real API refuses anything else (422).
        head = await _request(
            http, "GET", f"{repo_url}/git/ref/heads/{branch}", headers, missing_ok=True
        )
        if head is None:
            base_ref = await _request(http, "GET", f"{repo_url}/git/ref/heads/{args.base}", headers)
            parent_sha = base_ref["object"]["sha"]
        else:
            parent_sha = head["object"]["sha"]
        parent = await _request(http, "GET", f"{repo_url}/git/commits/{parent_sha}", headers)
        parent_tree_sha = parent["tree"]["sha"]

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
            json={"base_tree": parent_tree_sha, "tree": tree_entries},
        )
        # Trees are content-addressed: the same files on the same parent give back the
        # parent's own tree. That is a replay (ADR-019's at-least-once window: GitHub
        # answered, the process died before `tool.executed` committed), and it leaves the
        # branch exactly where it is instead of stacking an empty commit per retry.
        if head is None or tree["sha"] != parent_tree_sha:
            commit = await _request(
                http,
                "POST",
                f"{repo_url}/git/commits",
                headers,
                json={
                    "message": f"warden: {args.title}",
                    "tree": tree["sha"],
                    "parents": [parent_sha],
                },
            )
            if head is None:
                await _request(
                    http,
                    "POST",
                    f"{repo_url}/git/refs",
                    headers,
                    json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
                )
            else:
                await _request(
                    http,
                    "PATCH",
                    f"{repo_url}/git/refs/heads/{branch}",
                    headers,
                    json={"sha": commit["sha"], "force": False},
                )

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
