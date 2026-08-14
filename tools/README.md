# tools/ — API docs enrichment pipeline (DOCS-1537 / DOCS-1538 / DOCS-1539)

`enrich_openapi.py` turns the source OpenAPI YAML files in the repo root into
AI-readable, self-contained specs for Document360:

1. **Dereferences all local `$ref`s** (operation- and path-level) so every
   endpoint page states its auth headers and shared parameters inline — no
   unresolvable `#/components/...` pointers in the published pages.
2. **Generates a runnable curl example per endpoint** (method, base-URL
   placeholder, auth headers, realistic body built from schema examples) plus
   the documented example success response, injected at the top of each
   operation description.
3. **Hygiene**: strips raw `<br>` tags from descriptions.

Source YAML files in the repo root remain the editable truth. Run:

    pip install pyyaml
    python3 tools/enrich_openapi.py . dist

then upload/sync the files in `dist/` to Document360 (instead of the root
files). Re-run after every source edit — e.g. as a pre-sync step or CI job.

Idempotent: operations whose description already contains "### Example call"
are not re-injected.
