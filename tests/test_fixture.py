"""Test of the fictional sandbox data. The pre-commit hook runs it."""
from tools.reqcheck import gitio
from tools.reqcheck.rules import check_tree


def test_tree_is_valid_in_branch_mode():
    assert check_tree(gitio.from_worktree("."), branch=True) == []
