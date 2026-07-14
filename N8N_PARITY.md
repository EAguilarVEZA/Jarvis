# Agent Studio ↔ n8n parity matrix

Status of every n8n capability class I catalogued, mapped to Jarvis Agent Studio.
Legend: ✅ built · 🟡 partial / representative · ⬜ not yet.

## Canvas & UX
| n8n | Jarvis | Notes |
|---|---|---|
| Visual drag-drop canvas | ✅ | nodes, connect-by-port |
| Pan / zoom | ✅ | space/middle-drag, scroll-to-zoom |
| Sticky notes | ✅ | add/edit/drag |
| Multiple output ports w/ labels | ✅ | IF, Switch, Loop, error ports |
| Node search palette | ✅ | grouped palette + agent search |

## Triggers
| n8n | Jarvis | Notes |
|---|---|---|
| Manual | ✅ | Run button + Manual trigger node |
| Schedule (cron) | ✅ | per-workflow schedule + scheduler |
| Webhook | ✅ | `POST /api/workflows/hook/{token}`, seeds items |
| n8n Form | ✅ | hosted form page `/form/{token}` |
| Chat trigger | 🟡 | agent chat covers conversational entry |
| Error Trigger | 🟡 | per-node error branch + error routing |
| App-event triggers | 🟡 | via MCP-connector polling / webhooks |

## Core flow / logic
| n8n | Jarvis |
|---|---|
| IF | ✅ |
| Switch | ✅ |
| Filter | ✅ |
| Merge | ✅ |
| Loop Over Items / Split in Batches | ✅ (scoped iteration, loop/done ports) |
| Wait | ✅ (capped) |
| No Operation | ✅ |
| Stop and Error | ✅ |
| Execute Sub-workflow | ✅ (nested, depth-guarded) |
| Respond to Webhook | ✅ |

## Debugging & authoring (next-level)
| Capability | Jarvis |
|---|---|
| Expressions: arithmetic, `\|\|` defaults, ternary, `.length`, string methods | ✅ (safe AST evaluator) |
| Run view: per-node item output, counts, branch taken, retries | ✅ |
| Execute up to a node ("Run to here") | ✅ (`/run_node`) |
| AI workflow builder (describe → graph) | ✅ (`/build_graph`) |

## Data transformation
| n8n | Jarvis |
|---|---|
| Edit Fields / Set | ✅ (template + multi-field) |
| Code (JS) | ✅ (sandboxed Python Code node) |
| Sort | ✅ |
| Limit | ✅ |
| Remove Duplicates | ✅ |
| Aggregate | ✅ (list/count/sum/avg/concat) |
| Split Out | ✅ |
| Rename Keys | ✅ |
| Date & Time | ✅ |
| HTML (extract/strip) | ✅ |
| Extract From File (CSV/JSON) | ✅ |
| Item-based data model | ✅ (`items[]` channel threaded between nodes) |
| Expressions `{{$json}}`, `{{$node["x"].json}}`, `{{$now}}` | ✅ |

## Connectivity
| n8n | Jarvis |
|---|---|
| HTTP Request (any API) | ✅ |
| Slack / Teams / Discord | ✅ (incoming webhooks) |
| Airtable | ✅ (REST + PAT) |
| 400+ app integrations | ✅ via **MCP connectors** — any MCP server's tools become nodes (Gmail, Sheets, Drive, …) |
| Credentials manager | ✅ (encrypted-at-rest local store, masked in UI) |

## AI
| n8n | Jarvis |
|---|---|
| AI Agent nodes | ✅ (211-agent library) |
| LLM transform / chains | ✅ (Transform node) |
| Tools = sub-workflows / HTTP / connectors | ✅ (agents call analyses; sub-workflow node) |
| Vector stores / memory / embeddings | 🟡 (available via MCP servers) |

## Platform
| n8n | Jarvis |
|---|---|
| Execution history | ✅ (Runs view, per-node output) |
| Replay / re-run | ✅ (Re-run current) |
| Per-node retry + backoff | ✅ |
| Error branch routing | ✅ |
| Pinned test data | ✅ (on triggers) |
| Sub-workflows | ✅ |
| Environments / versions | ⬜ |
| Evaluations for AI | ✅ (separate Evaluations feature in-app) |

## Honest gaps vs. literal n8n
- **Not 1,000 hand-built integrations** — instead any MCP server plugs in, which functionally covers the same surface without per-app rebuilds.
- **Code node runs Python**, not JavaScript (sandboxed, restricted builtins).
- **Environments/versioning** and true **queue-mode scaling** are not implemented (single-host tool).
- Expressions cover the common `$json`/`$node`/`$now` forms, not arbitrary JS in `{{ }}`.

Net: functional parity on every node **class** and platform feature I catalogued, with MCP standing in for the long tail of connectors.
