# Changelog

Versions follow [Semantic Versioning](https://semver.org/). Each version is a [GitHub release](../../releases) with a zip of the files.

## 1.0.0 - 2026-11-05

First public release.

### Added

- Popup capture: reads MT5's Alert window through plain Win32 first and Windows UI Automation when that isn't enough, and sends the newest alert to Telegram.
- Experts log capture, on its own or as a safety net behind the popup. A log alert is sent only if it's within the recovery window and the same text wasn't already delivered around that time. Each popup send covers one log alert, so identical alerts from several charts are each recovered if the popup missed them.
- Several terminals, several chats and routes between them. Each chat can use its own bot.
- Multi-line `Alert()` messages are joined back together from the Experts log. Lines are matched by the text the popup showed when it's known, and by a 2 ms time window when it isn't, so the EA's own `Print()` lines aren't glued on.
- Filter mode: every alert, or only alerts that contain a keyword.
- Telegram limits: one message a second per chat, at most 19 a minute into one chat by default, and the full wait Telegram asks for after a 429.
- Protection against sending one alert twice when the popup flickers or comes back with the same text (`RESEND_GUARD`, `LOST_GRACE`).
- After midnight, yesterday's Experts log is still read for 10 minutes, because MT5 writes its log late.
- A daily log file in `logs/`, with files two or more days old deleted automatically and bot tokens hidden.
- The settings file is saved safely (temporary file, then swapped in) and kept as a copy if it can't be read.
- `run.bat` launcher with a Python path setting, optional conda activation, a warning when it isn't running as Administrator, and a check for missing packages.
- `requirements.txt` and `settings.example.json`.
