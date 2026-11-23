---
name: workspace-temp-cleaner
description: 自检并安全清理当前项目工作区内的无用临时文件与一次性辅助脚本。只读扫描生成报告与确认令牌后，必须立即主动弹出交互询问让用户选择清理方式，确认后才把候选文件移入系统回收站。当用户要求"清理临时文件""清理生成的脚本""工作区自检清理""清理无用文件/垃圾文件""清理 tmp/temp 文件"或在工作区收尾时清理中间产物时，应使用本 skill。绝不永久删除，绝不操作个人顶层目录。
agent_created: true
---

# Workspace Temp Cleaner

## Overview

提供一种**两阶段、可审计、可恢复**的工作区清理流程：先自检（只读扫描 + 规则判定 + 报告 + 令牌），再由用户确认，最后才把文件移入系统回收站。核心脚本把安全约束固化在代码里，避免任何"顺手删掉"的误操作。

## When to Use

- 用户要求清理当前项目工作区里的临时文件、`tmp`/`temp` 文件、备份文件（`.bak`/`.swp` 等）。
- 用户要求清理任务过程中生成的一次性辅助脚本（`tmp_*.py`、`scratch_*.sh` 等）。
- 用户说"自检一下有没有垃圾文件""收尾清理一下""把没用的临时脚本清掉"等。
- 一次复杂任务结束、需要清理中间产物时。

不适用：用户指定的是个人顶层目录（桌面、下载、文档、主目录、磁盘根目录）而不是具体项目工作区时，拒绝执行并说明原因。

## Hard Safety Rules（不可绕过）

1. **两阶段强制分离**：`scan` 只读，绝不修改文件系统；`clean` 必须携带 scan 生成的令牌。绝不跳过扫描直接删除。
2. **扫描后主动询问是硬门槛**：每次 `scan` 完成后，必须**立即主动发起交互询问**，把候选清单和处理选项摆到用户面前，得到明确回复后才可调用 `clean`。绝不跳过询问、绝不只写一句"是否确认"了事、绝不代替用户判断某个文件"看起来没用"。
3. **仅限工作区**：`--root` 必须是一个具体的项目工作区子目录。脚本内置对个人顶层目录和磁盘根目录的拒绝逻辑，不得用 `--root` 绕过。
4. **仅回收站**：任何情况下都不得永久删除（`rm`/`del /F`/`shutil.rmtree`）。后端不可用时脚本会中止，不得改用其他方式删除。
5. **保护目录不可触碰**：`.git`、`.workbuddy`、`.codebuddy`、`node_modules`、虚拟环境、构建目录等由脚本自动跳过，不要手动去删其中的文件。
6. **符号链接不跟随**：不删除符号链接本身，也不通过符号链接逃逸出工作区。
7. **令牌失效即中止**：若 `clean` 返回令牌不匹配（退出码 2），说明扫描后文件集合变化，必须重新 `scan` 并向用户展示新报告。

## Standard Workflow

### Step 1 — 确定工作区根目录

从用户描述或当前工作目录确定目标工作区，得到绝对路径 `ROOT`。若用户没有明确指定，使用当前会话的工作目录（cwd），并在报告中写明。

若 `ROOT` 是个人顶层目录（如 `C:\Users\<用户>`、`Desktop`、`Downloads`、`Documents`）或磁盘根目录，停止执行，提示用户指定具体项目目录。

### Step 2 — 执行只读扫描

```bash
python <SKILL_DIR>/scripts/clean_temp.py scan --root "<ROOT>"
```

需要完整结构化数据（例如候选很多、要程序化处理）时加 `--json`：

```bash
python <SKILL_DIR>/scripts/clean_temp.py scan --root "<ROOT>" --json
```

其中 `<SKILL_DIR>` 是本 skill 的目录。记下输出末尾的 `CONFIRM_TOKEN`。

### Step 3 — 扫描后主动询问用户（强制）

扫描一完成就**主动**发起交互询问，不要等用户开口，也不要只在正文里写一句"请确认"。

