from __future__ import annotations

import json
from datetime import datetime
from dataclasses import asdict
from pathlib import Path
from typing import Any

import requests

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

from .config import ModelConfig, ModelRoute
from .types import DuplicationGroup, LLMLineOpsPlan, LLMRefactorPlan


def _save_prompt_snapshot(
    *,
    mode: str,
    route: ModelRoute,
    system_prompt: str,
    user_prompt: str,
) -> Path:
    out_dir = Path("artifacts") / "prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    provider = route.provider.replace("/", "_").replace("\\", "_")
    model = route.model.replace("/", "_").replace("\\", "_").replace(":", "_")
    out_file = out_dir / f"{ts}_{mode}_{provider}_{model}.txt"
    content = (
        f"mode: {mode}\n"
        f"provider: {route.provider}\n"
        f"model: {route.model}\n"
        f"base_url: {route.base_url or ''}\n"
        "\n===== SYSTEM PROMPT =====\n"
        f"{system_prompt}\n"
        "\n===== USER PROMPT =====\n"
        f"{user_prompt}\n"
    )
    out_file.write_text(content, encoding="utf-8")
    print(f"[LLM] prompt snapshot saved: {out_file}")
    return out_file


def _effective_system_prompt(base_prompt: str, route: ModelRoute) -> str:
    if route.provider == "agent" and route.agent_name:
        return f"{base_prompt}\n\n你的角色标识: {route.agent_name}"
    return base_prompt


SYSTEM_PROMPT = """你是资深重构工程师。你将收到若干 CPD 重复代码组和相关文件上下文。
你必须输出严格 JSON（不要 markdown 代码块），格式如下：
{
  \"common_file\": \"相对路径\",
  \"common_code\": \"完整公共函数/类代码\",
  \"replacements\": [
    {\"file\": \"相对路径\", \"start_line\": 1, \"end_line\": 1, \"replacement\": \"替换文本\"}
  ],
  \"notes\": \"简短说明\"
}
规则：
1) 仅修改输入中涉及的文件。
2) start_line 和 end_line 必须是有效的行号（>=1，<=文件总行数）。
3) replacement 必须可直接替换对应行区间，保持语法正确。
4) common_file 使用项目内相对路径。
5) 尽量减少行为改变，优先提取公共函数。
6) 文件上下文中每行都带行号前缀（格式: 行号|代码），请根据行号确定准确的 start_line 和 end_line。
"""


LINE_OPS_SYSTEM_PROMPT = """你是资深重构工程师。
你将收到 PMD 重复组的结构化信息：文件路径与起止行号（不含代码内容）。
你不能输出整段替换方案，只能输出结构化行操作 JSON。

输出格式（严格 JSON，不要 markdown）：
{
    "operations": {
        "cut_paste": [
            {
                "source_file": "相对路径",
                "start_line": 1,
                "end_line": 10,
                "target_file": "相对路径",
                "target_line": 20,
                "position": "before|after"
            }
        ],
        "delete": [
            {"file": "相对路径", "start_line": 1, "end_line": 10}
        ],
        "insert": [
            {"file": "相对路径", "line": 1, "position": "before|after", "content": "要插入的文本"}
        ]
    },
    "notes": "简短说明"
}

约束：
1) 只能使用输入中出现的文件。
2) delete/cut_paste 只使用给定行号范围。
3) 所有行号都基于“原始文件内容”（修改前坐标），不要按修改后的行号推算。
4) insert 可写入新内容，但必须带明确行号与 position。
5) insert/cut_paste 的目标行不能落在任何 delete/cut 删除区间内。
6) 同一文件内不要出现“先删除某段、再把插入目标放在该删除段内”的冲突操作。
4) 优先策略：提取公共头文件，差异用宏开放。
"""


