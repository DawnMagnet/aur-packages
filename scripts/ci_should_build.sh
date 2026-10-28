#!/bin/bash
# Print "true" when this CI event should test-build and publish.
# Usage: ci_should_build.sh <changed:true|false> <event> <force:true|false|>
# force_publish is honored only for workflow_dispatch. Schedule and push
# runs pass a false force value; a true value on those events is an error.
set -euo pipefail

changed="${1:?changed}"
event="${2:?event}"
force="${3:-false}"

case "$changed" in
  true|false) ;;
  *)
    echo "invalid changed value: $changed" >&2
    exit 1
    ;;
esac

case "$force" in
  true|false|'') ;;
  *)
    echo "invalid force_publish value" >&2
    exit 1
    ;;
esac

if [[ -z "$force" ]]; then
  force=false
fi

if [[ "$event" != "workflow_dispatch" && "$force" == "true" ]]; then
  echo "force_publish is only valid for workflow_dispatch" >&2
  exit 1
fi

if [[ "$changed" == "true" || ( "$event" == "workflow_dispatch" && "$force" == "true" ) ]]; then
  printf '%s\n' true
else
  printf '%s\n' false
fi
