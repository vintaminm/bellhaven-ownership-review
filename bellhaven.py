import argparse
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BASE = os.environ.get('BELLHAVEN_BASE_URL', 'https://analyst-assessment-production.up.railway.app').rstrip('/')
STATE = Path(os.environ.get('BELLHAVEN_STATE_DIR', 'state'))
CARE = {'Short-Term Rehabilitation & Nursing': 'Skilled Nursing', 'Memory Support': 'Memory Care', 'Assisted Living': 'Assisted Living'}
FIELDS = ['name', 'parent_id', 'billing_street', 'billing_city', 'billing_state', 'billing_zip', 'care_type', 'status', 'note', 'duplicate_of_account', 'chow_current_account', 'lifetime_revenue', 'outstanding_ar']


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def norm(text):
    text = str(text or '').lower().replace('&', ' and ')
    words = re.findall(r'[a-z0-9]+', text)
    aliases = {'street': 'st', 'road': 'rd', 'avenue': 'ave', 'drive': 'dr', 'lane': 'ln', 'boulevard': 'blvd', 'northwest': 'nw', 'northeast': 'ne', 'southwest': 'sw', 'southeast': 'se', 'west': 'w', 'east': 'e', 'north': 'n', 'south': 's', 'pike': 'pk', 'centre': 'center'}
    return ' '.join(aliases.get(w, w) for w in words)


def financial(account):
    values = []
    for field in ('lifetime_revenue', 'outstanding_ar'):
        value = account.get(field)
        if isinstance(value, bool) or value is None:
            raise ValueError('Missing or invalid billing information: ' + field)
        value = float(value)
        if not (0 <= value < float('inf')):
            raise ValueError('Invalid billing information: ' + field)
        values.append(value)
    return values[0] > 0 and values[1] > 0


