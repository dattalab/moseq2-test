"""Python-3.7-compatible exact-URL fixture adapter for historical tests.

The controller copies this module to a private directory as ``sitecustomize.py``.
It then supplies a JSON map from the historical URL strings to verified,
content-addressed fixture objects.  Only those exact URLs are serviced; an
unexpected URL fails closed instead of reaching the network.
"""

import email.message
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MAP_ENVIRONMENT_VARIABLE = "MOSEQ2_TEST_OFFLINE_URL_MAP"


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_map():
    raw = os.environ.get(MAP_ENVIRONMENT_VARIABLE)
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError("offline URL map must be a non-empty JSON object")
    for url, record in parsed.items():
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError("offline URL map keys must be exact HTTPS URLs")
        if not isinstance(record, dict):
            raise RuntimeError("offline URL map values must be JSON objects")
        source = Path(record.get("source", ""))
        expected_size = record.get("size")
        expected_sha256 = record.get("sha256")
        if not source.is_absolute() or not source.is_file():
            raise RuntimeError(f"offline URL source is not an absolute file: {source}")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise RuntimeError(f"offline URL fixture size is invalid for {url}")
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise RuntimeError(f"offline URL fixture SHA-256 is invalid for {url}")
        if source.stat().st_size != expected_size or _sha256_file(source) != expected_sha256:
            raise RuntimeError(f"offline URL fixture differs from its lock for {url}")
    return parsed


def _mapped_urlretrieve(url, filename=None, reporthook=None, data=None):
    url_text = url.full_url if isinstance(url, urllib.request.Request) else str(url)
    record = _URL_MAP.get(url_text)
    if record is None:
        raise urllib.error.URLError(
            f"network disabled and URL is not a staged moseq2-test fixture: {url_text}"
        )
    if data is not None:
        raise urllib.error.URLError("offline fixture adapter does not accept request bodies")
    source = Path(record["source"])
    if source.stat().st_size != record["size"] or _sha256_file(source) != record["sha256"]:
        raise urllib.error.URLError(f"staged fixture changed before use: {url_text}")
    if filename is None:
        descriptor, temporary_name = tempfile.mkstemp(prefix="moseq2-test-urlretrieve-")
        os.close(descriptor)
        destination = Path(temporary_name)
    else:
        destination = Path(filename)
    if reporthook is not None:
        reporthook(0, record["size"], record["size"])
    shutil.copyfile(str(source), str(destination))
    copy_matches = (
        destination.stat().st_size == record["size"]
        and _sha256_file(destination) == record["sha256"]
    )
    if not copy_matches:
        raise urllib.error.URLError(f"offline fixture copy verification failed: {url_text}")
    if reporthook is not None:
        reporthook(1, record["size"], record["size"])
    headers = email.message.Message()
    headers["Content-Length"] = str(record["size"])
    headers["X-MoSeq2-Test-Fixture-SHA256"] = record["sha256"]
    return str(destination), headers


_URL_MAP = _validated_map()
if _URL_MAP is not None:
    urllib.request.urlretrieve = _mapped_urlretrieve
