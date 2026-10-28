#!/usr/bin/env bash
# Print the numbers of open Arch-digest reminder issues, oldest first.
#
# An issue counts only if github-actions opened it, it carries $LABEL and
# its title is exactly $TITLE. Matching on the title alone would let anyone
# pre-create a same-title issue and have the workflow edit it. Needs REPO,
# TITLE, LABEL and a GH_TOKEN that gh can use; read-only.
set -euo pipefail
: "${REPO:?}" "${TITLE:?}" "${LABEL:?}"

# `--author` takes `github-actions[bot]` (the GraphQL createdBy filter does not
# accept `app/github-actions`); the JSON reports it as `app/github-actions`.
gh issue list -R "$REPO" --state open --author 'github-actions[bot]' --label "$LABEL" \
  --limit 100 --json number,title,author,labels |
  jq -r --arg t "$TITLE" --arg l "$LABEL" '
    map(select(
      .author.login == "app/github-actions" and .author.is_bot == true
      and .title == $t
      and any(.labels[]; .name == $l)
    ))
    | sort_by(.number) | .[].number'