class Client:
    def __init__(self):
        self.token = os.environ.get('BELLHAVEN_API_TOKEN', '')
        if not self.token:
            raise ValueError('Set BELLHAVEN_API_TOKEN in your environment.')

    def request(self, path, payload=None, method='GET', auth=True):
        headers = {'Accept': 'application/json', 'User-Agent': 'BellhavenOwnershipReview/1.0'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.token
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        url = BASE + path
        for attempt in range(3 if method == 'GET' else 1):
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as response:
                    body = response.read().decode()
                    return json.loads(body) if 'application/json' in response.headers.get('Content-Type', '') else body
            except (urllib.error.URLError, TimeoutError):
                if method != 'GET' or attempt == 2:
                    raise RuntimeError('Remote request failed; refresh before retrying. No automatic mutation retry.') from None
                time.sleep(0.5 * (attempt + 1))

    def accounts(self):
        rows, page = [], 1
        while True:
            result = self.request('/api/v1/accounts?' + urllib.parse.urlencode({'page': page, 'page_size': 50}))
            rows.extend(result['data'])
            if len(rows) >= result['total']:
                break
            if not result['data']:
                raise ValueError('CRM pagination ended early')
            page += 1
        if len({a['account_id'] for a in rows}) != len(rows):
            raise ValueError('CRM pagination returned duplicates')
        return rows

    def account(self, account_id):
        result = self.request('/api/v1/accounts/' + account_id)
        return result.get('data', result)

    def patch(self, account_id, payload):
        return self.request('/api/v1/accounts/' + account_id, payload, 'PATCH')


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.title = []
        self.fields = {}
        self.label = ''
        self.mode = ''
        self.parts = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.links.append(attrs.get('href', ''))
        if tag in ('h1', 'dt', 'dd'):
            self.mode, self.parts = tag, []
        if tag in ('br', 'span') and self.mode == 'dd':
            self.parts.append('|')

    def handle_endtag(self, tag):
        if tag == self.mode:
            text = ' '.join(self.parts).strip()
            if tag == 'h1':
                self.title = text
            elif tag == 'dt':
                self.label = text
            elif tag == 'dd':
                self.fields[self.label] = text
            self.mode = ''

    def handle_data(self, data):
        self.text.append(data)
        if self.mode:
            self.parts.append(data.strip())


def location_from_html(url, html):
    page = Page(html)
    street, cityline = [p.strip() for p in page.fields['Address'].split('|')]
    match = re.fullmatch(r'(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', cityline)
    if not match or not page.title:
        raise ValueError('Incomplete community detail: ' + url)
    offerings = [p.strip() for p in page.fields['Care Offerings'].split('|') if p.strip()]
    if not offerings or any(o not in CARE for o in offerings):
        raise ValueError('Unknown care offering: ' + url)
    return {'name': page.title, 'street': street, 'city': match[1], 'state': match[2], 'zip': match[3], 'care_offerings': offerings, 'phone': page.fields.get('Phone', ''), 'source_url': BASE + url}


def scrape(client):
    pending, seen, detail, listing, expected = ['/', '/communities'], set(), set(), set(), None
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        html = client.request(path, auth=False)
        page = Page(html)
        if path.startswith('/communities'):
            count = re.search(r'(\d+) communities listed', html)
            if not count:
                raise ValueError('Directory count missing; refusing incomplete crawl')
            if expected is not None and expected != int(count[1]):
                raise ValueError('Directory count changed during crawl')
            expected = int(count[1])
        for link in page.links:
            if link.startswith('/communities/'):
                detail.add(link)
                if path.startswith('/communities'):
                    listing.add(link)
            elif link.startswith('/communities?') and link not in seen:
                pending.append(link)
    if not expected or len(listing) != expected:
        raise ValueError('Incomplete directory; no removal proposals generated')
    locations = [location_from_html(path, client.request(path, auth=False)) for path in sorted(detail)]
    return locations, {'fetched_at': now(), 'directory_count': expected, 'total_locations': len(locations), 'pages': sorted(seen), 'detail_pages': len(detail)}


def canonical(location, parent_id):
    return {'name': location['name'], 'billing_street': location['street'], 'billing_city': location['city'], 'billing_state': location['state'], 'billing_zip': location['zip'], 'care_type': '; '.join(CARE[o] for o in location['care_offerings']), 'parent_id': parent_id, 'status': 'Active'}


def score(location, account):
    street = norm(location['street']) == norm(account['billing_street'])
    city = norm(location['city']) == norm(account['billing_city'])
    state = location['state'] == account['billing_state']
    zip_match = location['zip'][:5] == account['billing_zip'][:5]
    name = norm(location['name']) == norm(account['name'])
    if street and city and state:
        return 100 if zip_match else 92, 'Same normalized street, city and state' + ('; ZIP agrees.' if zip_match else '; ZIP needs correction.')
    if name and city and state and zip_match:
        return 90, 'Exact name and city/state/ZIP; street requires review.'
    if city and state and zip_match and difflib.SequenceMatcher(None, norm(location['name']), norm(account['name'])).ratio() >= .80:
        return 65, 'Similar name and same city/state/ZIP; street does not agree.'
    return 0, ''


def proposal(kind, location, account, desired, reason, candidates=None, survivor=None):
    identity = {'kind': kind, 'source': location, 'account_id': account['account_id'] if account else None, 'desired': desired}
    return {'id': digest(identity), 'kind': kind, 'location': location, 'account': account, 'desired': desired, 'reason': reason, 'candidates': candidates or [], 'survivor': survivor, 'created_at': now()}


def plan(locations, accounts):
    parents = [a for a in accounts if a['name'] == 'Bellhaven Senior Living (Parent Account)' and not a['parent_id']]
    if len(parents) != 1:
        raise ValueError('Cannot identify one Bellhaven parent account')
    parent_id = parents[0]['account_id']
    proposals, observations, used = [], [], set()
    for loc in locations:
        ranked = []
        for account in accounts:
            if account.get('duplicate_of_account') or account.get('chow_current_account') or account['status'] == 'Inactive' or '(Parent Account)' in account['name']:
                continue
            points, reason = score(loc, account)
            if points:
                ranked.append((points, reason, account))
        ranked.sort(key=lambda x: (-x[0], -int(norm(x[2]['name']) == norm(loc['name'])), -int(x[2]['parent_id'] == parent_id), -float(x[2]['lifetime_revenue']), x[2]['account_id']))
        candidates = [{'score': s, 'reason': r, 'account': a} for s, r, a in ranked]
        desired = canonical(loc, parent_id)
        if not ranked:
            for account in accounts:
                if norm(account['name']) == norm(loc['name']):
                    proposals.append(proposal('ambiguous', loc, account, {}, 'Name-only candidate conflicts with physical address. Reject this link; the separate create proposal uses the website location.', [{'score': 0, 'reason': 'Different physical address; name alone is insufficient.', 'account': account}]))
            proposals.append(proposal('create', loc, None, desired, 'No address-backed or name/locality-backed match in the complete CRM snapshot.', candidates))
            continue
        points, reason, best = ranked[0]
        if points < 90:
            used.update(a['account_id'] for _, _, a in ranked)
            proposals.append(proposal('ambiguous', loc, best, {}, 'Insufficient evidence for a write. Reject to leave unchanged; investigate before creating an account.', candidates))
            continue
        used.add(best['account_id'])
        changes = {}
        for field, value in desired.items():
            same = norm(best[field]) == norm(value) if field.startswith('billing_') else best[field] == value
            if not same:
                changes[field] = value
        if changes:
            kind = 'update'
            if 'parent_id' in changes and financial(best):
                kind, changes = 'chow', desired
                reason += ' Revenue history and outstanding AR require a new current account; preserve every old field except the CHOW pointer.'
            proposals.append(proposal(kind, loc, best, changes, reason, candidates))
        else:
            observations.append({'classification': 'confident_match', 'location': loc, 'account_id': best['account_id'], 'reason': reason})
        for extra_points, _, extra in ranked[1:]:
            if extra_points < 92:
                continue
            used.add(extra['account_id'])
            protected = financial(extra)
            note = 'Same physical facility as ' + best['account_id'] + '; website: ' + loc['source_url']
            if protected:
                desired_extra = {'status': 'Needs Review', 'note': (extra['note'] + '\n' + 'Possible duplicate with billing history. ' + note).strip()}
            else:
                desired_extra = {'status': 'Inactive', 'duplicate_of_account': best['account_id'], 'note': (extra['note'] + '\n' + 'Duplicate. ' + note).strip()}
            if best.get('chow_current_account'):
                continue
            proposals.append(proposal('duplicate_review' if protected else 'duplicate', loc, extra, desired_extra, 'Matching physical address and care offering; keep the strongest name/parent match. ' + ('Billing history requires investigation.' if protected else 'No revenue plus open-AR constraint on this copy.'), candidates, best))
    for account in accounts:
        if account['account_id'] == parent_id or account['account_id'] in used or account['parent_id'] != parent_id or account.get('duplicate_of_account') or account.get('chow_current_account') or account['status'] != 'Active':
            continue
        note = 'Not found in the complete Bellhaven directory or homepage links. Ownership unverified; preserve parent and billing history pending research.'
        proposals.append(proposal('not_listed', None, account, {'status': 'Needs Review', 'note': (account['note'] + '\n' + note).strip()}, note))
    return proposals, observations


@contextlib.contextmanager
def db():
    STATE.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE / 'review.sqlite3')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', decision_at TEXT, reviewer TEXT, decision_note TEXT, result TEXT);
      CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, stage TEXT, account_id TEXT);
      CREATE TABLE IF NOT EXISTS runs (at TEXT, summary TEXT);
    ''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / '.lock').open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def pipeline(client):
    with locked():
        locations, crawl = scrape(client)
        accounts = client.accounts()
        proposals, observations = plan(locations, accounts)
        with db() as conn:
            previous = {row['id']: row['status'] for row in conn.execute('SELECT id,status FROM proposals')}
            recovery = [json.loads(row['body']) for row in conn.execute("SELECT p.body FROM proposals p JOIN operations o ON p.id=o.id WHERE p.status='pending'")]
            recovery_ids = {p['id'] for p in recovery}
            recovery_accounts = {p['account']['account_id'] for p in recovery if p['account']}
            proposals = [p for p in proposals if not (p['account'] and p['account']['account_id'] in recovery_accounts and p['id'] not in recovery_ids)]
            active = {p['id'] for p in proposals}
            active.update(recovery_ids)
            for row in conn.execute("SELECT id FROM proposals WHERE status='pending'").fetchall():
                if row['id'] not in active:
                    conn.execute("UPDATE proposals SET status='superseded' WHERE id=?", (row['id'],))
            for item in proposals:
                if item['id'] not in previous:
                    conn.execute('INSERT INTO proposals(id,body) VALUES (?,?)', (item['id'], json.dumps(item)))
                elif previous[item['id']] == 'pending':
                    conn.execute('UPDATE proposals SET body=? WHERE id=?', (json.dumps(item), item['id']))
            pending = conn.execute("SELECT count(*) FROM proposals WHERE status='pending'").fetchone()[0]
            summary = dict(crawl, crm_accounts=len(accounts), confident_matches=len(observations), generated=len(proposals), new_proposals=sum(p['id'] not in previous for p in proposals), pending=pending)
            conn.execute('INSERT INTO runs VALUES (?,?)', (now(), json.dumps(summary)))
        (STATE / 'latest.json').write_text(json.dumps({'summary': summary, 'locations': locations, 'matches': observations}, indent=2))
        return summary


