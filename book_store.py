"""
Book PDFs in a Railway Storage Bucket (S3-compatible), with the volume as a
cache.

Every book file is uploaded to the bucket under its path relative to
STORAGE_DIR ("123/library/x.pdf", "_shared/books/sb1.pdf"), so the bucket is
the lasting copy. The volume keeps the books in use: when the cached books
pass CACHE_LIMIT_BYTES, the least recently used ones (already safely in the
bucket, not opened for a while) are removed from the volume. Opening one of
those again downloads it back first -- ensure_local() is called wherever a
book's PDF is read, always from a worker thread, never the event loop.

Off (everything stays on the volume, as before) unless BOOKS_BUCKET is set.
Variables, referenced from the bucket service in Railway:
  BOOKS_BUCKET, BOOKS_BUCKET_ENDPOINT, BOOKS_BUCKET_REGION,
  BOOKS_BUCKET_ACCESS_KEY_ID, BOOKS_BUCKET_SECRET_ACCESS_KEY
"""

import json
import logging
import os
import threading
import time

from config import STORAGE_DIR

logger = logging.getLogger(__name__)

BUCKET = os.environ.get("BOOKS_BUCKET", "").strip()
CACHE_LIMIT_BYTES = int(float(os.environ.get("BOOKS_CACHE_GB", "3")) * 1024**3)
_KEEP_RECENT_SECONDS = 30 * 60   # a book opened in the last half hour is never evicted
_STATE_PATH = os.path.join(STORAGE_DIR, "_admin", "book_store.json")

_client = None
_client_lock = threading.Lock()
_state_lock = threading.RLock()
_path_locks: dict[str, threading.Lock] = {}
_state: dict | None = None   # {"uploaded": {key: size}, "used": {key: ts}}


def enabled() -> bool:
    return bool(BUCKET)


