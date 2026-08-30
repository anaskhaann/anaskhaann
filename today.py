# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""
GitHub profile card updater.

Two jobs:
  1. PROFILE SYNC  — copy the personal info from profile.toml into the id'd
     <tspan id="profile-*"> elements of dark_mode.svg / light_mode.svg.
     profile.toml is the single source of truth for name/location/tools/contact.
  2. STATS SYNC    — fetch live GitHub stats (repos, stars, commits, issues,
     PRs, lines of code) and write them into the id'd <tspan id="stat-*">.

Run locally:   uv run today.py
               reads .env for ACCESS_TOKEN/user_name(), syncs both profile AND stats
Run (profile only, no token/network):   uv run today.py --profile-only
Run in CI:     .github/workflows/build.yaml

Debug helper:  uv run today.py --list-tspans dark_mode.svg
               prints "index: [id] text" for every <tspan> — use after changing
               the SVG templates or profile.toml to see what moved.

Design doc: docs/superpowers/specs/2026-08-27-profile-readme-pipeline-design.md
"""

import hashlib
import os
import sys
import time
from xml.dom import minidom

import requests
import tomllib

# Single source of truth for the personalized (non-GitHub) card text.
PROFILE_FILE = "profile.toml"


def load_dotenv(path=".env"):
    """ak47-style dotenv: load KEY=VALUE lines from .env without overriding
    variables already set in the environment."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


load_dotenv()


# Token-dependent values are resolved lazily (via the getters below) so that
# `--profile-only` can run WITHOUT ACCESS_TOKEN/user_name() — it never needs the
# GitHub API. It's only the network/stats path that requires the .env.
# Fine-grained personal access token with All Repositories access:
# Repository permissions: read:Contents, read:Commit statuses, read:Issues,
#                         read:Metadata, read:Pull requests
def _require_env(name):
    try:
        return os.environ[name]
    except KeyError:
        raise SystemExit(
            f"Missing '{name}' in environment/.env — needed for the GitHub stats"
            " sync. Run `uv run today.py --profile-only` to update profile text "
            "without the API."
        ) from None


def github_headers():
    return {"Authorization": "Bearer " + _require_env("ACCESS_TOKEN")}


def user_name():
    return _require_env("user_name()")


def cache_file():
    return "cache/" + hashlib.sha256(user_name().encode("utf-8")).hexdigest() + ".txt"


COMMENT_SIZE = 7  # comment lines at the top of the cache file
SVG_FILES = ("dark_mode.svg", "light_mode.svg")

QUERY_COUNT = {
    "user_getter": 0,
    "stats_getter": 0,
    "graph_repos_stars": 0,
    "repo_loc": 0,
    "loc_query": 0,
}


# ---------------------------------------------------------------- utilities


def simple_request(func_name, query, variables):
    """POST a GraphQL query; return the response or raise with context."""
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=github_headers(),
    )
    if request.status_code == 200:
        return request
    raise Exception(
        f"{func_name} has failed with a {request.status_code}, "
        f"{request.text}, QUERY_COUNT={QUERY_COUNT}"
    )


def query_count(funct_id):
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """Run funct, return (result, elapsed_seconds)."""
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=None, whitespace=0):
    """Print a formatted timing line; optionally return a padded string."""
    print("{:<23}".format("   " + query_type + ":"), end="")
    if difference > 1:
        print("{:>12}".format("%.4f" % difference + " s "))
    else:
        print("{:>12}".format("%.4f" % (difference * 1000) + " ms"))
    if whitespace:
        return f"{'{:,}'.format(funct_return): >{whitespace}}"
    return funct_return


# ------------------------------------------------------------------ queries


