import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bellhaven as b


def location(**changes):
    value = {'name': 'Bellhaven of Testville', 'street': '10 West Main Street', 'city': 'Testville', 'state': 'OH', 'zip': '44000', 'care_offerings': ['Assisted Living'], 'phone': '555-0100', 'source_url': b.BASE + '/communities/test'}
    return dict(value, **changes)


def account(**changes):
    value = dict(b.canonical(location(), 'parent'), account_id='facility', parent_name='Bellhaven Senior Living (Parent Account)', phone='555-0100', lifetime_revenue=0, outstanding_ar=0, chow_current_account='', duplicate_of_account='', note='')
    return dict(value, **changes)


PARENT = account(account_id='parent', name='Bellhaven Senior Living (Parent Account)', parent_id='')


class FakeClient:
    def __init__(self, accounts):
        self.rows = copy.deepcopy(accounts)
        self.writes = []
        self.fail_pointer_once = False

    def accounts(self):
        return copy.deepcopy(self.rows)

    def account(self, account_id):
        return copy.deepcopy(next(a for a in self.rows if a['account_id'] == account_id))

    def patch(self, account_id, payload):
        if self.fail_pointer_once and 'chow_current_account' in payload:
            self.fail_pointer_once = False
            raise RuntimeError('Interrupted before pointer update')
        self.writes.append(('PATCH', account_id, payload))
        next(a for a in self.rows if a['account_id'] == account_id).update(payload)

    def request(self, path, payload, method):
        self.writes.append((method, path, payload))
        value = account(**payload, account_id='created' + str(len(self.rows)))
        self.rows.append(value)
        return copy.deepcopy(value)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = patch.object(b, 'STATE', Path(self.temp.name))
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def save(self, item):
        with b.db() as conn:
            conn.execute('INSERT INTO proposals(id,body) VALUES (?,?)', (item['id'], json.dumps(item)))

    def test_address_normalization(self):
        self.assertEqual(b.norm('1250 Northwest Franklin Street'), b.norm('1250 NW Franklin St'))
        self.assertEqual(b.norm('3313 Wilmington Pike'), b.norm('3313 Wilmington Pk'))

    def test_parse_detail(self):
        html = '<h1>A &amp; B</h1><dt>Address</dt><dd>10 Main St<br>Testville, OH 44000</dd><dt>Care Offerings</dt><dd><span>Assisted Living</span><span>Memory Support</span></dd>'
        loc = b.location_from_html('/communities/test', html)
        self.assertEqual(loc['name'], 'A & B')
        self.assertEqual(loc['care_offerings'], ['Assisted Living', 'Memory Support'])

    def test_complete_crawl_includes_homepage_only_location(self):
        detail = '<h1>Test</h1><dt>Address</dt><dd>10 Main St<br>Testville, OH 44000</dd><dt>Care Offerings</dt><dd><span>Assisted Living</span></dd>'
        class Web:
            def request(self, path, auth=False):
                return {'/': '<a href="/communities/featured">Featured</a>', '/communities': '1 communities listed<a href="/communities/listed">Listed</a>'}.get(path, detail)
        locations, summary = b.scrape(Web())
        self.assertEqual(len(locations), 2)
        self.assertEqual(summary['directory_count'], 1)

    def test_incomplete_crawl_blocks_pipeline(self):
        class Web:
            def request(self, path, auth=False):
                return '2 communities listed<a href="/communities/only-one">One</a>'
        with self.assertRaisesRegex(ValueError, 'Incomplete directory'):
            b.scrape(Web())

    def test_confident_match_has_no_proposal(self):
        proposals, matches = b.plan([location()], [PARENT, account()])
        self.assertEqual(proposals, [])
        self.assertEqual(len(matches), 1)

    def test_name_alone_does_not_match(self):
        proposals, _ = b.plan([location()], [PARENT, account(parent_id='other', billing_street='999 Elsewhere', billing_city='Denver', billing_state='CO', billing_zip='80000')])
        self.assertEqual([p['kind'] for p in proposals], ['ambiguous', 'create'])

    def test_wrong_zip_gets_corrected(self):
        proposals, _ = b.plan([location()], [PARENT, account(billing_zip='44099')])
        self.assertEqual(proposals[0]['desired'], {'billing_zip': '44000'})

    def test_billing_matrix(self):
        for revenue, ar, kind in [(0,0,'update'), (100,0,'update'), (0,20,'update'), (100,20,'chow')]:
            with self.subTest(revenue=revenue, ar=ar):
                proposals, _ = b.plan([location()], [PARENT, account(parent_id='old', lifetime_revenue=revenue, outstanding_ar=ar)])
                self.assertEqual(proposals[0]['kind'], kind)

    def test_unknown_billing_fails_closed(self):
        for value in [None, 'NaN', -1]:
            with self.assertRaises(ValueError):
                b.financial(account(outstanding_ar=value))

    def test_chow_preserves_old_account(self):
        old = account(parent_id='old', lifetime_revenue=200, outstanding_ar=30)
        client = FakeClient([PARENT, old])
        item = b.plan([location()], client.accounts())[0][0]
        self.save(item)
        result = b.decide(client, item['id'], 'approve', 'Reviewer', 'Address agrees; preserve open billing.')
        after = client.account('facility')
        self.assertEqual(dict(after, chow_current_account=''), old)
        self.assertEqual(result['result']['parent_id'], 'parent')
        self.assertEqual(after['chow_current_account'], result['result']['account_id'])

    def test_chow_retry_does_not_create_twice(self):
        client = FakeClient([PARENT, account(parent_id='old', lifetime_revenue=200, outstanding_ar=30)])
        item = b.plan([location()], client.accounts())[0][0]
        self.save(item)
        client.fail_pointer_once = True
        with self.assertRaises(RuntimeError):
            b.decide(client, item['id'], 'approve', 'Reviewer', 'Preserve billing.')
        b.decide(client, item['id'], 'approve', 'Reviewer', 'Resume reviewed operation.')
        self.assertEqual(sum(w[0] == 'POST' for w in client.writes), 1)

    def test_uncertain_post_is_not_retried(self):
        client = FakeClient([PARENT])
        item = b.plan([location()], client.accounts())[0][0]
        self.save(item)
        with b.db() as conn:
            conn.execute('INSERT INTO operations VALUES (?,?,?)', (item['id'], 'creating', None))
        with self.assertRaisesRegex(ValueError, 'uncertain outcome'):
            b.decide(client, item['id'], 'approve', 'Reviewer', 'New location.')
        self.assertEqual(client.writes, [])

    def test_daily_run_preserves_partial_chow_for_recovery(self):
        client = FakeClient([PARENT, account(parent_id='old', lifetime_revenue=200, outstanding_ar=30)])
        item = b.plan([location()], client.accounts())[0][0]
        self.save(item)
        client.fail_pointer_once = True
        with self.assertRaises(RuntimeError):
            b.decide(client, item['id'], 'approve', 'Reviewer', 'Preserve billing.')
        with patch.object(b, 'scrape', return_value=([location()], {'total_locations': 1})):
            result = b.pipeline(client)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(b.review_data()['proposals'][0]['status'], 'pending')
        b.decide(client, item['id'], 'approve', 'Reviewer', 'Resume reviewed operation.')
        self.assertEqual(sum(w[0] == 'POST' for w in client.writes), 1)

    def test_address_format_suggestion_is_explicit_and_deduplicated(self):
        client = FakeClient([PARENT, account(billing_street='10 W Main St')])
        self.assertEqual(b.plan([location()], client.accounts())[0], [])
        with patch.object(b, 'scrape', return_value=([location()], {})):
            first = b.suggest_address(client, 'facility')
            b.decide(client, first['proposal_id'], 'reject', 'Reviewer', 'Keep abbreviation.')
            second = b.suggest_address(client, 'facility')
        self.assertEqual(first, second)
        self.assertEqual(len(b.review_data()['proposals']), 1)
        self.assertEqual(client.writes, [])

    def test_rejection_never_writes_and_is_not_reproposed(self):
        client = FakeClient([PARENT])
        crawl = ([location()], {'total_locations': 1})
        with patch.object(b, 'scrape', return_value=crawl):
            b.pipeline(client)
            item = b.review_data()['proposals'][0]
            b.decide(client, item['id'], 'reject', 'Reviewer', 'Insufficient supporting evidence.')
            self.assertEqual(b.pipeline(client)['new_proposals'], 0)
            self.assertEqual(b.pipeline(client)['pending'], 0)
        self.assertEqual(client.writes, [])

    def test_already_decided_cannot_write_again(self):
        client = FakeClient([PARENT])
        item = b.plan([location()], client.accounts())[0][0]
        self.save(item)
        b.decide(client, item['id'], 'approve', 'Reviewer', 'Verified missing location.')
        before = len(client.writes)
        with self.assertRaises(ValueError):
            b.decide(client, item['id'], 'approve', 'Reviewer', 'Retry')
        self.assertEqual(len(client.writes), before)

    def test_stale_billing_blocks_parent_change(self):
        client = FakeClient([PARENT, account(parent_id='old')])
        item = b.plan([location()], client.accounts())[0][0]
        self.save(item)
        client.rows[1]['lifetime_revenue'] = 100
        client.rows[1]['outstanding_ar'] = 50
        with self.assertRaisesRegex(ValueError, 'changed since'):
            b.decide(client, item['id'], 'approve', 'Reviewer', 'Re-parent.')
        self.assertEqual(client.writes, [])

    def test_missing_location_keeps_parent_and_billing(self):
        proposals, _ = b.plan([], [PARENT, account(lifetime_revenue=200, outstanding_ar=20)])
        self.assertEqual(proposals[0]['kind'], 'not_listed')
        self.assertEqual(proposals[0]['desired']['status'], 'Needs Review')
        self.assertNotIn('parent_id', proposals[0]['desired'])

    def test_duplicate_retains_survivor(self):
        other = account(account_id='duplicate', name='Former Operator', parent_id='old')
        proposals, _ = b.plan([location()], [PARENT, account(), other])
        self.assertEqual(proposals[0]['kind'], 'duplicate')
        self.assertEqual(proposals[0]['desired']['duplicate_of_account'], 'facility')
        self.assertEqual(proposals[0]['desired']['status'], 'Inactive')

    def test_approved_rerun_is_clear(self):
        client = FakeClient([PARENT, account(parent_id='old')])
        with patch.object(b, 'scrape', return_value=([location()], {'total_locations': 1})):
            b.pipeline(client)
            item = b.review_data()['proposals'][0]
            b.decide(client, item['id'], 'approve', 'Reviewer', 'Address confirmed.')
            self.assertEqual(b.pipeline(client)['pending'], 0)
            self.assertEqual(b.pipeline(client)['new_proposals'], 0)


if __name__ == '__main__':
    unittest.main()
