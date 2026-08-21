# Contributing

Thanks for improving AceUnzipBot. The project welcomes focused fixes, tests, documentation, archive
format support, and owner/user experience improvements.

## Before opening an issue

- Search existing issues and read [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
- Use GitHub's security reporting flow or the private contacts in [SECURITY.md](SECURITY.md) for a
  vulnerability. Do not publish an exploit, bot token, API hash, session file, archive password, or
  user data in an issue.
- Include your release, Ubuntu/Python version, deployment type, sanitized logs, and exact steps.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q acebot tests
bash -n install.sh setup.sh run.sh scripts/*.sh
```

Install Ruff and run `ruff check acebot tests` before opening a pull request.

## Pull requests

1. Branch from `main` and keep the change focused.
2. Add a regression test for every behavior fix.
3. Preserve owner-only routing and secret-redaction boundaries.
4. Update README/docs and `CHANGELOG.md` when behavior changes.
5. Never commit `.env`, session/SQLite files, runtime data, user archives, or generated work files.
6. Explain how you tested the change and any deployment impact.

By contributing, you agree that your contribution is licensed under the project's MIT License.
