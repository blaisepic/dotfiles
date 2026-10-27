"""Read back an exact task PR. Process exit or a printed URL is not publication proof."""
import json
import re
import urllib.parse
import urllib.request


def _request(url, token):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'mac-agent', 'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        return json.loads(response.read())


def verify_pull_request(repository, branch, base_branch, token=None, request=None, expected_commit=None):
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'agent/[0-9a-f]{12}', branch):
        raise ValueError('Invalid publication identity')
    query = urllib.parse.urlencode({'head': repository.split('/')[0] + ':' + branch, 'base': base_branch, 'state': 'all'})
    records = (request or _request)('https://api.github.com/repos/' + repository + '/pulls?' + query, token)
    matches = []
    for record in records:
        head, base = record.get('head', {}), record.get('base', {})
        if (record.get('state') in ('open', 'closed') and head.get('ref') == branch and base.get('ref') == base_branch
                and head.get('repo', {}).get('full_name', '').lower() == repository.lower()
                and base.get('repo', {}).get('full_name', '').lower() == repository.lower()
                and record.get('user', {}).get('type') == 'Bot'
                and (expected_commit is None or head.get('sha') == expected_commit)
                and re.fullmatch('https://github.com/' + re.escape(repository) + r'/pull/[0-9]+', record.get('html_url', ''))):
            matches.append(record)
    if len(matches) != 1:
        raise ValueError('No unique verified bot PR exists for this task')
    return {'publication_verified': True, 'pr_url': matches[0]['html_url'], 'published_commit': matches[0]['head'].get('sha'),
            'pr_state': matches[0]['state'], 'merged': bool(matches[0].get('merged_at'))}
