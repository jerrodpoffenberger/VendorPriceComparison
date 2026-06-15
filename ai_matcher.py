import json
import os
import anthropic

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
    return _client


def _parse_json_response(text: str) -> any:
    text = text.strip()
    if '```' in text:
        parts = text.split('```')
        for part in parts[1::2]:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue
    return json.loads(text)


def identify_columns(rows: list[dict]) -> dict:
    """Given sample rows from a parsed file, ask Claude which columns hold description, price, and unit."""
    sample = json.dumps(rows[:15], indent=2, default=str)

    message = _get_client().messages.create(
        model='claude-sonnet-4-6',
        max_tokens=512,
        messages=[{
            'role': 'user',
            'content': f"""You are analyzing a vendor price sheet for beef and pork cuts.

Here are the first rows of the file as JSON:
{sample}

Identify which field keys contain:
1. The product description (cut name)
2. The price (numeric, per lb or per case)
3. The unit (lb, case, cwt, each) — may be null if embedded elsewhere

Return ONLY a JSON object (no explanation):
{{"description_field": "key_name", "price_field": "key_name", "unit_field": "key_name_or_null"}}"""
        }]
    )

    return _parse_json_response(message.content[0].text)


def match_cuts(raw_descriptions: list[str], existing_cuts: list[dict]) -> list[dict]:
    """Match a list of raw vendor descriptions to canonical cut names.

    Returns a list of dicts: {raw, canonical, category, is_new}
    Processes in batches of 80 to stay within token limits.
    """
    results = []
    batch_size = 80

    for i in range(0, len(raw_descriptions), batch_size):
        batch = raw_descriptions[i:i + batch_size]
        results.extend(_match_batch(batch, existing_cuts))
        # Update existing_cuts with any newly created cuts from this batch
        existing_names = {c['name'] for c in existing_cuts}
        for r in results:
            if r.get('is_new') and r['canonical'] not in existing_names:
                existing_cuts.append({'name': r['canonical'], 'category': r['category']})
                existing_names.add(r['canonical'])

    return results


def _match_batch(raw_descriptions: list[str], existing_cuts: list[dict]) -> list[dict]:
    canonical_list = json.dumps([c['name'] for c in existing_cuts], indent=2)
    desc_list = json.dumps(raw_descriptions, indent=2)

    message = _get_client().messages.create(
        model='claude-sonnet-4-6',
        max_tokens=4096,
        messages=[{
            'role': 'user',
            'content': f"""You are a meat industry expert normalizing beef and pork cut names across vendors.

Existing canonical cuts in our system:
{canonical_list}

Raw descriptions to classify (from a vendor price sheet):
{desc_list}

For each raw description:
- Match to an EXISTING canonical cut if it clearly refers to the same cut
- If no confident match, suggest a clean canonical name (e.g., "Ribeye Steak" not "Choice Angus Ribeye 1x1 12/14oz IW")
- Set category to "beef", "pork", or "other"
- Set category to "skip" for non-meat items (boxes, supplies, freight, etc.)
- Set is_new: true only when no existing canonical cut matches

Return ONLY a JSON array (no explanation):
[{{"raw": "original description", "canonical": "clean cut name", "category": "beef|pork|other|skip", "is_new": false}}]"""
        }]
    )

    return _parse_json_response(message.content[0].text)
