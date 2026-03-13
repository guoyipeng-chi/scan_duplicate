from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ModelRoute:
    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    agent_name: str | None = None


@dataclass
class ModelConfig:
    routes: list[ModelRoute]
    temperature: float = 0.1
    max_tokens: int = 4096


@dataclass
class BuildStepConfig:
    command: str | list[str]
    cwd: str = "."
    shell: bool = False


@dataclass
class BuildConfig:
    before_apply: BuildStepConfig | None = None
    after_apply: BuildStepConfig | None = None


@dataclass
class AppConfig:
    model: ModelConfig
    workspace: Path
    build: BuildConfig


DEFAULT_CONFIG_NAME = "deduper.config.json"


def load_config(config_path: Path | None = None) -> AppConfig:
    path = config_path or Path(DEFAULT_CONFIG_NAME)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}. 请先复制示例并填写模型参数。"
        )

    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    model_raw = raw.get("model", {})
    build_raw = raw.get("build", {})
    workspace = Path(raw.get("workspace", ".")).resolve()

    routes_raw = model_raw.get("routes", [])
    routes: list[ModelRoute] = []
    if isinstance(routes_raw, list) and routes_raw:
        for item in routes_raw:
            routes.append(
                ModelRoute(
                    provider=str(item.get("provider", "ollama")).lower(),
                    model=str(item.get("model", "qwen3-coder:latest")),
                    base_url=item.get("base_url"),
                    api_key=item.get("api_key"),
                    agent_name=item.get("agent_name"),
                )
            )
    else:
        provider = str(model_raw.get("provider", "agent")).lower()
        if provider == "agent":
            routes.extend(
                [
                    ModelRoute(
                        provider="agent",
                        model=str(model_raw.get("model", "claude-3-5-sonnet-20241022")),
                        base_url=model_raw.get("base_url"),
                        api_key=model_raw.get("api_key"),
                        agent_name=model_raw.get("agent_name"),
                    ),
                    ModelRoute(
                        provider="vllm",
                        model="Qwen/Qwen3-Coder-32B-Instruct",
                        base_url="http://127.0.0.1:8000",
                        api_key="EMPTY",
                    ),
                    ModelRoute(
                        provider="ollama",
                        model="qwen3-coder:480b-cloud",
                        base_url="http://localhost:11434",
                        api_key=None,
                    ),
                ]
            )
        else:
            routes.append(
                ModelRoute(
                    provider=provider,
                    model=str(model_raw.get("model", "qwen3-coder:480b-cloud")),
                    base_url=model_raw.get("base_url", "http://localhost:11434"),
                    api_key=model_raw.get("api_key"),
                    agent_name=model_raw.get("agent_name"),
                )
            )

    model = ModelConfig(
        routes=routes,
        temperature=float(model_raw.get("temperature", 0.1)),
        max_tokens=int(model_raw.get("max_tokens", 4096)),
    )

    def _parse_build_step(payload: Any) -> BuildStepConfig | None:
        if not isinstance(payload, dict):
            return None
        command = payload.get("command")
        if not isinstance(command, (str, list)) or not command:
            return None
        return BuildStepConfig(
            command=command,
            cwd=str(payload.get("cwd", ".")),
            shell=bool(payload.get("shell", False)),
        )

    build = BuildConfig(
        before_apply=_parse_build_step(build_raw.get("before_apply")),
        after_apply=_parse_build_step(build_raw.get("after_apply")),
    )
    return AppConfig(model=model, workspace=workspace, build=build)
