# Documentation diagrams

The documentation uses rendered diagrams instead of ASCII art. Sources live
here as Graphviz `.dot` files; the generated PNGs are committed under
`docs/assets/diagrams/` so MkDocs builds do not need Graphviz installed.

| Source | Output |
| --- | --- |
| `architecture.dot` / `architecture.zh.dot` | `docs/assets/diagrams/architecture*.png` |
| `run-pipeline.dot` / `run-pipeline.zh.dot` | `docs/assets/diagrams/run-pipeline*.png` |
| `request-flow.dot` / `request-flow.zh.dot` | `docs/assets/diagrams/request-flow*.png` |

Regenerate everything after editing a source:

```bash
bash dev/diagrams/render.sh
```

The renderer uses `dot` (Graphviz) and the `Noto Sans CJK SC` font for the
Chinese labels.
