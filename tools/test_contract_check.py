"""Tests for tools/contract_check.py on throwaway git repos (pytest tmp_path)."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import contract_check as cc  # noqa: E402

SUB_A = "c992aa61fdfe0846c5ed90324c596202a1c69b76"
SUB_B = "0123456789abcdef0123456789abcdef01234567"

BASE_FILES = {
    "src/tt_um_fesens_r32v.v": "module tt_um_fesens_r32v; endmodule\n",
    "src/alu.v": "module alu; endmodule\n",
    "src/config.json": "{\"CLOCK_PERIOD\": 50}\n",
    "verif/cosim/harness.cpp": "int main() { return 0; }\n",
    "tools/eval.py": "print('eval')\n",
    "test/test.py": "# cocotb test\n",
    "info.yaml": "project: {}\n",
    "docs/contract.md": "# contract\n",
    "docs/notes.md": "# notes\n",
    "CLAUDE.md": "# rules\n",
    "Makefile": "eval:\n\ttrue\n",
    ".github/workflows/gds.yaml": "name: gds\n",
    "loop/ledger.jsonl": "",
    ".claude/skills/area-loop/SKILL.md": "# skill\n",
    "physical/tilefit.py": "print('tilefit')\n",
}


def g(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def write(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def commit(repo, msg="change"):
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


def set_submodule(repo, sha):
    g(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},third_party/riscv-formal")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}.items():
        monkeypatch.setenv(k, v)
    r = str(tmp_path / "repo")
    os.makedirs(r)
    g(r, "init", "-q", "-b", "main")
    for rel, text in BASE_FILES.items():
        write(r, rel, text)
    # An unpopulated submodule: a gitlink in the index plus an empty directory.
    os.makedirs(os.path.join(r, "third_party", "riscv-formal"))
    g(r, "add", "-A")
    set_submodule(r, SUB_A)
    g(r, "commit", "-q", "-m", "base")
    return r


def base_sha(repo):
    return g(repo, "rev-list", "--max-parents=0", "HEAD")


def run(repo, physical=False):
    return cc.check(repo, base_sha(repo), "HEAD", physical=physical)


def test_fixture_has_gitlink(repo):
    assert cc.object_id(repo, "HEAD", "third_party/riscv-formal") == SUB_A


def test_identical_trees_pass(repo):
    ok, lines = run(repo)
    assert ok, lines
    assert "PASS: 0 changed path(s)" in lines[-1]


def test_allowed_src_changes_pass(repo):
    write(repo, "src/tt_um_fesens_r32v.v", "module tt_um_fesens_r32v; wire x; endmodule\n")
    write(repo, "src/serial_alu.v", "module serial_alu; endmodule\n")
    write(repo, "src/sub/dir/regfile.v", "module regfile; endmodule\n")
    write(repo, "src/test_serial_alu.py", "def test_x(): pass\n")
    os.remove(os.path.join(repo, "src", "alu.v"))
    commit(repo)
    ok, lines = run(repo)
    assert ok, lines
    assert "PASS: 5 changed path(s)" in lines[-1]


def test_verif_change_fails(repo):
    write(repo, "verif/cosim/harness.cpp", "int main() { return 1; }\n")
    commit(repo)
    ok, lines = run(repo)
    text = "\n".join(lines)
    assert not ok
    assert "verif/cosim/harness.cpp (modified)" in text
    assert "  verif:" in text          # the fingerprint catches it independently


def test_src_config_json_fails(repo):
    write(repo, "src/config.json", "{\"CLOCK_PERIOD\": 100}\n")
    commit(repo)
    ok, lines = run(repo)
    text = "\n".join(lines)
    assert not ok
    assert "src/config.json (modified): src/config.json is contract" in text
    assert "  src/config.json:" in text


def test_src_config_json_delete_fails(repo):
    os.remove(os.path.join(repo, "src", "config.json"))
    commit(repo)
    ok, lines = run(repo)
    assert not ok
    assert any("src/config.json (deleted)" in l for l in lines)


@pytest.mark.parametrize("rel", ["tools/eval.py", "test/test.py", "info.yaml",
                                 "docs/contract.md", "CLAUDE.md", "Makefile",
                                 ".github/workflows/gds.yaml", "loop/ledger.jsonl",
                                 ".claude/skills/area-loop/SKILL.md", "docs/notes.md"])
def test_each_contract_path_change_fails(repo, rel):
    write(repo, rel, "tampered\n")
    commit(repo)
    ok, lines = run(repo)
    assert not ok
    assert any(f"{rel} (modified)" in l for l in lines), lines


@pytest.mark.parametrize("rel", ["README.md", ".gitignore", ".gitmodules", "verif/new_check.sv",
                                 "srcx/core.v", "physicalx/x.tcl"])
def test_new_files_outside_allowed_paths_fail(repo, rel):
    write(repo, rel, "new\n")
    commit(repo)
    ok, lines = run(repo, physical=True)
    assert not ok
    assert any(f"{rel} (added)" in l for l in lines), lines


def test_deletion_outside_src_fails(repo):
    os.remove(os.path.join(repo, "docs", "notes.md"))
    commit(repo)
    ok, lines = run(repo)
    assert not ok
    assert any("docs/notes.md (deleted)" in l for l in lines)


def test_rename_out_of_src_fails(repo):
    g(repo, "mv", "src/alu.v", "verif/alu.v")
    commit(repo)
    ok, lines = run(repo)
    text = "\n".join(lines)
    assert not ok
    assert "verif/alu.v (added)" in text


def test_rename_into_src_fails(repo):
    # With rename detection, --name-only would only show src/eval.py; the deletion must count.
    g(repo, "mv", "tools/eval.py", "src/eval.py")
    commit(repo)
    ok, lines = run(repo)
    text = "\n".join(lines)
    assert not ok
    assert "tools/eval.py (deleted)" in text
    assert "  tools:" in text


def test_physical_needs_flag(repo):
    write(repo, "physical/tilefit.py", "print('denser')\n")
    write(repo, "physical/macro/rf.tcl", "# placement\n")
    commit(repo)
    ok, lines = run(repo)
    assert not ok
    assert any("physical/tilefit.py (modified): physical/** needs --physical" in l for l in lines)
    ok, lines = run(repo, physical=True)
    assert ok, lines


def test_submodule_pointer_change_fails(repo):
    set_submodule(repo, SUB_B)
    g(repo, "commit", "-q", "-m", "bump submodule")
    ok, lines = run(repo)
    text = "\n".join(lines)
    assert not ok
    assert "third_party/riscv-formal submodule pointer" in text
    assert "third_party/riscv-formal (modified)" in text


def test_symlink_in_src_fails(repo):
    os.symlink("../tools/eval.py", os.path.join(repo, "src", "evil.v"))
    commit(repo)
    ok, lines = run(repo)
    assert not ok
    assert any("src/evil.v (added): symlinks and submodules" in l for l in lines)


def test_reverted_contract_edit_passes_on_tree_diff(repo):
    # The check compares trees: an edit undone later in the branch leaves no difference.
    write(repo, "Makefile", "eval:\n\tfalse\n")
    commit(repo, "tamper")
    write(repo, "Makefile", BASE_FILES["Makefile"])
    write(repo, "src/alu.v", "module alu; wire y; endmodule\n")
    commit(repo, "undo and change src")
    ok, lines = run(repo)
    assert ok, lines


def test_branch_on_older_base_fails_when_contract_moved(repo):
    old = base_sha(repo)
    write(repo, "tools/eval.py", "print('eval v2')\n")
    new_base = commit(repo, "lead updates the evaluation")
    g(repo, "checkout", "-q", "-b", "hyp", old)
    write(repo, "src/alu.v", "module alu; wire z; endmodule\n")
    hyp = commit(repo, "hypothesis")
    ok, lines = cc.check(repo, new_base, hyp)
    assert not ok
    assert any("tools/eval.py (modified)" in l for l in lines)


def test_cli_exit_codes(repo):
    script = os.path.join(HERE, "contract_check.py")
    base = base_sha(repo)
    p = subprocess.run([sys.executable, script, "--repo", repo, base, "HEAD"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    write(repo, "verif/cosim/harness.cpp", "// changed\n")
    commit(repo)
    p = subprocess.run([sys.executable, script, "--repo", repo, base, "HEAD"],
                       capture_output=True, text=True)
    assert p.returncode == 1
    assert "verif/cosim/harness.cpp" in p.stdout
    p = subprocess.run([sys.executable, script, "--repo", repo, "no-such-rev", "HEAD"],
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert "ERROR" in p.stderr