def check_before(expected, current):
    if any(expected.get(k) != current.get(k) for k in FIELDS):
        raise ValueError('CRM changed since this proposal. Run the pipeline again and review the fresh evidence.')


def ensure_created(client, conn, item):
    marker = '[ownership-review:' + item['id'] + ']'
    matches = [a for a in client.accounts() if marker in a.get('note', '')]
    if len(matches) > 1:
        raise ValueError('Multiple create markers found; manual recovery required')
    if matches:
        result = matches[0]
    else:
        operation = conn.execute('SELECT * FROM operations WHERE id=?', (item['id'],)).fetchone()
        if operation:
            raise ValueError('An earlier create has an uncertain outcome. Refresh CRM and resolve it before retrying; another POST is blocked.')
        conn.execute('INSERT INTO operations VALUES (?,?,?)', (item['id'], 'creating', None))
        conn.commit()
        payload = dict(item['desired'], note=marker + ' Source: ' + item['location']['source_url'], phone=item['location']['phone'])
        result = client.request('/api/v1/accounts', payload, 'POST')
        result = result.get('data', result)
    if not result.get('account_id'):
        raise ValueError('Creation returned no account id')
    conn.execute('UPDATE operations SET stage=?,account_id=? WHERE id=?', ('created', result['account_id'], item['id']))
    conn.commit()
    return result['account_id']


