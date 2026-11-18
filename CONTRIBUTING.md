# Contributing to clipwatcher

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Where to start

- **Found a bug?** Open an [issue](https://github.com/YaShDsOnI/clipwatcher/issues/new/choose) with the bug form. Paste the relevant
  log lines, but never API keys or your `.env`.
- **Have an idea or a question?** Start a [discussion](https://github.com/YaShDsOnI/clipwatcher/discussions).
- **Want to code?** Issues labelled
  [good first issue](https://github.com/YaShDsOnI/clipwatcher/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
  are small and self-contained. Comment on one so nobody does it twice.

## Setting up

You need Python 3.12–3.14 (64-bit) and ffmpeg on your PATH.

```bash
git clone https://github.com/YaShDsOnI/clipwatcher.git && cd clipwatcher
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                              # add ,captions,speakers to work on those parts
clip-watcher                                         # the dashboard at http://localhost:8765
```

Your clips, recordings, models and `.env` live in the project folder but are ignored by git.
Set `CLIP_WATCHER_HOME=/some/folder` to keep test data somewhere else.

## Before you open a pull request

```bash
ruff check .        # lint
pytest              # tests: no network needed; the export test is skipped without ffmpeg
```

- Keep pull requests focused: one fix or feature each.
- Add or update a test in `tests/` for behaviour changes (`tests/test_editor.py` shows how to test ffmpeg output).
- The dashboard and editor are plain HTML/CSS/JS with no build step: keep it that way.
- Write user-facing text in plain, friendly English, like the rest of the app.
- Update the README and `CHANGELOG.md` (under "Unreleased") when you change what users see.

## How the code is organised

The [project layout](README.md#development) in the README lists every module. In short: `watcher.py` records
and finds moments, `clipper.py` renders clips, `editor.py` renders edits, `dashboard.py` serves the
web page in `web/`.

## Ground rules

- **Nothing downloads or installs without the user's OK**, and the AI step stays optional.
- **The original clip is never modified** by the editor.
- The dashboard only listens on `127.0.0.1` and refuses requests from other websites; keep it that way.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE), and to
follow the [code of conduct](CODE_OF_CONDUCT.md).
