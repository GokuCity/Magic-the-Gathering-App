#!/usr/bin/env python3
"""Refresh the card library embedded in index.html from Scryfall bulk data.

    python3 tools/refresh_cards.py              # cards + tags (see KEEP_TAGS below)
    python3 tools/refresh_cards.py --bump-sw    # ...and bump the service-worker cache name

What it does
  1. Reads https://api.scryfall.com/bulk-data and picks the "oracle_cards" and
     "oracle_tags" entries by type (download URLs are never hard-coded).
  2. Rebuilds `const CARD_DATA = [...]` in exactly the shape the app already uses,
     plus `oid` (the card's Scryfall oracle_id).
  3. Writes every Oracle Tag with its card count to tools/tag-report.txt, and embeds
     `const TAGS = {...}` (tag label -> oracle_ids) for the labels listed in KEEP_TAGS.

Standard library only (Python 3.8+). Downloads are cached in tools/.cache/.

CARD_DATA fields (one object per Commander-legal card, in bulk-file order):
  n    name                 mc  mana_cost         cmc  cmc (float)
  tl   type_line            ot  oracle_text       co   colors
  ci   color_identity       kw  keywords          gc   game_changer
  r    rarity               er  edhrec_rank       su   scryfall_uri
  leg  "Legendary" in the type line
  cbc  legendary creature anywhere in the type line, or "can be your commander" in the text
  cat  the export's own mechanic tags (see CATEGORY_RULES)
  oid  oracle_id
Multi-face cards keep the top-level fields only (so `ot` is empty for them); the app
fetches their face text from Scryfall at runtime. This matches the original export.
"""
import argparse
import collections
import difflib
import gzip
import json
import os
import re
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Oracle Tags to embed. Fill these with REAL labels from tools/tag-report.txt
# (the script prints the nearest matches for each concept in REQUESTED_TAG_CONCEPTS).
# While empty, the tag step only inspects the file and writes the report.
# ---------------------------------------------------------------------------
KEEP_TAGS = [
]

# Which keys in each oracle_tags line hold the tag label and its oracle_ids.
# Left unset on purpose: run once, read the printed sample lines, then fill these in.
# Until both are set, nothing is embedded (the report uses auto-detected keys, labelled as such).
TAG_LABEL_FIELD = None
TAG_IDS_FIELD = None

# The concepts the first tag selection is based on; used only for the nearest-match report.
REQUESTED_TAG_CONCEPTS = [
    'ramp', 'mana-rock', 'mana-dork', 'removal', 'creature-removal', 'board-wipe',
    'card-draw', 'tutor', 'sacrifice-outlet', 'protects-creature', 'counterspell',
    'recursion', 'reanimate', 'token-maker', 'anthem', 'lifegain', 'treasure',
]

# ---------------------------------------------------------------------------
# HTTP. Scryfall asks API clients to identify themselves with User-Agent and Accept
# headers and to space requests out. Check https://scryfall.com/docs/api for the current rules.
# ---------------------------------------------------------------------------
BULK_INDEX_URL = 'https://api.scryfall.com/bulk-data'
USER_AGENT = 'CommanderCardIndex-DataRefresh/1.0 (+https://github.com/GokuCity/Magic-the-Gathering-App)'
REQUEST_DELAY_S = 0.1