def decide(client, proposal_id, action, reviewer, note):
    if action not in ('approve', 'reject') or not reviewer.strip() or not note.strip():
        raise ValueError('Choose approve/reject and provide a reviewer and decision reason.')
    with locked(), db() as conn:
        row = conn.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if not row:
            raise ValueError('Unknown proposal')
        if row['status'] != 'pending':
            raise ValueError('This proposal was already decided or superseded.')
        item = json.loads(row['body'])
        result = None
        if action == 'approve':
            if item['kind'] == 'ambiguous':
                raise ValueError('Ambiguous matches cannot write to CRM.')
            current = client.account(item['account']['account_id']) if item['account'] else None
            if current:
                if item['kind'] == 'chow' and current.get('chow_current_account'):
                    linked = client.account(current['chow_current_account'])
                    if '[ownership-review:' + item['id'] + ']' not in linked.get('note', ''):
                        raise ValueError('CHOW pointer already set by another change')
                    check_before(item['account'], dict(current, chow_current_account=item['account']['chow_current_account']))
                else:
                    already_applied = all(current.get(k) == v for k, v in item['desired'].items()) and item['kind'] != 'chow'
                    if not already_applied:
                        check_before(item['account'], current)
            if item['kind'] in ('create', 'chow'):
                if item['kind'] == 'chow' and not financial(current):
                    raise ValueError('Billing conditions changed; refresh and review.')
                if item['kind'] == 'create':
                    marker = '[ownership-review:' + item['id'] + ']'
                    for a in client.accounts():
                        if '(Parent Account)' in a['name'] or a['status'] == 'Inactive' or a.get('chow_current_account'):
                            continue
                        if score(item['location'], a)[0] >= 90 and not a.get('duplicate_of_account') and marker not in a.get('note', ''):
                            raise ValueError('A matching CRM account now exists. Refresh before creating.')
                account_id = ensure_created(client, conn, item)
                if item['kind'] == 'chow':
                    fresh = client.account(current['account_id'])
                    check_before(item['account'], dict(fresh, chow_current_account=item['account']['chow_current_account']))
                    client.patch(current['account_id'], {'chow_current_account': account_id})
                result = client.account(account_id)
            else:
                if 'parent_id' in item['desired'] and current['parent_id'] != item['desired']['parent_id'] and financial(current):
                    raise ValueError('Revenue with open AR prohibits direct re-parenting.')
                if item.get('survivor'):
                    survivor = client.account(item['survivor']['account_id'])
                    if survivor['status'] != 'Active' or survivor.get('duplicate_of_account') or survivor.get('chow_current_account'):
                        raise ValueError('Duplicate survivor is no longer current.')
                    if score(item['location'], survivor)[0] < 90:
                        raise ValueError('Duplicate survivor no longer matches the location.')
                client.patch(current['account_id'], item['desired'])
                result = client.account(current['account_id'])
            if not all(result.get(k) == v for k, v in item['desired'].items()):
                raise ValueError('Read-after-write verification failed; inspect CRM before retrying.')
            if item['kind'] == 'chow':
                old = client.account(current['account_id'])
                check_before(item['account'], dict(old, chow_current_account=item['account']['chow_current_account']))
                if old['chow_current_account'] != result['account_id']:
                    raise ValueError('CHOW link verification failed')
        status = 'approved' if action == 'approve' else 'rejected'
        conn.execute('UPDATE proposals SET status=?,decision_at=?,reviewer=?,decision_note=?,result=? WHERE id=?', (status, now(), reviewer.strip(), note.strip(), json.dumps(result), proposal_id))
        return {'status': status, 'result': result}


