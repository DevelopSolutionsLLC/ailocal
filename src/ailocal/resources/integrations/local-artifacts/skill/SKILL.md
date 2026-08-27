---
name: local-artifact
description: >-
  Create, update or preview a rendered artifact - an architecture or system
  diagram, flowchart, dashboard, chart, report or interactive page. Use whenever
  the user asks to visualize, diagram, show, display, preview or present
  something, or to update an existing artifact.
---

# Local artifacts

Publish with the tool named exactly `mcp__artifact__publish`.
The `mcp__artifact__` prefix is part of the name.

## Choose the format

| The user wants | `format` | Send |
|---|---|---|
| architecture, system, deployment, request or data-flow diagram | `architecture` | JSON: `nodes`, `groups`, `edges` |
| flowchart, sequence, state, class, ER diagram | `mermaid` | Mermaid source |
| dashboard, interactive page, custom layout | `html` | one self-contained document |
| report, notes, prose | `markdown` | Markdown |

## architecture

Describe meaning only. Positions, spacing and edge routing are computed for you.

```json
{"title": "Request routing",
 "groups": [{"id": "ailocal", "label": "ailocal"}],
 "nodes": [{"id": "cc", "label": "Claude Code", "kind": "client"},
           {"id": "ll", "label": "LiteLLM", "kind": "service",
            "group": "ailocal", "subtitle": "127.0.0.1:4000"}],
 "edges": [{"from": "cc", "to": "ll", "kind": "request", "label": "/v1/messages"}]}
```

node `kind`: `client` `service` `router` `runtime` `model` `database` `external` `tool`
edge `kind`: `request` `inference` `tool` `data` `dependency`

Always set both — they colour the diagram and keep separate paths
distinguishable.

## Rules

- **Never** hand-write SVG coordinates or path data.
- **Never** reference remote scripts, styles, fonts or images. Artifacts have no
  network. Everything needed is supplied locally.
- To update, publish again with the same `artifact_id`.
- The source file is saved under `.artifacts/` automatically — you do not need
  to write it first.

## When not to use it

If the user asks for a file kept in the repository — "write this to README.md",
"create docs/architecture.md", "save this JSON to config/" — write that file
normally. Only publish when they want to *look* at something.
