# Ten Things That Broke Before My OpenSearch LLM Dashboard Worked

**Stack**: OpenSearch 2.17.1 · Data Prepper 2.10.1 · OpenSearch Dashboards 2.17.1 · OpenLLMetry

---

I built an LLM cost observability stack for a demo at an AWS MeetUp. The goal was simple on paper: instrument Flask LLM calls with OpenLLMetry, pipe the OTel spans through Data Prepper into OpenSearch, and display a cost-by-model dashboard in OpenSearch Dashboards.

The pipeline itself worked fine. Spans were flowing. OpenSearch had data. The dashboard imported without error. And yet every single cost panel showed either zero or a red error badge reading *"Saved field `span.attributes.gen_ai@cost@total_usd` is invalid for use with the Sum aggregation."*

What followed was a full debugging session through eight separate gotchas, most of which were not documented anywhere I could find. This is the account of what actually happened, in the order it actually happened.

---

## The Architecture

```
Flask + OpenLLMetry
  → OTel Collector
    → Data Prepper
      → OpenSearch
        → OpenSearch Dashboards
```

OpenLLMetry decorators (`@workflow`, `@task`) instrument LLM calls automatically. A custom `CostEnrichingSpanExporter` injects `gen_ai.cost.*` attributes by looking up token prices from the LiteLLM pricing database before the span leaves the process. Everything lands in OpenSearch, where a 9-panel dashboard shows total cost, calls, tokens, breakdown by model and provider, and a ranked table of most expensive calls.

Simple enough concept. Here is what it took to actually get it working.

---

## 1. Data Prepper renamed all my field keys and nobody warned me

First run. Spans are in OpenSearch. I open the Discover tab, search for `gen_ai.system: anthropic`. Zero results. I know the data is there — `_count` says 47 documents. I'm staring at the right index.

Half an hour later I found it: **Data Prepper replaces every `.` with `@` when flattening OTel span attributes**.

```
gen_ai.system              →  span.attributes.gen_ai@system
gen_ai.request.model       →  span.attributes.gen_ai@request@model
gen_ai.cost.total_usd      →  span.attributes.gen_ai@cost@total_usd
gen_ai.usage.input_tokens  →  span.attributes.gen_ai@usage@input_tokens
```

Every field reference in every dashboard visualization, every index template mapping, every OSD saved object — all of it has to use `@` separators, not `.`. The OTel semantic conventions use dots. Data Prepper silently changes them. There is no warning, no log line, no configuration option. It just happens.

```bash
# Find the actual field names in a real document
curl -s "http://localhost:9200/otel-v1-apm-span-*/_search?size=1" \
  | python3 -m json.tool | grep "gen_ai"
```

---

## 2. Every span attribute field is `keyword` by default — including the cost numbers

Once I had the right field names, the dashboard imports worked. Eleven saved objects loaded successfully. I navigated to the dashboard.

Eight of nine panels showed the error: *"Saved field `span.attributes.gen_ai@cost@total_usd` is invalid for use with the Sum aggregation."*

The one working panel was the call count — because `Count` aggregation doesn't care about field types.

**Root cause**: Data Prepper ships with a legacy index template that maps everything under `span.attributes.*` as `keyword`:

```json
{
  "span_attributes_map": {
    "path_match": "span.attributes.*",
    "mapping": { "type": "keyword" }
  }
}
```

`keyword` is a string type. You cannot run Sum or Avg on a string. A cost value stored as `"0.0423"` (keyword) is invisible to numeric aggregations. The panels were failing correctly — there was nothing wrong with the dashboard definitions. The problem was in the index.

**The obvious fix**: create a composable index template (`_index_template`) at priority 200 to override Data Prepper's default at priority 100, explicitly mapping the cost and token fields as `float` and `long`. This is exactly what the OpenSearch documentation tells you to do. I did it. The template showed up at `GET /_index_template`. Priority 200. Correct field definitions.

The dashboard still showed the same error.

---

## 3. Composable templates do not override legacy template dynamic rules in OpenSearch 2.17.1

This one took the longest to accept.

OpenSearch documentation says composable index templates take precedence over legacy templates. The Data Prepper template is a legacy template (`_template` API, `order: 0`). My override was a composable template (`_index_template` API, `priority: 200`). Higher priority wins. Case closed, right?

**What actually happens**: The legacy template's `dynamic_templates` section — the catch-all rule that maps `span.attributes.*` as `keyword` — does not get overridden by explicit property definitions in a composable template. When the first document containing `gen_ai@cost@total_usd` is indexed, the dynamic_template fires. The field is created as `keyword`. The composable template's explicit `float` definition for that field loses.

