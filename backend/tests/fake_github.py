"""A tiny, stateful stand-in for the GitHub Git Data + Pulls API, shared by the tests that
open a pull request (test_github_tool.py, test_publish*.py): never the real network."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


class FakeGitHub:
    """A tiny, stateful stand-in for the Git Data + Pulls API.

    Every request is recorded (`self.requests`) so a test can inspect what warden actually
    sent, in particular the `Authorization` header, without ever making a real HTTP call:
    `httpx.MockTransport` calls `handler` in place of opening a socket.

    Content-addressed where it matters, like git: a tree's sha is derived from its full
    path -> blob mapping, so the same files on the same base give the same tree. Commits
    remember their parents, and a ref update without `force` is refused (422) unless it is a
    fast-forward, which is what the real API does.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refs: dict[str, str] = {"heads/main": "base-sha"}
        self.trees: dict[str, dict[str, str]] = {"base-tree": {}}
        self.commits: dict[str, dict[str, object]] = {
            "base-sha": {"tree": "base-tree", "parents": []}
        }
        self.pulls: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else {}

        if method == "GET" and "/git/ref/" in path:
            ref = path.split("/git/ref/", 1)[1]
            if ref in self.refs:
                return httpx.Response(200, json={"object": {"sha": self.refs[ref]}})
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "GET" and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            commit = self.commits[sha]
            return httpx.Response(200, json={"sha": sha, "tree": {"sha": commit["tree"]}})
        if method == "POST" and path.endswith("/git/blobs"):
            digest = hashlib.sha1(body["content"].encode()).hexdigest()[:12]
            return httpx.Response(201, json={"sha": f"blob-{digest}"})
        if method == "POST" and path.endswith("/git/trees"):
            files = dict(self.trees[body["base_tree"]])
            files.update({entry["path"]: entry["sha"] for entry in body["tree"]})
            digest = hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()[:12]
            self.trees[f"tree-{digest}"] = files
            return httpx.Response(201, json={"sha": f"tree-{digest}"})
        if method == "POST" and path.endswith("/git/commits"):
            sha = f"commit-{len(self.commits)}"
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"]}
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path.endswith("/git/refs"):
            ref = body["ref"].removeprefix("refs/")
            if ref in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[ref] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if method == "PATCH" and "/git/refs/" in path:
            ref = path.split("/git/refs/", 1)[1]
            parents = self.commits[body["sha"]]["parents"]
            if not body.get("force") and self.refs[ref] not in parents:  # type: ignore[operator]
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[ref] = body["sha"]
            return httpx.Response(200, json={"ref": f"refs/{ref}", "object": {"sha": body["sha"]}})
        if method == "GET" and path.endswith("/pulls"):
            head = request.url.params.get("head")
            head_branch = head.split(":", 1)[1] if head else None
            return httpx.Response(200, json=[p for p in self.pulls if p["_branch"] == head_branch])
        if method == "POST" and path.endswith("/pulls"):
            number = len(self.pulls) + 1
            pr = {
                "number": number,
                "html_url": f"https://github.com/acme/widgets/pull/{number}",
                "_branch": body["head"],
            }
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        raise AssertionError(f"unexpected request: {method} {request.url}")

    def branch_files(self, branch: str) -> dict[str, str]:
        return self.trees[str(self.commits[self.refs[f"heads/{branch}"]]["tree"])]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class FakeGitHubServer:
    """`FakeGitHub` behind a real loopback socket, for the one test whose client lives in
    another OS process (a worker that gets killed): a `MockTransport` cannot cross that line.

    `block_first` parks the first request until `release` is set. That is what makes "the
    worker dies while GitHub is being called" something a test can aim at instead of race.
    """

    def __init__(self, fake: FakeGitHub, *, block_first: bool = False) -> None:
        self.fake = fake
        self.first_request = threading.Event()
        self.release = threading.Event()
        self._block_first = block_first
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                request = httpx.Request(
                    self.command, f"http://127.0.0.1{self.path}", content=body or None
                )
                with outer._lock:
                    first = not outer.first_request.is_set()
                    outer.first_request.set()
                if first and outer._block_first:
                    outer.release.wait(timeout=180)
                with outer._lock:
                    response = outer.fake.handler(request)
                payload = response.content
                try:
                    self.send_response(response.status_code)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client was killed while it waited: nobody is listening any more

            do_GET = do_POST = do_PATCH = _serve

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
