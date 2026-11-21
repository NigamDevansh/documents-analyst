# Project Core Routing & Execution

## Context & Agent Delegation Rules
- DO NOT perform massive codebase exploration, multi-file scans, or heavy regex searches in the main window.
- If a task requires scanning more than 3 files or deep debugging, DELEGATE immediately to a specialized subagent.
- Rely on progressive disclosure: Use subagents to process raw text/logs, returning only a 1-paragraph architecture summary to this main thread.

## Active Subagents Directory
When encountering the following scenarios, invoke the subagent via natural language (e.g., "Use the `code-explorer` subagent to..."):

1. **`code-explorer`**: Use for raw file searching, tracing imports, mapping out architectures, or broad repository scanning.

## Workflow Discipline
1. Always demand a concise structural plan before approving code writes.
2. Run `/clear` between unrelated features to completely purge context memory.
3. Proactively trigger `/compact` manually when the token usage gauge reaches ~60%.
