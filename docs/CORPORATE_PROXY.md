# Running behind a corporate proxy

`uv` does its own TLS, so a proxy that intercepts downloads with a **private CA** fails
`uv sync` in a way `pip` would not: `pip` honours `REQUESTS_CA_BUNDLE`, while uv verifies
against its own root store.

Everything here is an **environment variable**. Nothing in `pyproject.toml` changes,
because a company's CA and index are not project settings — committing them would break
everyone else's `uv sync`.

## Diagnose before you configure

Run these in order and read the first failure. Skipping step 1 is the common mistake: if
the interpreter itself has to be downloaded, no amount of index configuration will help.

```bash
uv python install 3.14 --verbose   # the interpreter: a separate download path
uv sync --verbose                 # the dependencies
```

## The two failures and their fixes

| Symptom | Cause | Fix |
|---|---|---|
| `error sending request ... url: ...` once the proxy is reached | TLS interception; the corporate CA is not trusted | `SSL_CERT_FILE` (below) |
| `Proxy CONNECT aborted` / `403` / a hang | the proxy or its index needs configuring | `HTTPS_PROXY`, `UV_DEFAULT_INDEX` |
| `Connection timed out` on a slow link | the default timeout is too short | `UV_HTTP_TIMEOUT` |

## Trusting a private CA

Pick **one**. They *replace* the default root store rather than adding to it, so a
corporate CA alone will not verify PyPI's own certificate and will break the very
downloads you are trying to fix. If that happens, concatenate the corporate CA onto the
system bundle instead of pointing at it on its own.

```bash
# 1. Your CA is a single PEM bundle (most common)
export SSL_CERT_FILE="$HOME/certs/corp-root-ca.pem"

# 2. Your CAs are loose files in a directory
export SSL_CERT_DIR="$HOME/certs/company/"

# 3. Your CA is already in the operating system's trust store — usually the best answer
export UV_SYSTEM_CERTS=1
```

`SSL_CERT_FILE` needs a PEM file (`cert.pem`, `ca-bundle.crt`); DER is not accepted.
`SSL_CERT_DIR` reads any regular file in the directory, ignores the ones that are not
parseable PEM, and resolves symlinks.

> **`UV_NATIVE_TLS=1` is deprecated.** uv prints a warning telling you to use
> `UV_SYSTEM_CERTS` instead. Prefer the variable above.

## The proxy itself

Standard variables, honoured by uv as-is:

```bash
export HTTPS_PROXY="http://proxy.corp.example:3128"
export NO_PROXY="localhost,127.0.0.1,.corp.example"   # keep internal traffic off the proxy
```

uv also accepts credentials inside the URL (`http://user:pass@proxy:3128`), but prefer
`UV_INDEX_<name>_USERNAME` / `UV_INDEX_<name>_PASSWORD` or a credential helper, so the
secret does not land in your shell history.

## An internal mirror

If the company proxies PyPI rather than blocking it, no index change is needed. If it
mirrors packages internally:

```bash
export UV_DEFAULT_INDEX="https://artifacts.corp.example/api/pypi/pypi/simple"
```

## Slow links

```bash
export UV_HTTP_TIMEOUT=120
export UV_HTTP_RETRIES=5
```

## Last resort, and why we do not recommend it

```bash
export UV_INSECURE_HOST="pypi.org"   # or --allow-insecure-host
```

This disables certificate verification for that host, which makes a man-in-the-middle
undetectable — and a MITM is the entire premise of TLS interception. Use it only to read
the exact error it silences, then fix the CA properly. See [SECURITY.md](../SECURITY.md).

## For CI

The same variables, set on the job rather than baked into a config file:

```yaml
env:
  HTTPS_PROXY: ${{ secrets.PROXY_URL }}
  SSL_CERT_FILE: ${{ secrets.CORP_CA_PEM }}
```

A runner image with the CA already installed is better still: it needs no secret at all.
