import tempfile
import unittest
from pathlib import Path
from unittest import mock

from terminals.utils import home_quota
from terminals.utils.env import NET_ADMIN_ENV_VARS
from terminals.utils.home_quota import (
    QuotaPaths,
    _build_helper_script,
    _migrate_legacy_home,
    dir_usage_bytes,
    prepare_quota_home,
    wipe_quota_home,
)


class BuildHelperScriptTests(unittest.TestCase):
    def test_attach_loop_reuses_existing_attachment(self) -> None:
        script = _build_helper_script(
            "/data/.terminals/u1/home.img",
            ["mount -o rw $DEV /data/.terminals/u1/home"],
            attach_loop=True,
        )
        self.assertIn("losetup -j", script)
        self.assertIn("losetup --find --show", script)
        self.assertIn("mount -o rw $DEV", script)
        self.assertNotIn("trap", script)

    def test_fresh_attach_releases_on_exit(self) -> None:
        script = _build_helper_script("/img", ["mkfs.ext4 -q -F $DEV"], attach_loop=False)
        self.assertIn("trap", script)
        self.assertIn("mkfs.ext4 -q -F $DEV", script)


class PrepareQuotaHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home_root = Path(self.tmp.name) / "u1"
        self.runs: list[tuple[str, str]] = []

        async def fake_runner(helper_image: str, script: str) -> None:
            self.runs.append((helper_image, script))

        self.runner = fake_runner

    def test_returns_none_for_ephemeral(self) -> None:
        self.assertIsNone(self._prepare("ephemeral"))
        self.assertIsNone(self._prepare(""))
        self.assertFalse(self.runs)

    def test_clamps_to_max_storage(self) -> None:
        with mock.patch.object(
            home_quota, "is_mounted", side_effect=[False, True]
        ):
            quota = self._prepare("100Gi", max_storage="5Gi")
        self.assertIsNotNone(quota)
        self.assertLessEqual(quota.image.stat().st_size, 5 * 1024**3)

    def test_full_prepare_creates_mkfs_and_mount(self) -> None:
        mounted = {"state": False}

        def fake_is_mounted(path) -> bool:
            # Pre-check sees the unmounted dir; the post-mount verification
            # must succeed for provisioning to accept the quota.
            was = mounted["state"]
            mounted["state"] = True
            return was

        with mock.patch.object(home_quota, "is_mounted", fake_is_mounted):
            # First call: not mounted -> create image, mkfs, mount.
            quota = self._prepare("1Gi")
        self.assertIsNotNone(quota)
        self.assertEqual(quota.image.stat().st_size, 1024**3)
        self.assertEqual(quota.bind_source(), str(quota.mount_dir))
        self.assertTrue(quota.image.exists())
        helper_scripts = [script for _, script in self.runs]
        self.assertTrue(any("mkfs.ext4" in s for s in helper_scripts))
        self.assertTrue(any("mount -o rw" in s for s in helper_scripts))
        # The mount command targets the mount dir.
        mount_cmds = [s for s in helper_scripts if "mount -o rw" in s]
        self.assertIn(str(self.home_root / "home"), mount_cmds[0])

    def test_unparseable_storage_returns_none(self) -> None:
        self.assertIsNone(self._prepare("not-a-size"))

    def _prepare(self, storage: str, max_storage: str = "") -> QuotaPaths | None:
        return self._run_prepare(storage, max_storage)

    def _run_prepare(self, storage: str, max_storage: str = ""):
        import asyncio

        return asyncio.run(
            prepare_quota_home(
                self.home_root,
                storage,
                max_storage_spec=max_storage,
                helper_image="debian:bookworm-slim",
                docker_runner=self.runner,
            )
        )


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home_root = Path(self.tmp.name) / "u1"
        self.mount_dir = self.home_root / "home"
        self.home_root.mkdir(parents=True)
        self.mount_dir.mkdir()

    def test_legacy_files_move_into_empty_mount(self) -> None:
        (self.home_root / "notes.txt").write_text("hello")
        (self.home_root / "project").mkdir()
        (self.home_root / "project" / "a.bin").write_bytes(b"x" * 10)

        _migrate_legacy_home(self.home_root, self.mount_dir)

        self.assertTrue((self.mount_dir / "notes.txt").exists())
        self.assertTrue((self.mount_dir / "project" / "a.bin").exists())
        self.assertFalse((self.home_root / "notes.txt").exists())
        self.assertEqual(dir_usage_bytes(self.mount_dir), 15)

    def test_legacy_files_backed_up_when_mount_populated(self) -> None:
        (self.home_root / "old.txt").write_text("data")
        (self.mount_dir / "existing.txt").write_text("keep")

        _migrate_legacy_home(self.home_root, self.mount_dir)

        self.assertFalse((self.home_root / "old.txt").exists())
        backups = list(self.home_root.glob(".legacy-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "old.txt").exists())
        self.assertTrue((self.mount_dir / "existing.txt").exists())

    def test_no_legacy_is_noop(self) -> None:
        _migrate_legacy_home(self.home_root, self.mount_dir)
        # Only the mount dir itself remains; nothing was moved or created.
        self.assertEqual([p.name for p in self.home_root.iterdir()], ["home"])
        self.assertEqual(list(self.mount_dir.iterdir()), [])


class WipeQuotaHomeTests(unittest.TestCase):
    def test_wipes_mount_contents_keeps_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "u1"
            (root / "home").mkdir(parents=True)
            (root / "home.img").write_bytes(b"")
            (root / "home" / "big.bin").write_bytes(b"x" * 5)
            (root / "home" / "sub").mkdir()
            (root / "home" / "sub" / "f").write_text("f")

            wipe_quota_home(root)

            self.assertTrue((root / "home.img").exists())
            self.assertEqual(list((root / "home").iterdir()), [])


class NetAdminEnvVarsTests(unittest.TestCase):
    def test_includes_download_cap_and_egress(self) -> None:
        self.assertIn("OPEN_TERMINAL_ALLOWED_DOMAINS", NET_ADMIN_ENV_VARS)
        self.assertIn("OPEN_TERMINAL_MAX_DOWNLOAD_BYTES", NET_ADMIN_ENV_VARS)


if __name__ == "__main__":
    unittest.main()
