"""
OBDb importer — builds a compact, app-friendly database of manufacturer-specific
OBD signal definitions from the OBDb GitHub organisation (https://github.com/OBDb).

DATA LICENCE: OBDb data is CC BY-SA 4.0. Everything this script produces is a
derivative work and MUST be published under CC BY-SA 4.0 with attribution.
The script writes LICENSE + ATTRIBUTION.md next to the output for that reason.

Not shipped in the app. Usage (PowerShell):
    py tools/obdb_import/obdb_import.py --limit 3          # quick test
    py tools/obdb_import/obdb_import.py                    # everything
Optional: $env:GITHUB_TOKEN = "<token>" raises the GitHub API rate limit.

Design:
- Uses the GitHub REST API only to LIST repos (~9 requests), then downloads each
  repo as a tarball from codeload.github.com. Tarballs are read in memory;
  nothing is extracted to disk and nothing downloaded is executed.
- Polite: one request at a time, small delay, cached tarballs are reused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ORG = "OBDb"
API = "https://api.github.com"
USER_AGENT = "sld-obd2-obdb-importer"
DELAY_S = 0.4
HERE = Path(__file__).resolve().parent
YEAR_FILE = re.compile(r"^(\d{4})-(\d{4})\.json$")


def http_get(url: str, accept: str | None = None, retries: int = 3) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(5, int(reset) - int(time.time())) if reset else 60
                if wait > 900:
                    raise SystemExit(f"Rate limited for {wait}s — set GITHUB_TOKEN or try later.")
                print(f"  rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
            elif e.code == 404:
                raise
            elif attempt == retries - 1:
                raise
            else:
                time.sleep(2 * (attempt + 1))
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def list_repos() -> list[dict]:
    repos, page = [], 1
    while True:
        data = json.loads(http_get(f"{API}/orgs/{ORG}/repos?per_page=100&page={page}&type=public",
                                   accept="application/vnd.github+json"))
        if not data:
            break
        repos += data
        page += 1
        time.sleep(DELAY_S)
    return repos


def fetch_tarball(repo: dict, cache: Path) -> bytes:
    name, branch, pushed = repo["name"], repo["default_branch"], repo.get("pushed_at", "")
    stamp = re.sub(r"[^0-9]", "", pushed)
    path = cache / f"{name}@{stamp}.tar.gz"
    if path.exists():
        return path.read_bytes()
    for old in cache.glob(f"{name}@*.tar.gz"):
        old.unlink()
    data = http_get(f"https://codeload.github.com/{ORG}/{name}/tar.gz/refs/heads/{branch}")
    path.write_bytes(data)
    time.sleep(DELAY_S)
    return data


def read_signalsets(tar_bytes: bytes) -> dict[str, dict]:
    """Return {filename: parsed json} for signalsets/v3/*.json, read in memory only."""
    out: dict[str, dict] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or m.size > 5_000_000:
                continue
            parts = m.name.split("/")
            # <repo>-<sha>/signalsets/v3/<file>.json
            if len(parts) == 4 and parts[1] == "signalsets" and parts[2] == "v3" and parts[3].endswith(".json"):
                f = tf.extractfile(m)
                if f is None:
                    continue
                try:
                    out[parts[3]] = json.loads(f.read().decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as e:
                    print(f"  skip {m.name}: {e}", file=sys.stderr)
    return out


HYPHEN_MAKES = ["Mercedes-Benz", "Alfa-Romeo", "Aston-Martin", "Land-Rover", "Rolls-Royce"]
SKIP_REPOS = {"SAEJ1979"}  # standard PIDs — the app already has its own


def split_make_model(repo_name: str) -> tuple[str, str]:
    """'Kia-EV6' → (Kia, EV6); 'Land-Rover-Defender' → (Land Rover, Defender); 'Kia' → (Kia, '')."""
    for mk in HYPHEN_MAKES:
        if repo_name == mk or repo_name.startswith(mk + "-"):
            return mk.replace("-", " "), repo_name[len(mk) + 1:].replace("-", " ").replace("_", " ")
    make, _, model = repo_name.partition("-")
    return make.replace("_", " "), model.replace("-", " ").replace("_", " ")


def count_signals(signalset: dict) -> int:
    return sum(len(c.get("signals", [])) for c in signalset.get("commands", []))


def services_used(signalset: dict) -> set[str]:
    return {svc for c in signalset.get("commands", []) for svc in c.get("cmd", {})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--cache", default=str(HERE / "cache"))
    ap.add_argument("--limit", type=int, default=0, help="only process the first N vehicle repos (testing)")
    ap.add_argument("--only", default="", help="comma-separated repo names")
    args = ap.parse_args()

    out, cache = Path(args.out), Path(args.cache)
    (out / "vehicles").mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    print("Listing repos…")
    repos = [r for r in list_repos() if not r["name"].startswith(".") and not r.get("archived")]
    repos.sort(key=lambda r: r["name"].lower())
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        repos = [r for r in repos if r["name"] in wanted]
    print(f"{len(repos)} candidate repos")

    index, sources, skipped, done = [], [], 0, 0
    for repo in repos:
        if args.limit and done >= args.limit:
            break
        name = repo["name"]
        if name in SKIP_REPOS:
            skipped += 1
            continue
        # Make-level repos (no model, e.g. "NissanInfiniti") are kept: OBDb falls back to them.
        # Tooling repos (obdb.community …) have no signalsets and are skipped below.
        try:
            sets = read_signalsets(fetch_tarball(repo, cache))
        except urllib.error.HTTPError as e:
            print(f"  {name}: HTTP {e.code}, skipped", file=sys.stderr)
            skipped += 1
            continue
        if not sets or sum(count_signals(s) for s in sets.values()) == 0:
            skipped += 1  # empty stub repo
            continue
        make, model = split_make_model(name)
        files = []
        for fname in sorted(sets, key=lambda f: (f != "default.json", f)):
            m = YEAR_FILE.match(fname)
            files.append({
                "file": fname,
                "years": [int(m.group(1)), int(m.group(2))] if m else None,
                "signals": count_signals(sets[fname]),
            })
        vehicle = {"id": name, "make": make, "model": model, "source": repo["html_url"],
                   "updated": repo.get("pushed_at"), "signalsets": sets}
        (out / "vehicles" / f"{name}.json").write_text(
            json.dumps(vehicle, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        services = sorted(set().union(*(services_used(s) for s in sets.values())))
        index.append({"id": name, "make": make, "model": model, "kind": "model" if model else "make",
                      "files": files, "services": services})
        sources.append(f"- [{ORG}/{name}]({repo['html_url']}) (updated {repo.get('pushed_at', '?')})")
        done += 1
        print(f"  {name}: {len(files)} file(s), {sum(f['signals'] for f in files)} signals")

    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (out / "index.json").write_text(json.dumps(
        {"format": 1, "generated": generated, "license": "CC-BY-SA-4.0",
         "attribution": "Data from OBDb (https://github.com/OBDb), CC BY-SA 4.0",
         "vehicles": index}, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    (out / "ATTRIBUTION.md").write_text(
        "# Attribution\n\n"
        "This database is derived from **OBDb** — community documentation for on-board diagnostics\n"
        "(https://github.com/OBDb, https://obdb.community), licensed under\n"
        "[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).\n\n"
        "Changes: signal sets re-packaged into one minified JSON file per vehicle plus an index.\n"
        "No values were altered. This derived database is licensed under CC BY-SA 4.0.\n\n"
        f"Generated {generated} from:\n\n" + "\n".join(sources) + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        "# SLD OBD2 — manufacturer data\n\n"
        "Manufacturer-specific OBD signal definitions used by the SLD OBD2 Android app's\n"
        "\"Manufacturer extras\" screen. Generated by `tools/obdb_import/obdb_import.py`.\n\n"
        "**Source:** [OBDb](https://github.com/OBDb) — community documentation for on-board diagnostics.\n"
        "**Licence:** [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) (see `LICENSE`, `ATTRIBUTION.md`).\n"
        "You may reuse this data under the same licence.\n\n"
        "## Layout\n\n"
        "- `index.json` — every vehicle: `id`, `make`, `model`, `kind` (`model` or make-wide `make`),\n"
        "  `files` (signal-set files with optional model-year range and signal count), `services` used.\n"
        "- `vehicles/<id>.json` — `{id, make, model, source, updated, signalsets: {<file>: <OBDb signalset v3>}}`.\n"
        "  Signal sets are copied unchanged from `signalsets/v3/` of each OBDb repo.\n\n"
        f"Generated {generated}: {done} vehicles.\n", encoding="utf-8")
    lic = HERE / "LICENSE-CC-BY-SA-4.0.txt"
    if lic.exists():
        (out / "LICENSE").write_text(lic.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\nDone: {done} vehicles, {skipped} skipped → {out}")


if __name__ == "__main__":
    main()
