# ---------------------------------------------------------------------------- #
#                      Workspace Manager - RM220507 - 2026                     #
# ---------------------------------------------------------------------------- #

import subprocess
import sys
import json
from pathlib import Path
import re
import hashlib
from datetime import datetime, timezone

# --------------------------- CONSTANTS DEFINITION --------------------------- #
MAIN_BRANCH = "main" # the name of the default branch in a repo (usually main / master)
WS_CONFIG_FILE = ".workspace_config.json" # name of the configuration file within a workspace
LFS_PATTERNS = [ # file extensions that should be marked to track LFS
    "*.ipt", "*.iam", "*.kicad_pcb", "*.kicad_sch", "*.step",
    "*.stp", "*.stl", "*.pdf", "*.png", "*.jpg", "*.jpeg"
]
SEMVER = re.compile(r"v(\d+)\.(\d+)\.(\d+)$") # regex for semantic versions
HOTFIX = re.compile(r"(v\d+\.\d+\.\d+)-hotfix\.(\d+)$") # regex for hotfix versions
ARTIFACT_REPO_NAME = "artifacts"

# ------------------------------ EXCEPTION CLASS ----------------------------- #
# a custom exception class that every 'application-level' problem will raise
class WMException(Exception):
    def __init__(self, msg, *args, **kwargs):
        print(f"[ERROR] {msg}.")

# ----------------------------- HELPER FUNCTIONS ----------------------------- #
def run(cmd, cwd):
    # run a command in the given working directory
    print(f"[{cwd}] {' '.join(map(str, cmd))}") # log
    subprocess.check_call(cmd, cwd=cwd) # actually run

def git(repo, *args):
    # run a git command in a given repo
    run(["git", *args], repo)
    
def resolve_alias(alias, ws_config):
    # find the project repo's path from its alias: e.g. "node/abc" -> "/bcs/node/abc"
    path_str = ws_config.get("projects", {}).get(alias, alias)
    return ws_config["root"] / path_str
    
def repo_key(repo):    
    return f"{repo.parent.name}/{repo.name}"
    
def find_repos(selectors, ws_config):
    # find the paths for all repos that a set of aliases refer to
    if not selectors:
        selectors = ["."]
        
    repos = set()
    for sel in selectors:
        path = resolve_alias(sel, ws_config)
        
        if path.is_dir() and (path / ".git").exists(): # directory is repo
            repos.add(path)
        elif path.is_dir(): # dir is not repo, just container
            for sub in path.iterdir():
                if (sub / ".git").exists():
                    repos.add(sub)
        else:
            raise WMException("Repo selector not found.")
        
    return sorted(repos) # sort in alphabetical order for no real reason

def get_ws_root(alias):
    # find the path root of a workspace by using the json lookup config
    with open("config.json", "r") as f:
        data = json.load(f)
        
    return data["workspaces"].get(alias)

def get_new_repo_name(args) -> tuple[str, str]:
    # given either "node/xz" or simply "xz" find the group ("node" or "") and repo name
    fullname = args[0]
    parts = fullname.split("/")
    
    if len(parts) == 1:
        return "", fullname
    elif len(parts) == 2:
        return (*parts,)
    else:
        raise WMException("Invalid repo name")
    
def sync_to_super(repo, ws_root):
    # do the subtree stuff that makes everything work
    prefix = repo.relative_to(ws_root)
    super_repo = ws_root
    
    try:
        subprocess.check_output(["git", "ls-tree", "HEAD", str(prefix)], cwd=super_repo)
        exists = True
    except subprocess.CalledProcessError:
        exists = False
        
    if not exists:
        git(super_repo, "subtree", "add", "--prefix", str(prefix), str(repo), MAIN_BRANCH)
    else:
        git(super_repo, "subtree", "pull", "--prefix", str(prefix), str(repo), MAIN_BRANCH)

    git(super_repo, "add", ".")
    git(super_repo, "commit", "-m", f"sync: {prefix}")

def is_binary_repo(repo):
    return (repo / ".bin").exists()

def sha256(path):
    # calculate SHA256 hash of a file, used for releasing
    h = hashlib.sha256()
    
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024), b""):
            h.update(chunk)
            
    return h.hexdigest()

def latest_version(repo):
    # check tags in repo to find the most recent version
    tags = subprocess.check_output(
        ["git", "tag"],
        cwd=repo,
        text=True
    ).splitlines()
    
    versions = []
    for t in tags:
        if SEMVER.match(t) or HOTFIX.match(t):
            versions.append(t)
            
    return sorted(versions)[-1] if versions else "v0.0.0"

