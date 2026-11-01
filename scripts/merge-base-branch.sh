#!/usr/bin/env bash
# Merge the base branch into the checked-out PR branch for the finalize workflow.
#   usage: scripts/merge-base-branch.sh <base-ref> <unresolvable-file>
# Conflicts are resolved automatically only when they are the expected result of
# an ID collision: id-registry.yaml (take the base registry; finalize re-adds this
# branch's entries) and requirement files added on both sides (keep the branch
# version; finalize renumbers it). Anything else aborts the merge, lists the
# paths in <unresolvable-file> and exits 1.
set -uo pipefail
base="$1"; out="$2"
: > "$out"
if git merge --no-edit "$base"; then
  exit 0
fi
while IFS= read -r line; do
  code="${line:0:2}"; path="${line:3}"
  case "$code" in
    UU|AA|DD|AU|UA|DU|UD) ;;
    *) continue ;;
  esac
  case "$path" in
    id-registry.yaml)
      git checkout "$base" -- id-registry.yaml ;;
    requirements/*)
      if [ "$code" = AA ]; then
        git checkout --ours -- "$path" && git add -- "$path"
      else
        echo "$path ($code)" >> "$out"
      fi ;;
    *) echo "$path ($code)" >> "$out" ;;
  esac
done < <(git status --porcelain)
if [ -s "$out" ]; then
  git merge --abort
  exit 1
fi
git commit --no-edit
