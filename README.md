# API-Tester

A simple repository for testing APIs.

## GitHub Pages

The static site is served from the `docs` directory and includes a `.nojekyll`
file to bypass Jekyll processing.

## Deployment

The project includes a minimal `requirements.txt` and `Procfile` so it can be
deployed on platforms like [Railway](https://railway.app) using Nixpacks.
`Procfile` starts the Flask UI with `python api_tester.py --ui --port $PORT`.
