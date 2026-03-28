#!/usr/bin/env bash

# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mvp-ai-lab/mvp-orbit/main/install-skills.sh | bash
#   INSTALL_TARGET=claude curl -fsSL https://raw.githubusercontent.com/mvp-ai-lab/mvp-orbit/main/install-skills.sh | bash

set -euo pipefail

REPO_OWNER="${REPO_OWNER:-mvp-ai-lab}"
REPO_NAME="${REPO_NAME:-mvp-orbit}"
REPO_REF="${REPO_REF:-main}"
ARCHIVE_URL="${ARCHIVE_URL:-https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REPO_REF}.tar.gz}"
SKILLS_SUBDIR="${SKILLS_SUBDIR:-skills}"
INSTALL_TARGET="${INSTALL_TARGET:-}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

prompt_install_target() {
  if [[ -n "$INSTALL_TARGET" ]]; then
    return
  fi

  printf 'Select the client to install skills for:\n'
  printf '  1) Codex\n'
  printf '  2) Claude\n'
  read -r -p 'Enter 1 or 2: ' selection

  case "$selection" in
    1)
      INSTALL_TARGET="codex"
      ;;
    2)
      INSTALL_TARGET="claude"
      ;;
    *)
      printf 'Invalid selection: %s\n' "$selection" >&2
      exit 1
      ;;
  esac
}

normalize_install_target() {
  INSTALL_TARGET="$(printf '%s' "$INSTALL_TARGET" | tr '[:upper:]' '[:lower:]')"

  case "$INSTALL_TARGET" in
    codex|claude)
      ;;
    *)
      printf 'Unsupported INSTALL_TARGET: %s\n' "$INSTALL_TARGET" >&2
      printf 'Expected one of: codex, claude\n' >&2
      exit 1
      ;;
  esac
}

resolve_install_root() {
  case "$INSTALL_TARGET" in
    codex)
      printf '%s/skills' "${CODEX_HOME:-$HOME/.codex}"
      ;;
    claude)
      printf '%s/skills' "${CLAUDE_HOME:-$HOME/.claude}"
      ;;
  esac
}

download_archive() {
  local archive_path="$1"
  printf 'Downloading skills from %s\n' "$ARCHIVE_URL"
  curl -fsSL "$ARCHIVE_URL" -o "$archive_path"
}

find_repo_root() {
  local extract_root="$1"
  local repo_root

  repo_root="$(find "$extract_root" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "$repo_root" ]]; then
    printf 'Failed to locate extracted repository root.\n' >&2
    exit 1
  fi

  printf '%s' "$repo_root"
}

main() {
  require_cmd curl
  require_cmd tar
  require_cmd find
  require_cmd cp
  require_cmd mktemp
  require_cmd sort

  prompt_install_target
  normalize_install_target

  local install_root
  install_root="$(resolve_install_root)"

  local tmpdir
  tmpdir="$(mktemp -d)"
  trap 'rm -rf '"'"$tmpdir"'"'' EXIT

  local archive_path
  archive_path="$tmpdir/repo.tar.gz"
  download_archive "$archive_path"

  tar -xzf "$archive_path" -C "$tmpdir"

  local repo_root
  repo_root="$(find_repo_root "$tmpdir")"

  local source_root
  source_root="$repo_root/$SKILLS_SUBDIR"
  if [[ ! -d "$source_root" ]]; then
    printf 'Skills directory not found in archive: %s\n' "$source_root" >&2
    exit 1
  fi

  mkdir -p "$install_root"

  local installed_count=0
  while IFS= read -r skill_dir; do
    local skill_name
    skill_name="$(basename "$skill_dir")"

    local destination
    destination="$install_root/$skill_name"

    if [[ -e "$destination" ]]; then
      local backup
      backup="${destination}.bak.$(date +%Y%m%d%H%M%S)"
      mv "$destination" "$backup"
      printf 'Backed up existing skill: %s -> %s\n' "$destination" "$backup"
    fi

    cp -R "$skill_dir" "$install_root/"
    printf 'Installed skill: %s\n' "$skill_name"
    installed_count=$((installed_count + 1))
  done < <(find "$source_root" -mindepth 1 -maxdepth 1 -type d | LC_ALL=C sort)

  if [[ "$installed_count" -eq 0 ]]; then
    printf 'No skill directories found under %s\n' "$source_root" >&2
    exit 1
  fi

  printf 'Installed %d skill(s) into %s\n' "$installed_count" "$install_root"
}

main "$@"
