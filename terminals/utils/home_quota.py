"""Per-user home-directory quotas for the Docker backend.

Docker cannot quota a bind mount, and ``StorageOpt`` only caps the container's
writable layer — so a policy's ``storage`` value historically did nothing to
bound ``/home/user`` (see the comment in ``backends/docker.py``).  This module
backs ``/home/user`` with a loopback-mounted ext4 image sized from the policy
so the configured cap is a real wall (ENOSPC) instead of advisory.

The loopback mount must happen in the *host's* mount namespace for the Docker
daemon to bind it into the terminal container.  The orchestrator container
normally has no such privileges, so the work runs through a short-lived
privileged helper container that ``nsenter``s into the host mount namespace
(the standard ``--privileged --pid=host`` trick).  The helper image needs
``mkfs.ext4``/``mount``/``losetup`` — ``debian:bookworm-slim`` ships all three
(e2fsprogs and util-linux are Priority: required), and the orchestrator image
also qualifies once they are installed explicitly (see the repo Dockerfile).

Every step is best-effort: on any failure the caller falls back to the plain
unquoted bind mount, so provisioning never breaks because quotaing failed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from terminals.utils.parsing import parse_size

log = logging.getLogger(__name__)

_HOME_IMG_NAME = "home.img"
_HOME_MOUNT_NAME = "home"
_MIN_QUOTA_BYTES = 256 * 1024 * 1024  # never size an image below 256 MiB


@dataclass
class QuotaPaths:
    """Resolved paths for one terminal's quota-backed home.

    ``bind_source()`` is the path the Docker daemon should bind to
    ``/home/user`` (already mapped into host-path space when
    ``TERMINALS_DOCKER_HOST_DATA_DIR`` is set).  ``image`` and ``mount_dir``
    are orchestrator-side paths, usable for direct file operations.
    """

    image: Path
    mount_dir: Path
    daemon_image: str
    daemon_mount: str

    def bind_source(self) -> str:
        return self.daemon_mount


def is_mounted(path: Path) -> bool:
    """Return True if *path* is a mount point according to /proc/mounts."""
    try:
        target = str(path.resolve())
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == target:
                    return True
    except OSError:
        pass
    return False


def dir_usage_bytes(path: Path) -> int:
    """Sum of apparent file sizes under *path* (a cheap ``du -sb``)."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                else:
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _run(cmd: list[str], *, timeout: int = 60) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}"
        )


def _truncate_to(img: Path, size_bytes: int) -> None:
    _run(["truncate", "-s", str(size_bytes), str(img)])


def _build_helper_script(host_img_path: str, commands: list[str], *, attach_loop: bool) -> str:
    """Compose the shell script executed inside the privileged helper.

    ``commands`` may reference the loop device as ``$DEV``.  When
    ``attach_loop`` is set, an existing attachment for the image is reused
    (so resize2fs can target the device backing a mounted fs); otherwise a
    fresh loop device is attached and released on exit.
    """
    if attach_loop:
        attach = (
            f'DEV=$(losetup -j "{host_img_path}" | head -1 | cut -d: -f1); '
            f'[ -n "$DEV" ] || DEV=$(losetup --find --show "{host_img_path}"); '
            '[ -n "$DEV" ] || exit 3; '
        )
    else:
        attach = (
            f'DEV=$(losetup --find --show "{host_img_path}"); '
            '[ -n "$DEV" ] || exit 3; '
            "trap 'losetup -d \"$DEV\" 2>/dev/null' EXIT; "
        )
    body = " && ".join(cmd.replace("$DEV", "$DEV") for cmd in commands)
    return f"set -e; {attach} {body}"


async def _run_via_helper(
    docker_runner,
    helper_image: str,
    host_img_path: str,
    commands: list[str],
    *,
    attach_loop: bool,
) -> None:
    """Run filesystem commands inside the host mount namespace.

    ``docker_runner(script)`` is supplied by the backend: it starts a
    short-lived privileged ``--pid=host`` helper container running
    ``nsenter -t 1 -m`` and awaits its completion, raising on failure.
    """
    script = _build_helper_script(host_img_path, commands, attach_loop=attach_loop)
    await docker_runner(helper_image, script)


