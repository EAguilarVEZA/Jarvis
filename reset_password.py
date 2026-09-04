"""
reset_password.py — reset your Smart with Martin (Jarvis) login password.

Run it in Terminal:   cd ~/Jarvis && python3 reset_password.py

It lists the accounts in your user store, lets you pick one, and sets a NEW
password (typed hidden — never shown, never logged). Matches auth.py's hashing:
password_hash = sha256(password_salt + password).
"""
import getpass
import hashlib
import json
import secrets
from pathlib import Path

STORE = Path.home() / ".config" / "jarvis" / "users.json"


def main():
    if not STORE.exists():
        print(f"No user store found at {STORE}. Is this the right machine?")
        return
    data = json.loads(STORE.read_text(encoding="utf-8"))
    users = data.get("users", []) if isinstance(data, dict) else []
    if not users:
        print("No accounts found in the user store.")
        return

    print("\nAccounts in your Smart with Martin login:\n")
    for i, u in enumerate(users, 1):
        print(f"  {i}. {u.get('email','(no email)')}   "
              f"[{u.get('role','user')}, {u.get('status','active')}]")

    if len(users) == 1:
        idx = 0
        print(f"\nResetting the only account: {users[0].get('email')}")
    else:
        raw = input("\nWhich number do you want to reset? ").strip()
        if not raw.isdigit() or not (1 <= int(raw) <= len(users)):
            print("Cancelled — not a valid choice.")
            return
        idx = int(raw) - 1

    pw1 = getpass.getpass("New password (typing is hidden): ")
    pw2 = getpass.getpass("Confirm new password: ")
    if not pw1:
        print("Cancelled — empty password.")
        return
    if pw1 != pw2:
        print("Cancelled — the two passwords didn't match.")
        return

    salt = secrets.token_hex(16)
    users[idx]["password_salt"] = salt
    users[idx]["password_hash"] = hashlib.sha256((salt + pw1).encode()).hexdigest()
    users[idx]["status"] = users[idx].get("status") or "active"
    data["users"] = users
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\nDone. Log in at https://localhost:8000/app as "
          f"{users[idx].get('email')} with your new password.")


if __name__ == "__main__":
    main()
