"""Generate a user entry for DATAQ_USERS.

    uv run python -m dataq.api.newuser alice

Prints ``alice:scrypt$...``. The password is read without echo and never
written anywhere: the hash is the only thing that leaves this process.
"""

from __future__ import annotations

import getpass
import secrets
import string
import sys

from .users import hash_password


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    username = argv[0]

    password = getpass.getpass("Password (blank to generate one): ")
    generated = not password
    if generated:
        alphabet = string.ascii_lowercase + string.digits
        password = "-".join(
            "".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(5)
        )
    elif password != getpass.getpass("Again: "):
        print("passwords do not match", file=sys.stderr)
        return 1

    if generated:
        print(f"\nPassword for {username}: {password}")
        print("Write it down now -- it is not stored anywhere.\n")
    print(f"{username}:{hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