async def prepare_quota_home(
    home_root: Path,
    storage_spec: str,
    *,
    max_storage_spec: str = "",
    daemon_path_map=None,
    helper_image: str = "debian:bookworm-slim",
    docker_runner=None,
) -> QuotaPaths | None:
    """Prepare a quota-backed home under *home_root*.

    Returns a :class:`QuotaPaths` whose ``bind_source()`` is what the Docker
    daemon should bind to ``/home/user``, or ``None`` when quotaing is not
    possible (the caller keeps the plain bind mount).

    ``daemon_path_map`` maps an orchestrator-side path to the Docker-host path
    (identity when ``TERMINALS_DOCKER_HOST_DATA_DIR`` is unset).
    ``docker_runner(helper_image, script)`` starts the privileged helper.
    """
    storage = (storage_spec or "").strip()
    if not storage or storage.lower() in {"ephemeral", "none", "0"}:
        return None
    try:
        wanted = parse_size(storage)
    except Exception:
        log.warning("Unparseable storage value %r — no home quota applied", storage)
        return None
    if max_storage_spec:
        try:
            wanted = min(wanted, parse_size(max_storage_spec))
        except Exception:
            pass
    wanted = max(wanted, _MIN_QUOTA_BYTES)

    img = home_root / _HOME_IMG_NAME
    mount_dir = home_root / _HOME_MOUNT_NAME
    try:
        home_root.mkdir(parents=True, exist_ok=True)
        mount_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create quota home under %s: %s", home_root, exc)
        return None

    def map_to_daemon(p: Path) -> str:
        if daemon_path_map is None:
            return str(p)
        try:
            return daemon_path_map(p)
        except Exception:
            return str(p)

    try:
        if not is_mounted(mount_dir):
            if not img.exists():
                await asyncio.to_thread(_truncate_to, img, wanted)
                await _run_via_helper(
                    docker_runner,
                    helper_image,
                    map_to_daemon(img),
                    [f"mkfs.ext4 -q -F $DEV"],
                    attach_loop=False,
                )
            else:
                await _maybe_grow(img, wanted, helper_image, docker_runner, map_to_daemon)
            await _run_via_helper(
                docker_runner,
                helper_image,
                map_to_daemon(img),
                [f"mount -o rw $DEV {map_to_daemon(mount_dir)}"],
                attach_loop=True,
            )
            if not is_mounted(mount_dir):
                raise RuntimeError("mount did not register in /proc/mounts")
        else:
            await _maybe_grow(img, wanted, helper_image, docker_runner, map_to_daemon)

        await asyncio.to_thread(_migrate_legacy_home, home_root, mount_dir)
        return QuotaPaths(
            image=img,
            mount_dir=mount_dir,
            daemon_image=map_to_daemon(img),
            daemon_mount=map_to_daemon(mount_dir),
        )
    except Exception as exc:
        log.warning(
            "Home quota setup failed for %s (%s) — falling back to an unquoted bind mount",
            home_root, exc,
        )
        return None


async def _maybe_grow(
    img: Path,
    wanted: int,
    helper_image: str,
    docker_runner,
    map_to_daemon,
) -> None:
    """Grow an existing image/filesystem when the policy asks for more space.

    Growing keeps user data: extend the sparse file, then ``resize2fs`` the
    (reused) loop device — online resize works for mounted ext4 filesystems
    that carry the default ``resize_inode`` feature.
    """
    try:
        current = img.stat().st_size
    except OSError:
        return
    if current >= wanted:
        return
    log.info("Growing home quota image %s from %d to %d bytes", img, current, wanted)
    await asyncio.to_thread(_truncate_to, img, wanted)
    mounted = is_mounted(img.parent / _HOME_MOUNT_NAME)
    await _run_via_helper(
        docker_runner,
        helper_image,
        map_to_daemon(img),
        ["resize2fs $DEV"],
        # When already mounted the loop device is attached: reuse it.  When
        # unmounted, attach temporarily for the offline resize.
        attach_loop=mounted,
    )


def _migrate_legacy_home(home_root: Path, mount_dir: Path) -> None:
    """Move pre-quota files that lived directly in *home_root* into the new fs.

    Homes created before quotas existed were bind-mounted from ``home_root``
    itself, so existing user data sits beside ``home.img``/``home``.  Move it
    into the freshly mounted filesystem (or a backup folder when the fs
    already has content, e.g. after a partial migration).
    """
    try:
        legacy = [
            entry
            for entry in home_root.iterdir()
            if entry.name not in (_HOME_IMG_NAME, _HOME_MOUNT_NAME)
            and not entry.name.startswith(".legacy-")
        ]
        if not legacy:
            return
        existing = list(mount_dir.iterdir())
        if existing:
            backup = home_root / f".legacy-{int(time.time())}"
            backup.mkdir(exist_ok=True)
            for entry in legacy:
                shutil.move(str(entry), str(backup / entry.name))
            log.info(
                "Moved %d legacy home entries of %s to %s (mount already populated)",
                len(legacy), home_root, backup,
            )
            return
        for entry in legacy:
            shutil.move(str(entry), str(mount_dir / entry.name))
        log.info(
            "Migrated %d legacy home entries of %s into the quota fs", len(legacy), home_root
        )
    except Exception as exc:  # migration is best-effort; never block provisioning
        log.warning("Legacy home migration under %s failed: %s", home_root, exc)


def wipe_quota_home(home_root: Path) -> None:
    """Reset a quota-backed home: empty the mounted filesystem, keep the image.

    Mirrors ``DockerBackend.reset`` semantics for loop-backed homes — the
    image and its mount survive so the next provision is ready to go.
    """
    mount_dir = home_root / _HOME_MOUNT_NAME
    if not mount_dir.exists():
        return
    for child in mount_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