# ---------------------------------------------------------------------------
# The 12 `cat` tags come from the original export, whose generator was not kept.
# These rules were fitted against its 31,830 labelled cards: 8 categories reproduce it
# exactly; Ramp 99.76%, Board Wipe 99.91%, Removal 99.99%, Recursion 99.997%.
# They run on the full Oracle text, reminder text included, case-insensitively.
# Cards already in the app with unchanged name + text keep their existing tags,
# so the rules only decide tags for new or errata'd cards (use --retag-all to override).
# Order matters: it is the order tags appear in each card's list.
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    ('Ramp', r"add \{c\}|add one mana of any color|search your library for (a|an|up to \w+) (basic )?(land|forest|plains|island|swamp|mountain)"
             r"|search your library for [^.]*?basic land|(plains|island|swamp|mountain|forest)cycling|create a treasure token"
             r"|put a land card from your hand onto the battlefield|play an additional land"),
    ('Card Draw', r"\bdraws? (a|an|one|two|three|four|five|six) (additional )?cards?\b|\bdraw cards equal"),
    ('Removal (Spot)', r"\b(destroy|exile) (another )?target (artifact|creature|enchantment|planeswalker|permanent)"
                       r"|\bdeals? \d+ damage to (any target|target)|\btarget creature gets -\d+/-\d+|target creature[^.]*?loses all abilities"),
    ('Board Wipe', r"\b(destroy|exile) all creatures|\ball creatures get -|\bdeals? \d+ damage to each creature"),
    ('Recursion', r"\breturn [^.]*?from your graveyard"),
    ('Tutor', r"search your library for (a card|a creature card|an artifact|an enchantment card|an instant or sorcery card)"),
    ('Counterspell', r"\bcounter target spell"),
    ('Protection', r"hexproof|indestructible|protection from|\bward\b(?! ability)"),
    ('Token Generation', r"\bcreate\b[^.]*?\btokens?\b"),
    ('Lifegain', r"\bgain \d+ life|\bgains? life equal|\byou gain life\b"),
    ('Discard', r"\bdiscards? a card|\beach opponent discards"),
    ('Mana Fixing', r"mana of any color"),
]
_CATEGORY_RX = [(name, re.compile(rx, re.I)) for name, rx in CATEGORY_RULES]

# Fields compared against the current app data before anything is written. A wrong field
# mapping would make agreement collapse; small differences are normal (errata, new keywords).
STABLE_FIELDS = ['mc', 'cmc', 'tl', 'ot', 'co', 'ci', 'kw', 'leg', 'cbc']
INFO_FIELDS = ['gc', 'r', 'su', 'er']     # legitimately drift: game-changer list, printing, EDHREC rank
MIN_AGREEMENT = 0.90

HERE = os.path.dirname(os.path.abspath(__file__))
CARD_PREFIX = 'const CARD_DATA = '
TAGS_PREFIX = 'const TAGS = '
UUID_RX = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def dumps(obj):
    # Same serialisation as the original export (byte-identical round trip verified).
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


# ---------------------------------------------------------------- download
_last_request = [0.0]


def http_get(url, accept, dest=None):
    wait = REQUEST_DELAY_S - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': accept})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            if dest is None:
                return res.read()
            tmp = dest + '.part'
            with open(tmp, 'wb') as f:
                while True:
                    chunk = res.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dest)
            return dest
    finally:
        _last_request[0] = time.time()


def bulk_entries(args):
    if args.bulk_index_file:
        index = json.load(open(args.bulk_index_file, encoding='utf-8'))
    else:
        print(f'GET {BULK_INDEX_URL}')
        index = json.loads(http_get(BULK_INDEX_URL, 'application/json;q=0.9,*/*;q=0.8'))
    entries = index.get('data') if isinstance(index, dict) else None
    if not isinstance(entries, list):
        sys.exit(f'Unexpected bulk-data response; top-level keys: {list(index)[:10]}')
    return {e.get('type'): e for e in entries if isinstance(e, dict)}


def fetch_bulk(entries, bulk_type, cache_dir):
    entry = entries.get(bulk_type)
    if not entry:
        sys.exit(f'No "{bulk_type}" entry in bulk-data. Types offered: {sorted(t for t in entries if t)}')
    url = entry.get('download_uri')
    if not url:
        sys.exit(f'"{bulk_type}" entry has no download_uri. Keys: {sorted(entry)}')
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, url.rstrip('/').rsplit('/', 1)[-1])
    if os.path.exists(dest):
        print(f'{bulk_type}: using cached {os.path.relpath(dest)}')
    else:
        print(f'{bulk_type}: downloading {url}')
        http_get(url, '*/*', dest)
    print(f'{bulk_type}: {os.path.getsize(dest):,} bytes  (updated_at {entry.get("updated_at")})')
    return dest


def open_maybe_gzip(path):
    with open(path, 'rb') as f:
        magic = f.read(2)
    return gzip.open(path, 'rt', encoding='utf-8') if magic == b'\x1f\x8b' else open(path, encoding='utf-8')


