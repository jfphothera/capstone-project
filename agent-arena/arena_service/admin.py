"""arena-admin: local administration of the Arena Event Service.

Runs on the service host against its database file. There is deliberately no
network administration API: the admin hub's screen (admin_console) is the same
administration, served only to a browser on the service host itself.
Secrets are printed once and never stored in clear.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys

from . import CONTRACT
from .contract import APP_ID, NODE_ID, safe_text
from .store import Store


def _config(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _is_address(text):
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def head_identity(address):
    """The fleet's naming convention for a pre-addressed head: (node_id, display name)."""
    address = str(ipaddress.IPv4Address(address))
    return "head-" + address.replace(".", "-"), f"Head {address.rsplit('.', 1)[1]} ({address})"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arena-admin", description=__doc__)
    parser.add_argument("--config", required=True, help="service configuration JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("app-add", help="register an application")
    p.add_argument("app_id"); p.add_argument("display_name")
    p = sub.add_parser("orchestrator-add", help="register an orchestrator location for an application")
    p.add_argument("app_id"); p.add_argument("orchestrator_id"); p.add_argument("display_name")
    p = sub.add_parser("producer-issue", help="issue a producer credential")
    p.add_argument("app_id"); p.add_argument("orchestrators", nargs="+")
    p = sub.add_parser("head-add", help="register an inference head location and its display "
                                         "(head-add 192.0.2.11 derives the id and name from the address)")
    p.add_argument("node_id"); p.add_argument("display_name", nargs="?"); p.add_argument("--address")
    p.add_argument("--apps", default="*", help="comma list of app_ids allowed to assign work here, or *")
    p = sub.add_parser("head-remove", help="retire a head: no new work there; its display credential is revoked")
    p.add_argument("node_id")
    p = sub.add_parser("orchestrator-display-add", help="register a display that shows an orchestrator")
    p.add_argument("display_id"); p.add_argument("location_id"); p.add_argument("--address")
    p = sub.add_parser("display-remove", help="disable a display and revoke its credential")
    p.add_argument("display_id")
    sub.add_parser("displays", help="list displays with their addresses")
    p = sub.add_parser("enroll", help="print a one-time enrollment code for a display")
    p.add_argument("display_id"); p.add_argument("--hours", type=float, default=24)
    p.add_argument("--out", help="also write the code to this file (for a USB enroll folder)")
    p = sub.add_parser("revoke", help="revoke a credential"); p.add_argument("credential_id")
    p = sub.add_parser("rotate", help="rotate a producer or viewer credential"); p.add_argument("credential_id")
    sub.add_parser("list", help="list registry and credentials (no secrets)")
    p = sub.add_parser("endpoints-set", help="publish the endpoint list displays should use")
    p.add_argument("urls", nargs="+")
    args = parser.parse_args(argv)

    store = Store(_config(args.config)["db_path"])
    out = sys.stdout
    if args.command == "app-add":
        if not APP_ID.match(args.app_id):
            parser.error("app_id must be lowercase letters, digits and dashes")
        store.add_application(args.app_id, safe_text(args.display_name, "display_name", 60))
        print(f"application {args.app_id} = {args.display_name}", file=out)
    elif args.command == "orchestrator-add":
        location = store.add_orchestrator(args.app_id, args.orchestrator_id, safe_text(args.display_name, "display_name", 60))
        print(f"orchestrator location {location}", file=out)
    elif args.command == "producer-issue":
        credential_id, secret = store.issue_producer(args.app_id, args.orchestrators)
        print(json.dumps({"contract": CONTRACT, "credential_id": credential_id, "secret": secret}), file=out)
        print("Store this secret in the producer's protected credential file now; it is not shown again.", file=sys.stderr)
    elif args.command == "head-add":
        node_id, name, address = args.node_id, args.display_name, args.address
        if _is_address(node_id):
            address, (node_id, default_name) = node_id, head_identity(node_id)
            name = name or default_name
        if not NODE_ID.match(node_id):
            parser.error("node_id has invalid characters")
        if not name:
            parser.error("display_name is required unless the head is given by address")
        apps = ["*"] if args.apps == "*" else [a.strip() for a in args.apps.split(",") if a.strip()]
        location = store.add_head(node_id, safe_text(name, "display_name", 60), address, apps)
        store.add_display(node_id, "head", location, address)
        print(f"head {node_id} at {location} (address {address or 'any'}); display {node_id} registered", file=out)
    elif args.command == "head-remove":
        displays, open_count = store.remove_head(args.node_id)
        print(f"head {args.node_id} retired; displays disabled and credentials revoked: {', '.join(displays) or 'none'}",
              file=out)
        if open_count:
            print(f"{open_count} open assignment(s) are still located there; they end through their producer "
                  "or lease expiry", file=out)
    elif args.command == "orchestrator-display-add":
        store.add_display(args.display_id, "orchestrator", args.location_id, args.address)
        print(f"display {args.display_id} shows {args.location_id}", file=out)
    elif args.command == "display-remove":
        store.remove_display(args.display_id)
        print(f"display {args.display_id} disabled; its credentials are revoked", file=out)
    elif args.command == "displays":
        print(json.dumps(store.list_displays(), indent=2), file=out)
    elif args.command == "enroll":
        code = store.issue_enrollment(args.display_id, args.hours)
        print(code, file=out)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(f"{code}\r\ndisplay {args.display_id}\r\nvalid {args.hours:g} h from issue; usable once, "
                             "only from that display's registered address\r\n")
        print(f"One-time code for {args.display_id}, valid {args.hours:g} h. Type it into the Agent Arena installer.",
              file=sys.stderr)
    elif args.command == "revoke":
        print("revoked" if store.revoke(args.credential_id) else "not found or already revoked", file=out)
    elif args.command == "rotate":
        credential_id, secret = store.rotate(args.credential_id)
        print(json.dumps({"credential_id": credential_id, "secret": secret}), file=out)
    elif args.command == "list":
        print(json.dumps({"registry": store.registry(), "displays": store.list_displays(),
                          "credentials": store.list_credentials()}, indent=2, default=str), file=out)
    elif args.command == "endpoints-set":
        store.set_meta("endpoints", args.urls)
        print("endpoints: " + ", ".join(args.urls), file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
