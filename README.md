# CPD + LLM 代码去重工具

这个项目支持：
1. 用 PMD CPD 扫描 C/C++ 仓库重复代码（Windows/Linux）。
2. 解析 XML 报告并列出重复组。
3. 用户选择重复组（命令行交互或参数）。
4. 调用 Claude Agent（Python SDK）、vLLM 或 Ollama 生成重构计划。
5. 应用提取公共代码修改，并可演示生成 commit。

## 1. 安装

```bash
pip install -r requirements.txt
```

## 2. 配置模型

复制 `deduper.config.example.json` 为 `deduper.config.json` 并修改。

### 路由与优先级（当前实现）

- 支持 provider：`agent`（Claude SDK）、`vllm`、`ollama`
- 运行时尝试顺序固定为：`agent -> vllm -> ollama`
- 即使你在 `routes` 里写了不同顺序，也会按上面的优先级执行

### Claude Agent 示例（推荐）

```json
{
  "workspace": ".",
  "model": {
    "routes": [
      {
        "provider": "agent",
        "model": "claude-3-5-sonnet-20241022",
        "base_url": null,
        "api_key": null,
        "agent_name": "Senior Refactor Agent"
      },
      {
        "provider": "vllm",
        "model": "Qwen/Qwen3-Coder-32B-Instruct",
        "base_url": "http://127.0.0.1:8000",
        "api_key": "EMPTY"
      },
      {
        "provider": "ollama",
        "model": "qwen3-coder:480b-cloud",
        "base_url": "http://localhost:11434",
        "api_key": null
      }
    ],
    "temperature": 0.1,
    "max_tokens": 4096
  }
}
```

说明：
- `agent` 走 Anthropic Python SDK（`anthropic`）
- `api_key` 可为空；为空时会使用 SDK 的默认认证链路
- 如果你生产机已配置好 Claude 凭据，工具侧通常不需要额外配置
- 用户选中重复组后，工具会自动生成一个仅包含所选片段信息的 Markdown（默认 `artifacts/claude_refactor_input.md`），供 Claude SDK 读取

### Ollama 示例

```json
{
  "workspace": ".",
  "model": {
    "routes": [
      {
        "provider": "vllm",
        "model": "Qwen/Qwen3-Coder-32B-Instruct",
        "base_url": "http://127.0.0.1:8000",
        "api_key": "EMPTY"
      },
      {
        "provider": "ollama",
        "model": "qwen3-coder:480b-cloud",
        "base_url": "http://localhost:11434",
        "api_key": null
      }
    ],
    "temperature": 0.1,
    "max_tokens": 4096
  }
}
```

说明：`routes` 用于配置候选路由；实际执行时会按 `agent -> vllm -> ollama` 尝试并自动回退。

### vLLM 示例

```json
{
  "workspace": ".",
  "model": {
    "provider": "vllm",
    "model": "Qwen/Qwen3-Coder-32B-Instruct",
    "base_url": "http://127.0.0.1:8000",
    "api_key": "EMPTY",
    "temperature": 0.1,
    "max_tokens": 4096
  }
}
```

## 3. 跨平台 CPD 扫描

优先使用 Python 扫描器（跨平台）：

```bash
python scan_c_duplication.py <repo_path> --out-dir artifacts --min-tokens 40
```

可按需关闭忽略项（更容易命中示例重复）：

```bash
python scan_c_duplication.py <repo_path> --no-ignore-identifiers --no-ignore-literals
```

扫描器会先检查 Java（PMD 依赖）。默认开启自动安装：
- Windows：通过 `winget` 安装 `EclipseAdoptium.Temurin.17.JDK`
- Linux：通过系统包管理器安装 OpenJDK 17

可关闭自动安装：

```bash
python scan_c_duplication.py <repo_path> --no-auto-install-java
```

可选包装脚本：
- Linux/macOS: `./scan_c_duplication.sh <repo_path>`
- Windows PowerShell: `./scan_c_duplication.ps1 -Repo <repo_path>`

如果 PMD 不在 PATH：
- 传参 `--pmd <pmd_or_pmd.bat_path>`
- 或设置环境变量 `PMD_BIN`

默认也会自动安装 PMD 到当前目录 `.tools` 下（可关闭）：

```bash
python scan_c_duplication.py <repo_path> --no-auto-install-pmd
```

## 4. 去重流程

### 4.0 一体化入口：只扫描 / 全流程

只跑 PMD 生成 XML：

```bash
python main.py workflow --repo <repo_path> --mode scan-only
```

扫描完成后会进入重复组表格预览，按 `重复数 × 重复行数` 倒序，可翻页查看。

在交互表格中，可直接输入重复组 ID（例如 `1` 或 `1,3`）进入大模型重构流程，不再必须输入 `s 1,3`。
交互表格默认先显示“完全雷同（exact）”结果，输入 `f` 可在 `exact/all` 间切换，先处理简单的完全重复场景。
`workflow --mode scan-only` 和 `list --preview` 下也支持这一点；只要提供可用配置文件，就能从预览页直接进入重构。
如果未提供 `--config`，工具会按如下顺序找配置：
1) `deduper.config.json`
2) `demo_assets/deduper.demo.config.json`
3) `deduper.config.example.json`

从扫描直接进入完整流程：

```bash
python main.py workflow --repo <repo_path> --mode full --groups 1 --config deduper.config.json --apply
```

