# EODHD fetcher autonomy (laptop-only systemd units)

These `systemd --user` units keep the weeks-long EODHD backfill rolling forward unattended and
report the daily budget spend. The fetcher runs on the laptop only (the server runs no fetcher). See
`docs/briefs/M5_trackA_fetcher_autonomy_spec_brief.md` for the design (P6 = the daily heartbeat).

## What they do
- `eodhd-backfill.service` (oneshot) runs one resumable backfill pass. It is gated by
  `ConditionPathIsMountPoint=/mnt/ssd`, so it skips cleanly when the SSD is detached. Its
  `ExecStartPost=-... report` appends one budget-spend line to `~/eodhd_daily_report.log` after each
  run (the leading `-` means a report hiccup never marks the backfill failed).
- `eodhd-backfill.timer` fires it every six hours and ~2 min after boot, with `Persistent=true`.
  The backfill is cap-aware: a fire while the day's quota is spent (or a run is still active) is a
  cheap no-op, so frequent re-fires resume within six hours of the daily reset.
- `eodhd-report.service` (oneshot) runs `report --if-absent` — it appends one heartbeat line to
  `~/eodhd_daily_report.log` (root disk, readable when the SSD is detached). It has **no** mount
  condition on purpose, so an SSD-unmounted day still gets a "no spend" line instead of silence.
- `eodhd-report.timer` fires it once a day (23:50) with `Persistent=true`, as a backstop: on days a
  backfill ran, the `ExecStartPost` already wrote a line and `--if-absent` makes this a no-op; on
  SSD-unmounted (skipped-backfill) days, this is what writes the "no spend" line.

The in-code mountpoint guard (`config.require_staging_mounted`) and the per-write re-check in
`storage.write_atomic` are the real safety net; the timer condition is belt-and-suspenders.

The budget-spend answer in each line is `quota=<used>/<limit> qdate=<date>` (the live `/user` counter and
the UTC date it is FOR); a stale or missing line for a date is itself the signal the chain broke.

## Budget reset behavior + the `--force` run-now path (DECISIONS #181)

EODHD's `/user` counter resets at midnight GMT, but it CARRIES the prior day's `apiRequests` count (and its
`apiRequestsDate`) until the first request of the new day triggers the reset. So a `quota=98031/100000
qdate=<yesterday>` line on a new day is STALE — the real quota is fresh. The fetcher seeds its cap ledger
from `/user`, so it now treats a prior-day date (or `--force`) as a seed of 0 and only trusts a same-day
count; this is what stops a stale near-cap display from false-stalling a fresh quota. If `status`/`report`
shows `qdate=` a day behind today, the number is not the real spend.

`backfill --force` ignores the displayed count at the initial seed (the run-now path Tamer triggers): use it
to spend when you know the display is stale. It stays bounded — local charges + the periodic reconcile
(always honest, `force=False`) + the 2000 margin are the safety net. A genuinely-capped day will send a few
requests that EODHD rejects (cheap) rather than pre-stopping; the exact daily-limit response is captured to
the journal (not guessed) for later classification.

## Install (laptop, one time)
```bash
# 1. lingering must be on so the user manager runs without an active login
loginctl show-user "$USER" | grep -q 'Linger=yes' || loginctl enable-linger "$USER"

# 2. the ExecStart interpreter must resolve (deploy gate) — fails here if the venv is broken
/home/tamer/trading/research_lab/worker/.venv/bin/python -c "import research_lab.fetcher" && echo OK

# 3. install + enable (all four units: the backfill pair + the report pair)
mkdir -p ~/.config/systemd/user
cp research_lab/fetcher/ops/eodhd-backfill.service ~/.config/systemd/user/   # re-copy: now has ExecStartPost
cp research_lab/fetcher/ops/eodhd-backfill.timer   ~/.config/systemd/user/
cp research_lab/fetcher/ops/eodhd-report.service   ~/.config/systemd/user/
cp research_lab/fetcher/ops/eodhd-report.timer     ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eodhd-backfill.timer
systemctl --user enable --now eodhd-report.timer

# 4. verify
systemctl --user list-timers eodhd-backfill.timer eodhd-report.timer
systemctl --user start eodhd-backfill.service     # one manual run
journalctl --user -u eodhd-backfill.service -n 30 # a clean resuming run
systemctl --user start eodhd-report.service       # writes one heartbeat line
tail -n 3 ~/eodhd_daily_report.log                # the budget-spend lines
```

Once installed, the manual `setsid /home/tamer/run_eodhd_fetcher.sh` launch is retired.

## Post-reboot SSD remount (Tamer-gated, NOT installed by this unit)
A USB bare-mount does not survive a reboot, and `wsl --mount` needs admin (UAC), which a
`systemd --user` timer cannot do. To make the remount hands-off, create a Windows Scheduled Task
(elevated, one time) that runs the existing remount helper at logon:

```
schtasks /Create /TN "EodhdSsdRemount" /SC ONLOGON /RL HIGHEST ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\tamer\ssd-remount.ps1"
```

The task name must be `EodhdSsdRemount` (no spaces) — that is the name `register-ssd-task.ps1`
creates and the one verified registered in session 103. Using a different name here would create a
duplicate task. Until that task exists: after a reboot, run `C:\Users\tamer\ssd-remount.ps1` as administrator once,
and the timer resumes the backfill on its next fire.
