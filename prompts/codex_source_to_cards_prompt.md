You are converting pending study sources into Anki card candidates.

Input:
- A JSONL file where each line is one source object.
- Each source has at least: `id`, `source_type`, `source_label`, `content_text`, `file_path`, `url`.

Output requirements:
- Return valid JSON only.
- Return an object with a top-level `notes` array.
- Each note must include:
  - `type`: `"basic"` or `"cloze"`
  - `front` (string)
  - `back` (string)
  - `cloze` (string)
  - `extra` (string)
  - `tags` (string array)
  - `source_id` (integer from input source id)
  - `origin`: `"codex"`
- For `basic`, include non-empty `front` and `back`; set `cloze` to `""`.
- For `cloze`, include non-empty `cloze`; set `front` and `back` only if useful.
- Keep each card concise and testable.
- Use 1-3 cards per source unless source is clearly not testable.
- If source has no testable content, generate no cards for that source.

Quality rules:
- Prefer cloze for definitions or contiguous prose.
- Prefer basic for fact recall or direct Q/A.
- Keep tags short and topical; use kebab-case.
- Add source context in `extra` when useful.
