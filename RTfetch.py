import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests


def load_config(path: Path):
    raw = path.read_text(encoding='utf-8')
    lines = [line for line in raw.splitlines() if not line.strip().startswith('//')]
    return json.loads('\n'.join(lines))


def build_token_settings(oauth2_cfg):
    oauth2_cfg = oauth2_cfg or {}
    client_id = str(oauth2_cfg.get('client_id') or '').strip()
    redirect_uri = str(oauth2_cfg.get('redirect_url') or oauth2_cfg.get('redirect_uri') or 'http://localhost').strip()
    tenant = str(oauth2_cfg.get('tenant') or oauth2_cfg.get('authority') or 'common').strip().strip('/') or 'common'

    scopes = oauth2_cfg.get('Scopes') or oauth2_cfg.get('scopes') or []
    if isinstance(scopes, str):
        auth_scope = scopes.strip()
    else:
        auth_scope = ' '.join(str(s).strip() for s in scopes if str(s).strip())

    graph_scopes = []
    imap_scopes = []
    common_scopes = []
    for item in auth_scope.split():
        low = item.lower()
        if 'graph.microsoft.com/' in low:
            graph_scopes.append(item)
        elif 'outlook.office.com/' in low:
            imap_scopes.append(item)
        else:
            common_scopes.append(item)

    rt_scope = str(oauth2_cfg.get('rt_scope') or oauth2_cfg.get('RTScope') or oauth2_cfg.get('rtScope') or 'graph').strip().lower()
    if rt_scope == 'imap':
        selected = imap_scopes
    else:
        rt_scope = 'graph'
        selected = graph_scopes

    token_scope = ' '.join(common_scopes + selected).strip() or auth_scope
    token_url = f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'
    return client_id, redirect_uri, token_scope, token_url, rt_scope


def extract_code(text: str):
    text = (text or '').strip()
    if not text:
        return ''
    if text.startswith('http://') or text.startswith('https://'):
        query = parse_qs(urlparse(text).query)
        vals = query.get('code') or []
        return vals[0].strip() if vals else ''
    return text


def main():
    root = Path(__file__).resolve().parent
    config = load_config(root / 'config.json')
    client_id, redirect_uri, token_scope, token_url, rt_scope = build_token_settings(config.get('oauth2'))

    print(f'RT scope mode: {rt_scope}')
    callback = input('Paste localhost callback URL: ').strip()
    code = extract_code(callback)
    if not code:
        raise SystemExit('No code found in input.')

    response = requests.post(
        token_url,
        data={
            'client_id': client_id,
            'code': code,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
            'scope': token_scope,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30,
    )

    try:
        data = response.json()
    except Exception:
        response.raise_for_status()
        raise SystemExit('Token endpoint did not return JSON.')

    if response.status_code >= 400 or 'refresh_token' not in data:
        msg = data.get('error_description') or data.get('error') or response.text
        raise SystemExit(f'Token exchange failed: {msg}')

    print('\nrefresh_token:\n')
    print(data['refresh_token'])


if __name__ == '__main__':
    main()