I verified this by checking the actual index mapping after indexing a single span:

```bash
curl -s "http://localhost:9200/otel-v1-apm-span-000001/_mapping" \
  | python3 -c "
import sys, json
m = json.load(sys.stdin)
idx = list(m.keys())[0]
attrs = m[idx]['mappings']['properties']['span']['properties']['attributes']['properties']
print(attrs.get('gen_ai@cost@total_usd', {}).get('type'))
"
# keyword
```

The composable template at priority 200 existed. It was being recognized by OpenSearch. It had no effect on the dynamic mapping of new fields.

This may be a version-specific behavior or an interaction between the two template systems that isn't well documented. Either way, the priority override approach is a dead end for this use case in OpenSearch 2.17.1.

**The fix that actually works**: bypass templates entirely. Directly `PUT` the explicit field mappings onto the index while it is empty:

```bash
curl -X PUT "http://localhost:9200/otel-v1-apm-span-000001/_mapping" \
  -H "Content-Type: application/json" \
  -d '{
    "properties": {
      "span": {
        "properties": {
          "attributes": {
            "properties": {
              "gen_ai@cost@total_usd":      { "type": "float" },
              "gen_ai@cost@input_usd":      { "type": "float" },
              "gen_ai@cost@output_usd":     { "type": "float" },
              "gen_ai@usage@input_tokens":  { "type": "long" },
              "gen_ai@usage@output_tokens": { "type": "long" },
              "gen_ai@usage@total_tokens":  { "type": "long" }
            }
          }
        }
      }
    }
  }'
```

Explicit property mappings within an index always win over `dynamic_templates` within that same index. Once `gen_ai@cost@total_usd` is explicitly defined as `float` in the index mapping, when the first document arrives with that field, OpenSearch uses the explicit definition and ignores the dynamic rule.

The constraint: this must run on an **empty** index. Once a field has been indexed as `keyword`, you cannot change its type. The only recovery is to delete the index and recreate it.

---

## 4. Data Prepper creates the index the moment it starts up — long before you run any setup script

After figuring out the direct `PUT /_mapping` approach, I wired it into a `setup-index-template.sh` script and hooked it to `make template`. Ran the full sequence: `make clean → make up → make template → make test → make dashboard`. Same error.

This time the script was running and returning success. I checked the mapping. All keyword again.

I added a timestamp check:

```bash
curl -s "http://localhost:9200/_cat/indices/otel-v1-apm-span-*?v&h=index,creation.date.string,docs.count"
```

```
index                   creation.date.string      docs.count
otel-v1-apm-span-000001 2026-03-17T02:21:25.216Z  0
```

The index was created at 02:21:25. `make template` ran at 02:23:10. By the time the setup script even tried to check for the index, it had existed for nearly two minutes.

**What's happening**: Data Prepper uses Index State Management (ISM) to manage the lifecycle of the span index. ISM creates `otel-v1-apm-span-000001` automatically as part of Data Prepper's initialization sequence — before any traces arrive, before `make template` runs, before anything else. The index comes into existence with the dynamic mapping already in place.

So the earlier script version that said *"No span index yet — template will apply when Data Prepper creates it"* was simply wrong. It had checked, found the index missing (because Data Prepper was still initializing), reported all clear, and exited. Data Prepper then created the index on its own schedule with the wrong mapping.

**The fix**: the setup script has to wait for the index to exist, not assume it doesn't:

```
wait for OpenSearch to be ready
  └─ poll for otel-v1-apm-span-000001 (every 3s, up to 90s)
       └─ index exists → check gen_ai@cost@total_usd type
              ├─ missing (field not yet in mapping) → PUT explicit types now  ✓
              ├─ float (already correct)            → nothing to do  ✓
              └─ keyword (already has wrong types)  → delete index
                                                    → restart data-prepper
                                                    → wait for recreation
                                                    → PUT explicit types  ✓
```

The "missing" case — where the index exists but the field hasn't appeared in the mapping yet — is the happy path. The index was just created by ISM, no documents have been indexed, and the field doesn't exist in the properties yet. Putting the explicit mapping now means it will be there when the first document arrives.

---

## 5. The string fields also broke — Terms aggregations need `keyword`, not `text`

Fixed the numeric fields. Cost totals appeared in the metric panels. Then I noticed the "Cost by Model" donut chart was empty. The Terms aggregation on `gen_ai@request@model` returned no buckets.