def bump_version(repo, bump):
    # find latest version and bump one part of it to generate next release tag
    base = latest_version(repo)
    
    m = SEMVER.match(base) # gets rid of hotfix
    if not m:
        raise WMException("Cannot bump non-semver base")
    
    major, minor, patch = map(int, m.groups())
    
    match bump:
        case "major":
            return f"v{major + 1}.0.0"
        case "minor":
            return f"v{major}.{minor + 1}.0"
        case "patch":
            return f"v{major}.{minor}.{patch + 1}"
    
    raise WMException("Unknown bump keyword")

def next_hotfix(base, repo):
    # find the latest hotfix version and bump it
    tags = subprocess.check_output(
        ["git", "tag"],
        cwd=repo,
        text=True
    ).splitlines()
    
    hotfixes = [
        int(m.group(2))
        for t in tags
        if (m := HOTFIX.match(t)) and m.group(1) == base
    ]
    
    n = max(hotfixes) + 1 if hotfixes else 1
    return f"{base}-hotfix.{n}"

def write_manifest(repo, version, artifact_dir, outputs, mode):
    # write and store a release manifest
    commit = subprocess.check_output( # get commit details
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    
    branch = subprocess.check_output( # get branch details
        ["git", "branch", "--show-current"], cwd=repo, text=True
    ).strip()

    artifacts = { # hash checksums
        f.name : {"sha256" : sha256(f)}
        for f in outputs
    }
    
    manifest = {
        "repo" : repo_key(repo),
        "version" : version,
        "mode" : mode,
        "timestamp" : datetime.now(timezone.utc).isoformat(),
        "source" : {
            "commit" : commit,
            "branch" : branch
        },
        "artifacts": artifacts
    }

    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )

def build(repo, cfg, dest):
    # build a repo and store output artifacts in dest
    run(cfg["cmd"], repo)
    dest.mkdir(parents=True, exist_ok=True)
    
    outputs = []
    for out in cfg["outputs"]:
        src = repo / out
        tgt = dest / src.name
        
        run(["cp", src, tgt], repo)
        outputs.append(tgt)
        
    return outputs

def branch_exists(repo, branch):
    # check if a branch exists
    out = subprocess.check_output(
        ["git", "branch", "--list", branch],
        cwd=repo,
        text=True
    ).strip()
    
    return bool(out)


def current_branch(repo):
    # check which branch is currently checked out
    return subprocess.check_output(
        ["git", "branch", "--show-current"],
        cwd=repo,
        text=True
    ).strip()

# ------------------------------- CLI FUNCTIONS ------------------------------ #
def new_repo(group, name, ws_root):
    path = ws_root / group / name
    path.mkdir(parents=True, exist_ok=True)
    
    git(path, "init")
    git(path, "checkout", "-b", MAIN_BRANCH)
    
    # make initial commit in main branch
    (path / ".gitkeep").write_text("")
    (path / ".gitignore").write_text("*/.vscode\n")
    
    git(path, "add", ".")
    git(path, "commit", "-m", "initial commit")

    git(path, "checkout", "-b", "dev") # create dev branch and checkout
    
    sync_to_super(path, ws_root) # sync to super
    
    print(f"[new] Created {path} as local repo.")
    
def init_super_repo(ws_root, remote_url: str | None = None):
    # init super repo
    if not (ws_root / ".git").exists():
        run(["git", "init"], ws_root)
        run(["git", "checkout", "-b", MAIN_BRANCH], ws_root)

    # create standard workspace directories
    (ws_root / "dev_builds").mkdir(exist_ok=True)
    (ws_root / ARTIFACT_REPO_NAME).mkdir(exist_ok=True)

    # create artifact repo
    artifact_repo = ws_root / ARTIFACT_REPO_NAME
    if not (artifact_repo / ".git").exists():
        run(["git", "init"], artifact_repo)
        run(["git", "checkout", "-b", MAIN_BRANCH], artifact_repo)
        git(artifact_repo, "commit", "--allow-empty", "-m", "init artifact repo")

    # create workspace config file
    cfg = ws_root / "workspace_config.json"
    if not cfg.exists():
        cfg.write_text(
            json.dumps(
                {
                    "projects" : {},
                    "builds" : {},
                },
                indent=2
            )
        )

    # create git ignore file
    gitignore = ws_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("dev_builds/\n*/.git\n*/.vscode\n")

    # commit super repo
    git(ws_root, "add", ".")
    git(ws_root, "commit", "-m", "chore: initialize workspace")

    # connect to remote
    if remote_url:
        git(ws_root, "remote", "add", "origin", remote_url)
        git(ws_root, "push", "-u", "origin", MAIN_BRANCH)

    print("[init-super] Workspace initialised")
        
