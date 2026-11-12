#!/bin/sh

set -eu

project_root=$(CDPATH= cd "$(dirname "$0")/../../../.." && pwd)
cd "$project_root"

status=0

require_file() {
    if [ ! -f "$1" ] || [ ! -s "$1" ]; then
        printf '缺少文件或文件为空：%s\n' "$1" >&2
        status=1
    fi
}

require_file AGENTS.md
require_file .agents/skills/check-project-structure/references/task-delivery.md

for skill_dir in .agents/skills/*; do
    if [ -d "$skill_dir" ]; then
        require_file "$skill_dir/SKILL.md"
    fi
done

if [ "$status" -eq 0 ]; then
    printf '项目结构检查通过。\n'
fi

exit "$status"