This was a second instance of the same underlying problem, affecting string fields differently. When a string field hits Data Prepper's dynamic template before any explicit mapping exists, it gets mapped as `text` with a `.keyword` subfield — the classic Elasticsearch/OpenSearch default for string fields. OSD's Terms aggregation looks for a directly aggregatable `keyword` field. It does not automatically fall back to `.keyword` subfields. `span.attributes.gen_ai@request@model` as `text` fails the check. `span.attributes.gen_ai@request@model.keyword` would work but the visualization didn't reference the subfield.

The fix was to include the string fields in the same `PUT /_mapping` call with `"type": "keyword"`, applied before any documents are indexed. Explicit `keyword` wins over the dynamic `text` mapping.

```json
"gen_ai@system":              { "type": "keyword" },
"gen_ai@request@model":       { "type": "keyword" },
"gen_ai@response@model":      { "type": "keyword" },
"gen_ai@cost@provider":       { "type": "keyword" },
"gen_ai@cost@model_resolved": { "type": "keyword" }
```

---

## 6. The OSD field refresh API that everyone points to doesn't exist in OSD 2.x

Even with correct field types in OpenSearch, OSD's index-pattern had cached old field definitions from the import. The Discover field list showed all numeric fields as "unknown type". Visualizations that checked the stored field type before sending the query were still rejecting them.

I found several Stack Overflow answers and GitHub issues pointing to this endpoint:

```bash
POST /api/index_patterns/index_pattern/{id}/fields?overwrite=true
```

It returns 404 in OpenSearch Dashboards 2.x. This API path does not exist.

**Finding the real API**: I used Playwright to intercept the actual browser network traffic while manually clicking "Reload field list" in Stack Management → Index Patterns. The real sequence is a two-step process:

**Step 1 — discover current fields from OpenSearch**:
```bash
GET /api/index_patterns/_fields_for_wildcard
  ?pattern=otel-v1-apm-span-*
  &meta_fields=_source&meta_fields=_id&meta_fields=_type&meta_fields=_index&meta_fields=_score
```

**Step 2 — write them back to the saved object**:
```bash
PUT /api/saved_objects/index-pattern/{id}
osd-xsrf: true
Content-Type: application/json

{"attributes": {"fields": "<serialized JSON string>"}}
```

**The trap**: the `fields` value must be a double-serialized JSON string — the array first serialized to a string, then embedded inside the outer JSON object. Passing the raw array causes OSD to internally store `[object Object]` and then fail to parse it later with "Unexpected token 'o', '[object Obj]' is not valid JSON."

```python
# Correct — fields_str is a string containing JSON
fields_str = json.dumps(fields, separators=(',', ':'))
body = json.dumps({'attributes': {'fields': fields_str}})

# Wrong — fields is a list, OSD serializes it as "[object Object]"
body = json.dumps({'attributes': {'fields': fields}})
```

---

## 7. Deleting the backing index also deletes the ISM alias, which silently breaks ingestion

At one point during debugging I deleted `otel-v1-apm-span-000001` directly to force a clean recreate. Restarted Data Prepper. Ran the test script. Waited. No documents appeared in OpenSearch.

Data Prepper logs:

```
index [otel-v1-apm-span] not found
```

The issue: `otel-v1-apm-span` is not a concrete index — it's an ISM-managed alias that points to the backing index. When you delete the backing index (`otel-v1-apm-span-000001`), the alias is removed along with it. Data Prepper holds a reference to the alias in its pipeline configuration and cannot write anywhere.

The fix is not to manually recreate the alias. Let ISM handle it: restart the `data-prepper` container, and ISM will reinitialize its state machine, recreate both the backing index and the alias, and resume ingestion cleanly.

```bash
docker restart data-prepper

# Verify the alias came back
curl -s "http://localhost:9200/_cat/aliases/otel-v1-apm-span?v"
```

---

## 8. The dashboard showed errors in the browser even though the API said everything was correct

After the full fix sequence was in place — correct index mapping, correct OSD field definitions, fresh `make dashboard` run — I navigated to the dashboard URL. Eight error badges. Same "invalid for Sum aggregation" message.

I checked the OSD API directly:

```bash
curl -s "http://localhost:5601/api/saved_objects/index-pattern/otel-apm-span-pattern" \
  | python3 -c "
import sys, json
p = json.loads(sys.stdin.read())
fields = json.loads(p['attributes']['fields'])
f = next(f for f in fields if 'gen_ai@cost@total_usd' in f['name'])
print(f['type'], f.get('esTypes'), f.get('aggregatable'))
"
# number ['float'] True
```