# ---------------------------------------------------------------- html
def read_html(path):
    return open(path, 'rb').read().decode('utf-8').split('\n')


def find_line(lines, prefix):
    hits = [i for i, l in enumerate(lines) if l.startswith(prefix)]
    if len(hits) > 1:
        sys.exit(f'{prefix!r} appears on {len(hits)} lines; expected one.')
    return hits[0] if hits else None


def old_cards(lines):
    i = find_line(lines, CARD_PREFIX)
    if i is None or not lines[i].endswith(';'):
        sys.exit('Could not find a single-line `const CARD_DATA = [...];` in the HTML.')
    return i, json.loads(lines[i][len(CARD_PREFIX):-1])


# ---------------------------------------------------------------- cards
def categories(ot):
    return [name for name, rx in _CATEGORY_RX if ot and rx.search(ot)]


def to_app_card(c, carried):
    tl = c.get('type_line', '')
    ot = c.get('oracle_text', '')
    card = {
        'n': c['name'],
        'mc': c.get('mana_cost', ''),
        'cmc': float(c.get('cmc', 0)),
        'tl': tl,
        'ot': ot,
        'co': c.get('colors', []),
        'ci': c.get('color_identity', []),
        'kw': c.get('keywords', []),
        'gc': bool(c.get('game_changer', False)),
        'r': c.get('rarity', ''),
        'er': c.get('edhrec_rank'),
        'su': c.get('scryfall_uri', ''),
        'leg': bool(re.search(r'\bLegendary\b', tl)),
        'cbc': bool(re.search(r'\bLegendary\b', tl) and re.search(r'\bCreature\b', tl)) or 'can be your commander' in ot.lower(),
    }
    card['cat'] = carried if carried is not None else categories(ot)
    card['oid'] = c.get('oracle_id')
    return card


def build_cards(path, old, retag_all):
    print(f'Reading {os.path.relpath(path)} ...')
    with open_maybe_gzip(path) as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        sys.exit('oracle_cards file is not a JSON array.')
    legal = [c for c in raw if isinstance(c, dict) and (c.get('legalities') or {}).get('commander') == 'legal']
    print(f'oracle_cards: {len(raw):,} objects, {len(legal):,} legal in Commander')
    for key in ('name', 'type_line', 'cmc', 'color_identity', 'keywords', 'rarity', 'scryfall_uri', 'oracle_id'):
        have = sum(1 for c in legal if key in c)
        if have < 0.99 * len(legal):
            sys.exit(f'Field "{key}" present on only {have:,}/{len(legal):,} legal cards. Has the Scryfall schema changed?')
    if not any('game_changer' in c for c in legal):
        sys.exit('No card carries "game_changer"; check the Scryfall card schema before trusting `gc`.')

    old_by_name = {c['n']: c for c in old}
    cards, retagged = [], 0
    for c in legal:
        prev = old_by_name.get(c['name'])
        carry = None
        if prev is not None and not retag_all and prev.get('ot') == c.get('oracle_text', ''):
            carry = prev.get('cat', [])
        card = to_app_card(c, carry)
        retagged += carry is None
        cards.append(card)
    return cards, retagged


def verify_against_old(old, new, force):
    new_by_name = {c['n']: c for c in new}
    both = [(o, new_by_name[o['n']]) for o in old if o['n'] in new_by_name]
    print(f'\nField check on {len(both):,} cards present before and after:')
    bad = []
    for f in STABLE_FIELDS + INFO_FIELDS:
        agree = sum(1 for o, n in both if o.get(f) == n.get(f)) / max(1, len(both))
        flag = ''
        if f in STABLE_FIELDS and agree < MIN_AGREEMENT:
            flag = '  <-- below threshold'
            bad.append(f)
        print(f'  {f:4} {agree:8.2%}{"  (expected to drift)" if f in INFO_FIELDS else ""}{flag}')
    if bad and not force:
        sys.exit(f'Stopping before writing: {bad} disagree with the current data far more than errata would '
                 f'explain, which points to a field-mapping problem. Re-run with --force to override.')


