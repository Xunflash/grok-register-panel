from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path('/data/claw-relocated/releases/panel-18a85b0')
DATA = Path('/data/claw-relocated/grok-register-panel')
REPORT = Path('/home/claw/releases/build-reauthorize-20261020')
PREVIOUS = Path('/home/claw/releases/build-relogin-check-20261020')


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def guard_state():
    query = '''import sqlite3,json
db=sqlite3.connect('file:/var/lib/account-risk-guard/state.db?mode=ro',uri=True,timeout=15)
db.row_factory=sqlite3.Row
print(json.dumps([dict(row) for row in db.execute('SELECT account_id,email,state,managed_quarantine,quarantine_reason,last_classification,pool_risk_strikes,sso_bot_flag_source,pool_verified_at FROM accounts')]))
'''
    rows = json.loads(subprocess.check_output([
        'docker', 'exec', 'grok2api-account-risk-guard', 'python', '-c', query], text=True, timeout=45))
    return {str(row['account_id']): row for row in rows}


class Admin:
    def __init__(self, config):
        from grok2api_client import Grok2APIClient
        self.config = config
        self.client_type = Grok2APIClient
        self.lock = threading.Lock()
        self.token = ''
        self.local = threading.local()

    def client(self):
        if not hasattr(self.local, 'client'):
            self.local.client = self.client_type.from_config(self.config, import_timeout=240)
        return self.local.client

    def access_token(self, rejected=''):
        from sso_to_auth_json import decode_jwt_payload
        with self.lock:
            expires = decode_jwt_payload(self.token).get('exp', 0) if self.token else 0
            if not self.token or expires < time.time() + 90 or rejected == self.token:
                self.token = self.client().login(force=True)
            return self.token

    def request(self, method, path, **kwargs):
        for attempt in range(2):
            token = self.access_token()
            response = self.client().session.request(
                method, self.client().base_url + '/api/admin/v1' + path,
                headers={'Authorization': 'Bearer ' + token}, timeout=90, **kwargs)
            if response.status_code == 401 and attempt == 0:
                response.close()
                self.access_token(rejected=token)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError('admin request unauthorized')

    def get_account(self, account_id):
        response = self.request('GET', '/accounts/' + str(account_id))
        payload = response.json()
        return payload.get('data', payload)

    def import_file(self, path):
        self.client()._access_token = self.access_token()
        return self.client().import_auth_file(path, format_name='grok_build')


def prepare(admin):
    if (REPORT / 'plan.json').exists():
        print('existing_plan_preserved', flush=True)
        return
    original = json.loads((PREVIOUS / 'inventory.json').read_text())['invalid']
    current = []
    for page in range(1, 101):
        response = admin.request('GET', '/accounts', params={'provider': 'grok_build', 'page': page, 'pageSize': 500})
        payload = response.json()
        data = payload.get('data', payload)
        current.extend(data['items'])
        if len(data['items']) < int(data.get('pageSize') or 500):
            break
    else:
        raise RuntimeError('account pagination limit')
    by_id = {str(row['id']): row for row in current}
    guards = guard_state()
    rows = []
    for original_row in original:
        account_id = str(original_row['id'])
        row = by_id.get(account_id)
        planned = {'id': account_id, 'email': original_row['email'], 'local_login_source': original_row['local_login_source']}
        if row is None:
            planned['skip'] = 'deleted_by_operator'
        else:
            planned['before'] = row
            if not row['enabled'] or guards.get(account_id, {}).get('managed_quarantine'):
                planned['skip'] = 'disabled_or_quarantined'
            elif row.get('buildBotFlagged'):
                planned['skip'] = 'bot_flagged'
        rows.append(planned)
    write_json(REPORT / 'accounts.before.json', current)
    write_json(REPORT / 'guard.before.json', guards)
    ids = [row['id'] for row in rows if 'before' in row]
    for offset in range(0, len(ids), 500):
        response = admin.request('POST', '/accounts/export', json={'provider': 'grok_build', 'ids': ids[offset:offset + 500]})
        write_json(REPORT / f'credentials.before-{offset:05d}.json', response.json())
    write_json(REPORT / 'plan.json', {'created_at': time.time(), 'accounts': rows})
    print(json.dumps({'planned': len(rows), 'pre_skipped': dict(collections.Counter(row['skip'] for row in rows if row.get('skip'))),
                      'credential_backup_files': len(list(REPORT.glob('credentials.before-*.json')))}), flush=True)