def _s3():
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client(
                "s3",
                endpoint_url=os.environ.get("BOOKS_BUCKET_ENDPOINT") or None,
                region_name=os.environ.get("BOOKS_BUCKET_REGION") or "auto",
                aws_access_key_id=os.environ.get("BOOKS_BUCKET_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("BOOKS_BUCKET_SECRET_ACCESS_KEY"),
                config=Config(
                    retries={"max_attempts": 5, "mode": "standard"},
                    s3={"addressing_style": os.environ.get("BOOKS_BUCKET_URL_STYLE") or "virtual"},
                ),
            )
        return _client


def key_for(path: str) -> str | None:
    """The bucket key of a book file, or None for a file outside STORAGE_DIR (e.g. a chat upload's temp copy)."""
    root = os.path.realpath(STORAGE_DIR)
    full = os.path.realpath(path)
    if not full.startswith(root + os.sep):
        return None
    return os.path.relpath(full, root).replace(os.sep, "/")


# --- state: what's in the bucket, and when each book was last used -------

def _load_state() -> dict:
    global _state
    with _state_lock:
        if _state is None:
            try:
                with open(_STATE_PATH) as f:
                    _state = json.load(f)
            except (OSError, json.JSONDecodeError):
                _state = {}
            _state.setdefault("uploaded", {})
            _state.setdefault("used", {})
        return _state


def _save_state() -> None:
    with _state_lock:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = f"{_STATE_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            json.dump(_load_state(), f)
        os.replace(tmp, _STATE_PATH)


def _mark_used(key: str) -> None:
    with _state_lock:
        st = _load_state()
        now = time.time()
        # Written at most every few minutes per book: this runs on every read.
        if now - st["used"].get(key, 0) > 300:
            st["used"][key] = now
            _save_state()


# --- the three operations ---------------------------------------------------

def _lock_for(key: str) -> threading.Lock:
    with _state_lock:
        return _path_locks.setdefault(key, threading.Lock())


def ensure_local(path: str) -> str:
    """
    Make sure a book's PDF is on the volume, downloading it from the bucket if
    it was evicted. Returns the path. Blocking: call from a worker thread.
    """
    if not path:
        return path
    key = key_for(path)
    if key is None:
        return path
    if os.path.exists(path):
        if enabled():
            _mark_used(key)
        return path
    if not enabled():
        return path  # the caller reports the missing file as before
    with _lock_for(key):
        if not os.path.exists(path):  # another thread may have fetched it meanwhile
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{threading.get_ident()}.download"
            started = time.time()
            try:
                _s3().download_file(BUCKET, key, tmp)
                os.replace(tmp, path)
            except Exception as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                logger.warning("Couldn't fetch %s from the bucket: %r", key, e)
                return path  # caller sees the file missing and says so
            logger.info("Fetched %s from the bucket (%.1f MB in %.1f s)", key,
                        os.path.getsize(path) / 2**20, time.time() - started)
            threading.Thread(target=evict, daemon=True, name="book-evict").start()
    _mark_used(key)
    return path


def upload(path: str) -> bool:
    """Copy a book's PDF into the bucket (after it is added). Blocking. True when it is safely there."""
    if not enabled():
        return False
    key = key_for(path)
    if key is None or not os.path.exists(path):
        return False
    size = os.path.getsize(path)
    try:
        _s3().upload_file(path, BUCKET, key, ExtraArgs={"ContentType": "application/pdf"})
    except Exception as e:
        logger.warning("Couldn't upload %s to the bucket: %r", key, e)
        return False
    with _state_lock:
        st = _load_state()
        st["uploaded"][key] = size
        st["used"][key] = time.time()
        _save_state()
    return True


def delete(path: str) -> None:
    """A book was removed: its bucket copy goes too. Blocking."""
    key = key_for(path or "")
    if not enabled() or key is None:
        return
    try:
        _s3().delete_object(Bucket=BUCKET, Key=key)
    except Exception as e:
        logger.warning("Couldn't delete %s from the bucket: %r", key, e)
    with _state_lock:
        st = _load_state()
        st["uploaded"].pop(key, None)
        st["used"].pop(key, None)
        _save_state()


def delete_async(path: str) -> None:
    if enabled():
        threading.Thread(target=delete, args=(path,), daemon=True, name="book-delete").start()


def upload_async(path: str) -> None:
    """Upload in the background (a new book is usable at once from the volume meanwhile)."""
    if enabled():
        threading.Thread(target=lambda: (upload(path), evict()), daemon=True, name="book-upload").start()


# --- keeping the volume within its share ------------------------------------

def evict(limit: int | None = None, keep_recent: int | None = None) -> int:
    """
    Remove least-recently-used books (already in the bucket) from the volume
    past `limit` (CACHE_LIMIT_BYTES by default). Returns bytes freed.
    """
    limit = CACHE_LIMIT_BYTES if limit is None else limit
    keep_recent = _KEEP_RECENT_SECONDS if keep_recent is None else keep_recent
    if not enabled():
        return 0
    root = os.path.realpath(STORAGE_DIR)
    with _state_lock:
        st = _load_state()
        cached = []
        for key, size in st["uploaded"].items():
            path = os.path.join(root, key)
            if os.path.exists(path):
                cached.append((st["used"].get(key, 0), key, path, os.path.getsize(path)))
    total = sum(c[3] for c in cached)
    freed = 0
    now = time.time()
    for used, key, path, size in sorted(cached):
        if total - freed <= limit:
            break
        if now - used < keep_recent:
            continue
        with _lock_for(key):
            try:
                os.remove(path)
                freed += size
            except OSError:
                pass
    if freed:
        logger.info("Book cache: freed %.0f MB on the volume (books stay in the bucket)", freed / 2**20)
    return freed


# ---------------------------------------------------------------------
# Housekeeping: the volume must never fill up (a full disk stops uploads,
# indexing and even saving settings).
# ---------------------------------------------------------------------

MIN_FREE_BYTES = int(float(os.environ.get("VOLUME_MIN_FREE_GB", "1")) * 1024**3)
_HOUSEKEEPING_EVERY = 10 * 60
_TEMP_SUFFIXES = (".download", ".repair.tmp", ".tmp")
_TEMP_MAX_AGE = 6 * 3600


def _dir_size(path: str, seen: set) -> int:
    # Hard links (the content cache links index files) are counted once.
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += st.st_size
    return total


def disk_report() -> dict:
    """What uses the volume: {top-level entry: bytes}, largest first, plus free space."""
    import shutil

    root = os.path.realpath(STORAGE_DIR)
    sizes, seen = {}, set()
    try:
        for name in sorted(os.listdir(root), key=lambda n: n == "_content_cache"):  # links counted where they're used
            full = os.path.join(root, name)
            sizes[name] = _dir_size(full, seen) if os.path.isdir(full) else os.path.getsize(full)
    except OSError:
        pass
    # Users' own folders (numeric ids) are summed into one line.
    users = sum(v for k, v in sizes.items() if k.isdigit())
    out = {k: v for k, v in sizes.items() if not k.isdigit()}
    if users:
        out["(users' folders)"] = users
    usage = shutil.disk_usage(root)
    return {
        "used": usage.used, "free": usage.free, "total": usage.total,
        "by_folder": dict(sorted(out.items(), key=lambda kv: -kv[1])),
    }


def _remove_stale_temp_files() -> int:
    """Half-written downloads and repairs left by a restart or a crash."""
    root = os.path.realpath(STORAGE_DIR)
    freed, now = 0, time.time()
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if not f.endswith(_TEMP_SUFFIXES):
                continue
            path = os.path.join(dirpath, f)
            try:
                st = os.stat(path)
                if now - st.st_mtime > _TEMP_MAX_AGE:
                    os.remove(path)
                    freed += st.st_size
            except OSError:
                pass
    return freed


def housekeeping() -> None:
    """Trim the cache, clear stale temp files, and free room when the disk runs low."""
    import shutil

    freed = _remove_stale_temp_files()
    if enabled():
        freed += evict()
        free = shutil.disk_usage(os.path.realpath(STORAGE_DIR)).free
        if free < MIN_FREE_BYTES:
            # Low on space: let the book cache shrink well below its limit,
            # and give up the "opened in the last half hour" grace (a book
            # being read just comes back from the bucket).
            with _state_lock:
                st = _load_state()
                cached = sum(sz for k, sz in st["uploaded"].items()
                             if os.path.exists(os.path.join(os.path.realpath(STORAGE_DIR), k)))
            target = max(0, cached - (MIN_FREE_BYTES - free) - 512 * 1024**2)
            got = evict(limit=target, keep_recent=5 * 60)
            freed += got
            logger.warning("Volume low on space (%.2f GB free): trimmed the book cache by %.0f MB",
                           free / 1024**3, got / 2**20)
    if freed:
        logger.info("Housekeeping freed %.0f MB on the volume", freed / 2**20)


def _housekeeping_loop() -> None:
    last_report = 0.0
    while True:
        try:
            housekeeping()
            if time.time() - last_report > 3600:
                last_report = time.time()
                rep = disk_report()
                logger.info(
                    "Volume: %.2f GB used, %.2f GB free; by folder: %s",
                    rep["used"] / 1024**3, rep["free"] / 1024**3,
                    ", ".join(f"{k} {v / 2**20:.0f} MB" for k, v in list(rep["by_folder"].items())[:12]),
                )
        except Exception:
            logger.exception("Volume housekeeping failed")
        time.sleep(_HOUSEKEEPING_EVERY)


def sync_existing(book_paths: list[str]) -> None:
    """Upload every book not yet in the bucket (the first run moves the existing library), then evict. Blocking."""
    if not enabled():
        return
    st = _load_state()
    done = 0
    for path in book_paths:
        key = key_for(path)
        if key is None or key in st["uploaded"] or not os.path.exists(path):
            continue
        if upload(path):
            done += 1
    if done:
        logger.info("Book store: %d existing book(s) copied to the bucket", done)
    evict()


# --- uploads straight from the phone to the bucket ---------------------------
# The mini app gets presigned URLs for the parts of a multipart upload and
# PUTs each part to the bucket itself; the server only creates and completes
# the upload, then fetches the finished file once (bucket to server, same
# region) to check it. The file lands under _incoming/ until it is accepted.

DIRECT_PART_BYTES = 5 * 1024 * 1024   # S3's smallest part (except the last)
_PRESIGN_SECONDS = 6 * 3600


def incoming_key(upload_id: str) -> str:
    return f"_incoming/{upload_id}.pdf"


def start_direct_upload(upload_id: str, size: int) -> dict:
    """{key, mpu, part_size, urls}: one presigned PUT URL per part. Blocking."""
    key = incoming_key(upload_id)
    s3 = _s3()
    mpu = s3.create_multipart_upload(Bucket=BUCKET, Key=key, ContentType="application/pdf")["UploadId"]
    parts = max(1, -(-size // DIRECT_PART_BYTES))
    urls = [
        s3.generate_presigned_url(
            "upload_part",
            Params={"Bucket": BUCKET, "Key": key, "UploadId": mpu, "PartNumber": n},
            ExpiresIn=_PRESIGN_SECONDS,
        )
        for n in range(1, parts + 1)
    ]
    return {"key": key, "mpu": mpu, "part_size": DIRECT_PART_BYTES, "urls": urls}


def complete_direct_upload(key: str, mpu: str, parts: list[dict], dest_path: str) -> None:
    """Join the parts in the bucket, then fetch the file to dest_path (for the server's checks). Blocking."""
    s3 = _s3()
    s3.complete_multipart_upload(
        Bucket=BUCKET, Key=key, UploadId=mpu,
        MultipartUpload={"Parts": [{"PartNumber": p["n"], "ETag": p["etag"]} for p in sorted(parts, key=lambda p: p["n"])]},
    )
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    s3.download_file(BUCKET, key, dest_path)


def abort_direct_upload(key: str, mpu: str) -> None:
    try:
        _s3().abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=mpu)
    except Exception as e:
        logger.info("Abort of %s: %r", key, e)


def delete_key(key: str) -> None:
    try:
        _s3().delete_object(Bucket=BUCKET, Key=key)
    except Exception as e:
        logger.info("Delete of %s: %r", key, e)


def bucket_origins() -> list[str]:
    """Where the mini app sends parts (for its Content-Security-Policy)."""
    from urllib.parse import urlsplit

    host = urlsplit(os.environ.get("BOOKS_BUCKET_ENDPOINT") or "").hostname
    if not enabled() or not host:
        return []
    return [f"https://{host}", f"https://*.{host}"]


def configure_cors(app_origins: list[str]) -> None:
    """Let the mini app's page PUT parts to the bucket and read their ETag. Blocking; best effort."""
    try:
        _s3().put_bucket_cors(Bucket=BUCKET, CORSConfiguration={"CORSRules": [{
            "AllowedOrigins": app_origins,
            "AllowedMethods": ["PUT"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 3600,
        }]})
        logger.info("Book store: bucket CORS set for %s", app_origins)
    except Exception as e:
        logger.warning("Book store: couldn't set the bucket's CORS (direct uploads fall back to the server): %r", e)


def all_book_paths() -> list[str]:
    """Every book PDF on the volume: each user's library and the specialty library."""
    import glob

    return sorted(set(
        glob.glob(os.path.join(STORAGE_DIR, "*", "library", "*.pdf"))
        + glob.glob(os.path.join(STORAGE_DIR, "_shared", "books", "*.pdf"))
    ))


def start_background_sync() -> None:
    """At startup: copy any book not yet in the bucket (the first run moves the whole library), then trim the cache."""
    threading.Thread(target=_housekeeping_loop, daemon=True, name="volume-housekeeping").start()
    if not enabled():
        logger.info("Book store: no bucket configured (BOOKS_BUCKET); books stay on the volume only")
        return

    def run():
        try:
            from urllib.parse import urlsplit

            from config import WEBAPP_URL

            if WEBAPP_URL:
                u = urlsplit(WEBAPP_URL)
                configure_cors([f"{u.scheme}://{u.netloc}"])
            sync_existing(all_book_paths())
            logger.info("Book store: %s", status())
        except Exception:
            logger.exception("Book store background sync failed")

    threading.Thread(target=run, daemon=True, name="book-sync").start()


def status() -> dict:
    """For the admin: what's in the bucket and what's on the volume."""
    st = _load_state()
    root = os.path.realpath(STORAGE_DIR)
    in_bucket = sum(st["uploaded"].values())
    local = sum(os.path.getsize(os.path.join(root, k)) for k in st["uploaded"] if os.path.exists(os.path.join(root, k)))
    return {
        "enabled": enabled(),
        "books_in_bucket": len(st["uploaded"]),
        "bucket_bytes": in_bucket,
        "cached_bytes": local,
        "cache_limit_bytes": CACHE_LIMIT_BYTES,
    }