def _build_user_prompt(groups: list[DuplicationGroup], files_context: dict[str, str]) -> str:
    groups_payload = []
    for group in groups:
        occurrences_payload = [
            {
                "path": str(item.path),
                "start_line": item.line,
                "end_line": item.line + group.lines - 1,
            }
            for item in group.occurrences
        ]
        groups_payload.append(
            {
                "id": group.id,
                "lines": group.lines,
                "tokens": group.tokens,
                "occurrences": occurrences_payload,
            }
        )

    files_context_with_lines = {}
    for path, content in files_context.items():
        lines = content.splitlines()
        numbered_lines = [f"{i+1}|{line}" for i, line in enumerate(lines)]
        files_context_with_lines[path] = "\n".join(numbered_lines)

    payload = {
        "selected_duplication_groups": groups_payload,
        "requirements": {
            "goal": "提取重复代码到公共文件并回填调用",
            "output": "strict_json_only",
            "line_number_info": "文件上下文中每行都带行号前缀（格式: 行号|代码）",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_agent_prompt_from_markdown(markdown_content: str) -> str:
    return (
        "你将收到一份由系统整理的 Markdown，上面是用户在 duplication.xml 中选中的重复片段信息。\n"
        "请严格基于该 Markdown 的信息生成重构方案，并输出严格 JSON（不要 markdown 代码块）。\n\n"
        + markdown_content
    )


def _build_line_ops_prompt(line_ops_markdown: str, feedback: str | None = None) -> str:
    prompt = (
        "下面是从 PMD 和差异分析得到的结构化输入，请据此生成 line-ops JSON。\n"
        "禁止输出解释性 markdown，只返回 JSON。\n\n"
        + line_ops_markdown
    )
    if feedback:
        prompt += (
            "\n\n上一次计划未通过校验，请修复后重新输出完整 JSON。"
            f"\n校验错误: {feedback}\n"
            "请重点确保 insert/cut_paste 目标行不落在任何删除区间内。"
        )
    return prompt


def _strip_fences(text: str) -> str:
    trimmed = text.strip()
    if trimmed.startswith("```"):
        trimmed = trimmed.strip("`")
        if trimmed.startswith("json"):
            trimmed = trimmed[4:].strip()
    return trimmed.strip()


def _call_ollama(route: ModelRoute, config: ModelConfig, user_prompt: str) -> str:
    if not route.base_url:
        raise ValueError("ollama route 缺少 base_url")
    url = route.base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": route.model,
        "stream": False,
        "options": {
            "temperature": config.temperature,
            "num_predict": config.max_tokens,
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "format": "json",
    }
    response = requests.post(url, json=payload, timeout=180)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return str(data.get("message", {}).get("content", "")).strip()


def _call_ollama_with_system(route: ModelRoute, config: ModelConfig, system_prompt: str, user_prompt: str) -> str:
    if not route.base_url:
        raise ValueError("ollama route 缺少 base_url")
    url = route.base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": route.model,
        "stream": False,
        "options": {
            "temperature": config.temperature,
            "num_predict": config.max_tokens,
        },
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": "json",
    }
    response = requests.post(url, json=payload, timeout=180)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return str(data.get("message", {}).get("content", "")).strip()


def _call_vllm(config: ModelConfig, user_prompt: str) -> str:
    raise NotImplementedError()


def _vllm_model_available(route: ModelRoute) -> bool:
    if not route.base_url:
        raise ValueError("vllm route 缺少 base_url")
    url = route.base_url.rstrip("/") + "/v1/models"
    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"

    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    models = payload.get("data", [])
    ids = {str(item.get("id", "")) for item in models}
    return route.model in ids


def _call_vllm(route: ModelRoute, config: ModelConfig, user_prompt: str) -> str:
    if not route.base_url:
        raise ValueError("vllm route 缺少 base_url")
    url = route.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"

    payload = {
        "model": route.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    response = requests.post(url, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    content = data["choices"][0]["message"]["content"]
    return str(content).strip()


def _call_vllm_with_system(route: ModelRoute, config: ModelConfig, system_prompt: str, user_prompt: str) -> str:
    if not route.base_url:
        raise ValueError("vllm route 缺少 base_url")
    url = route.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"

    payload = {
        "model": route.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    response = requests.post(url, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    content = data["choices"][0]["message"]["content"]
    return str(content).strip()


def _call_agent(route: ModelRoute, config: ModelConfig, user_prompt: str) -> str:
    if Anthropic is None:
        raise RuntimeError("未安装 anthropic。请执行: pip install anthropic")

    client = Anthropic(api_key=route.api_key) if route.api_key else Anthropic()
    system_prompt = SYSTEM_PROMPT
    if route.agent_name:
        system_prompt = f"{SYSTEM_PROMPT}\n\n你的角色标识: {route.agent_name}"

    response = client.messages.create(
        model=route.model,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    fragments: list[str] = []
    for item in response.content:
        text = getattr(item, "text", None)
        if text:
            fragments.append(str(text))
    return "\n".join(fragments).strip()


def _call_agent_with_system(route: ModelRoute, config: ModelConfig, system_prompt: str, user_prompt: str) -> str:
    if Anthropic is None:
        raise RuntimeError("未安装 anthropic。请执行: pip install anthropic")

    client = Anthropic(api_key=route.api_key) if route.api_key else Anthropic()
    prompt = system_prompt
    if route.agent_name:
        prompt = f"{system_prompt}\n\n你的角色标识: {route.agent_name}"

    response = client.messages.create(
        model=route.model,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        system=prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    fragments: list[str] = []
    for item in response.content:
        text = getattr(item, "text", None)
        if text:
            fragments.append(str(text))
    return "\n".join(fragments).strip()


def generate_refactor_plan(
    config: ModelConfig,
    groups: list[DuplicationGroup],
    files_context: dict[str, str],
    agent_markdown: str | None = None,
) -> LLMRefactorPlan:
    default_user_prompt = _build_user_prompt(groups, files_context)
    agent_user_prompt = _build_agent_prompt_from_markdown(agent_markdown) if agent_markdown else default_user_prompt
    errors: list[str] = []
    provider_priority = {"agent": 0, "vllm": 1, "ollama": 2}
    ordered_routes = sorted(config.routes, key=lambda item: provider_priority.get(item.provider, 99))

    for route in ordered_routes:
        try:
            print(f"[LLM] trying {route.provider} model={route.model} base_url={route.base_url}")
            if route.provider == "agent":
                system_prompt = _effective_system_prompt(SYSTEM_PROMPT, route)
                user_prompt = agent_user_prompt
            else:
                system_prompt = SYSTEM_PROMPT
                user_prompt = default_user_prompt

            _save_prompt_snapshot(
                mode="replacement",
                route=route,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

            if route.provider == "agent":
                raw = _call_agent(route, config, agent_user_prompt)
            elif route.provider == "vllm":
                if not _vllm_model_available(route):
                    raise RuntimeError(
                        f"vLLM 未找到模型 {route.model}，自动尝试下一个 provider"
                    )
                raw = _call_vllm(route, config, default_user_prompt)
            elif route.provider == "ollama":
                raw = _call_ollama(route, config, default_user_prompt)
            else:
                raise ValueError(f"不支持的 provider: {route.provider}")

            payload = json.loads(_strip_fences(raw))
            print(f"[LLM] success via {route.provider} model={route.model}")
            return LLMRefactorPlan.from_dict(payload)
        except Exception as exc:
            print(f"[LLM] fallback from {route.provider} model={route.model}: {exc}")
            errors.append(f"{route.provider}({route.model}) -> {exc}")

    raise RuntimeError("所有模型路由均失败:\n" + "\n".join(errors))


def plan_to_pretty_json(plan: LLMRefactorPlan) -> str:
    payload = asdict(plan)
    payload["common_file"] = str(plan.common_file)
    payload["replacements"] = [
        {
            "file": str(item.file),
            "start_line": item.start_line,
            "end_line": item.end_line,
            "replacement": item.replacement,
        }
        for item in plan.replacements
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def line_ops_plan_to_pretty_json(plan: LLMLineOpsPlan) -> str:
    payload = {
        "operations": {
            "cut_paste": [
                {
                    "source_file": str(item.source_file),
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "target_file": str(item.target_file),
                    "target_line": item.target_line,
                    "position": item.position,
                }
                for item in plan.cut_paste
            ],
            "delete": [
                {
                    "file": str(item.file),
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                }
                for item in plan.delete
            ],
            "insert": [
                {
                    "file": str(item.file),
                    "line": item.line,
                    "position": item.position,
                    "content": item.content,
                }
                for item in plan.insert
            ],
        },
        "notes": plan.notes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def generate_line_ops_plan(
    config: ModelConfig,
    line_ops_markdown: str,
    feedback: str | None = None,
) -> LLMLineOpsPlan:
    user_prompt = _build_line_ops_prompt(line_ops_markdown, feedback=feedback)
    errors: list[str] = []
    provider_priority = {"agent": 0, "vllm": 1, "ollama": 2}
    ordered_routes = sorted(config.routes, key=lambda item: provider_priority.get(item.provider, 99))

    for route in ordered_routes:
        try:
            print(f"[LLM] trying line-ops via {route.provider} model={route.model} base_url={route.base_url}")
            system_prompt = _effective_system_prompt(LINE_OPS_SYSTEM_PROMPT, route)
            _save_prompt_snapshot(
                mode="line-ops",
                route=route,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if route.provider == "agent":
                raw = _call_agent_with_system(route, config, LINE_OPS_SYSTEM_PROMPT, user_prompt)
            elif route.provider == "vllm":
                if not _vllm_model_available(route):
                    raise RuntimeError(f"vLLM 未找到模型 {route.model}，自动尝试下一个 provider")
                raw = _call_vllm_with_system(route, config, LINE_OPS_SYSTEM_PROMPT, user_prompt)
            elif route.provider == "ollama":
                raw = _call_ollama_with_system(route, config, LINE_OPS_SYSTEM_PROMPT, user_prompt)
            else:
                raise ValueError(f"不支持的 provider: {route.provider}")

            payload = json.loads(_strip_fences(raw))
            print(f"[LLM] success line-ops via {route.provider} model={route.model}")
            return LLMLineOpsPlan.from_dict(payload)
        except Exception as exc:
            print(f"[LLM] fallback line-ops from {route.provider} model={route.model}: {exc}")
            errors.append(f"{route.provider}({route.model}) -> {exc}")

    raise RuntimeError("所有模型路由均失败(line-ops):\n" + "\n".join(errors))