The backend said `number`, `float`, `aggregatable: True`. OpenSearch had `float`. The field refresh returned HTTP 200. Everything in the API was correct. The browser disagreed.

**Browser cache.** OSD loads the index-pattern field definitions into memory when the page first opens. If the page was already open when `make dashboard` ran the field refresh, the in-memory copy was stale. The API on the backend was correct; the React app state was not.

Hard reload. Ctrl+F5. All nine panels rendered correctly, immediately.

The lesson for any automated verification: always navigate fresh to the dashboard after running `make dashboard`, don't verify a tab that was already open.

---

## 9. The cost exporter never fired — Traceloop dropped `gen_ai.system`

After getting the dashboard working, I ran a fresh stack weeks later. Spans were flowing, the index had documents, field types were correct. Every cost panel showed zero. The call count panel showed the right number, which meant the KQL filter was letting documents through — or so I thought.

I checked the index directly:

```bash
curl -s "http://localhost:9200/otel-v1-apm-span-*/_search?size=1&sort=startTime:desc" \
  | python3 -c 'import json,sys; h=json.load(sys.stdin)["hits"]["hits"][0]["_source"]; print({k:v for k,v in h.items() if "cost" in k})'
# {}
```

No cost attributes on any span. The `CostEnrichingSpanExporter` was being initialized and reporting success, but `_enrich_with_cost()` was never running.

The gating condition was in `_is_llm_span()`:

```python
def _is_llm_span(self, span):
    attrs = span.attributes or {}
    return 'gen_ai.system' in attrs   # ← this was the problem
```

None of the spans had `gen_ai.system`. The Traceloop SDK had been updated to follow the current OTel GenAI semantic conventions, which replaced `gen_ai.system` with `gen_ai.provider.name`. Indexed docs confirmed: `gen_ai@provider@name = "openai"` present, `gen_ai@system` absent on every document.

The dashboard KQL filter had the same problem — `span.attributes.gen_ai@system: *` was matching zero documents. The call count panel had appeared correct only because it was counting all documents regardless of the filter (a misleading coincidence at that time range).

**The fix — two parts:**

Part 1: Update `_is_llm_span()` to recognize any current or legacy LLM marker:

```python
_LLM_MARKER_ATTRS = (
    'gen_ai.system',           # older Traceloop
    'gen_ai.provider.name',    # current Traceloop / OTel GenAI semconv
    'gen_ai.request.model',
    'gen_ai.response.model',
)

def _is_llm_span(self, span):
    attrs = span.attributes or {}
    return any(k in attrs for k in self._LLM_MARKER_ATTRS)
```

Part 2: Replace the dashboard KQL filter in all 9 visualization saved objects from `span.attributes.gen_ai@system: *` to `span.attributes.gen_ai@request@model: *`. The request model field is universally present on LLM spans regardless of SDK version.

A third stale key surfaced in the same pass: `gen_ai.usage.cache_read_tokens` had been renamed to `gen_ai.usage.cache_read_input_tokens` in the current semconv. Updated with a fallback:

```python
cached_tokens = attrs.get('gen_ai.usage.cache_read_input_tokens',
                           attrs.get('gen_ai.usage.cache_read_tokens', 0))
```

The lesson: OTel GenAI semantic conventions are still evolving. Any attribute name used as a detection signal (`gen_ai.system`) or a data key (`cache_read_tokens`) should be treated as potentially renamed in a future SDK version. Anchor detection on the most stable attributes (`gen_ai.request.model`) rather than the most descriptive ones.

---

## 10. Editing the Flask app source requires rebuilding the Docker image — restart is not enough

After fixing `llm_cost_injector.py`, I ran:

```bash
docker compose restart flask-recipe-app
```

The container restarted. Logs showed "LLM COST TRACKING SUCCESSFULLY ENABLED". I ran `make test`. Still no cost attributes on spans.

The Flask service is defined in `docker-compose.yml` as:

```yaml
flask-recipe-app:
  build:
    context: ./app
    dockerfile: Dockerfile
```

There is no `volumes:` mount for the `app/` directory. The Python source is baked into the image at build time. `docker compose restart` only stops and starts the existing container from the cached image — the edited `.py` files on the host are never read. The "SUCCESSFULLY ENABLED" message in the logs came from the old code.

