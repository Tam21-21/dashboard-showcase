# Worker ops units

Version-controlled systemd units for the laptop research worker, following the
precedent set by `research_lab/fetcher/ops/`. Before this directory existed the
worker's unit lived only at `~/.config/systemd/user/research-worker.service`
with no copy in the repo, so a change to it was an untracked edit to an
untracked file.

## Install

```
cp research-worker.service ~/.config/systemd/user/research-worker.service
systemctl --user daemon-reload
systemctl --user restart research-worker.service
```

Verify the start limit actually took (it is silently ignored if it ends up in
the wrong section):

```
systemctl --user show research-worker.service -p StartLimitIntervalUSec -p StartLimitBurst
```

Expect `StartLimitIntervalUSec=6h` and `StartLimitBurst=5`. The systemd defaults
are 10s / 5, so seeing those means the file did not apply.

## Recovery

A permanent fault, or an hour of unbroken failure, now stops the unit at
`failed` instead of restarting it forever. That is deliberate: it is what makes
a permanent fault visible rather than a quiet infinite retry. It also means the
unit does not come back on its own.

```
systemctl --user reset-failed research-worker.service
systemctl --user start research-worker.service
```

The same command clears the counter after routine maintenance restarts, which
consume the same burst.

## Environment overrides

Read at process start, both used by the deploy gate to demonstrate ceilings that
would otherwise take an hour to fire:

- `RESEARCH_WORKER_FAULT_CEILING_S` — general tolerance ceiling, default 3600.
- `RESEARCH_WORKER_CERT_CEILING_S` — certificate-verify ceiling, default 300.

`RESEARCH_WORKER_SLOTS` (lane W1) is a standing setting, not a gate override: it lives in
`~/.research-worker.env` and sets how many fetch jobs run at once (default 1; the laptop uses 4).
Anything that is not a whole number of at least 1 stops the worker at start with an error naming it.

Apply them with a drop-in for the duration of a gate run, then remove it:

```
systemctl --user edit research-worker.service     # [Service] Environment=...
systemctl --user revert research-worker.service
```