def find_sso(row, admin):
    from sso_to_auth_json import parse_sso_line
    email = str(row['email']).strip().lower()
    source = row.get('local_login_source')
    if source:
        for line in Path(source['path']).read_text(errors='replace').splitlines():
            record = parse_sso_line(line)
            if record and str(record.email).strip().lower() == email:
                return record.sso
    for prefix in ('grok-web-', 'grok-console-'):
        path = DATA / 'grok2api_auth' / (prefix + email + '.json')
        if path.is_file():
            document = json.loads(path.read_text())
            values = document.get('accounts') or []
            if isinstance(values, dict):
                values = list(values.values())
            for value in values:
                if str(value.get('email') or '').strip().lower() == email and value.get('sso_token'):
                    return value['sso_token']
    linked = row.get('before', {}).get('linkedAccounts') or []
    web_ids = [str(value['id']) for value in linked if value.get('provider') == 'grok_web']
    if not web_ids:
        response = admin.request('GET', '/accounts', params={'provider': 'grok_web', 'search': email, 'pageSize': 100})
        payload = response.json()
        values = payload.get('data', payload)['items']
        web_ids = [str(value['id']) for value in values if str(value.get('email') or '').lower() == email]
    if web_ids:
        response = admin.request('POST', '/accounts/export', json={'provider': 'grok_web', 'ids': web_ids})
        document = response.json()
        write_json(REPORT / ('web-source-' + row['id'] + '.json'), document)
        values = document.get('accounts') or []
        if isinstance(values, dict):
            values = list(values.values())
        for value in values:
            if value.get('sso_token'):
                return value['sso_token']
    return ''


