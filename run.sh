#!/bin/bash
# launchd 入口：采集 + 抽取 + 导出 + 推送。claude -p 走订阅额度，需要 ~/.local/bin 在 PATH 里。
export PATH="/Users/beckywang/.local/bin:/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/local/bin:/usr/bin:/bin"
export LANG=en_US.UTF-8
cd "/Users/beckywang/Projects/ai-capital-flows" || exit 1
python3 src/run.py "$@" 2>&1 | grep -v "Permission allow rule" >> logs/run.log