def user_getter(username):
    """Return ({"id": ...}, createdAt) for the user."""
    query_count("user_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }"""
    request = simple_request(user_getter.__name__, query, {"login": username})
    data = request.json()["data"]["user"]
    return {"id": data["id"]}, data["createdAt"]


def repo_loc_via_contributors(owner, repo_name):
    """REST stats/contributors → (additions, deletions, commit_count) for me."""
    query_count("repo_loc")
    url = f"https://api.github.com/repos/{owner}/{repo_name}/stats/contributors"
    for attempt in range(5):
        if attempt:
            time.sleep(2)
        try:
            request = requests.get(url, headers=github_headers(), timeout=15)
        except requests.exceptions.ConnectionError:
            continue  # transient network error, retry
        if request.status_code == 202:
            continue  # GitHub is still computing stats, retry
        if request.status_code != 200:
            return 0, 0, 0
        for contributor in request.json():
            author = contributor.get("author") if contributor else None
            if author and author.get("login") == user_name():
                weeks = contributor.get("weeks", [])
                return (
                    sum(w["a"] for w in weeks),
                    sum(w["d"] for w in weeks),
                    contributor.get("total", 0),
                )
        break  # API responded but user not among contributors
    return 0, 0, 0


def loc_query(
    owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None
):
    """Paginate all repos (60/page) with default-branch commit counts,
    then hand the full edge list to cache_builder."""
    if edges is None:
        edges = []
    query_count("loc_query")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }"""
    variables = {
        "owner_affiliation": owner_affiliation,
        "login": user_name(),
        "cursor": cursor,
    }
    request = simple_request(loc_query.__name__, query, variables)
    repos = request.json()["data"]["user"]["repositories"]
    if repos["pageInfo"]["hasNextPage"]:
        return loc_query(
            owner_affiliation,
            comment_size,
            force_cache,
            repos["pageInfo"]["endCursor"],
            edges + repos["edges"],
        )
    return cache_builder(edges + repos["edges"], comment_size, force_cache)


def commit_counter(comment_size):
    """Total of my commits across all cached repos."""
    total_commits = 0
    with open(cache_file(), "r") as f:
        data = f.readlines()
    for line in data[comment_size:]:
        total_commits += int(line.split()[2])
    return total_commits


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """GraphQL: total repo count ("repos") or total stars ("stars")."""
    query_count("graph_repos_stars")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }"""
    variables = {
        "owner_affiliation": owner_affiliation,
        "login": user_name(),
        "cursor": cursor,
    }
    request = simple_request(graph_repos_stars.__name__, query, variables)
    data = request.json()["data"]["user"]["repositories"]
    if count_type == "repos":
        return data["totalCount"]
    if count_type == "stars":
        total_stars = 0
        while True:
            for node in data["edges"]:
                if node["node"] is None:  # deleted/invisible repo edge
                    continue
                total_stars += node["node"]["stargazers"]["totalCount"]
            if not data["pageInfo"]["hasNextPage"]:
                break
            variables["cursor"] = data["pageInfo"]["endCursor"]
            request = simple_request(graph_repos_stars.__name__, query, variables)
            data = request.json()["data"]["user"]["repositories"]
        return total_stars
    raise ValueError(f"unknown count_type: {count_type}")


