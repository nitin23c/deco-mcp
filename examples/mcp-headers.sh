#!/bin/sh
# Claude Code headersHelper for deco-mcp on macOS.
# Prints the Authorization header, reading the token from the login Keychain,
# so the token never sits in a Claude Code config file.
#
# Store the token once (paste it at the hidden prompt):
#   security add-generic-password -a deco-mcp -s deco-mcp-token -w
t=$(security find-generic-password -a deco-mcp -s deco-mcp-token -w 2>/dev/null) || {
  echo "deco-mcp token not found in Keychain" >&2; exit 1; }
printf '{"Authorization": "Bearer %s"}\n' "$t"