def review_data():
    with db() as conn:
        rows = [dict(row) for row in conn.execute('SELECT * FROM proposals ORDER BY rowid')]
        run = conn.execute('SELECT summary FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
    for row in rows:
        row['body'] = json.loads(row['body'])
        row['result'] = json.loads(row['result']) if row['result'] else None
    return {'proposals': rows, 'summary': json.loads(run[0]) if run else {}}


def suggest_address(client, account_id):
    with locked():
        locations, _ = scrape(client)
        account = client.account(account_id)
        matches = [loc for loc in locations if score(loc, account)[0] >= 90]
        if len(matches) != 1:
            raise ValueError('Address suggestion requires one location-backed match.')
        loc = matches[0]
        if norm(loc['street']) != norm(account['billing_street']):
            raise ValueError('This is a substantive address conflict; use the main review pipeline.')
        if loc['street'] == account['billing_street']:
            raise ValueError('The street already uses the website formatting.')
        item = proposal('update', loc, account, {'billing_street': loc['street']}, 'Optional formatting only: the normalized street already agrees. A reviewer may keep the existing CRM abbreviation or adopt the website spelling.')
        with db() as conn:
            conn.execute('INSERT OR IGNORE INTO proposals(id,body) VALUES (?,?)', (item['id'], json.dumps(item)))
        return {'proposal_id': item['id'], 'name': loc['name']}


def serve(client, port):
    csrf = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json'):
            body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
                return self.send(403, {'error': 'Local access only'})
            if self.path == '/':
                html = Path(__file__).with_name('review.html').read_text().replace('__CSRF__', csrf)
                return self.send(200, html, 'text/html; charset=utf-8')
            if self.path == '/api/review':
                return self.send(200, review_data())
            if self.path.startswith('/api/account/'):
                account_id = self.path.rsplit('/', 1)[1]
                if re.fullmatch('[A-Za-z0-9]+', account_id):
                    return self.send(200, client.account(account_id))
            self.send(404, {'error': 'Not found'})

        def do_POST(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}') or not secrets.compare_digest(self.headers.get('X-Review-CSRF', ''), csrf):
                return self.send(403, {'error': 'Invalid review session'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if size < 1 or size > 8192:
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(size))
                if self.path == '/api/decide':
                    result = decide(client, body['id'], body['action'], body['reviewer'], body['note'])
                elif self.path == '/api/run':
                    result = pipeline(client)
                else:
                    return self.send(404, {'error': 'Not found'})
                self.send(200, result)
            except Exception as error:
                self.send(409, {'error': str(error)})
    print(f'Review app: http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'serve', 'export', 'suggest-address'])
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--account-id')
    args = parser.parse_args()
    if args.command == 'export':
        print(json.dumps(review_data(), indent=2))
    elif args.command == 'run':
        print(json.dumps(pipeline(Client()), indent=2))
    elif args.command == 'suggest-address':
        if not args.account_id:
            parser.error('--account-id is required')
        print(json.dumps(suggest_address(Client(), args.account_id), indent=2))
    else:
        serve(Client(), args.port)


if __name__ == '__main__':
    main()