# ---------------------------------------------------------------- tags
def inspect_tags(path, card_oids, report):
    with open_maybe_gzip(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    print(f'\noracle_tags: {len(rows):,} lines. First 3, verbatim:')
    with open_maybe_gzip(path) as f:
        for k, line in enumerate(f):
            if k == 3:
                break
            print('  ' + line.rstrip()[:600] + (' …' if len(line.rstrip()) > 600 else ''))

    label_f, ids_f = TAG_LABEL_FIELD, TAG_IDS_FIELD
    confirmed = bool(label_f and ids_f)
    if not confirmed:
        # Suggest keys from the data's shape only: a list of UUIDs, and a non-UUID string unique per line.
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        id_keys = [k for k, v in first.items() if isinstance(v, list) and v and all(isinstance(x, str) and UUID_RX.match(x) for x in v[:20])]
        str_keys = [k for k, v in first.items() if isinstance(v, str) and not UUID_RX.match(v)
                    and len({r.get(k) for r in rows}) == len(rows)]
        label_f = label_f or (str_keys[0] if len(str_keys) == 1 else None)
        ids_f = ids_f or (id_keys[0] if len(id_keys) == 1 else None)
        print(f'\nKeys in the first line: { {k: type(v).__name__ for k, v in first.items()} }')
        print(f'Auto-detected (unconfirmed): label key = {label_f!r} (candidates {str_keys}), '
              f'oracle_id list key = {ids_f!r} (candidates {id_keys})')
        if not (label_f and ids_f):
            print('Could not pick the keys unambiguously. Set TAG_LABEL_FIELD / TAG_IDS_FIELD and re-run.')
            return None

    tags = {}
    for r in rows:
        label, ids = r.get(label_f), r.get(ids_f)
        if isinstance(label, str) and isinstance(ids, list):
            tags[label] = ids
    ranked = sorted(tags.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    with open(report, 'w', encoding='utf-8') as out:
        out.write(f'# Scryfall Oracle Tags: {len(ranked):,} labels, sorted by card count\n')
        out.write(f'# keys used: label={label_f!r} ids={ids_f!r} ({"confirmed" if confirmed else "AUTO-DETECTED, unconfirmed"})\n')
        out.write('# columns: total oracle_ids | of those, cards in this app | label\n')
        for label, ids in ranked:
            out.write(f'{len(ids):7d} {sum(1 for x in ids if x in card_oids):7d}  {label}\n')
        out.write('\n# Nearest real labels for each requested concept\n')
        labels = [l for l, _ in ranked]
        for concept in REQUESTED_TAG_CONCEPTS:
            exact = concept in tags
            near = difflib.get_close_matches(concept, labels, n=6, cutoff=0.5)
            near += [l for l in labels if concept in l and l not in near][:6]
            out.write(f'{concept:18} {"FOUND" if exact else "missing":8} nearest: {", ".join(near[:8]) or "-"}\n')
    print(f'Wrote {os.path.relpath(report)} ({len(ranked):,} labels).')
    missing = [c for c in REQUESTED_TAG_CONCEPTS if c not in tags]
    if missing:
        print(f'Requested concepts with no exact label: {missing} (nearest matches are in the report).')
    return tags if confirmed else None


def build_tags(tags, card_oids):
    unknown = [t for t in KEEP_TAGS if t not in tags]
    if unknown:
        sys.exit(f'KEEP_TAGS has labels that do not exist in oracle_tags: {unknown}')
    # Only ids of cards in this app; sorted so the output is stable between runs.
    return {t: sorted(x for x in tags[t] if x in card_oids) for t in KEEP_TAGS}


# ---------------------------------------------------------------- main
def bump_sw(path):
    s = open(path, encoding='utf-8').read()
    m = re.search(r"const CACHE = 'card-index-v(\d+)';", s)
    if not m:
        sys.exit(f'No card-index-vN cache name in {path}')
    new = f"const CACHE = 'card-index-v{int(m.group(1)) + 1}';"
    open(path, 'w', encoding='utf-8').write(s.replace(m.group(0), new))
    print(f'sw.js: {m.group(0)} -> {new}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--html', default=os.path.join(HERE, '..', 'index.html'))
    ap.add_argument('--cache-dir', default=os.path.join(HERE, '.cache'))
    ap.add_argument('--bulk-index-file', help='use a saved copy of the bulk-data response')
    ap.add_argument('--cards-file', help='use a local oracle_cards file instead of downloading')
    ap.add_argument('--tags-file', help='use a local oracle_tags file instead of downloading')
    ap.add_argument('--skip-tags', action='store_true')
    ap.add_argument('--tag-report', default=os.path.join(HERE, 'tag-report.txt'))
    ap.add_argument('--retag-all', action='store_true', help='recompute `cat` for every card, not only new/changed ones')
    ap.add_argument('--force', action='store_true', help='write even if the field check fails')
    ap.add_argument('--dry-run', action='store_true', help='report only; do not modify index.html')
    ap.add_argument('--bump-sw', action='store_true', help='increment the card-index-vN cache name in sw.js')
    args = ap.parse_args()

    lines = read_html(args.html)
    html_before = os.path.getsize(args.html)
    card_line, old = old_cards(lines)

    entries = None
    if not (args.cards_file and (args.tags_file or args.skip_tags)):
        entries = bulk_entries(args)
    cards_path = args.cards_file or fetch_bulk(entries, 'oracle_cards', args.cache_dir)
    cards, retagged = build_cards(cards_path, old, args.retag_all)

    names = [c['n'] for c in cards]
    dupes = sorted(n for n, k in collections.Counter(names).items() if k > 1)
    if dupes:
        print(f'WARNING: {len(dupes)} duplicate names (the app indexes by name): {dupes[:10]}')
    verify_against_old(old, cards, args.force)

    old_names, new_names = {c['n'] for c in old}, set(names)
    gone = sorted(old_names - new_names)
    added = [n for n in names if n not in old_names]
    print(f'\nCards: {len(old):,} before -> {len(cards):,} after ({len(cards) - len(old):+,})')
    print(f'New cards: {len(added):,}' + (f'  e.g. {", ".join(added[:8])}' if added else ''))
    print(f'`cat` computed by rules for {retagged:,} new or changed cards; kept for the rest.')
    print(f'In the old data but not the new ({len(gone)}):')
    for n in gone:
        print(f'  - {n}')

    tags_out = None
    if not args.skip_tags:
        tags_path = args.tags_file or fetch_bulk(entries, 'oracle_tags', args.cache_dir)
        card_oids = {c['oid'] for c in cards}
        tags = inspect_tags(tags_path, card_oids, args.tag_report)
        if tags is not None and KEEP_TAGS:
            tags_out = build_tags(tags, card_oids)
            print('\nEmbedding TAGS:')
            for t, ids in tags_out.items():
                print(f'  {len(ids):6,}  {t}')
        elif KEEP_TAGS:
            print('KEEP_TAGS is set but the tag keys are not confirmed; TAGS not embedded.')
        else:
            print('KEEP_TAGS is empty; TAGS not embedded.')

    if args.dry_run:
        print('\n--dry-run: index.html not modified.')
        return

    lines[card_line] = CARD_PREFIX + dumps(cards) + ';'
    tags_line = find_line(lines, TAGS_PREFIX)
    if tags_out is not None:
        new_line = TAGS_PREFIX + dumps(tags_out) + ';'
        if tags_line is None:
            lines.insert(card_line + 1, new_line)
        else:
            lines[tags_line] = new_line
    out = '\n'.join(lines)
    open(args.html, 'wb').write(out.encode('utf-8'))

    html_after = os.path.getsize(args.html)
    tags_bytes = len((TAGS_PREFIX + dumps(tags_out) + ';').encode('utf-8')) if tags_out is not None else 0
    print(f'\nindex.html: {html_before:,} -> {html_after:,} bytes ({html_after - html_before:+,}); '
          f'gzipped {len(gzip.compress(out.encode("utf-8"), 9)):,}')
    if tags_out is not None:
        print(f'TAGS line: {tags_bytes:,} bytes (gzipped {len(gzip.compress((TAGS_PREFIX + dumps(tags_out)).encode(), 9)):,})')
    if args.bump_sw:
        bump_sw(os.path.join(os.path.dirname(os.path.abspath(args.html)), 'sw.js'))


if __name__ == '__main__':
    main()
