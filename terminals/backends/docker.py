"""Docker backend — provisions Open Terminal inside containers via aiodocker."""

import asyncio
import hashlib
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import Optional

import aiodocker
import httpx

from terminals.backends.base import Backend, parse_started_at
from terminals.config import settings
from terminals.utils.template_specs import TemplateNotFoundError, resolve_template_spec
from terminals.utils.context import (
    CONTEXT_HASH_LABEL,
    CONTEXT_ID_ANNOTATION,
    CONTEXT_TYPE_LABEL,
    DEFAULT_CONTEXT_ID,
    context_hash,
    normalize_context_id,
    get_terminal_id,
)
from terminals.utils.env import NET_ADMIN_ENV_VARS, build_terminal_env
from terminals.utils.home_quota import (
    is_mounted as home_is_mounted,
    prepare_quota_home,
    wipe_quota_home,
)
from terminals.utils.docker_mounts import parse_docker_mounts
from terminals.utils.parsing import parse_cpu_nanos, parse_memory, parse_size
from terminals.utils.network_profiles import resolve_network_profile
from terminals.utils.image_pull import ensure_docker_image

log = logging.getLogger(__name__)

# Container name prefix used for discovery during reconciliation.
_CONTAINER_PREFIX = "terminals-"
_DNS_SAFE = re.compile(r"[^a-z0-9-]")
_QUIET_LOG_LEVELS = {"WARNING", "ERROR", "CRITICAL"}


