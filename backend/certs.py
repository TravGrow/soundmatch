"""TLS trust setup.

Corporate proxies and consumer antivirus (Norton, Kaspersky, Zscaler...) inspect
HTTPS by re-signing traffic with a local root CA. Python doesn't use the Windows
certificate store, so those connections fail unless we add the extra root
explicitly. This finds such a CA if one is configured and folds it into the
normal certifi bundle.
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path

import certifi

# Captured before install() can patch certifi.where(). The merged bundle is
# built *from* the stock roots, so reading a possibly-already-patched
# certifi.where() would make the bundle self-referential -- and would break
# outright if the file it points at has since been deleted.
_STOCK_CA = certifi.where()

# The merged bundle lives with the other caches, never inside a per-job
# directory: those get cleaned up, and a patched certifi.where() must keep
# pointing at a file that still exists.
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "cache"

# Environment variables commonly set by tools that install an interception CA,
# plus well-known on-disk locations.
_ENV_VARS = ("SOUNDMATCH_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
             "SSL_CERT_FILE", "CURL_CA_BUNDLE")
_KNOWN_PATHS = (
    r"C:\ProgramData\Norton\Antivirus\wscert.pem",
    r"C:\ProgramData\Kaspersky Lab\AVP\Data\Cert\(fake)Kaspersky Anti-Virus personal root certificate.cer",
)


def _is_own_bundle(path: str) -> bool:
    """Is this our own merged bundle?

    install() points SSL_CERT_FILE at the bundle it generates, and that variable
    is one of the ones scanned below. Without this guard each call would fold the
    previous bundle into the next one and the file would grow without bound.
    """
    try:
        return Path(path).resolve() == (DEFAULT_CACHE / "ca-bundle.pem").resolve()
    except Exception:
        return False


def extra_ca_paths() -> list[str]:
    found, seen = [], set()

    def consider(p: str):
        # Dedupe on the resolved path: the same certificate is often reachable
        # both via an environment variable and via a known location.
        if not p or _is_own_bundle(p) or not Path(p).is_file():
            return
        try:
            key = str(Path(p).resolve()).lower()
        except Exception:
            key = p.lower()
        if key not in seen:
            seen.add(key)
            found.append(p)

    for var in _ENV_VARS:
        consider(os.environ.get(var, "").strip().strip('"'))
    for p in _KNOWN_PATHS:
        consider(p)
    return found


def ssl_context() -> ssl.SSLContext:
    """An SSL context trusting the public roots plus any local interception CA."""
    ctx = ssl.create_default_context(cafile=_STOCK_CA)
    for path in extra_ca_paths():
        try:
            ctx.load_verify_locations(path)
        except Exception:
            # A malformed or unreadable extra CA shouldn't break normal traffic.
            pass
    return ctx


def ca_bundle_path(cache_dir: str | Path | None = None) -> str:
    """A merged PEM file, for tools that take a bundle path rather than a context.

    ``yt-dlp`` is the main consumer here.
    """
    extras = extra_ca_paths()
    if not extras:
        return _STOCK_CA

    cache_dir = Path(cache_dir or DEFAULT_CACHE)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "ca-bundle.pem"

    parts = [Path(_STOCK_CA).read_text(encoding="utf-8", errors="ignore")]
    for p in extras:
        try:
            parts.append(Path(p).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
    merged = "\n".join(parts)

    if not dest.exists() or dest.read_text(encoding="utf-8", errors="ignore") != merged:
        dest.write_text(merged, encoding="utf-8")
    return str(dest)


def install(cache_dir: str | Path | None = None) -> str:
    """Make libraries that hardcode ``certifi.where()`` use the merged bundle.

    yt-dlp builds its SSL context from certifi directly, so pointing certifi at
    a bundle that contains the public roots *plus* the local interception CA is
    the least invasive way to let it work behind TLS inspection -- and it keeps
    certificate verification fully enabled, unlike --no-check-certificate.
    """
    bundle = ca_bundle_path(cache_dir)
    if bundle != certifi.where():
        certifi.where = lambda: bundle          # type: ignore[assignment]
        os.environ.setdefault("SSL_CERT_FILE", bundle)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
    return bundle
