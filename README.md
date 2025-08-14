# API-Tester

A simple repository for testing APIs.

## Deployment

The project includes a minimal `requirements.txt` and `Procfile` so it can be
deployed on platforms like [Railway](https://railway.app) using Nixpacks.
`Procfile` starts the Flask UI with `python api_tester.py --ui --port $PORT`.

## Local test script download

Use `python api_tester.py --download <URL>` to fetch a helper script and
Markdown instructions for running API checks locally. If the remote server
requires authentication, supply `--token <YOUR_TOKEN>` and the token will be
cached until the server rejects it.

The web UI also exposes these features: enter an optional token and the URL of
the helper script, then click **Tải script** to receive a zip containing the
script and usage instructions.

## Authorization token caching

When running tests, an `Authorization` header found in the cURL command or
provided via `--token` is remembered and automatically applied to subsequent
requests. The cache is cleared after a `401` or `403` response. The web UI
contains a token input box so the value can be supplied without editing the
cURL command.