def run(admin, workers, limit):
    import grok_register_ttk as app
    from sso_to_auth_json import decode_jwt_payload, inspect_token_bundle_bfs, sso_to_token, token_to_grok2api_account

    app.load_config()
    rows = json.loads((REPORT / 'plan.json').read_text())['accounts']
    receipt_path = REPORT / 'receipts.json'
    receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else []
    finished = {row['id'] for row in receipts}
    rows = [row for row in rows if row['id'] not in finished]
    if limit:
        rows = rows[:limit]
    guards = guard_state()
    guard_lock = threading.Lock()
    guard_updated = time.monotonic()
    receipt_lock = threading.Lock()
    import_lock = threading.BoundedSemaphore(4)
    stop = threading.Event()
    samples = {str(row['account_id']): row for row in json.loads((PREVIOUS / 'samples.json').read_text()) if row.get('success')}

    def live_guard(account_id):
        nonlocal guards, guard_updated
        with guard_lock:
            if time.monotonic() - guard_updated > 20:
                guards = guard_state()
                guard_updated = time.monotonic()
            return guards.get(account_id, {})

    def save_receipt(result):
        with receipt_lock:
            receipts.append(result)
            write_json(receipt_path, receipts)
            summary = {'updated_at': time.time(), 'planned': len(json.loads((REPORT / 'plan.json').read_text())['accounts']),
                       'processed': len(receipts), 'workers': workers,
                       'results': dict(collections.Counter(row['status'] for row in receipts)),
                       'new_accounts_created': sum(row.get('created', 0) for row in receipts),
                       'guard_policy_unchanged': True, 'running': True}
            write_json(REPORT / 'summary.json', summary)
            if len(receipts) <= 3 or len(receipts) % 25 == 0 or result['status'] == 'fatal':
                print(json.dumps(summary, ensure_ascii=False), flush=True)

    def recover(item):
        index, row = item
        account_id = row['id']
        result = {'id': account_id, 'email': row['email'], 'started_at': time.time()}
        worker_id = 950000 + index
        messages = []
        token_path = REPORT / ('token-' + account_id + '.json')
        import_path = REPORT / ('import-' + account_id + '.json')
        try:
            if stop.is_set():
                return
            if row.get('skip'):
                result['status'] = row['skip']
                return
            current = admin.get_account(account_id)
            if current.get('provider') != 'grok_build' or str(current.get('email') or '').lower() != str(row['email']).lower():
                result['status'] = 'identity_changed'
                return
            if not current.get('enabled') or live_guard(account_id).get('managed_quarantine') or current.get('buildBotFlagged'):
                result['status'] = 'disabled_or_quarantined'
                return
            if current.get('authStatus') == 'active':
                result['status'] = 'already_active'
                return
            sso = find_sso(row, admin)
            if not sso:
                result['status'] = 'missing_login_source'
                return
            token = None
            if token_path.exists():
                token = json.loads(token_path.read_text())
            elif account_id in samples:
                token = json.loads((PREVIOUS / samples[account_id]['private_token_file']).read_text())
            if token and decode_jwt_payload(token.get('access_token', '')).get('exp', 0) < time.time() + 300:
                token = None
            if token is None:
                proxy = app.pick_proxy_for_worker(worker_id, index)
                if not proxy:
                    raise RuntimeError('no configured proxy')
                for attempt in range(2):
                    result['oauth_attempts'] = attempt + 1
                    token = sso_to_token(sso, proxy=proxy, prefer='auth_code', allow_fallback=False,
                                         log=lambda message: messages.append(str(message)))
                    if token and token.get('access_token'):
                        break
                    if any('sso 无效' in message or '被服务端拒绝' in message for message in messages):
                        break
                    if attempt == 0:
                        time.sleep(2)
                if not token or not token.get('access_token') or not token.get('refresh_token'):
                    result['status'] = 'sso_rejected' if any('sso 无效' in message for message in messages) else 'authorization_failed'
                    result['stage_counts'] = {stage: sum(stage in message for message in messages)
                                              for stage in ('网络错误', 'timeout', '403', '429', '被服务端拒绝', 'token HTTP', 'consent 失败')}
                    return
                write_json(token_path, token)
            claims = decode_jwt_payload(token['access_token'])
            identity = str(claims.get('sub') or claims.get('principal_id') or '')
            if not identity or not current.get('userId') or identity != current['userId']:
                result['status'] = 'token_identity_mismatch'
                return
            if str(claims.get('team_id') or '') != str(current.get('teamId') or ''):
                result['status'] = 'token_team_mismatch'
                return
            metadata = inspect_token_bundle_bfs(access_token=token['access_token'], sso=sso,
                                                 id_token=token.get('id_token', ''), refresh_token=token.get('refresh_token', ''))
            if metadata.get('has_bfs'):
                result['status'] = 'new_token_bot_flagged'
                return
            document = token_to_grok2api_account(token, row['email'])
            document['name'] = current['name']
            write_json(import_path, {'provider': 'grok_build', 'accounts': [document]})
            with import_lock:
                if stop.is_set():
                    return
                latest = admin.get_account(account_id)
                if not latest.get('enabled') or live_guard(account_id).get('managed_quarantine') or latest.get('buildBotFlagged'):
                    result['status'] = 'disabled_or_quarantined'
                    return
                if latest.get('authStatus') == 'active':
                    result['status'] = 'already_active'
                    return
                payload = admin.import_file(import_path)
                result['created'] = int(payload.get('created') or 0)
                result['updated'] = int(payload.get('updated') or 0)
                result['failed'] = int(payload.get('failed') or 0)
                result['sync_failed'] = int(payload.get('syncFailed') or 0)
                result['imported_ids'] = [str(value) for value in payload.get('accountIds') or []]
                if result['created'] or result['updated'] != 1 or (result['imported_ids'] and result['imported_ids'] != [account_id]):
                    stop.set()
                    result['status'] = 'fatal'
                    result['failure_reason'] = 'import_did_not_update_exact_original_account'
                    return
                after = admin.get_account(account_id)
                result['auth_status_after'] = after['authStatus']
                result['enabled_after'] = after['enabled']
                result['status'] = 'reauthorized' if after['authStatus'] == 'active' else 'import_not_active'
                if result['status'] == 'reauthorized':
                    auth_path = DATA / 'grok2api_auth' / ('g2a-' + row['email'] + '.json')
                    if auth_path.exists():
                        backup = REPORT / 'local-auth.before' / auth_path.name
                        backup.parent.mkdir(mode=0o700, exist_ok=True)
                        if not backup.exists():
                            shutil.copyfile(auth_path, backup)
                    write_json(auth_path, {'provider': 'grok_build', 'accounts': [document]})
                import_path.unlink(missing_ok=True)
        except Exception as error:
            status_code = getattr(getattr(error, 'response', None), 'status_code', None)
            result['status'] = 'deleted_by_operator' if status_code == 404 else 'error'
            result['error_type'] = type(error).__name__
            if status_code:
                result['http_status'] = status_code
        finally:
            app.release_proxy_lease(worker_id)
            if result.get('status'):
                result['finished_at'] = time.time()
                save_receipt(result)

    print(json.dumps({'starting': len(rows), 'workers': workers, 'already_processed': len(receipts)}), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(recover, enumerate(rows, 1)))
    summary_path = REPORT / 'summary.json'
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary.update({'running': False, 'finished_at': time.time(), 'stopped_on_identity_error': stop.is_set()})
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    os.umask(0o077)
    REPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (REPORT / 'run.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'run'))
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    admin = Admin(json.loads((ROOT / 'config.json').read_text()))
    if args.mode == 'prepare':
        prepare(admin)
    else:
        run(admin, args.workers, args.limit)


if __name__ == '__main__':
    main()
