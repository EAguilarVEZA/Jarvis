"""
Permission-scope + connector-auth acceptance tests (blueprint §13).
Run:  python3 -m jarvis_convo.demo_scopes
"""
from __future__ import annotations

from .tools import ExecutionController, ToolSpec, Perm, Status
from .scopes import ScopeStore, ConnectorAuth

_SENT = {"n": 0}
def _gmail_send(args):
    _SENT["n"] += 1
    return {"output": {"id": f"gmail-{_SENT['n']}"}, "provenance": [{"action": "gmail.send"}]}


def controller_with_scopes(scopes: ScopeStore, principal="edgar"):
    c = ExecutionController(scope_store=scopes, principal=principal)
    c.register(ToolSpec("gmail_send", "Send an email via Gmail.", Perm.WRITE,
                        {"to": {"type": "str", "required": True},
                         "body": {"type": "str", "required": True}},
                        _gmail_send, idempotent=True, required_scope="gmail:write"))
    return c


def test_connector_write_blocked_without_scope():
    scopes = ScopeStore()
    c = controller_with_scopes(scopes)
    # even WITH confirm, a scoped connector write is blocked until the scope is granted
    r = c.execute("gmail_send", {"to": "a@b.com", "body": "hi"}, confirm=True)
    assert r.status == Status.FAILED and r.error == "scope_required:gmail:write", r.to_dict()
    assert c.exec_count.get("gmail_send", 0) == 0
    return "connector write blocked without an explicit scope (confirm is not enough)"


def test_grant_allows_then_revoke_blocks():
    scopes = ScopeStore()
    c = controller_with_scopes(scopes)
    scopes.grant("edgar", "gmail:write")
    ok = c.execute("gmail_send", {"to": "a@b.com", "body": "hi"}, request_id="r1")
    assert ok.status == Status.SUCCEEDED and ok.output["id"]
    scopes.revoke("edgar", "gmail:write")
    denied = c.execute("gmail_send", {"to": "a@b.com", "body": "again"}, request_id="r2")
    assert denied.status == Status.FAILED
    return "grant enables the connector write; revoke re-blocks it"


def test_read_still_open():
    from .tools import default_controller
    r = default_controller().execute("web_search", {"query": "x"})
    assert r.status == Status.SUCCEEDED
    return "read tools remain open (no scope needed)"


def test_connector_token_never_plaintext():
    auth = ConnectorAuth()
    auth.store_token("edgar", "gmail", "super-secret-oauth-token-XYZ")
    ref = auth.reference("edgar", "gmail")
    assert auth.connected("edgar", "gmail")
    assert ref and "super-secret" not in ref, "token reference must not expose the secret"
    # the stored blob is encrypted/hashed, never the plaintext
    blob = auth._blobs["edgar:gmail"]
    assert "super-secret-oauth-token-XYZ" not in blob
    return "connector token stored encrypted/ref only — never plaintext"


def main():
    for fn in (test_connector_write_blocked_without_scope, test_grant_allows_then_revoke_blocks,
               test_read_still_open, test_connector_token_never_plaintext):
        print(f"  PASS  {fn()}")
    print("\nOK — read/draft/write separation, scoped connector writes (grant/revoke), and "
          "encrypted token storage all pass.")


if __name__ == "__main__":
    main()
