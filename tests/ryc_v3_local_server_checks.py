"""Public HTTP behavior checks for the localhost-only adapters."""
from __future__ import annotations

import json
import copy
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ryc_policy_vertical_slice import create_decision_run_export
from ryc_policy_vertical_slice_checks import cash_case, manifest, policy
from ryc_holder_baseline_builder import create_holder_baseline_export
from ryc_holder_baseline_builder_checks import request_fixture
from ryc_holder_presentation import present_holder_baseline

ROOT = Path(__file__).resolve().parent


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class LocalServerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT/'ryc_v3_local_server.py'), '--port', str(cls.port)],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                with urlopen(f'http://127.0.0.1:{cls.port}/api/health', timeout=.2) as response:
                    if response.status == 200:
                        return
            except (URLError, TimeoutError):
                time.sleep(.05)
        cls.process.terminate()
        raise RuntimeError('Local test server did not become healthy')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)

    def post(self, path, payload):
        request = Request(f'http://127.0.0.1:{self.port}{path}',
                          data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def post_bytes(self, path, body):
        request = Request(f'http://127.0.0.1:{self.port}{path}', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_ledger_terminal_assessment_runs_through_python_http_adapter(self):
        case = dict(scope='SYNTHETIC_ONLY', currency='VND',
                    decision_at='2026-01-01T10:00:00+07:00',
                    terminal_at='2026-01-10T10:00:00+07:00',
                    initial_cash_vnd=600, initial_positions={},
                    cash_rate_decimal='0', events=[])
        status, result = self.post('/api/v3/ledger-assess', {
            'case': case,
            'mandate': {'mode': 'FULL_CASH_EXIT', 'minimum_cash_vnd': 500},
        })
        self.assertEqual(status, 200)
        self.assertEqual(result['analysis_version'], 'RYC-P2-TERMINAL-ASSESSMENT-1')
        self.assertEqual(result['status'], 'CASH_EXIT_SATISFIED')
        self.assertEqual(result['objective_cash_vnd'], '600')
        self.assertEqual(len(result['ledger_bundle_sha256']), 64)

    def test_holder_baseline_http_matches_direct_builder(self):
        declared = request_fixture()
        expected = create_holder_baseline_export(declared)
        status, result = self.post('/api/v3/holder-baseline',
                                   {'request': declared})
        self.assertEqual(status, 200)
        self.assertEqual(result, expected)
        status, replay = self.post('/api/v3/holder-baseline-replay',
                                   {'export': result})
        self.assertEqual(status, 200)
        self.assertEqual(replay['status'], 'HOLDER_BASELINE_REPLAY_VERIFIED')
        self.assertEqual(replay['holder_run_id'], result['holder_run_id'])

    def test_holder_baseline_http_fails_closed_on_contract_error(self):
        status, result = self.post('/api/v3/holder-baseline',
                                   {'request': request_fixture(), 'extra': True})
        self.assertEqual(status, 422)
        self.assertEqual(result['error'], 'HOLDER_BASELINE_FIELDS')

        declared = request_fixture()
        declared['sell_now']['net_proceeds_vnd'] = 999
        status, result = self.post('/api/v3/holder-baseline',
                                   {'request': declared})
        self.assertEqual(status, 422)
        self.assertEqual(result['error'], 'SALE_NET_IDENTITY_MISMATCH')

    def test_holder_presentation_http_matches_verified_direct_view(self):
        exported = create_holder_baseline_export(request_fixture())
        expected = present_holder_baseline(exported)
        status, result = self.post('/api/v3/holder-presentation',
                                   {'export': exported})
        self.assertEqual(status, 200)
        self.assertEqual(result, expected)
        tampered = copy.deepcopy(exported)
        tampered['request']['sell_now']['fees_vnd'] = 0
        status, result = self.post('/api/v3/holder-presentation',
                                   {'export': tampered})
        self.assertEqual(status, 422)
        self.assertEqual(result['error'], 'REQUEST_HASH_MISMATCH')

    def test_policy_evaluate_round_trip_matches_direct_core(self):
        worlds = [dict(world_id='w', probability='1', observation_label='same',
                       action_cases={'cash': cash_case(100)})]
        declared = manifest(worlds, [policy('cash', 'cash')])
        expected = create_decision_run_export(declared)

        status, result = self.post('/api/v3/policy-evaluate', {'manifest': declared})
        self.assertEqual(status, 200)
        self.assertEqual(result, expected)

        replay_status, replay = self.post('/api/v3/policy-replay', {'export': result})
        self.assertEqual(replay_status, 200)
        self.assertEqual(replay['status'], 'REPLAY_VERIFIED')
        self.assertEqual(replay['result'], expected['result'])

    def test_policy_replay_rejects_tamper_and_evaluate_rejects_unknown_fields(self):
        worlds = [dict(world_id='w', probability='1', observation_label='same',
                       action_cases={'cash': cash_case(100)})]
        exported = create_decision_run_export(manifest(worlds, [policy('cash', 'cash')]))
        tampered = copy.deepcopy(exported)
        tampered['result']['selected_policy']['expected_objective_cash_vnd'] = '999'

        status, result = self.post('/api/v3/policy-replay', {'export': tampered})
        self.assertEqual(status, 422)
        self.assertEqual(result['error'], 'RESULT_HASH_MISMATCH')
        status, result = self.post('/api/v3/policy-evaluate',
                                   {'manifest': exported['manifest'], 'extra': True})
        self.assertEqual(status, 422)
        self.assertEqual(result['error'], 'POLICY_EVALUATE_FIELDS')

        stale = copy.deepcopy(exported)
        stale['manifest']['worlds'][0]['action_cases']['cash']['initial_cash_vnd'] = 101
        stale['manifest_sha256'] = create_decision_run_export(stale['manifest'])['manifest_sha256']
        # Keep the old cached result and old run ID: replay must not return it.
        status, result = self.post('/api/v3/policy-replay', {'export': stale})
        self.assertEqual(status, 422)
        self.assertEqual(result['error'], 'DECISION_RUN_ID_MISMATCH')

    def test_policy_endpoint_rejects_oversized_body(self):
        status, result = self.post_bytes('/api/v3/policy-evaluate', b'x' * 1_000_001)
        self.assertEqual(status, 413)
        self.assertEqual(result['error'], 'REQUEST_BODY_TOO_LARGE')

        status, result = self.post_bytes('/api/v3/policy-evaluate', b'{')
        self.assertEqual(status, 400)
        self.assertEqual(result['error'], 'INVALID_JSON')


if __name__ == '__main__':
    unittest.main(verbosity=2)
