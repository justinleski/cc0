"""Issue one-hour local bearer tokens without exposing a token-minting HTTP route."""

import argparse

from app.auth.local import ACCOUNTS, issue_token
from app.core.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["token"])
    parser.add_argument("account", choices=ACCOUNTS)
    args = parser.parse_args()
    try:
        token = issue_token(Settings(), args.account)
    except Exception:
        parser.exit(1, "Local token issuance unavailable; check explicit auth configuration.\n")
    print(token)


if __name__ == "__main__":
    main()
