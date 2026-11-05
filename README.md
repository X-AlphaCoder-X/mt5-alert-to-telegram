# MT5 Alert to Telegram

Forward MetaTrader 5 `Alert()` messages to Telegram from any indicator or Expert Advisor, without changing its code. It also works with closed-source `.ex5` files. Windows only.

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-blue)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)

When an indicator or EA calls `Alert()`, MT5 shows a small Alert window. This script watches for that window, reads the alert text and sends it to your Telegram chats, usually within a fraction of a second. It can also read MT5's Experts log as a safety net, so alerts the popup missed are still delivered.

It's one Python script plus a `.bat` launcher. Nothing is installed into MT5.

## What it does

- Sends every alert, or only alerts that contain a keyword you choose.
- Watches several MT5 terminals at once and sends to several Telegram chats. You decide which terminal goes to which chat, and each chat can use its own bot.
- Joins multi-line alerts back into one message.
- Skips repeats. When the popup and the Experts log both see the same alert, it's sent once.
- Stays inside Telegram's limits of one message a second per chat and about 20 a minute per group, and waits the full time Telegram asks for if it's told to slow down.
- Keeps working when MT5 restarts, and finds both normal and portable MT5 installs.
- Writes a daily log file next to the script and deletes log files that are two or more days old. Bot tokens are hidden in the log.

## Requirements

- Windows 10 or 11, or Windows Server. The script reads MT5's windows through Windows UI Automation, so it can't run on Linux or macOS.
- MetaTrader 5.
- Python 3.8 or newer. It was tested with Python 3.12 from Miniconda.
- The packages in `requirements.txt`: `pywinauto`, `psutil`, `requests` and `comtypes`.
- A Telegram bot token and the chat ID of each chat you want to send to (see below).

## Quick start

1. Download the [latest release](../../releases/latest) and unzip it, or clone this repository.
2. Install the packages:

   ```
   pip install -r requirements.txt
   ```

3. Open `run.bat` in a text editor and set your Python. Put the full path to `python.exe` on the `set "PYTHON="` line, or leave it empty to use the Python on your PATH. If you use Miniconda or Anaconda, you can set `CONDA` and `CONDA_ENV` there instead.
4. Start MT5, then double-click `run.bat`.
5. In the menu:
   - `2` Channels: add your chat ID and set the default bot token.
   - `1` Sources: pick your running MT5 terminal.
   - `3` Routes: link the terminal to the chat. Adding a source or channel also offers this.
   - `10` sends a test message to every chat.
   - `12` starts watching. Press Ctrl+C to go back to the menu.

The script saves your choices in `mt5_alert_to_telegram_settings.json` next to it, so next time you only need `12`.

## Getting a bot token and chat ID