说明：
- `scan-only`：只生成 PMD XML 报告，不调用大模型
- `full`：扫描完成后继续执行重复组选择、LLM 重构、建分支、编译校验与应用
- `full` 成功后会自动再次扫描，并回到选择列表；同一次成功会话中只在第一轮拉分支，后续轮次复用当前工作分支

> 说明：PMD 7 XML 带命名空间，工具已按命名空间统计 `Duplicate blocks`，与 XML/列表展示保持一致。

### 4.1 列出重复组

```bash
python main.py list --xml artifacts/.../duplication.xml
```

### 4.2 用户选择重复组并生成/应用

交互式选择：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --interactive
```

进入表格后可直接输入 ID 数字开始处理，例如 `1` 或 `1,3`。

在生成重构计划前，工具会先输出一个给 Claude 使用的片段 Markdown（默认路径：`artifacts/claude_refactor_input.md`）。
可用 `--out-claude-markdown` 指定输出路径。

指定组：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1,3,5
```

真正落盘：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1 --apply
```

### 4.4 可切换模式：`replacement` / `line-ops`

默认是 `replacement`（整段替换计划）。

如果希望大模型只基于 PMD 行号与差异摘要输出结构化行操作 JSON，可使用 `line-ops`：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1 --plan-mode line-ops
```

`line-ops` 输出 JSON 包含三类操作：
- `cut_paste`：剪切粘贴（只含行号 + 目标行）
- `delete`：删除行区间
- `insert`：按行插入内容

真正应用：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1 --plan-mode line-ops --apply
```

或使用固定脚本应用已有 `line-ops` 计划：

```bash
python apply_line_ops_plan.py --plan artifacts/refactor_plan.json --config deduper.config.json --apply
```

自定义 Claude 输入 Markdown 输出路径：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1 --out-claude-markdown artifacts/my_selected_groups.md
```

默认行为：执行 `--apply` 前，工具会先在当前 Git 提交上创建并切换到一个新分支，再写入修改。

可指定分支名：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1 --apply --git-branch-name deduper/my-change
```

如需关闭自动建分支：

```bash
python main.py refactor --xml artifacts/.../duplication.xml --groups 1 --apply --no-git-branch
```

### 4.3 使用已有计划应用

```bash
python main.py apply-plan --plan artifacts/refactor_plan.json --apply
```

## 5. 一键 Demo（从扫描到 commit）

仓库自带 `demo_c` 重复 C 代码示例，以及离线重构计划。

离线模式（不依赖在线模型）：

```bash
python scripts/run_demo.py --mode offline
```

说明：若本机未安装 PMD，`offline` 模式会自动使用仓库内预置的 `demo_assets/duplication.demo.xml` 继续跑通。

LLM 模式（调用你在配置里指定的模型）：

```bash
python scripts/run_demo.py --mode llm
```

运行后会在 `demo_c` 下创建/更新 git 提交：
- baseline: `chore: baseline duplicated C demo`
- 去重后: `refactor: extract duplicated normalization logic`

## 6. 目录说明

- `scan_c_duplication.py`: 跨平台 CPD 扫描器
- `scan_c_duplication.sh`: Linux/macOS 包装脚本
- `scan_c_duplication.ps1`: Windows PowerShell 包装脚本
- `apply_line_ops_plan.py`: 固定执行脚本，读取 line-ops JSON 并实际修改文件
- `artifacts/claude_refactor_input.md`: 用户选中重复组后自动生成，供 Claude SDK 读取的输入文件
- `demo_c/`: C 重复代码示例项目
- `demo_assets/refactor_plan_demo.json`: 离线 demo 重构计划
- `scripts/run_demo.py`: 端到端 demo 执行脚本

## 7. 注意事项

- `workspace` 必须对齐 CPD 报告中的文件路径根目录。
- 建议先不加 `--apply` 做 dry-run，再正式落盘。
- 工具会检查写入路径是否越界，避免改写工作区外文件。

## 8. 用户操作流程指导（Claude 优先，自动回退）

1) 准备模型服务
- Claude Agent：确保环境可用 Anthropic SDK 认证（可选显式配置 `api_key`）
- 可选：启动 vLLM OpenAI 兼容服务（默认示例地址 `http://127.0.0.1:8000`）
- 启动 Ollama 服务，并确保有可用模型（例如 `qwen3-coder:480b-cloud`）

2) 配置路由优先级
- 在配置文件中设置 `model.routes`
- 工具会按 `agent -> vllm -> ollama` 尝试
- 上游不可用时会自动回退到下一个 provider

3) 扫描重复代码（CPD）

```bash
python scan_c_duplication.py demo_c --out-dir artifacts --min-tokens 10 --no-ignore-identifiers --no-ignore-literals
```

4) 查看重复组

```bash
python main.py list --xml artifacts/<latest>/duplication.xml
```

5) 执行 LLM 去重并应用修改

```bash
python main.py refactor --xml artifacts/<latest>/duplication.xml --groups 1 --config demo_assets/deduper.demo.config.json --apply
```

6) 提交改动

```bash
git -C demo_c add .
git -C demo_c commit -m "refactor: extract duplicated normalization logic"
```

7) 一键跑通（含扫描、去重、提交）

```bash
python scripts/run_demo.py --mode llm
```

运行输出中会显示路由日志：先尝试 Claude Agent，失败后自动回退到 vLLM / Ollama。
