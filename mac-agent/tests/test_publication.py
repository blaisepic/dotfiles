import unittest
from mac_agent.publication import verify_pull_request

class PublicationTests(unittest.TestCase):
    def record(self):
        return {'html_url': 'https://github.com/owner/repo/pull/1', 'state': 'open', 'user': {'type': 'Bot'}, 'head': {'ref': 'agent/abcdef123456', 'sha': 'a' * 40, 'repo': {'full_name': 'owner/repo'}}, 'base': {'ref': 'main', 'repo': {'full_name': 'owner/repo'}}}

    def test_rejects_pr_head_that_differs_from_worker_commit(self):
        record = self.record()
        with self.assertRaises(ValueError):
            verify_pull_request('owner/repo', 'agent/abcdef123456', 'main',
                                expected_commit='b' * 40, request=lambda *_: [record])
        result = verify_pull_request('owner/repo', 'agent/abcdef123456', 'main',
                                     expected_commit='a' * 40, request=lambda *_: [record])
        self.assertTrue(result['publication_verified'])
        self.assertEqual(result['published_commit'], 'a' * 40)

    def test_verifies_exact_task_pr_and_bot_identity(self):
        result = verify_pull_request('owner/repo', 'agent/abcdef123456', 'main', request=lambda *_: [self.record()])
        self.assertTrue(result['publication_verified'])
        self.assertEqual(result['pr_url'], 'https://github.com/owner/repo/pull/1')

    def test_accepts_exact_closed_pr_as_historical_publication(self):
        record = self.record()
        record['state'] = 'closed'
        record['merged_at'] = '2026-10-24T00:00:00Z'
        seen = []
        def request(url, token):
            seen.append(url)
            return [record]
        result = verify_pull_request('owner/repo', 'agent/abcdef123456', 'main', request=request)
        self.assertIn('state=all', seen[0])
        self.assertTrue(result['publication_verified'])
        self.assertEqual(result['pr_state'], 'closed')
        self.assertTrue(result['merged'])

    def test_rejects_absent_or_wrong_repository_pr(self):
        with self.assertRaises(ValueError):
            verify_pull_request('owner/repo', 'agent/abcdef123456', 'main', request=lambda *_: [])
        record = self.record()
        record['head']['repo']['full_name'] = 'other/repo'
        with self.assertRaises(ValueError):
            verify_pull_request('owner/repo', 'agent/abcdef123456', 'main', request=lambda *_: [record])

    def test_rejects_wrong_branch_or_external_url(self):
        for field in ('branch', 'url'):
            record = self.record()
            if field == 'branch': record['head']['ref'] = 'main'
            else: record['html_url'] = 'https://example.com/stolen'
            with self.assertRaises(ValueError):
                verify_pull_request('owner/repo', 'agent/abcdef123456', 'main', request=lambda *_: [record])
