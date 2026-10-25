"""Paid-request accounting survives downstream protocol and review failures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from runtime.providers import ProviderClient, ProviderError
from runtime.telemetry import UsageLedger


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = UsageLedger(Path(self.tmp.name)/'usage.jsonl')
        self.client = ProviderClient({'base_url':'https://example.test/v1','api_key':'offline-secret','model':'fixture'},
                                     ledger=self.ledger, stage='executor')

    def test_usage_recorded_before_chat_protocol_validation(self):
        invalid = {'choices':[], 'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}}
        with mock.patch.object(self.client, '_http_request', return_value=invalid), self.assertRaises(ProviderError):
            self.client.chat([{'role':'user','content':'data'}])
        self.assertEqual(10,self.ledger.summary()['total_tokens'])

    def test_partial_success_then_failure_keeps_known_cost_and_unknown(self):
        response = {'choices':[{'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}}
        with mock.patch.object(self.client, '_http_request', side_effect=[response,ProviderError('safe',code='timeout')]):
            self.client.chat([{'role':'user','content':'data'}])
            with self.assertRaises(ProviderError):
                self.client.chat([{'role':'user','content':'data'}])
        summary=self.ledger.summary()
        self.assertIsNone(summary['total_tokens'])
        self.assertEqual(10,summary['known_totals']['total_tokens'])
        self.assertEqual(2,summary['requests_started'])
        self.assertEqual(1,summary['requests_failed'])

    def test_guard_refuses_before_start_and_before_network(self):
        self.client.request_guard=mock.Mock(side_effect=ValueError('budget stopped'))
        with mock.patch.object(self.client,'_http_request') as network, self.assertRaises(ValueError):
            self.client.chat([{'role':'user','content':'data'}])
        network.assert_not_called()
        self.assertEqual(0,self.ledger.summary()['requests_started'])


if __name__=='__main__':
    unittest.main()
