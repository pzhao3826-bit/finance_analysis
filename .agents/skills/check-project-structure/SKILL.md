---
name: check-project-structure
description: 检查 Codex 项目结构，适用于核对 AGENTS.md、项目 Skills 及其附带的脚本和参考资料。
---

# 检查项目结构

1. 从项目根目录运行 `sh .agents/skills/check-project-structure/scripts/check-project.sh`，记录缺项和退出状态。
2. 如果任务涉及改动过的入口，核对 `AGENTS.md` 和技能引用的路径。对于改动过的 Skill，检查 `SKILL.md` 的 `name`、`description` 和适用场景。
3. 反馈检查结果和需要修正的路径。用户要求修复时，在任务范围内修复并重跑结构检查。