def init_submodules(repos):
    for repo in repos:
        print(f"[init-submodules] Initialising submodules in {repo}.")
        
        try:
            git(repo, "submodule", "update", "--init", "--recursive")
        except subprocess.CalledProcessError:
            print(f"[init-submodules] No submodules found in {repo}, skipping.")

def commit_workspace_changes(ws_root):
    git(ws_root, "add", WS_CONFIG_FILE)
    git(ws_root, "add", ".gitignore")
    
    git(ws_root, "commit", "-m", "chore: update workspace config and gitignore")
    
    print("[commit-config] Workspace config committed.")
    
def push_super_repo(ws_root):
    git(ws_root, "push")
    print("[push] Super repo pushed to remote.")
    
def pull_super_repo(ws_root):
    git(ws_root, "pull")
    print("[pull] Super repo pulled from remote.")
    
def mark_binary(repos, ws_root):
    for repo in repos:
        bin_file = repo / ".bin"
        bin_file.touch(exist_ok=True)
        
        git(repo, "add", ".bin")
        git(repo, "lfs", "install")
        for pattern in LFS_PATTERNS:
            git(repo, "lfs", "track", pattern)
            
        git(repo, "add", ".gitattributes")
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
        if status.strip():
            git(repo, "commit", "-m", "chore: mark repo as binary and enable LFS")
        
        sync_to_super(repo, ws_root)
        
        print(f"[mark-bin] Marked {repo} as binary.")
    
def build_without_release(repos, ws_config):
    dev_build_root = ws_config["root"] / "dev_builds"
    dev_build_root.mkdir(exist_ok=True)

    for repo in repos:
        key = repo_key(repo)
        if key not in ws_config["builds"]:
            continue

        cfg = ws_config["builds"][key]
        dest = dev_build_root / key
        
        build(repo, cfg, dest)

        print(f"[build] dev output → {dest}")
        
def release(repos, ws_config, bump=None, hotfix_base=None):    
    repo0 = repos[0]
    base = latest_version(repo0)

    if hotfix_base:
        version = next_hotfix(hotfix_base, repo0)
        mode = "hotfix"
    else:
        version = bump_version(base, bump)
        mode = "release"

    for repo in repos:
        git(repo, "checkout", "main")
        git(repo, "pull")

    for repo in repos:
        key = repo_key(repo)
        if key not in ws_config["builds"]:
            continue

        release_dir = ARTIFACT_REPO_NAME / key / version
        outputs = build(repo, ws_config["builds"][key], release_dir)

        write_manifest(
            repo=repo,
            version=version,
            artifact_dir=release_dir,
            outputs=outputs,
            mode=mode
        )

        git(repo, "tag", "-a", version, "-m", version)
        git(repo, "push", "--tags")

    git(ARTIFACT_REPO_NAME, "add", ".")
    git(ARTIFACT_REPO_NAME, "commit", "-m", f"{mode}: {version}")
    git(ARTIFACT_REPO_NAME, "push")
    
    sync_to_super(ws_config["root"] / ARTIFACT_REPO_NAME, ws_config["root"])
    
def status(repos):
    for repo in repos:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=repo,
            text=True
        ).strip()
        
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo,
            text=True
        ).strip() != ""
        
        lfs = ".gitattributes" in [p.name for p in (repo / ".").glob("*gitattributes")]
        binary = is_binary_repo(repo)
        
        print(f"{repo_key(repo)} | branch: {branch} | binary: {binary} | LFS: {lfs} | dirty: {dirty}")

def verify_release(version, repo_filter=None):
    failures = False

    for manifest in ARTIFACT_REPO_NAME.rglob("manifest.json"):
        data = json.loads(manifest.read_text())
        
        if data["version"] != version:
            continue
        
        if repo_filter and data["repo"] not in repo_filter:
            continue

        release_dir = manifest.parent

        for name, meta in data["artifacts"].items():
            path = release_dir / name
            if not path.exists():
                print(f"[FAIL] {data['repo']} missing {name}")
                failures = True
                continue

            actual = sha256(path)
            if actual != meta["sha256"]:
                print(f"[FAIL] {data['repo']} {name} hash mismatch")
                failures = True
            else:
                print(f"[OK] {data['repo']} {name}")

    if failures:
        sys.exit(1)
        
