# syntax=docker/dockerfile:1.7
# research-runner:m3-latest = research-runner:m2-latest + the M3 strategy contract,
# layered so we NEVER reinstall the ~5-7 GB vbt-pro stack per milestone (the base
# already has it). See the auto-memory "research-runner-image-shared-base".
#
# Build CONTEXT is research_lab/ (so the strategies/ package can be baked). Build:
#   cd research_lab
#   DOCKER_BUILDKIT=1 docker build -f docker/research-runner/Dockerfile \
#     --build-arg GIT_COMMIT=$(git rev-parse HEAD) -t research-runner:m3-latest .
# No --secret needed: VBT Pro is already installed in the base image.
#
# Note: this chains on the m2-latest tag. A fresh machine builds m2-latest first from
# ./Dockerfile.m2-base (restored to version control by pre-M4 Brief A1; it pins
# python:3.12.13-slim + vbt commit b1727ac6 from the token). A machine that already has
# m2-latest re-bakes only the fetch path cheaply via ./Dockerfile.m2-patch (no vbt
# reinstall). The cleaner long-game shape is a shared research-runner:base both milestones
# layer onto; deferred (memory: research-runner-image-shared-base).
FROM research-runner:m2-latest

# Bake the build-provenance string the run-backtest entrypoint reads into meta.json
# (DECISIONS #20). The base ends as USER runner, so write /etc as root then drop back.
# GIT_COMMIT is passed at build (.git is not in the context); vbt commit is the pin above.
USER root
ARG GIT_COMMIT=unknown
RUN echo "git:${GIT_COMMIT}+vbt:b1727ac6" > /etc/build_provenance

# Bake ONLY the strategy CONTRACT (arch §7 / M4 R1), not the user strategy files. The package
# marker (strategies/__init__.py) plus the contract (strategies/base/) is everything the worker
# imports as `from strategies.base ...`; the user's strategy mounts separately at /app/strategy.py
# at run time. Baking the user strategies (ma_cross.py, newsbot_v3.py, aapl_fund_e2e.py) was dead
# weight the worker never reads (strategy_mirror.py: "the worker NEVER reads them") and blurred the
# one-source-of-truth line, so the COPY is narrowed to the contract. The package is locked to this image
# tag, never host-mounted.
COPY strategies/__init__.py /app/strategies/__init__.py
COPY strategies/base/ /app/strategies/base/
# M4 R15: the shared strategy-author helpers package — baked beside base/ so a mounted strategy can
# `from strategies.helpers import ...` at run time (ma_cross reads its costs via strategies.helpers).
COPY strategies/helpers/ /app/strategies/helpers/
COPY docker/research-runner/run_backtest_entrypoint.py /app/run_backtest_entrypoint.py
# M5 1B: the sweep engine (sibling of run_backtest_entrypoint; imports it for the shared strategy loader +
# environment authoring). Baked beside it so a `run_sweep` job runs the wide universe x grid pass.
COPY docker/research-runner/run_sweep_entrypoint.py /app/run_sweep_entrypoint.py
# M5 Track C: the full-metrics compute entrypoint (sibling of run_backtest/run_sweep; imports BOTH for the
# shared strategy loader + the member-OHLC/window helpers, plus strategies.helpers.full_metrics). Baked so a
# `compute_run_metrics` job runs `python /app/compute_metrics_entrypoint.py` — the worker mounts only
# run_spec.json (+ the cell's strategy/data), NEVER the entrypoint, so without this COPY the compute container
# fails "file not found at /app". (Deploy-gate catch: the Gate-2 probe imports the entrypoint as a module, so
# it proved the logic but not the baked invocation path.)
COPY docker/research-runner/compute_metrics_entrypoint.py /app/compute_metrics_entrypoint.py

USER runner
# ENTRYPOINT ["/app/entrypoint.sh"] is inherited from m2-latest (forwards argv).
