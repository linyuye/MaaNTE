import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

from utils.logger import logger


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_param(raw_param):
    if not raw_param:
        return {}
    if isinstance(raw_param, dict):
        return raw_param
    try:
        params = json.loads(raw_param)
    except (TypeError, json.JSONDecodeError):
        return {}
    return params if isinstance(params, dict) else {}


def _find_restart_program(root: Path) -> str:
    for name in ("MaaNTE.exe", "MaaNTE"):
        path = root / name
        if path.exists():
            return str(path)
    return ""


@AgentServer.custom_action("program_update")
class ProgramUpdate(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = _parse_param(argv.custom_action_param)
        root = _project_root()
        updater = root / "tools" / "ci" / "update_client.py"
        if not updater.exists():
            logger.error("更新脚本不存在: %s", updater)
            return CustomAction.RunResult(success=False)

        temp_dir = Path(tempfile.mkdtemp(prefix="maante-updater-launch-"))
        temp_updater = temp_dir / "update_client.py"
        shutil.copy2(updater, temp_updater)

        cmd = [
            sys.executable,
            str(temp_updater),
            "--target-root",
            str(root),
            "--wait-pid",
            str(os.getpid()),
            "--restart",
            params.get("restart", "") or _find_restart_program(root),
        ]

        for key, option in (
            ("owner", "--owner"),
            ("repo", "--repo"),
            ("release_api", "--release-api"),
            ("manifest_url", "--manifest-url"),
            ("base_url", "--base-url"),
            ("archive_url", "--archive-url"),
            ("asset_hint", "--asset-hint"),
        ):
            value = params.get(key)
            if value:
                cmd.extend([option, str(value)])

        logger.info("启动后台更新进程: %s", " ".join(cmd))
        try:
            subprocess.Popen(
                cmd,
                cwd=str(root),
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception:
            logger.exception("启动后台更新进程失败")
            return CustomAction.RunResult(success=False)

        logger.warning("更新进程已启动。请关闭 MaaNTE，更新完成后会尝试自动重启。")
        return CustomAction.RunResult(success=True)
