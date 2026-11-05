# Security

The script runs on your own PC. It reads MetaTrader 5's Alert window and Experts log, and talks to one server only: `api.telegram.org`. It doesn't place trades and doesn't read your MT5 account or password.

Your bot tokens and chat IDs are stored as plain text in `mt5_alert_to_telegram_settings.json` next to the script. Anyone who can read that file can send messages as your bot. Keep the file private, and if a token leaks, revoke it with [@BotFather](https://t.me/botfather) and set the new one in menu `2`.

If you find a security problem in the script, please don't open a public issue. Report it privately with GitHub's [private vulnerability reporting](../../security/advisories/new) instead.