1. In Telegram, open [@BotFather](https://t.me/botfather), send `/newbot` and follow the steps. It gives you a token like `123456789:AA...`. Treat it like a password.
2. Add the bot to the chat you want alerts in:
   - Private chat: open your bot and press Start.
   - Group: add the bot as a member.
   - Channel: add the bot as an administrator, so it can post.
3. Send a message in that chat. In a group, send a command such as `/start@YourBotName`, because bots in groups only see commands by default.
4. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser, with your token in place of `<TOKEN>`. Find `"chat":{"id":...}` in the reply. Group and channel IDs are negative, and channel IDs start with `-100`.

## How alerts are captured

| Mode | Speed | When to use it |
|---|---|---|
| Popup only (default) | About 0.2 s | Most setups. The popup shows the whole alert. |
| Experts log only | Often 10 to 50 s late | When the popup can't be read. MT5 writes its log in delayed bursts. |
| Both | About 0.2 s | The popup sends at once. The log then recovers any alert the popup missed, as long as it's within the recovery window (60 s by default) and the same text wasn't already sent. |

Switch modes with menu option `4`. The script finds the Experts log folder by itself for normal and portable installs. You can also set it by hand under Sources, Tools.

## Menu

| Option | What it does |
|---|---|
| `1` Sources | Add or remove MT5 terminals. Tools show recent alerts, a log format sample, the window titles, and can switch MT5 to portable mode |
| `2` Channels | Add, edit or remove Telegram chats, and set the default bot token |
| `3` Routes | Link terminals to chats |
| `4` Capture mode | Popup, Experts log, or both |
| `5` Filter mode | Every alert, or only alerts with a keyword |
| `6` | The popup window title to watch (default `Alert`) |
| `7` | How often to check for popups (0.05 to 5 s) |
| `8` | Send again when an open popup shows a new alert |
| `9` | Send the popup that's already open when watching starts (useful for testing) |
| `10` | Send a test message to every chat |
| `11` | Dump the popup's controls to the log (for debugging) |
| `12` | Start watching |
| `13` | Show the last 40 lines of today's log |

## Settings file

`mt5_alert_to_telegram_settings.json` is created next to the script the first time you save something in the menu. [settings.example.json](settings.example.json) shows its layout with empty values.

Everything in this file is stored as plain text, including your bot tokens and chat IDs. It never leaves your PC, except that the token and chat ID are sent to Telegram when an alert goes out. Don't share the file or commit it anywhere. This repository's `.gitignore` already keeps it out.

| Key | Default | Meaning |
|---|---|---|
| `bot_token` | empty | Default bot, used by chats without their own token |
| `channels` | none | Telegram chats: `name`, `chat_id`, and an optional `bot_token` |
| `sources` | none | MT5 terminals: `name`, `exe` (path to `terminal64.exe`), and an optional `log_dir` |
| `routes` | none | Which `source` sends to which `channel` |
| `popup_enabled` | `true` | Read the Alert popup |
| `log_enabled` | `false` | Read the Experts log |
| `recover_seconds` | `60` | With both on, how old a log alert may be and still be sent |
| `window_title` | `Alert` | Text in the popup's title |
| `global_mode` | `true` | `true` sends every alert, `false` only alerts that contain `keyword` |
| `keyword` | empty | Keyword for filter mode |
| `require_text` | empty | In filter mode, text the popup's top block must also contain |
| `poll_seconds` | `0.15` | Seconds between popup checks |
| `send_on_update` | `true` | Send when an open popup shows a new alert |
| `send_existing_on_start` | `false` | Send the popup that's already open when watching starts |

Advanced timing switches, such as how multi-line alerts are joined and Telegram's burst limit, are set near the top of `mt5_alert_to_telegram.py`. Each one is explained where it's set.

## Logs

The script writes `logs/log<year>-<month>-<day>.log` next to itself and also prints to the screen. Files two or more days old are deleted when the script starts and again after midnight. Bot tokens are replaced with `***` in the log. Each alert's text is logged, so the folder shows exactly what was sent and when.

## Troubleshooting

**Nothing is sent and the log says the popup can't be read.** If MT5 runs as Administrator, run `run.bat` as Administrator too. `run.bat` warns you when it isn't.

**"MT5 is not running" although it is.** The script matches the terminal by the path to `terminal64.exe`. Re-add the source from menu `1` while MT5 is running.

**Some alerts in a burst are missing.** The popup only shows the newest alert. Turn on the Experts log as a safety net in menu `4`.

**Telegram says 400 or 403.** Check the chat ID, and that the bot is in the chat (as an administrator in a channel). Menu `10` tests every chat.

## FAQ

**Do I have to edit my indicator or EA?** No. Anything that calls `Alert()` works, including compiled `.ex5` files you don't have the source for.

**Does it work with MetaTrader 4?** It was only tested with MetaTrader 5.

**Does it place trades?** No. It only reads alerts and sends them to Telegram.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately, as [SECURITY.md](SECURITY.md) describes.

## Disclaimer

Not affiliated with MetaQuotes or Telegram. MetaTrader is a trademark of MetaQuotes Ltd. This tool forwards messages and isn't financial advice. Trading carries risk.

## License

[MIT](LICENSE)
