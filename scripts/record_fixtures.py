"""Record every HTTP response of a real refresh into tests/fixtures/<name>.json.

Fixture keys never hold credentials: the RPC URL is replaced by "RPC", and the OpenSea key travels only in
a request header, which is not recorded. Run: uv run --env-file <your env file> python scripts/record_fixtures.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402
from sources.cache import Cache  # noqa: E402
from sources.http import urllib_transport  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def fixture_key(method, url, body, rpc_url):
    u = "RPC" if url == rpc_url else url
    return f"{method} {u} {body.decode() if body else ''}".strip()


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "wallet"
    env = {k: os.environ.get(k, "").strip() for k in ("OPENSEA_API_KEY", "ETH_RPC_URL", "LEAK_TEST_WALLET")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        sys.exit(f"missing: {', '.join(missing)}")
    private = [env["OPENSEA_API_KEY"], env["ETH_RPC_URL"]]
    recorded = {}

    def rec(method, url, headers, body):
        status, rh, raw = urllib_transport(method, url, headers, body)
        if status == 200:
            recorded[fixture_key(method, url, body, env["ETH_RPC_URL"])] = json.loads(raw) if raw else None
        return status, rh, raw

    wallet = env["LEAK_TEST_WALLET"].lower()
    with tempfile.TemporaryDirectory() as d:
        cache = Cache(Path(d) / "c.sqlite")
        cli.refresh(cache, [wallet], "2022-06-14", env, transport=rec)
        as_of = cache.get_meta(f"as_of:{wallet}")
    text = json.dumps({"wallet": wallet, "as_of": as_of, "responses": dict(sorted(recorded.items()))},
                      sort_keys=True, separators=(",", ":"))
    if any(p and p in text for p in private):
        sys.exit("refusing to write: a credential appears in the recorded responses")
    out = ROOT / "tests" / "fixtures" / f"{name}.json"
    out.write_text(text)
    print(f"wrote {out.relative_to(ROOT)}: {len(recorded)} responses, {len(text) // 1024} KiB")


if __name__ == "__main__":
    main()
