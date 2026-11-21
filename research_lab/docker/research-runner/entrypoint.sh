#!/bin/sh
# Container entrypoint. Forwards to whatever python module the worker requested.
exec "$@"
