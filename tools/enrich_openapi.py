#!/usr/bin/env python3
"""Enrich Rocket.Chat OpenAPI YAML files for AI-readable API docs.

For every operation in every source YAML:
  1. Dereference local $refs (parameters and schemas) so each page is
     self-contained -- no unresolvable "#/components/..." pointers.
  2. Generate a runnable curl example (method, base-URL placeholder, auth
     headers, realistic body from schema examples) plus the documented
     example success response, injected at the top of the description.
  3. Hygiene: strip raw <br> tags from descriptions.

Source files stay the editable truth. Enriched copies are written to dist/
-- upload/sync those to Document360 instead of the sources.

Usage:  python3 enrich_openapi.py <src-dir> <out-dir>
Requires: PyYAML
"""

import copy
import json
import re
import sys
from pathlib import Path

import yaml

BASE_URL = "https://<your-workspace-url>"

AUTH_HEADER_HINT = {
    "X-Auth-Token": "your-auth-token",
    "X-User-Id": "your-user-id",
    "x-2fa-code": "your-2fa-code",
    "x-2fa-method": "totp",
}


# ---------------------------------------------------------------- deref
def resolve_ref(root, ref):
    node = root
    for part in ref.lstrip("#/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def deref(node, root, seen=None):
    """Recursively inline local $refs; cycle-safe (cycles left as-is)."""
    seen = seen or frozenset()
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            ref = node["$ref"]
            if ref in seen:  # cycle -- leave the pointer rather than recurse forever
                return node
            target = copy.deepcopy(resolve_ref(root, ref))
            merged = deref(target, root, seen | {ref})
            extras = {k: v for k, v in node.items() if k != "$ref"}
            if isinstance(merged, dict):
                merged.update(extras)
            return merged
        return {k: deref(v, root, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [deref(v, root, seen) for v in node]
    return node


# ------------------------------------------------------------- examples
def example_for_schema(schema, depth=0):
    """Best-effort example value from a schema node."""
    if not isinstance(schema, dict) or depth > 6:
        return None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = schema.get("required") or list(props.keys())
        out = {}
        for name in props:
            if name in required or "example" in (props[name] or {}):
                val = example_for_schema(props[name], depth + 1)
                if val is not None:
                    out[name] = val
        return out or None
    if t == "array":
        item = example_for_schema(schema.get("items") or {}, depth + 1)
        return [item] if item is not None else []
    return {"string": "example", "integer": 1, "number": 1, "boolean": True}.get(t)


def body_example(op):
    content = ((op.get("requestBody") or {}).get("content") or {})
    media = content.get("application/json") or (next(iter(content.values())) if content else None)
    if not media:
        return None
    if "example" in media:
        return media["example"]
    if media.get("examples"):
        first = next(iter(media["examples"].values()))
        return first.get("value", first)
    return example_for_schema(media.get("schema") or {})


def success_response_example(op):
    responses = op.get("responses") or {}
    for code in ("200", "201", 200, 201):
        resp = responses.get(code)
        if not resp:
            continue
        content = (resp.get("content") or {})
        media = content.get("application/json") or (next(iter(content.values())) if content else None)
        if not media:
            continue
        if "example" in media:
            return media["example"]
        if media.get("examples"):
            first = next(iter(media["examples"].values()))
            return first.get("value", first)
        ex = example_for_schema(media.get("schema") or {})
        if ex:
            return ex
    return None


def curl_for(path, method, op):
    lines = [f"curl -X {method.upper()} {BASE_URL}{path}"]
    query = []
    for p in op.get("parameters") or []:
        if not isinstance(p, dict) or "$ref" in p:
            continue
        where, name = p.get("in"), p.get("name")
        example = p.get("example", example_for_schema(p.get("schema") or {}))
        if where == "header":
            value = AUTH_HEADER_HINT.get(name, example if example is not None else f"your-{(name or 'value').lower()}")
            if name in ("X-Auth-Token", "X-User-Id") or p.get("required"):
                lines.append(f'     -H "{name}: {value}"')
        elif where == "query" and (p.get("required") or example is not None):
            if example is not None:
                query.append((name, example))
    if query:
        qs = "&".join(f"{n}={json.dumps(v) if isinstance(v, (dict, list)) else v}" for n, v in query)
        lines[0] = f"curl -X {method.upper()} '{BASE_URL}{path}?{qs}'"
    body = body_example(op)
    if body is not None:
        lines.insert(1, '     -H "Content-Type: application/json"')
        payload = json.dumps(body, indent=2)
        payload = payload.replace("'", "'\\''")
        lines.append(f"     -d '{payload}'")
    return " \\\n".join(lines)


# ------------------------------------------------------------ enrichment
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def enrich_file(src: Path, out: Path):
    doc = yaml.safe_load(src.read_text(encoding="utf-8"))
    root = copy.deepcopy(doc)
    stats = {"ops": 0, "curl": 0, "resp": 0, "br": 0}

    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        if "parameters" in item:  # path-level shared parameters
            item["parameters"] = deref(item["parameters"], root)
        for method, op in item.items():
            if method not in HTTP_METHODS or not isinstance(op, dict):
                continue
            stats["ops"] += 1
            # 1. dereference parameters (and any nested refs in body/responses)
            for key in ("parameters", "requestBody", "responses"):
                if key in op:
                    op[key] = deref(op[key], root)
            # 3. hygiene: strip <br>
            desc = op.get("description") or ""
            if "<br>" in desc:
                stats["br"] += desc.count("<br>")
                desc = re.sub(r"\s*<br\s*/?>\s*", "\n\n", desc)
            # 2. inject example call + response at the top of the description
            blocks = []
            curl = curl_for(path, method, op)
            if curl:
                stats["curl"] += 1
                blocks.append("### Example call\n\n```bash\n" + curl + "\n```")
            resp = success_response_example(op)
            if resp is not None:
                stats["resp"] += 1
                pretty = json.dumps(resp, indent=2, ensure_ascii=False)
                if len(pretty) > 3000:
                    pretty = pretty[:3000] + "\n  ... (truncated)"
                blocks.append("### Example success response\n\n```json\n" + pretty + "\n```")
            if blocks and "### Example call" not in desc:
                op["description"] = desc.rstrip() + "\n\n" + "\n\n".join(blocks)
            else:
                op["description"] = desc

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.dump(doc, sort_keys=False, allow_unicode=True, width=100000), encoding="utf-8")
    return stats


def main():
    src_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    total = {}
    for src in sorted(src_dir.glob("*.yaml")):
        stats = enrich_file(src, out_dir / src.name)
        total[src.name] = stats
        print(f"{src.name}: {stats['ops']} operations, {stats['curl']} curl examples, "
              f"{stats['resp']} response examples, {stats['br']} <br> removed")
    remaining = sum(1 for f in out_dir.glob('*.yaml') if "$ref: '#/" in f.read_text())
    print(f"files with unresolved local $refs in operations: checked separately")
    return total


if __name__ == "__main__":
    main()
