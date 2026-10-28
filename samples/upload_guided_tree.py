"""Upload a guided question tree from a nested JSON file.

`POST /guided-tree/` creates one node per request and a child needs its
parent's id, so a tree cannot be sent in one body. This walks the file
parents-first, posts each node, and passes the id it gets back down to the
children — which is the whole reason this script exists rather than a curl
one-liner.

    python samples/upload_guided_tree.py samples/una_guided_tree.json \
        --base https://chatbot-api.zaychain.com/api/v1 --token "$ACCESS"

**The token decides whose tree this becomes.** The API is multi-tenant: the
credential identifies the tenant and the nodes are filed under them — nothing in
the file or the arguments names an account. So before writing anything, this
asks the server who the token belongs to and prints it, then waits for a yes.
Uploading a tree under the wrong tenant is not a typo you can undo in one call:
it is 22 rows to hunt down, so the confirmation is deliberate. Pass `--yes` to
skip it in a script you have already checked.

Authenticate with --token (JWT) or --api-key.

Writes always apply to the canonical language; the other active languages are
mirrored in the background, so re-fetch a mirror a moment later rather than
expecting it immediately.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 30


def call(base: str, path: str, headers: dict, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def whoami(base: str, headers: dict) -> dict:
    """Who the credential belongs to — the tenant this tree will be filed under."""
    try:
        return call(base, "/users/me/", headers)
    except urllib.error.HTTPError as exc:
        hint = "The token is expired or wrong." if exc.code == 401 else exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Could not identify the account ({exc.code}). {hint}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {base}: {exc.reason}") from None


def post_node(base: str, headers: dict, payload: dict) -> dict:
    try:
        return call(base, "/guided-tree/", headers, payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(
            f"\n{exc.code} while creating {payload['title'][:60]!r}\n{body}\n"
            + {
                401: "Check the token — it may have expired (they last 3 days).",
                402: "The guided tree is off for this tenant; an admin enables it in features.",
                400: "Most often a clash on `order`: it must be unique among siblings.",
            }.get(exc.code, "")
            + "\nNodes created before this point are still there — delete them before re-running."
        ) from None


def upload(nodes: list, base: str, headers: dict, parent: str | None = None, depth: int = 0) -> int:
    """Create each node, then its children under the id the server assigned."""
    count = 0
    for order, node in enumerate(nodes):
        payload = {"title": node["title"], "order": order, "is_active": True}
        if node.get("answer"):
            payload["answer"] = node["answer"]
        if parent:
            payload["parent"] = parent

        created = post_node(base, headers, payload)
        count += 1
        print(f"{'  ' * depth}created {created['id']}  {node['title'][:70]}")

        children = node.get("children") or []
        if children:
            count += upload(children, base, headers, parent=created["id"], depth=depth + 1)
    return count


def count_nodes(nodes: list) -> int:
    return sum(1 + count_nodes(n.get("children") or []) for n in nodes)


def main() -> None:
    # Titles and answers are Arabic; a Windows console defaults to cp1252 and
    # would raise on the first one printed.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Nested JSON tree (see una_guided_tree.json)")
    parser.add_argument("--base", default="http://localhost:8000/api/v1")
    parser.add_argument("--token", help="JWT access token — its owner gets the tree")
    parser.add_argument("--api-key", help="Tenant API key, as an alternative to --token")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation")
    args = parser.parse_args()

    if not args.token and not args.api_key:
        parser.error("pass --token or --api-key")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {"X-API-Key": args.api_key}

    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    nodes = data["nodes"] if isinstance(data, dict) else data

    user = whoami(args.base, headers)
    total = count_nodes(nodes)
    print(f"Server  : {args.base}")
    print(f"Account : {user.get('username')}  <{user.get('email') or 'no email'}>  (role: {user.get('role')})")
    print(f"Tree    : {args.file} — {total} node(s)")

    if not args.yes:
        if input("\nUpload this tree to that account? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("Nothing was uploaded.")
        print()

    created = upload(nodes, args.base, headers)
    print(f"\n{created} node(s) created for {user.get('username')}.")
    print(f"Read it back: GET {args.base.rstrip('/')}/guided-tree/")


if __name__ == "__main__":
    sys.exit(main())