def checkout_branch(branch, repo):
    if not branch_exists(repo, branch):
        raise WMException(f"{branch} does not exist in {repo_key(repo)}")
    
    git(repo, "checkout", branch)
    print(f"[checkout] {repo_key(repo)} → {branch}")
    
def switch_branch(branch, repo):
    if branch_exists(repo, branch):
        git(repo, "checkout", branch)
        print(f"[switch] {repo_key(repo)} → {branch}")
    else:
        raise WMException(f"{repo_key(repo)} (no {branch})")
    
def list_current_branch(repos):
    for repo in repos:
        print(f"{repo_key(repo)} → {current_branch(repo)}")
        
def list_all_branches(repo):
    out = subprocess.check_output(
        ["git", "branch"],
        cwd=repo,
        text=True
    )
    
    print(out.rstrip())

def start_branch(kind, name, repo):
    if kind == "feature":
        base = "dev"
        branch = f"feature/{name}"
    elif kind == "hotfix":
        base = "main"
        branch = f"hotfix/{name}"
    else:
        raise WMException("Kind must be feature or hotfix")

    git(repo, "checkout", base)
    git(repo, "pull")

    if branch_exists(repo, branch):
        git(repo, "checkout", branch)
        print(f"[resume] {repo_key(repo)} → {branch}")
    else:
        git(repo, "checkout", "-b", branch)
        print(f"[start] {repo_key(repo)} → {branch}")
            
def finish_branch(kind, name, repo):
    if kind == "feature":
        branch = f"feature/{name}"
        targets = ["dev"]
    elif kind == "hotfix":
        branch = f"hotfix/{name}"
        targets = ["main", "dev"]
    else:
        raise WMException("Kind must be feature or hotfix")

    if not branch_exists(repo, branch):
        raise WMException(f"{branch} does not exist in {repo_key(repo)}")

    for target in targets:
        git(repo, "checkout", target)
        git(repo, "pull")
        git(repo, "merge", "--no-ff", branch)

    git(repo, "branch", "-d", branch)
    print(f"[finish] {repo_key(repo)} {branch} → {', '.join(targets)}")

# -------------------------------- CLI HANDLER ------------------------------- #
def main():
    if len(sys.argv) < 2:
        raise WMException("No subcommand provided")
    
    cmd = sys.argv[1]
    ws_alias = sys.argv[2]
    args = sys.argv[3:]
    
    ws_root = get_ws_root(ws_alias)
    if ws_root is None:
        raise WMException("Unknown workspace alias")
    ws_root = Path(ws_root)
    
    with open(ws_root / WS_CONFIG_FILE, "r") as f:
        ws_config = json.load(f)
    ws_config["root"] = ws_root
    
    match cmd:
        case "new":
            new_repo(*get_new_repo_name(args), ws_root)
        case "super-init":
            init_super_repo(ws_root, args[0] if args else None)
        case "super-sync":
            for repo in find_repos(args, ws_config):
                sync_to_super(repo, ws_root)
        case "init-submodules":
            repos = find_repos(args if args else ["."], ws_config)
            init_submodules(repos)
        case "super-commit":
            commit_workspace_changes(ws_root)
        case "super-push":
            push_super_repo(ws_root)
        case "super-pull":
            pull_super_repo(ws_root)
        case "mark-bin":
            repos = find_repos(args, ws_config)
            mark_binary(repos, ws_root)
        case "build":
            repos = find_repos(args, ws_config)
            build(repos, ws_config)
        case "status":
            repos = find_repos(args, ws_config)
            status(repos)
        case "release":
            bump = args[0]
            repos = find_repos(args[1:])
            release(repos, bump)
        case "release-hotfix":
            base = args[0]
            repos = find_repos(args[1:])
            release(repos, hotfix_base=base)
        case "release-verify":
            version = args[0]
            repos = find_repos(args[1:])
            verify_release(version, repos)
        case "branch-checkout":
            checkout_branch(args[0], find_repos(args[1:])[0])
        case "branch-switch":
            switch_branch(args[0], find_repos(args[1:])[0])
        case "branch-list-current":
            list_current_branch(find_repos(args, ws_config))
        case "branch-list-all":
            list_all_branches(find_repos(args[1:])[0])
        case "branch-start":
            start_branch(args[0], args[1], find_repos(args[1:])[0])
        case "branch-finish":
            finish_branch(args[0], args[1], find_repos(args[1:])[0])
        case _:
            raise WMException("Unknown command")
            
if __name__ == "__main__":
    main()