class DockerBackend(Backend):
    """Manage terminal environments as Docker containers."""

    def __init__(self) -> None:
        super().__init__()
        self._docker: Optional[aiodocker.Docker] = None
        self._mounts = parse_docker_mounts(settings.docker_mounts)

    async def _get_docker(self) -> aiodocker.Docker:
        if self._docker is None:
            self._docker = aiodocker.Docker()
        return self._docker

    @staticmethod
    def _container_name(template_id: str, context_id: str = DEFAULT_CONTEXT_ID) -> str:
        return f"terminal-{get_terminal_id(template_id, context_id)}"

    @staticmethod
    def _home_dir(template_id: str, context_id: str = DEFAULT_CONTEXT_ID) -> Path:
        root = Path(settings.docker_data_dir) / ".terminals"
        home = root / get_terminal_id(template_id, context_id)
        if root.is_symlink() or home.is_symlink():
            raise ValueError("Terminal directories must not be symlinks")
        return home

    def _map_to_daemon_path(self, path: Path) -> str:
        """Map an orchestrator-side data-dir path into Docker-host path space.

        Identity unless ``TERMINALS_DOCKER_HOST_DATA_DIR`` is set (deployments
        where the orchestrator container's view of the data dir differs from
        the host path the Docker daemon bind-mounts from).
        """
        if not settings.docker_host_data_dir:
            return str(path)
        relative = path.relative_to(Path(settings.docker_data_dir))
        return str(Path(settings.docker_host_data_dir) / relative)

    async def _run_quota_helper(self, helper_image: str, script: str) -> None:
        """Run *script* inside the host mount namespace via a privileged helper.

        Starts a short-lived ``--privileged --pid=host`` container running
        ``nsenter -t 1 -m -- sh -c <script>`` and awaits its exit.  Mounts and
        losetup operations performed this way are visible to the Docker daemon
        (they happen on the host), which plain in-container mounts are not.
        """
        docker = await self._get_docker()
        await ensure_docker_image(docker, helper_image)
        config = {
            "Image": helper_image,
            "Cmd": ["nsenter", "-t", "1", "-m", "--", "sh", "-c", script],
            "HostConfig": {
                "Privileged": True,
                "PidMode": "host",
                "NetworkMode": "none",
            },
            "Labels": {"app.kubernetes.io/managed-by": "terminals-quota-helper"},
        }
        container = await docker.containers.run(config)
        try:
            result = await container.wait(timeout=180)
            code = (result or {}).get("StatusCode", 1)
            if code != 0:
                try:
                    logs = await container.log(stdout=True, stderr=True)
                    tail = "".join(logs or [])[-400:]
                except Exception:
                    tail = ""
                raise RuntimeError(f"quota helper exited {code}: {tail}")
        finally:
            try:
                await container.delete(force=True)
            except aiodocker.exceptions.DockerError:
                pass

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    async def provision(
        self,
        template_id: str,
        context_id: str = DEFAULT_CONTEXT_ID,
        spec: dict | None = None,
    ) -> dict:
        profile = resolve_network_profile(spec, "docker")
        docker = await self._get_docker()
        api_key = secrets.token_urlsafe(24)
        context_id = normalize_context_id(context_id)
        workload_name = self._container_name(template_id, context_id)
        host_data_dir = str(self._home_dir(template_id, context_id).resolve())
        if settings.docker_host_data_dir:
            relative_home = self._home_dir(template_id, context_id).relative_to(settings.docker_data_dir)
            host_data_dir = str(Path(settings.docker_host_data_dir) / relative_home)
        s = spec or {}

        # Home-directory quota: when the policy sets `storage`, back /home/user
        # with a loop-mounted ext4 image sized from the policy (Docker cannot
        # quota a bind mount, and StorageOpt only caps the writable layer).
        # Falls back to the plain bind mount when quotaing is not possible.
        bind_source = host_data_dir
        quota = None
        storage_spec = str(s.get("storage") or "").strip()
        if storage_spec:
            quota = await prepare_quota_home(
                self._home_dir(template_id, context_id),
                storage_spec,
                max_storage_spec=settings.max_storage,
                daemon_path_map=self._map_to_daemon_path,
                helper_image=settings.docker_quota_helper_image,
                docker_runner=self._run_quota_helper,
            )
            if quota is not None:
                bind_source = quota.bind_source()
                log.info(
                    "Home quota %s active for %s (bind %s)",
                    storage_spec, workload_name, bind_source,
                )
            else:
                log.warning(
                    "Home quota %s unavailable for %s — /home/user is unbounded",
                    storage_spec, workload_name,
                )

        image = s.get("image", settings.image)
        network = profile.get("docker_network", settings.docker_network)
        networks = [name.strip() for name in network.split(",") if name.strip()]
        network_name = None
        if len(networks) == 1:
            network_name = networks[0]
        elif networks:
            key = get_terminal_id(template_id, context_id).encode()
            network_name = networks[int(hashlib.sha256(key).hexdigest(), 16) % len(networks)]

        host_config: dict = {
            "Binds": [f"{bind_source}:/home/user"],
            "PublishAllPorts": not network_name,
        }
        if self._mounts:
            host_config["Mounts"] = self._mounts
        if (settings.log_level or "").strip().upper() in _QUIET_LOG_LEVELS:
            host_config["LogConfig"] = {"Type": "none"}

        # Resources
        if s.get("memory_limit"):
            host_config["Memory"] = parse_memory(s["memory_limit"])
        if s.get("cpu_limit"):
            host_config["NanoCpus"] = parse_cpu_nanos(s["cpu_limit"])

        # Storage caps the container's writable layer via StorageOpt.  This is
        # only a fallback for deployments where the home quota above is not
        # available: it never bounds the bind-mounted /home/user (Docker can't
        # quota a bind mount) and it needs a storage driver that supports it
        # (e.g. overlay2 on xfs with pquota); on others the create below
        # fails, so the loop drops the quota and retries.
        if storage_spec and quota is None:
            host_config["StorageOpt"] = {"size": str(parse_size(storage_spec))}
            log.info(
                "Applying storage quota %s to writable layer of %s (home quota unavailable)",
                storage_spec, workload_name,
            )

        # Egress filtering (dnsmasq + ipset + iptables) and the per-connection
        # download cap (connbytes rule) are set up inside the container and
        # need CAP_NET_ADMIN, which the entrypoint drops after setup.  Grant
        # it at create time when any netfilter-based feature is configured.
        terminal_env = build_terminal_env(
            s.get("env", {}),
            cpu_limit=s.get("cpu_limit"),
            memory_limit=s.get("memory_limit"),
        )
        if any(var in terminal_env for var in NET_ADMIN_ENV_VARS):
            host_config["CapAdd"] = ["NET_ADMIN"]
        if network_name:
            host_config["NetworkMode"] = network_name

        # Env vars
        env = [f"OPEN_TERMINAL_API_KEY={api_key}"]
        for k, v in terminal_env.items():
            env.append(f"{k}={v}")

        config: dict = {
            "Image": image,
            "Env": env,
            "HostConfig": host_config,
            "ExposedPorts": {"8000/tcp": {}},
            "Labels": {
                "app.kubernetes.io/managed-by": "terminals",
                "openwebui.com/instance": get_terminal_id(template_id, context_id),
                "openwebui.com/template": template_id,
                CONTEXT_ID_ANNOTATION: context_id,
                CONTEXT_TYPE_LABEL: (
                    DEFAULT_CONTEXT_ID
                    if context_id == DEFAULT_CONTEXT_ID
                    else context_id.split(":", 1)[0]
                ),
                CONTEXT_HASH_LABEL: context_hash(context_id),
            },
        }

        log.info(
            "Provisioning container %s (template=%s, context=%s, network=%s)",
            workload_name,
            template_id,
            context_id,
            network_name or "default",
        )

        await ensure_docker_image(docker, image)

        max_conflict_retries = 3
        container = None
        for attempt in range(max_conflict_retries + 1):
            try:
                container = await docker.containers.run(config, name=workload_name)
                break
            except aiodocker.exceptions.DockerError as exc:
                if exc.status == 409 and attempt < max_conflict_retries:
                    try:
                        old = await docker.containers.get(workload_name)
                        old_info = await old.show()
                        if old_info.get("State", {}).get("Running"):
                            for entry in old_info.get("Config", {}).get("Env", []):
                                if entry.startswith("OPEN_TERMINAL_API_KEY="):
                                    api_key = entry.split("=", 1)[1]
                                    break
                            container = old
                            log.info("Adopted running container %s", workload_name)
                            break
                        log.warning(
                            "Container %s conflict (attempt %d/%d), removing stopped container",
                            workload_name, attempt + 1, max_conflict_retries,
                        )
                        await old.delete(force=True)
                    except aiodocker.exceptions.DockerError as adopt_exc:
                        log.warning("Could not adopt/remove %s: %s", workload_name, adopt_exc)
                    await asyncio.sleep(1)
                    continue
                # The storage driver may not support StorageOpt size quotas
                # (e.g. overlay2 on ext4).  Drop the quota and retry once rather
                # than failing to provision.  host_config is the same object
                # referenced by config["HostConfig"], so this mutation applies
                # to the next create attempt.
                if "StorageOpt" in host_config:
                    log.warning(
                        "Storage quota not supported by the Docker storage driver "
                        "for %s (%s), provisioning without a storage limit",
                        workload_name, exc,
                    )
                    host_config.pop("StorageOpt", None)
                    continue
                log.error("Failed to provision container for %s: %s", template_id, exc)
                raise

        if container is None:
            raise RuntimeError(f"Failed to provision or adopt container {workload_name}")

        result = await self._extract_instance_info(container, workload_name, api_key)
        await self._wait_until_ready(result, timeout=15)
        return result

    async def _wait_until_ready(self, instance: dict, timeout: int = 15) -> None:
        """Poll the container's /health endpoint until it responds."""

        url = f"http://{instance['host']}:{instance['port']}/health"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        log.info("Container %s is ready", instance["workload_name"])
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        log.warning("Container %s did not become ready within %ds", instance["workload_name"], timeout)

    async def _extract_instance_info(
        self,
        container,
        workload_name: str,
        api_key: str,
    ) -> dict:
        """Read container metadata and return the instance info dict."""
        info = await container.show()

        # When using a custom Docker network, containers can reach each other
        # by name.  Otherwise, use the published port on the Docker host.
        network_mode = info.get("HostConfig", {}).get("NetworkMode", "default")
        if network_mode not in ("default", "bridge", "host", "none", ""):
            host = workload_name
            port = 8000
        else:
            port_bindings = (
                info.get("NetworkSettings", {})
                .get("Ports", {})
                .get("8000/tcp", [])
            )
            if port_bindings:
                port = int(port_bindings[0]["HostPort"])
            else:
                port = 8000
            host = settings.docker_host

        return {
            "workload_name": workload_name,
            "api_key": api_key,
            "host": host,
            "port": port,
            "started_at": parse_started_at(info.get("State", {}).get("StartedAt")),
        }

    # ------------------------------------------------------------------
    # Reconciliation — rediscover running containers on startup
    # ------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Scan running Docker containers and repopulate ``_instances``.

        Called during startup to recover state after a restart without
        tearing down existing containers.  Uses Docker labels to identify
        the template and context.  The API key is read from the container's
        ``OPEN_TERMINAL_API_KEY`` env var.
        """
        docker = await self._get_docker()
        containers = await docker.containers.list(
            filters={
                "label": ["app.kubernetes.io/managed-by=terminals"],
                "status": ["running"],
            },
        )

        recovered = 0
        adopted_specs: dict[str, dict] = {}
        for container in containers:
            info = await container.show()
            name = info.get("Name", "").lstrip("/")
            labels = info.get("Config", {}).get("Labels", {})

            template_id = labels.get("openwebui.com/template")
            context_id = labels.get(CONTEXT_ID_ANNOTATION)
            if not template_id or not context_id:
                log.warning("Skipping container %s without template/context metadata", name)
                continue
            if name != self._container_name(template_id, context_id):
                continue

            key = self._key(template_id, context_id)

            # Already tracked
            if key in self._instances:
                continue

            api_key = ""
            for entry in info.get("Config", {}).get("Env", []):
                if entry.startswith("OPEN_TERMINAL_API_KEY="):
                    api_key = entry.split("=", 1)[1]
                    break

            if template_id not in adopted_specs:
                try:
                    adopted_specs[template_id] = await resolve_template_spec(template_id)
                except TemplateNotFoundError:
                    log.warning("Skipping container %s with unknown template %s", name, template_id)
                    continue
            instance_info = await self._extract_instance_info(container, name, api_key)
            self._instances[key] = instance_info
            self._specs[key] = adopted_specs[template_id]
            self._instances[key].update(template_id=template_id, context_id=context_id)
            recovered += 1
            log.info("Reconciled container %s → %s:%s", name, instance_info["host"], instance_info["port"])

        if recovered:
            log.info("Reconciled %d running container(s)", recovered)

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------



    async def reset(
        self,
        template_id: str,
        context_id: str = DEFAULT_CONTEXT_ID,
        spec: dict | None = None,
    ) -> None:
        home_dir = self._home_dir(template_id, context_id).resolve()
        home_dir.mkdir(parents=True, exist_ok=True)
        img = home_dir / "home.img"
        if img.exists():
            # Quota-backed home: empty the mounted filesystem (keeping the
            # image and its mount), or drop both when currently unmounted.
            if home_is_mounted(home_dir / "home"):
                wipe_quota_home(home_dir)
            else:
                img.unlink(missing_ok=True)
                shutil.rmtree(home_dir / "home", ignore_errors=True)
            return
        for child in home_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    async def status(self, name: str) -> str:
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(name)
            info = await container.show()
            state = info.get("State", {})
            if state.get("Running"):
                return "running"
            return "stopped"
        except aiodocker.exceptions.DockerError as exc:
            if exc.status != 404:
                raise
            return "missing"

    async def close(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None