**The correct command whenever any file in `app/` changes:**

```bash
docker compose build flask-recipe-app && docker compose up -d flask-recipe-app
```

This applies to `app.py`, `llm_cost_injector.py`, `requirements.txt`, or any other file under `app/`. The build step takes about 10 seconds since pip dependencies are cached by Docker's layer cache.

Verify the new code is actually running by checking the startup log for a message that only appears in the updated version:

```bash
docker compose logs flask-recipe-app | grep -E "COST TRACKING|✓✓✓|✗✗✗"
```

---

## The working sequence

After all of the above:

```bash
make clean        # kill all containers, delete all volumes
make up           # start opensearch, osd, data-prepper, otel-collector, flask
make template     # wait for ISM to create the index, PUT correct field types
make test         # generate LLM spans (now indexed with float/long/keyword)
make dashboard    # import the NDJSON, run field refresh
# navigate fresh to http://localhost:5601/app/dashboards#/view/llm-cost-dashboard
```

`make template` is the pivot point. It has to run after Data Prepper creates the empty backing index and before any span document is indexed. Everything else can be re-run safely. This step cannot.

---

## Quick reference: OTel → Data Prepper → OpenSearch

| OTel attribute | Data Prepper field | Required type | OSD aggregation |
|---|---|---|---|
| `gen_ai.cost.total_usd` | `span.attributes.gen_ai@cost@total_usd` | `float` | Sum |
| `gen_ai.cost.input_usd` | `span.attributes.gen_ai@cost@input_usd` | `float` | Sum |
| `gen_ai.cost.output_usd` | `span.attributes.gen_ai@cost@output_usd` | `float` | Sum |
| `gen_ai.usage.input_tokens` | `span.attributes.gen_ai@usage@input_tokens` | `long` | Sum |
| `gen_ai.usage.output_tokens` | `span.attributes.gen_ai@usage@output_tokens` | `long` | Sum |
| `gen_ai.usage.total_tokens` | `span.attributes.gen_ai@usage@total_tokens` | `long` | Sum |
| `gen_ai.system` *(older Traceloop)* | `span.attributes.gen_ai@system` | `keyword` | Terms |
| `gen_ai.provider.name` *(current Traceloop)* | `span.attributes.gen_ai@provider@name` | `keyword` | Terms |
| `gen_ai.request.model` | `span.attributes.gen_ai@request@model` | `keyword` | Terms |
| `gen_ai.response.model` | `span.attributes.gen_ai@response@model` | `keyword` | Terms |

---

## Summary

| What broke | Why | How it was actually fixed |
|---|---|---|
| Queries returned zero results | Data Prepper replaces `.` with `@` in all attribute names | Use `@` everywhere — index templates, dashboard queries, field references |
| Sum aggregations failed | `span.attributes.*` dynamic template maps everything as `keyword` | `PUT /_mapping` explicit float/long types on the empty index |
| Composable template had no effect | In OpenSearch 2.17.1, legacy `_template` dynamic rules win over `_index_template` explicit properties | Direct mapping on the index, not via template API |
| Mapping still wrong on fresh stack | Data Prepper ISM creates the backing index on startup, before setup scripts run | Script polls for the index, then PUTs mappings immediately on the empty index |
| Terms aggregations empty | String fields mapped as `text` by dynamic rule; OSD Terms requires direct `keyword` | Include string fields in the same `PUT /_mapping` as keyword |
| Field refresh API returned 404 | `POST /api/index_patterns/index_pattern/{id}/fields` does not exist in OSD 2.x | Two-step: `GET _fields_for_wildcard` then `PUT saved_objects/index-pattern/{id}` |
| Field refresh stored garbage | `fields` value passed as raw array | Double-serialize: `json.dumps(json.dumps(fields))` pattern |
| ISM alias disappeared | Deleting the backing index removes the alias | Always restart `data-prepper` after deleting the index; let ISM recreate both |
| Browser showed errors after correct API fix | OSD cached stale field definitions in React state | Hard reload the browser after `make dashboard` |
| Cost exporter fired but added no cost attributes | Traceloop dropped `gen_ai.system`; `_is_llm_span()` matched nothing | Detect on `gen_ai.provider.name` / `gen_ai.request.model` as well; update dashboard KQL filter to `gen_ai@request@model: *` |
| Code change had no effect after container restart | Flask app is baked into the Docker image; `restart` uses the cached image | `docker compose build flask-recipe-app && docker compose up -d flask-recipe-app` |
