#!/usr/bin/env bash
# Run from the project root on codex-lab. Ctrl+C stops watching, not the API.
set -eu
trace_log="${1:-.runtime/knowledge-summary/backend.log}"
if [[ ! -f "$trace_log" ]]; then
    echo "Backend log not found: $trace_log" >&2
    exit 1
fi
tail -n 0 -F "$trace_log" | grep --line-buffered 'KNOWLEDGE_TRACE'
