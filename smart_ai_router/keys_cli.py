"""`smart-ai-router keys ...` — manage per-user API keys from the command line.

Operates directly on the local SQLite store (same DB the server uses), so it
works on the box without going through HTTP auth. Intended for the operator on
the machine hosting the router.

Subcommands:
    list                          Show all keys (metadata only, never secrets)
    add <user> [--scope JSON]     Mint a key; prints the plaintext ONCE
             [--max-tier N] [--window-s S] [--max-req N] [--max-tokens N]
    disable <prefix>              Revoke a key (reversible)
    enable  <prefix>              Re-enable a key
    delete  <prefix>              Delete a key permanently
"""
from __future__ import annotations

import argparse
import sys

from smart_ai_router.apikeys import display_prefix, generate_key, hash_key
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ApiKey


def _fmt_key(k: ApiKey) -> str:
    state = "enabled" if k.enabled else "DISABLED"
    limits = []
    if k.scope_models:
        limits.append(f"scope={k.scope_models}")
    if k.max_tier:
        limits.append(f"max_tier={k.max_tier}")
    if k.rl_window_s and (k.rl_max_req or k.rl_max_tokens):
        cap = []
        if k.rl_max_req:
            cap.append(f"{k.rl_max_req}req")
        if k.rl_max_tokens:
            cap.append(f"{k.rl_max_tokens}tok")
        limits.append(f"limit={'/'.join(cap)} per {k.rl_window_s}s")
    extra = ("  " + ", ".join(limits)) if limits else ""
    last = k.last_used_at or "never"
    return f"  {k.key_prefix:20} {k.user:16} {state:9} last_used={last}{extra}"


def _cmd_list(cr: CapabilityRouter, _args) -> int:
    keys = cr.all_api_keys()
    if not keys:
        print("No API keys. Mint one with:  smart-ai-router keys add <user>")
        return 0
    print(f"{len(keys)} key(s):")
    for k in keys:
        print(_fmt_key(k))
    return 0


def _cmd_add(cr: CapabilityRouter, args) -> int:
    user = args.user.strip()
    if not user:
        print("error: user must not be empty", file=sys.stderr)
        return 2
    plaintext = generate_key()
    cr.create_api_key(ApiKey(
        key_hash=hash_key(plaintext),
        user=user,
        key_prefix=display_prefix(plaintext),
        scope_models=args.scope or "",
        max_tier=args.max_tier,
        rl_window_s=args.window_s,
        rl_max_req=args.max_req,
        rl_max_tokens=args.max_tokens,
    ))
    print(f"Created key for {user!r}. Save it now — it will NOT be shown again:\n")
    print(f"    {plaintext}\n")
    return 0


def _cmd_set_enabled(cr: CapabilityRouter, prefix: str, enabled: bool) -> int:
    if not cr.set_api_key_enabled(prefix, enabled):
        print(f"error: no key with prefix {prefix!r}", file=sys.stderr)
        return 1
    print(f"{'Enabled' if enabled else 'Disabled'} key {prefix}")
    return 0


def _cmd_delete(cr: CapabilityRouter, args) -> int:
    if not cr.delete_api_key(args.prefix):
        print(f"error: no key with prefix {args.prefix!r}", file=sys.stderr)
        return 1
    print(f"Deleted key {args.prefix}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-ai-router keys",
        description="Manage per-user API keys (operates on the local store).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list all keys (metadata only)")

    p_add = sub.add_parser("add", help="mint a new key (prints plaintext once)")
    p_add.add_argument("user", help="identity label for the key")
    p_add.add_argument("--scope", default="",
                       help='scope_models JSON, e.g. \'{"allow":["ollama/"]}\'')
    p_add.add_argument("--max-tier", type=int, default=0, dest="max_tier",
                       help="cost-tier ceiling (0 = no ceiling)")
    p_add.add_argument("--window-s", type=int, default=0, dest="window_s",
                       help="rate-limit window seconds (0 = no limit)")
    p_add.add_argument("--max-req", type=int, default=0, dest="max_req",
                       help="max requests per window")
    p_add.add_argument("--max-tokens", type=int, default=0, dest="max_tokens",
                       help="max tokens per window")

    p_dis = sub.add_parser("disable", help="revoke a key (reversible)")
    p_dis.add_argument("prefix", help="key prefix (from `keys list`)")

    p_en = sub.add_parser("enable", help="re-enable a disabled key")
    p_en.add_argument("prefix", help="key prefix (from `keys list`)")

    p_del = sub.add_parser("delete", help="permanently delete a key")
    p_del.add_argument("prefix", help="key prefix (from `keys list`)")

    return parser


def run_keys_cli(argv: list[str]) -> int:
    """Entry point for `smart-ai-router keys ...`. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    cr = CapabilityRouter()
    if args.cmd == "list":
        return _cmd_list(cr, args)
    if args.cmd == "add":
        return _cmd_add(cr, args)
    if args.cmd == "disable":
        return _cmd_set_enabled(cr, args.prefix, False)
    if args.cmd == "enable":
        return _cmd_set_enabled(cr, args.prefix, True)
    if args.cmd == "delete":
        return _cmd_delete(cr, args)
    return 2