def stats_getter():
    """Total issues and pull requests opened by the user."""
    query_count("stats_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            pullRequests(first: 1) {
                totalCount
            }
            issues {
                totalCount
            }
        }
    }"""
    request = simple_request(stats_getter.__name__, query, {"login": user_name()})
    data = request.json()["data"]["user"]
    return {
        "prs": int(data["pullRequests"]["totalCount"]),
        "issues": int(data["issues"]["totalCount"]),
    }


# -------------------------------------------------------------------- cache


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """Re-fetch LOC only for repos whose commit count changed since last run.
    Cache line format: hash total_commits my_commits loc_added loc_deleted"""
    cached = True
    try:
        with open(cache_file(), "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        os.makedirs(os.path.dirname(cache_file()), exist_ok=True)
        data = [
            "This line is a comment block. Write whatever you want here.\n"
        ] * comment_size
        with open(cache_file(), "w") as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        print("  Cache miss: flushing cache and recalculating LOC for all repos...")
        flush_cache(edges, cache_file(), comment_size)
        with open(cache_file(), "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        node = edges[index]["node"]
        if (
            repo_hash
            == hashlib.sha256(node["nameWithOwner"].encode("utf-8")).hexdigest()
        ):
            try:
                total = node["defaultBranchRef"]["target"]["history"]["totalCount"]
                if int(commit_count) != total:
                    owner, repo_name = node["nameWithOwner"].split("/")
                    print(f"  LOC: {repo_name}...", end=" ", flush=True)
                    loc = repo_loc_via_contributors(owner, repo_name)
                    print(f"+{loc[0]} -{loc[1]}")
                    data[index] = f"{repo_hash} {total} {loc[2]} {loc[0]} {loc[1]}\n"
            except TypeError:  # empty repo
                data[index] = repo_hash + " 0 0 0 0\n"
        if (index + 1) % 10 == 0:  # checkpoint so a cancelled run loses little
            with open(cache_file(), "w") as f:
                f.writelines(cache_comment)
                f.writelines(data)
    with open(cache_file(), "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """Reset the cache to one zeroed line per repo, preserving the comment block."""
    try:
        with open(filename, "r") as f:
            data = f.readlines()[:comment_size]
    except FileNotFoundError:
        data = []
    with open(filename, "w") as f:
        f.writelines(data)
        for node in edges:
            digest = hashlib.sha256(
                node["node"]["nameWithOwner"].encode("utf-8")
            ).hexdigest()
            f.write(digest + " 0 0 0 0\n")


# ---------------------------------------------------------------------- svg


def svg_overwrite(filename, values):
    """Overwrite the text of every id'd <tspan> in `values` (id → string).
    Id-based, so hand edits elsewhere in the SVG can never shift targets."""
    svg = minidom.parse(filename)
    for tspan in svg.getElementsByTagName("tspan"):
        tid = tspan.getAttribute("id")
        if tid in values:
            if tspan.firstChild is None:
                tspan.appendChild(svg.createTextNode(values[tid]))
            else:
                tspan.firstChild.data = values[tid]
    with open(filename, mode="w", encoding="utf-8") as f:
        f.write(svg.toxml("utf-8").decode("utf-8"))


def load_profile():
    """Read profile.toml -> { <tspan id>: <replace-with> } for the profile-* ids.

    THE single source of truth for the personalized (non-GitHub) card text.
    The two-space inline indent seen after each About/Contact label lives in
    the svg template; it's re-added here so profile.toml can hold clean text.
    """
    with open(PROFILE_FILE, "rb") as f:
        data = tomllib.load(f)

    values = {
        "profile-title": data["title"],
        "profile-name": "  " + data["about"]["name"],
        "profile-location": "  " + data["about"]["location"],
        "profile-major": "  " + data["about"]["major"],
        "profile-ide": "  " + data["about"]["ide"],
        "profile-os": "  " + data["about"]["os"],
    }
    for i, tool in enumerate(data["tools"], start=1):
        values[f"profile-tool-{i}"] = tool["text"]
        values[f"profile-tag-{i}"] = "  " + tool["tag"]
    values["profile-email"] = "  " + data["contact"]["email"]
    values["profile-linkedin"] = "  " + data["contact"]["linkedin"]
    values["profile-discord"] = "  " + data["contact"]["discord"]
    return values


def svg_element_getter(filename):
    """Print 'index: [id] text' for every <tspan> — the remap/debug helper."""
    svg = minidom.parse(filename)
    for index, tspan in enumerate(svg.getElementsByTagName("tspan")):
        tid = tspan.getAttribute("id")
        text = "".join(c.data for c in tspan.childNodes if c.nodeType == c.TEXT_NODE)
        print(f"{index}: [{tid or '-'}] {text}")


# --------------------------------------------------------------------- main

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--profile-only":
        for filename in SVG_FILES:
            svg_overwrite(filename, load_profile())
        print(f"Profile synced from {PROFILE_FILE} to {', '.join(SVG_FILES)}")
        sys.exit(0)

    if len(sys.argv) >= 3 and sys.argv[1] == "--list-tspans":
        svg_element_getter(sys.argv[2])
        sys.exit(0)

    print("Calculation times:")
    user_data, user_time = perf_counter(user_getter, user_name())
    OWNER_ID, acc_date = user_data
    formatter("account data", user_time)

    total_loc, loc_time = perf_counter(
        loc_query, ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], COMMENT_SIZE
    )
    formatter("LOC (cached)" if total_loc[-1] else "LOC (no cache)", loc_time)

    commit_data, commit_time = perf_counter(commit_counter, COMMENT_SIZE)
    star_data, star_time = perf_counter(graph_repos_stars, "stars", ["OWNER"])
    repo_data, repo_time = perf_counter(graph_repos_stars, "repos", ["OWNER"])
    contrib_data, contrib_time = perf_counter(
        graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )
    stats_data, stats_time = perf_counter(stats_getter)
    formatter("issues/prs stats", stats_time)

    commit_data = formatter("commit counter", commit_time, commit_data)
    star_data = formatter("star counter", star_time, star_data)
    repo_data = formatter("my repositories", repo_time, repo_data)
    contrib_data = formatter("contributed repos", contrib_time, contrib_data)

    loc_strings = ["{:,}".format(n) for n in total_loc[:-1]]  # added, deleted, net

    # profile values (single source of truth) + live GitHub stats — merged so a
    # full run keeps both the personal text AND the stats in sync, idempotently.
    values = {
        **load_profile(),
        "stat-repos": repo_data,
        "stat-contrib": contrib_data,
        "stat-stars": star_data,
        "stat-commits": commit_data,
        "stat-issues": "{:,}".format(stats_data["issues"]),
        "stat-prs": "{:,}".format(stats_data["prs"]),
        "stat-loc": loc_strings[2],
        "stat-loc-add": loc_strings[0] + "++",
        "stat-loc-del": loc_strings[1] + "--",
    }
    for filename in SVG_FILES:
        svg_overwrite(filename, values)

    total_time = (
        user_time
        + loc_time
        + commit_time
        + star_time
        + repo_time
        + contrib_time
        + stats_time
    )
    print("{:<23}{:>12}".format("   Total function time:", "%.4f s" % total_time))
    print("Total GitHub GraphQL API calls:", "{:>3}".format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print("{:<28}".format("   " + funct_name + ":"), "{:>6}".format(count))
