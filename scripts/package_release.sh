#!/usr/bin/env bash
set -euo pipefail

export GIT_NO_REPLACE_OBJECTS=1
export GIT_ATTR_NOSYSTEM=1
export GIT_CONFIG_NOSYSTEM=1
unset GIT_CONFIG_COUNT

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

die() {
  printf '[nexpoly-release] %s\n' "$*" >&2
  exit 1
}

for command_name in git python3 tar gzip; do
  command -v "$command_name" >/dev/null 2>&1 || die "Required command is unavailable: $command_name"
done

INCLUDE_DATA="${INCLUDE_DATA:-0}"
case "$INCLUDE_DATA" in
  0|1)
    ;;
  *)
    die "INCLUDE_DATA must be exactly 0 or 1."
    ;;
esac

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Run this script inside a Git worktree."
[[ "$(git rev-parse --show-toplevel)" == "$ROOT_DIR" ]] || die "Run this script from its repository worktree."
git rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1 || die "A valid Git HEAD commit is required."
mapfile -t REPLACE_REFS < <(git replace -l)
[[ "${#REPLACE_REFS[@]}" -eq 0 ]] || die "Git replace refs are not allowed for release packaging."
for local_git_metadata in info/attributes info/grafts; do
  metadata_path="$(git rev-parse --git-path "$local_git_metadata")"
  if [[ -e "$metadata_path" || -L "$metadata_path" ]]; then
    [[ -f "$metadata_path" && ! -L "$metadata_path" && ! -s "$metadata_path" ]] \
      || die "Git $local_git_metadata must be an empty regular file when present."
  fi
done
SOURCE_COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"

verify_clean_checkout() {
  local index_entry
  local index_flag
  local -a index_entries
  local -a untracked_paths

  mapfile -d '' -t index_entries < <(git ls-files -v -z)
  for index_entry in "${index_entries[@]}"; do
    index_flag="${index_entry:0:1}"
    case "$index_flag" in
      S|[a-z])
        die "Tracked paths with assume-unchanged or skip-worktree flags are not allowed."
        ;;
    esac
  done

  git diff --quiet --ignore-submodules=none || die "Tracked unstaged changes are present; package only a clean HEAD."
  git diff --cached --quiet --ignore-submodules=none || die "Staged changes are present; package only a clean HEAD."
  mapfile -d '' -t untracked_paths < <(git ls-files --others --exclude-standard -z)
  [[ "${#untracked_paths[@]}" -eq 0 ]] || die "Unignored untracked paths are present; package only a clean HEAD."
}

verify_clean_checkout

TEST_SKIP_BUILD="${NEXPOLY_RELEASE_TEST_SKIP_BUILD:-0}"
case "$TEST_SKIP_BUILD" in
  0)
    command -v npm >/dev/null 2>&1 || die "Required command is unavailable: npm"
    npm --prefix frontend run build
    ;;
  1)
    git cat-file -e 'HEAD:.nexpoly-release-test-fixture' 2>/dev/null \
      || die "NEXPOLY_RELEASE_TEST_SKIP_BUILD is restricted to committed release test fixtures."
    printf '[nexpoly-release] Skipping frontend build for committed test fixture.\n'
    ;;
  *)
    die "NEXPOLY_RELEASE_TEST_SKIP_BUILD must be exactly 0 or 1."
    ;;
esac

[[ "$(git rev-parse --verify 'HEAD^{commit}')" == "$SOURCE_COMMIT" ]] \
  || die "HEAD changed during the frontend build; retry from a stable checkout."
verify_clean_checkout

python3 scripts/release_package.py \
  --root "$ROOT_DIR" \
  --commit "$SOURCE_COMMIT" \
  --include-data "$INCLUDE_DATA"