先把扫描报告原文（或 JSON 整理后的清单）作为文字输出，包含：文件路径、命中规则、大小、修改时间。随后立即调用交互询问工具（如 `AskUserQuestion`）弹出确认卡片：

- 问题写明："自检发现 N 个可清理文件（清单见上），要如何处理？确认后会移入系统回收站，可恢复。"
- 提供以下选项：
  1. **全部清理（推荐）** —— 将所有候选文件移入回收站
  2. **保留部分文件** —— 用户可在输入框中列出要保留的文件名或序号
  3. **取消，不清理** —— 保持现状，不做任何改动

根据用户的选择分支处理：

- **全部清理**：直接进入 Step 4，使用当前 `CONFIRM_TOKEN`。
- **保留部分文件**：把用户要保留的文件作为排除项，重新扫描以生成匹配的新令牌：
  ```bash
  python <SKILL_DIR>/scripts/clean_temp.py scan --root "<ROOT>" --exclude "tmp_a.py,subdir/scratch_b.sh" --json
  ```
  然后用**新令牌和相同的 `--exclude` 值**进入 Step 4。用户已在询问中明确表达意图，无需二次弹窗；但仍需把实际清理的清单在结果中列清楚。
- **取消，不清理**：结束流程，不调用 `clean`。

候选数为 0 时，跳过询问，直接告知用户"自检未发现可清理文件"，流程结束。

### Step 4 — 执行清理

仅在得到用户明确确认后执行：

```bash
python <SKILL_DIR>/scripts/clean_temp.py clean --root "<ROOT>" --token "<CONFIRM_TOKEN>"
```

建议先用 `--dry-run` 复核令牌有效性与文件数量（可选）：

```bash
python <SKILL_DIR>/scripts/clean_temp.py clean --root "<ROOT>" --token "<CONFIRM_TOKEN>" --dry-run
```

### Step 5 — 汇报结果

向用户报告成功移入回收站的数量、失败项（若有）以及回收站可恢复的说明。

## Detection Rules

| 类别 | 判定条件 |
|---|---|
| 临时命名文件 | 文件名以 `~` 开头/结尾；后缀为 `.tmp` `.temp` `.bak` `.backup` `.swp` `.swo` `.orig` `.rej` `.part` `.crdownload`；stem 为 `tmp`/`temp`/`scratch` 或以 `tmp_`/`temp_`/`_tmp`/`_temp` 等开头；含 "conflicted copy" |
| 一次性辅助脚本 | 扩展名为 `.py` `.sh` `.js` `.ts` `.ps1` `.bat` `.cmd` `.rb` `.php` 等，且 stem 含 `tmp` `temp` `scratch` `oneoff` `one_off` `throwaway` `probe` `quicktest` `quick_test` |

规则刻意保守：报告中的每一项都必须由用户复核。完整判定逻辑见 `scripts/clean_temp.py` 顶部的策略常量。

## Script Reference

`scripts/clean_temp.py`

- `scan [--root PATH] [--json] [--exclude "a.py,sub/x.tmp"]` — 只读扫描，输出报告与令牌；退出码 0 表示成功。`--exclude` 用于排除用户希望保留的文件。
- `clean --token TOKEN [--root PATH] [--json] [--dry-run] [--exclude "..."]` — 校验令牌后移入回收站，`--exclude` 必须与 scan 时完全一致。
  - 退出码 0：成功；1：参数/目录校验失败；2：令牌不匹配（未删除任何文件）；3：至少一个文件移入回收站失败。
- 脚本不依赖第三方库，Windows / macOS / Linux 均使用系统回收站机制。
- 不指定 `--root` 时默认当前目录；建议每次都显式传入绝对路径。

## Resources

### scripts/

- `clean_temp.py` — 核心脚本，实现扫描、令牌生成/校验、回收站后端与安全边界检查。直接执行即可，无需载入上下文；仅在需要调整判定规则时阅读。
