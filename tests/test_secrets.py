"""Checks that the file holding passwords and a token is not sitting in a repo.

Idea, design and architecture by Stas Mitin.
Copyright (c) 2026 Stas Mitin. SPDX-License-Identifier: MIT
"""
import io
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkfinder.engine import exposed_by_git

FAILURES = []


def check(name, got, want):
    ok = got == want
    print("%-62s %s" % (name, "ok" if ok else "FAIL"))
    if not ok:
        FAILURES.append("%s: got %r want %r" % (name, got, want))


def git(where, *args):
    return subprocess.run(["git", "-C", where] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=30)


print("-- a secret written into a repository is one `git add .` from leaving --")
# Finding a folder that is NOT in a work tree is harder than it sounds: a home
# directory can itself be a repository, and then everything under it - the temp
# folder included - is inside one too.
def outside_any_repo():
    for base in (tempfile.gettempdir(), os.path.abspath(os.sep + "Temp"),
                 os.path.abspath(os.sep)):
        try:
            where = tempfile.mkdtemp(prefix="lf-plain-", dir=base)
        except OSError:
            continue
        probe = os.path.join(where, ".linkfinder.json")
        io.open(probe, "w", encoding="utf-8").write("{}")
        if not exposed_by_git(probe):
            return probe
    return ""


loose = outside_any_repo()
if loose:
    check("a folder outside any repository is not a worry",
          exposed_by_git(loose), "")
else:
    print("%-62s %s" % ("every writable folder here is inside a repository",
                        "noted"))

repo = tempfile.mkdtemp(prefix="lf-repo-")
started = git(repo, "init")
if started.returncode != 0:
    print("skipped: no git here")
else:
    inside = os.path.join(repo, ".linkfinder.json")
    io.open(inside, "w", encoding="utf-8").write("{}")
    said = exposed_by_git(inside)
    check("a file in a work tree is called out", bool(said), True)
    check("and the line says why",
          "git add" in said and "not ignored" in said, True)

    git(repo, "remote", "add", "origin", "https://example.invalid/some/repo.git")
    said = exposed_by_git(inside)
    check("naming the remote, so it is clear where it would go",
          "example.invalid" in said, True)

    # The negative control: once git is told to leave it alone, the warning
    # must stop. A warning that cannot be satisfied is one people learn to
    # ignore.
    io.open(os.path.join(repo, ".gitignore"), "w",
            encoding="utf-8").write(".linkfinder.json\n")
    check("ignoring it settles the matter", exposed_by_git(inside), "")

    io.open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8").write("\n")
    check("and removing that brings the warning back",
          bool(exposed_by_git(inside)), True)

print()
if FAILURES:
    print("FAILURES:")
    for line in FAILURES:
        print("  " + line)
    sys.exit(1)
print("all secret-placement checks passed")
