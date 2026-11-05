# Contributing

Thanks for helping with MT5 Alert to Telegram. Bug reports, fixes and README improvements are all welcome.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting a bug

Search the [open issues](../../issues) first. If it's new, open one with the bug report form. It asks for:

- what happened and what you expected,
- your Windows, Python and MetaTrader 5 versions,
- the capture mode you use (popup, Experts log or both),
- the matching lines from the script's log in `logs/`.

Before you paste log lines, remove your chat IDs and any alert text you'd rather keep private. Bot tokens are already replaced with `***` in the log, but check anyway.

If an alert window can't be read, run menu `11` while the popup is open. It writes the popup's controls to the log, which shows what the script sees.

## Making a change

1. Fork the repo and create a branch from `main`.
2. Keep the script working on Python 3.8 and in a plain Windows console window.
3. Don't add new packages unless there's no other way. If you must, add them to `requirements.txt` and to the check in `run.bat`.
4. Test with a real MT5: trigger an alert from an indicator or EA and check it arrives in Telegram once. If you changed the log reader, test with the Experts log on as well.
5. Update `README.md` if you changed menu options or settings, and add a line to `CHANGELOG.md`.
6. Open a pull request against `main` and fill in the template.

Keep each pull request to one fix or feature. `.py` and `.bat` files use CRLF line endings, and `.gitattributes` keeps them that way.

## License

The project is MIT licensed. By contributing you agree that your work is released under the [MIT License](LICENSE) too. Only submit code you wrote yourself or have the right to share.
