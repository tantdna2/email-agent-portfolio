# Daily digest systemd templates

The daily digest uses a Linux user timer at 07:00 Asia/Bangkok. The timer catches up after downtime and also fires 90 seconds after the user manager starts. The daily ledger deduplicates completed days and preserves checkpoints across restarts. A separate handler sends a fixed Telegram alert on failure, and a startup timer can replay a pending alert. [Schedule and checkpoints](../../docs/task020-reliability-design.md#schedule-and-checkpoints)

## Units and configuration

| Units | Purpose | Environment example |
| --- | --- | --- |
| [email-agent.timer](email-agent.timer) and [email-agent.service](email-agent.service) | Run the daily sender digest. The service reads `%h/.config/email-agent/email-agent.env`. | [email-agent-daily.env.example](email-agent-daily.env.example) |
| [email-agent-failure-alert.service](email-agent-failure-alert.service), [email-agent-alert-replay.timer](email-agent-alert-replay.timer) and [email-agent-alert-replay.service](email-agent-alert-replay.service) | Notify the operator of service failure and replay a pending alert on startup. Both services read `%h/.config/email-agent/email-agent-monitoring.env`. | [email-agent-monitoring.env.example](email-agent-monitoring.env.example) |
| [email-agent-subscriber.service](email-agent-subscriber.service), [email-agent-watch-renew.timer](email-agent-watch-renew.timer) and [email-agent-watch-renew.service](email-agent-watch-renew.service) | Run the optional Gmail Watch/Pub/Sub subscriber and renew the watch. The services read `%h/.config/email-agent/realtime.env`. | [email-agent-realtime.env.example](email-agent-realtime.env.example) |

Use `email-agent-daily.env.example` for the daily service. The [basic environment example](email-agent.env.example) describes options for the older reader/runner.

1. Prepare a Linux Python environment using [realtime-requirements.txt](realtime-requirements.txt). Render copies of the required units by replacing `@REPO_ROOT@` with the checkout's absolute path and `@PYTHON@` with the Python executable's absolute path.
2. Create the environment files outside the checkout with directory mode 0700 and file mode 0600. The daily systemd file and the OpenClaw plugin's JSON configuration need the same account binding, paths and runtime user. Keep `EMAIL_AGENT_AUTO_SEND=false` and keep model and Gmail secrets out of the monitoring environment.
3. Validate the rendered units with `systemd-analyze --user verify`. Run the Python and Node suites and the systemd startup sandbox on a disposable Linux environment. [Verification commands](../../README.md#verification)
4. Review application readiness, timer activation and recovery before installing and enabling the daily units. Realtime is an optional integration with its own configuration and scheduling. [Daily deployment and recovery](../../docs/task020-antigravity-runbook.md) · [Realtime setup](../../docs/realtime-email-architecture.md#manual-google-cloud-and-credential-setup)

## Monitoring and health

The failure handler passes a fixed alert to the private OpenClaw Telegram route without email contents, identifiers or exception details. It uses a bounded transport call. The replay unit handles pending alerts independently of the daily report. A lost acknowledgment can cause an alert to repeat. [Alert and routing design](../../docs/task020-reliability-design.md#routing-and-failure-alerts)

```sh
python3 scripts/check_email_agent_health.py
```

The command queries the user systemd manager for the daily timer, daily service and realtime subscriber. It returns 0 for healthy, 1 for a known error or 2 for an unknown snapshot. A healthy result requires the intended timer state, a disabled/inactive subscriber and successful daily service execution after the latest timer activation in the current boot. It makes no Gmail, Telegram or model requests.
