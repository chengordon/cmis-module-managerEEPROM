"""Comprehensive API tests for CMIS optical module management tool."""
import os
import re
import sys
import json
import base64
import math
import struct
import unittest
import unittest.mock

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
from app import app, _state

# Shape of a real release-asset URL. The updater refuses anything that is not
# https on a GitHub host, so fixtures have to look genuine or they would pass
# for the wrong reason.
_GH_URL = ('https://github.com/zhh198903-ctrl/cmis-module-manager/releases/'
           'download/v2.1.0/CMIS_dist_v2_1_0.zip')


def reset_state():
    """Reset global app state between tests."""
    if _state['backend'] is not None:
        try:
            _state['backend'].disconnect()
        except Exception:
            pass
    _state['backend'] = None
    _state['connected'] = False
    _state['bus'] = None
    _state['address'] = None


def connect_mock(client):
    """Helper: connect to mock backend."""
    rv = client.post('/api/connect',
                     data=json.dumps({'backend': 'mock_dr8', 'bus': 0, 'address': 80}),
                     content_type='application/json')
    assert rv.status_code == 200, f"connect failed: {rv.data}"
    return rv


# ============================================================
# Test helpers / fixture
# ============================================================

class CMISTestCase(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        reset_state()

    def tearDown(self):
        reset_state()

    # --------------------------------------------------------
    # helper assertions
    # --------------------------------------------------------
    def assertOk(self, rv, code=200):
        self.assertEqual(rv.status_code, code,
                         f"Expected {code}, got {rv.status_code}: {rv.data}")
        body = json.loads(rv.data)
        self.assertEqual(body['status'], 'ok', f"Expected ok: {body}")
        return body

    def assertErr(self, rv, code=None):
        body = json.loads(rv.data)
        self.assertEqual(body['status'], 'error', f"Expected error: {body}")
        if code is not None:
            self.assertEqual(rv.status_code, code,
                             f"Expected HTTP {code}, got {rv.status_code}")
        return body

    def connect(self):
        return connect_mock(self.client)


# ============================================================
# 1. GET /api/backends
# ============================================================

class TestVersion(CMISTestCase):

    def test_version_endpoint(self):
        body = self.assertOk(self.client.get('/api/version'))
        self.assertEqual(body['data']['version'], app_module.__version__)

    def test_version_rendered_in_ui(self):
        """The header badge and sidebar must show the real version."""
        html = self.client.get('/').data.decode('utf-8')
        self.assertIn(f'v{app_module.__version__}', html)
        self.assertNotIn('{{ version }}', html)

    def test_version_matches_manual(self):
        """The manual ships next to the EXE; a stale version there misleads users."""
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        if not os.path.exists(path):
            self.skipTest('manual not present')
        text = io.open(path, encoding='utf-8').read()
        self.assertIn(app_module.__version__, text,
                      'operation manual version is out of sync with app.__version__')


class TestMonitoringPresentation(CMISTestCase):
    """Guards for values that must not be presented as trustworthy."""

    def _js(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'app.js')
        return io.open(path, encoding='utf-8').read()

    def test_alarm_colouring_uses_the_modules_own_thresholds(self):
        """A fixed +/-10 dBm pair contradicted the module's advertised limits,
        so the monitoring table, the thresholds card and the lane flags gave
        three different answers about one lane."""
        js = self._js()
        self.assertIn('_powerLimits()', js)
        self.assertIn('tx_power_low_alarm_dbm', js)
        self.assertIn('_moduleThresholds = d', js,
                      'thresholds are read but never fed back into colouring')

    def test_thresholds_endpoint_supplies_what_colouring_needs(self):
        self.connect()
        d = self.assertOk(self.client.get('/api/module/thresholds'))['data']
        for k in ('tx_power_low_alarm_dbm', 'tx_power_high_alarm_dbm',
                  'rx_power_low_alarm_dbm', 'rx_power_high_alarm_dbm'):
            self.assertIn(k, d)
            self.assertIsInstance(d[k], (int, float))

    def test_counters_expose_pattern_sync_loss(self):
        """Counts taken while sync is lost are not a BER measurement."""
        self.connect()
        d = self.assertOk(self.client.get('/api/module/counters'))['data']
        self.assertTrue(d['lanes'], 'no lanes returned')
        for lane in d['lanes']:
            self.assertIn('host_psl', lane)
            self.assertIn('media_psl', lane)
        self.assertIn('no sync', self._js(),
                      'the UI does not surface pattern sync loss')

    def test_zero_ber_is_not_dressed_up_as_a_measured_bound(self):
        """A zero F16 word is a zero count. Rendering it as "< 1e-15" invented
        a figure with no basis - the format bottoms out at 1e-24 - and that is
        the sort of number that gets quoted in a test report."""
        js = self._js()
        self.assertNotIn('1e-15', js)
        body = js.split('function formatBer(')[1].split('\n}')[0]
        self.assertIn('not a', body, 'the zero case is unexplained')

    def test_reconnect_clears_every_panel(self):
        """Checkbox cells and the summary line are not plain tables, so a
        sweep of table bodies leaves the previous module's squelch settings and
        temperature on screen."""
        js = self._js()
        body = js.split('function clearTabContent(')[1].split('\n}\n')[0]
        for marker in ("'sq', 'sf', 'od', 'rd'", "'mso', 'msi', 'hso', 'hsi'",
                       'monitor-summary'):
            self.assertIn(marker, body, f'{marker} survives a reconnect')

    def test_diagnostics_tab_loads_every_card(self):
        js = self._js()
        block = js.split("if (name === 'diagnostics')")[1].split('\n  }')[0]
        for fn in ('loadLoopback', 'loadPrbs', 'loadBer', 'loadSnr',
                   'loadCounters', 'loadLaser'):
            self.assertIn(fn, block, f'{fn} is not loaded when the tab opens')


class TestUpdater(unittest.TestCase):
    """Update logic, exercised without touching the network."""

    def test_version_parsing_and_ordering(self):
        import updater as u
        self.assertEqual(u.parse_version('v2.0.1'), (2, 0, 1))
        self.assertEqual(u.parse_version('2.0.1-rc1'), (2, 0, 1))
        self.assertEqual(u.parse_version(''), (0,))
        self.assertTrue(u.is_newer('2.0.2', '2.0.1'))
        self.assertTrue(u.is_newer('2.1', '2.0.9'), 'shorter version mis-padded')
        self.assertFalse(u.is_newer('2.0.1', '2.0.1'))
        self.assertFalse(u.is_newer('2.0.0', '2.0.1'), 'offered a downgrade')

    def test_release_payload_normalises(self):
        import updater as u
        rel = u.normalize_release({
            'tag_name': 'v2.1.0',
            'html_url': 'https://example.invalid/r',
            'body': 'notes',
            'assets': [
                {'name': 'source.zip',
                 'browser_download_url': 'https://github.com/o/r/a/source.zip'},
                {'name': 'CMIS_dist_v2_1_0.zip', 'size': 123,
                 'digest': 'sha256:ABC',
                 'browser_download_url': _GH_URL},
            ],
        })
        self.assertEqual(rel['version'], '2.1.0')
        self.assertEqual(rel['asset_name'], 'CMIS_dist_v2_1_0.zip')
        self.assertEqual(rel['asset_url'], _GH_URL)
        self.assertEqual(rel['sha256'], 'abc')

    def test_release_without_our_asset_is_rejected(self):
        """A release carrying only source tarballs must not look installable."""
        import updater as u
        # A GitHub-hosted URL, so this can only be rejected on the asset name.
        self.assertIsNone(u.normalize_release(
            {'tag_name': 'v9.9.9', 'assets': [{'name': 'notes.txt',
                                               'browser_download_url': _GH_URL}]}))
        self.assertIsNone(u.normalize_release({'tag_name': 'v9.9.9', 'assets': []}))
        self.assertIsNone(u.normalize_release(None))

    def test_extract_rejects_paths_escaping_the_target(self):
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'evil.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr('../escaped.exe', b'x')
                zf.writestr(u.EXE_NAME, b'x')
            with self.assertRaises(ValueError):
                u.extract_payload(zp, os.path.join(tmp, 'out'))

    def test_extract_requires_the_executable(self):
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'partial.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr('manual.html', b'x')
            with self.assertRaises(ValueError):
                u.extract_payload(zp, os.path.join(tmp, 'out'))

    def test_extract_unpacks_the_whole_payload(self):
        """Manual and images travel with the exe; a new exe beside an old
        manual would document behaviour the build no longer has."""
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'ok.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr(u.EXE_NAME, b'exe')
                zf.writestr('manual.html', b'doc')
                zf.writestr('qrcode.jpg', b'img')
            out = os.path.join(tmp, 'out')
            names = u.extract_payload(zp, out)
            self.assertCountEqual(names, [u.EXE_NAME, 'manual.html', 'qrcode.jpg'])
            self.assertTrue(os.path.isfile(os.path.join(out, u.EXE_NAME)))

    def _download_harness(self, responses):
        """Drive download_asset against scripted responses, no network.

        Returns (fake_open, requests) - requests records each urllib Request so
        a test can assert which byte range was actually asked for.
        """
        import io as _io
        requests = []

        class _Resp:
            def __init__(self, body, code=200, content_length=None):
                self._buf = _io.BytesIO(body)
                self._code = code
                n = len(body) if content_length is None else content_length
                self.headers = {'Content-Length': str(n)}

            def getcode(self):
                return self._code

            def read(self, n=-1):
                return self._buf.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        scripted = list(responses)

        def fake_open(req, timeout):
            requests.append(req)
            item = scripted.pop(0)
            if isinstance(item, Exception):
                raise item
            return _Resp(*item)

        return fake_open, requests

    def _with_fake_open(self, fake_open):
        import updater as u
        original = u._open
        u._open = fake_open
        self.addCleanup(lambda: setattr(u, '_open', original))

    def test_a_dropped_connection_resumes_instead_of_starting_over(self):
        """Restarting from zero can never finish on a link that drops more
        often than a full download takes - which is what a 16 MB asset over a
        slow proxy looks like."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8          # 2048 bytes
        fake, reqs = self._download_harness([
            (payload[:800], 200, len(payload)),  # closes early
            (payload[800:], 206, None),          # honours Range
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset(_GH_URL, dest, total_hint=len(payload), retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
            self.assertFalse(os.path.exists(dest + '.part'))
        self.assertIsNone(reqs[0].get_header('Range'))
        self.assertEqual(reqs[1].get_header('Range'), 'bytes=800-',
                         'the retry must ask only for the missing bytes')

    def test_a_server_ignoring_range_restarts_rather_than_corrupting(self):
        """Appending a full body onto a partial file would produce a plausible
        archive of the wrong length; the checksum would catch it, but only
        after another full download."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8
        fake, _ = self._download_harness([
            (payload[:800], 200, len(payload)),
            (payload, 200, None),                # ignores Range, sends it all
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset(_GH_URL, dest, total_hint=len(payload), retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)

    def test_a_download_that_gets_nowhere_gives_up_and_leaves_nothing(self):
        """`attempts` bounds consecutive attempts that add nothing. A .part
        left next to the staged files would be mistaken for the asset."""
        import tempfile
        import updater as u
        fake, reqs = self._download_harness([IOError('refused')] * 3)
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            with self.assertRaises(Exception):
                u.download_asset(_GH_URL, dest, total_hint=2048,
                                 attempts=3, retry_wait=0)
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest + '.part'))
        self.assertEqual(len(reqs), 3, 'it must stop after that many dead rounds')

    def test_a_download_still_inching_forward_is_not_given_up_on(self):
        """A real 16 MB download here ran 44 minutes and died at 58% with six
        attempts spent - every one of which had transferred megabytes. The
        budget was protecting against "slow"; the only thing worth abandoning
        is a transfer that has stopped moving.
        """
        import tempfile
        import updater as u
        payload = bytes(range(250)) * 4          # 1000 bytes
        steps = [(payload[:200], 200, len(payload))]
        for a in range(200, 1000, 200):
            steps.append((payload[a:a + 200], 206, None))
        fake, reqs = self._download_harness(steps)
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset(_GH_URL, dest, total_hint=len(payload),
                             attempts=2, retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(len(reqs), 5, 'each partial delivery must buy another round')

    def test_a_source_dribbling_forever_still_hits_a_ceiling(self):
        """Progress resetting the budget must not become "never give up"."""
        import tempfile
        import updater as u
        fake, reqs = self._download_harness([(b'x', 200, 10_000)] +
                                            [(b'x', 206, None)] * 40)
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            with self.assertRaises(Exception):
                u.download_asset(_GH_URL, dest, total_hint=10_000,
                                 attempts=3, retry_wait=0, max_rounds=6)
        self.assertEqual(len(reqs), 6, 'max_rounds is the backstop')

    def test_a_definitive_http_error_is_not_retried(self):
        """A 404, or the handler's refusal to follow a redirect off GitHub,
        will not fix itself - retrying only delays the error."""
        import tempfile
        import urllib.error
        import updater as u
        err = urllib.error.HTTPError(_GH_URL, 404, 'Not Found', {}, None)
        fake, reqs = self._download_harness([err, err, err])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(urllib.error.HTTPError):
                u.download_asset(_GH_URL, os.path.join(tmp, 'a.zip'),
                                 attempts=3, retry_wait=0)
        self.assertEqual(len(reqs), 1)

    def test_the_mirror_may_carry_bytes_but_never_metadata(self):
        """The download site publishes no version list and no digest, so it is
        never asked what the newest release is - only for a copy of an asset
        GitHub already named and hashed. That split is what makes plain http
        acceptable for it."""
        import updater as u
        mirror = u.mirror_url('2.2.3', 'CMIS_dist_v2_2_3.zip')
        self.assertTrue(u.is_allowed_source(mirror))
        self.assertFalse(u.is_trusted_url(mirror),
                         'the mirror must not pass the metadata check')
        self.assertIsNone(u.normalize_release({
            'tag_name': 'v2.2.3',
            'assets': [{'name': 'CMIS_dist_v2_2_3.zip', 'size': 1,
                        'browser_download_url': mirror}],
        }), 'a release pointing at the mirror is not installable')

    def test_widening_the_source_check_did_not_open_it_to_anyone(self):
        import updater as u
        for bad in ('http://attacker.example/p.zip',
                    'https://attacker.example/p.zip',
                    'http://106.14.76.130.evil.example/p.zip',
                    'https://106.14.76.130.evil.example/p.zip'):
            self.assertFalse(u.is_allowed_source(bad), bad)
        with self.assertRaises(ValueError):
            u.download_asset(['http://attacker.example/p.zip'], 'unused.zip')

    def test_the_faster_source_is_tried_first(self):
        """Picking wrong costs minutes on a 16 MB asset, and which one wins
        changes by the hour, so it is measured rather than assumed."""
        import updater as u
        slow = _GH_URL
        fast = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, _ = self._download_harness([(b'x' * 200, 206, None),
                                          (b'y' * 400000, 206, None)])
        self._with_fake_open(fake)
        ranked = u.order_sources([slow, fast], seconds=0.05)
        self.assertEqual([url for url, _ in ranked], [fast, slow])
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_an_unreachable_source_scores_zero_but_is_still_offered(self):
        """Four seconds of silence is not proof the host is gone, and refusing
        to try it would strand the update when both probes happen to fail."""
        import updater as u
        fake, _ = self._download_harness([IOError('refused'), IOError('refused')])
        self._with_fake_open(fake)
        ranked = u.order_sources([_GH_URL, u.mirror_url('9.9.9', 'a.zip')],
                                 seconds=0.05)
        self.assertEqual(len(ranked), 2)
        self.assertEqual([rate for _, rate in ranked], [0.0, 0.0])

    def test_a_source_that_does_not_carry_the_release_falls_back(self):
        """The mirror is filled by hand, so it routinely lags a release by
        hours - a 404 there must not abort an update GitHub can serve."""
        import tempfile
        import urllib.error
        import updater as u
        payload = b'p' * 1500
        mirror = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, reqs = self._download_harness([
            urllib.error.HTTPError(mirror, 404, 'Not Found', {}, None),
            (payload, 200, None),
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset([mirror, _GH_URL], dest,
                             total_hint=len(payload), attempts=3, retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(reqs[0].full_url, mirror)
        self.assertEqual(reqs[1].full_url, _GH_URL, 'it must switch source')

    def test_switching_source_keeps_the_bytes_already_fetched(self):
        """Every mirror serves the identical asset - that is what the shared
        SHA-256 asserts - so a dead source's progress is still good."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8
        mirror = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, reqs = self._download_harness([
            (payload[:700], 200, len(payload)),   # dies partway
            (payload[700:], 206, None),           # other source finishes it
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset([mirror, _GH_URL], dest,
                             total_hint=len(payload), attempts=3, retry_wait=0)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(reqs[1].full_url, _GH_URL)
        self.assertEqual(reqs[1].get_header('Range'), 'bytes=700-',
                         'the second source must continue, not restart')

    def test_a_truncated_mirror_cannot_end_the_download_early(self):
        """The size came from the release metadata over TLS. A mirror holding
        a half-uploaded copy must not be able to talk the download into
        calling that complete."""
        import tempfile
        import updater as u
        payload = b'w' * 2000
        mirror = u.mirror_url('9.9.9', 'CMIS_dist_v9_9_9.zip')
        fake, _ = self._download_harness([
            (payload[:500], 200, 500),        # mirror claims the asset is 500 B
            (payload[500:], 206, None),
        ])
        self._with_fake_open(fake)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            u.download_asset([mirror, _GH_URL], dest,
                             total_hint=len(payload), attempts=3, retry_wait=0)
            self.assertEqual(os.path.getsize(dest), len(payload))

    def test_a_kept_partial_lets_a_later_run_carry_on(self):
        """Retries alone still lose everything once they run out. On a link
        that drops this often, the bytes already fetched are the only thing
        that makes the next attempt shorter than the last."""
        import tempfile
        import updater as u
        payload = bytes(range(256)) * 8
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            part = os.path.join(tmp, 'keep', 'a.zip.part')

            fake, _ = self._download_harness([(payload[:900], 200, len(payload))])
            self._with_fake_open(fake)
            with self.assertRaises(Exception):
                u.download_asset(_GH_URL, dest, total_hint=len(payload),
                                 attempts=1, retry_wait=0,
                                 part_path=part, keep_partial=True)
            self.assertTrue(os.path.exists(part), 'the progress must survive')
            self.assertEqual(os.path.getsize(part), 900)

            fake2, reqs2 = self._download_harness([(payload[900:], 206, None)])
            self._with_fake_open(fake2)
            u.download_asset(_GH_URL, dest, total_hint=len(payload),
                             attempts=1, retry_wait=0,
                             part_path=part, keep_partial=True)
            self.assertEqual(reqs2[0].get_header('Range'), 'bytes=900-')
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)
            self.assertFalse(os.path.exists(part))

    def test_a_partial_longer_than_the_asset_is_thrown_away(self):
        """A release rebuilt under the same name leaves a partial that is not
        a prefix of anything; resuming past it would fail the checksum on
        every future attempt, which is a loop the user cannot get out of."""
        import tempfile
        import updater as u
        payload = b'z' * 1000
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'a.zip')
            part = os.path.join(tmp, 'a.zip.part')
            with open(part, 'wb') as fh:
                fh.write(b'Q' * 4000)
            fake, reqs = self._download_harness([(payload, 200, None)])
            self._with_fake_open(fake)
            u.download_asset(_GH_URL, dest, total_hint=len(payload),
                             attempts=2, retry_wait=0, part_path=part)
            self.assertIsNone(reqs[0].get_header('Range'),
                              'it must not resume from a bogus offset')
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), payload)

    def test_partials_for_other_versions_are_dropped(self):
        """Otherwise every abandoned upgrade parks 16 MB on disk for good."""
        import tempfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            original = u.download_dir
            u.download_dir = lambda: tmp
            self.addCleanup(lambda: setattr(u, 'download_dir', original))
            for name in ('CMIS_dist_v2_2_1.zip.part', 'CMIS_dist_v2_2_2.zip.part'):
                open(os.path.join(tmp, name), 'wb').close()
            removed = u.discard_stale_partials('CMIS_dist_v2_2_2.zip')
            self.assertEqual(removed, ['CMIS_dist_v2_2_1.zip.part'])
            self.assertEqual(os.listdir(tmp), ['CMIS_dist_v2_2_2.zip.part'])

    def test_the_partial_is_not_kept_where_staging_gets_wiped(self):
        """api_update_apply rmtree's the staging directory before every
        download; a partial in there could never survive to be resumed."""
        import updater as u
        staged = os.path.normcase(os.path.abspath(u.staging_dir()))
        part = os.path.normcase(os.path.abspath(u.partial_path('CMIS_dist_v9_9_9.zip')))
        self.assertFalse(part.startswith(staged + os.sep))

    def test_a_wrapped_payload_still_stages_flat(self):
        """The layout v2.1.0 and v2.2.0 actually shipped.

        Those zips hold a CMIS2Customer/ folder rather than the four files.
        Staging kept the folder, so the swap helper found no exe where it
        looks, retried for 75 s and quit before ever relaunching - the update
        silently did nothing on every real upgrade into those two versions.
        """
        import tempfile, zipfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            zp = os.path.join(tmp, 'wrapped.zip')
            with zipfile.ZipFile(zp, 'w') as zf:
                zf.writestr('CMIS2Customer/' + u.EXE_NAME, b'exe')
                zf.writestr('CMIS2Customer/manual.html', b'doc')
                zf.writestr('CMIS2Customer/qrcode.jpg', b'img')
            out = os.path.join(tmp, 'out')
            names = u.extract_payload(zp, out)
            self.assertCountEqual(names, [u.EXE_NAME, 'manual.html', 'qrcode.jpg'])
            self.assertTrue(os.path.isfile(os.path.join(out, u.EXE_NAME)),
                            'the helper looks for the exe in the staging root')
            self.assertFalse(os.path.isdir(os.path.join(out, 'CMIS2Customer')))

    def test_extract_refuses_a_payload_it_cannot_stage_flat(self):
        """Failing loudly beats the 75 s silent stall the helper used to hit."""
        import tempfile, zipfile
        import updater as u
        for label, entries in [
            ('two levels deep', ['a/b/' + u.EXE_NAME, 'a/b/manual.html']),
            ('only some nested', [u.EXE_NAME, 'sub/manual.html']),
            ('same name twice', ['a/' + u.EXE_NAME, 'b/' + u.EXE_NAME]),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                zp = os.path.join(tmp, 'odd.zip')
                with zipfile.ZipFile(zp, 'w') as zf:
                    for n in entries:
                        zf.writestr(n, b'x')
                with self.assertRaises(ValueError, msg=label):
                    u.extract_payload(zp, os.path.join(tmp, 'out'))

    def test_an_unverifiable_download_is_refused_like_a_failed_one(self):
        """This test used to assert the opposite, and its old name said so.

        Accepting a download with no digest to check against meant an attacker
        who could strip one field got an unverified install with nothing shown
        to the user - while the manual told that user the tool refuses exactly
        this case. Every release asset this project has published carries a
        digest, so nothing that works today is refused.
        """
        import tempfile
        import updater as u
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'f.bin')
            with open(p, 'wb') as fh:
                fh.write(b'hello')
            digest = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
            self.assertFalse(u.verify_sha256(p, None), 'no digest must not pass')
            self.assertFalse(u.verify_sha256(p, ''), 'empty digest must not pass')
            self.assertFalse(u.verify_sha256(p, '00' * 32))
            self.assertTrue(u.verify_sha256(p, digest), 'a real match must pass')

    def test_swap_script_is_bounded_and_quotes_paths(self):
        """Unbounded waits would leave a hidden PowerShell spinning forever."""
        import updater as u
        ps = u.build_swap_script(r'C:\stage dir', r'C:\install dir')
        self.assertIn('-lt 150', ps, 'the unlock wait is unbounded')
        self.assertIn('-le 2', ps, 'the relaunch is retried without limit')
        self.assertIn('-lt 15', ps, 'the health wait is unbounded')
        # Single-quoted, so a path with spaces survives and one containing
        # $(...) is not evaluated - see test_install_path_cannot_inject_powershell.
        self.assertIn(r"'C:\install dir\CMIS_Module_Manager.exe'", ps)

    def test_swap_script_verifies_the_app_came_back(self):
        """Moving the files is not the same as the app running again; the
        helper must check rather than assume, and retry when it has not."""
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t')
        self.assertIn('Invoke-WebRequest', ps)
        self.assertIn(u.HEALTH_URL, ps)

    def test_relaunch_avoids_shellexecute(self):
        """Start-Process goes through ShellExecute, which needs a usable window
        station. Spawned from an exiting console app it created nothing while
        reporting no error, so the app never came back."""
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t')
        self.assertIn('[System.Diagnostics.Process]::Start', ps)
        self.assertIn('UseShellExecute = $false', ps)
        self.assertNotIn('Start-Process', ps)

    def test_swap_script_leaves_a_log(self):
        """A failed update is otherwise invisible - the app is simply gone."""
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t')
        self.assertIn(r'C:\t\update.log', ps)
        for moment in ('update helper started', 'executable replaced',
                       'relaunch attempt', 'started pid',
                       'the files are updated'):
            self.assertIn(moment, ps, f'the log never records "{moment}"')

    def test_relaunch_suppresses_the_browser(self):
        """The user is already looking at a page that reloads itself; opening
        another tab on every update is how tabs pile up."""
        import updater as u
        self.assertIn('CMIS_NO_BROWSER', u.build_swap_script(r'C:\s', r'C:\t'))

    def test_no_relaunch_leaves_the_health_check_out(self):
        import updater as u
        ps = u.build_swap_script(r'C:\s', r'C:\t', relaunch=False)
        self.assertNotIn('Start-Process', ps)

    def test_helper_keeps_a_console_so_it_can_relaunch(self):
        """Regression, found by actually upgrading a real 2.0.1 build.

        With DETACHED_PROCESS the helper has no console, so its `start` cannot
        allocate one for the console-mode exe: the files swapped correctly and
        the app simply never came back. Nothing errors, so only this assertion
        catches a reintroduction.
        """
        import io
        import updater as u
        self.assertFalse(hasattr(u, '_DETACHED_PROCESS'),
                         'DETACHED_PROCESS leaves the helper unable to relaunch')
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'updater.py')
        src = io.open(path, encoding='utf-8').read()
        body = src.split('def stage_and_swap(')[1]
        self.assertIn('creationflags=_CREATE_NO_WINDOW', body)
        self.assertNotIn('0x00000008', body)

    def test_self_update_refused_when_running_from_source(self):
        import updater as u
        self.assertFalse(u.is_frozen(), 'tests should not run frozen')
        with self.assertRaises(RuntimeError):
            u.stage_and_swap('anywhere')


class TestUpdateRoutes(CMISTestCase):

    def test_nothing_checks_or_updates_without_a_click(self):
        """The tool is used on isolated lab networks and on modules that are
        mid-measurement. It must never reach out to GitHub, and must never
        replace itself, unless the operator asked for it.
        """
        import io
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'static', 'app.js')
        js = io.open(js_path, encoding='utf-8').read()

        # checkForUpdate may only be reachable from the button.
        callers = [ln.strip() for ln in js.splitlines()
                   if 'checkForUpdate' in ln and 'function checkForUpdate' not in ln]
        self.assertEqual(len(callers), 1, f'unexpected callers: {callers}')
        self.assertIn("getElementById('btn-check-update')", callers[0])

        # No timer or load hook may drive it, and apply is only ever called
        # from inside checkForUpdate, after the user confirms.
        for pattern in ('setInterval(checkForUpdate', 'setTimeout(checkForUpdate',
                        "addEventListener('load'", 'checkForUpdate()'):
            if pattern == 'checkForUpdate()':
                continue
            self.assertNotIn(pattern, js, f'{pattern} would update unprompted')
        body = js.split('async function checkForUpdate(')[1].split('\n}')[0]
        self.assertIn('confirm(', body, 'apply runs without asking the user')
        self.assertEqual(js.count("apiPost('/api/update/apply'"), 1)

    def test_completion_screen_says_what_to_do_immediately(self):
        """Spinning first and only then admitting a restart is needed reads as
        a hang. The files are already in place by that point, so the
        instruction comes first and the reconnect poll runs behind it.
        """
        import io
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'static', 'app.js')
        js = io.open(js_path, encoding='utf-8').read()
        body = js.split('async function _waitForNewVersion(')[1].split('\n}\n')[0]
        instruction = body.index('CMIS_Module_Manager.exe')
        poll = body.index("fetch('/api/version'")
        self.assertLess(instruction, poll,
                        'the restart instruction is shown only after polling')
        self.assertIn('update.log', body, 'no pointer to the update record')

    def test_apply_refused_from_source(self):
        rv = self.client.post('/api/update/apply')
        self.assertErr(rv, 400)
        self.assertIn('git pull', json.loads(rv.data)['message'])

    def _arm_fake_update(self, download):
        """Point the update machinery at temp dirs and a scripted download.

        Never let a test reach the end of _run_update: it hands over to the
        swap helper and then os._exit()s the process, which here is the test
        runner. Every case below ends in the error branch.
        """
        import tempfile
        import updater as u
        tmp = tempfile.mkdtemp()
        rel = {'version': '9.9.9', 'asset_name': 'CMIS_dist_v9_9_9.zip',
               'asset_url': _GH_URL, 'asset_size': 10240, 'sha256': 'ab' * 32,
               'tag': 'v9.9.9', 'html_url': '', 'notes': '', 'published_at': ''}
        saved = {name: getattr(u, name) for name in
                 ('is_frozen', 'fetch_latest_release', 'order_sources',
                  'discard_stale_partials', 'staging_dir', 'partial_path',
                  'download_asset')}
        u.is_frozen = lambda: True
        u.fetch_latest_release = lambda *a, **k: dict(rel)
        u.order_sources = lambda urls, **k: [(urls[0], 1.0)]
        u.discard_stale_partials = lambda name: []
        u.staging_dir = lambda: os.path.join(tmp, '_cmis_update')
        u.partial_path = lambda name: os.path.join(tmp, 'parts', name + '.part')
        u.download_asset = download

        def restore():
            for name, fn in saved.items():
                setattr(u, name, fn)
            app_module._update.update(state='idle', version='', done=0,
                                      total=0, source='', message='')
        self.addCleanup(restore)
        return rel

    def _await_update_state(self, wanted, timeout=5.0):
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            if app_module._update['state'] in wanted:
                return app_module._update['state']
            _t.sleep(0.02)
        return app_module._update['state']

    def test_apply_returns_at_once_and_reports_progress_separately(self):
        """Downloading inside the request froze every other endpoint for as
        long as the transfer took - forty minutes on a slow link, with nothing
        on screen. The request loop has to stay free to answer the poll."""
        import threading as _th
        gate = _th.Event()

        def download(urls, dest, progress_cb=None, **kw):
            progress_cb(4096, 10240)
            gate.wait(5)
            raise IOError('link died')

        self.addCleanup(gate.set)
        self._arm_fake_update(download)

        rv = self.client.post('/api/update/apply')
        body = self.assertOk(rv)['data']
        self.assertEqual(body['version'], '9.9.9')

        self.assertEqual(self._await_update_state(('downloading',)), 'downloading')
        prog = self.assertOk(self.client.get('/api/update/progress'))['data']
        self.assertEqual(prog['state'], 'downloading')
        self.assertEqual((prog['done'], prog['total']), (4096, 10240))

        gate.set()
        self.assertEqual(self._await_update_state(('error',)), 'error')
        prog = self.assertOk(self.client.get('/api/update/progress'))['data']
        self.assertIn('link died', prog['message'])

    def test_a_second_apply_is_refused_while_one_is_running(self):
        """Two downloads into one staging directory would delete each other's
        files halfway through."""
        import threading as _th
        gate = _th.Event()

        def download(urls, dest, progress_cb=None, **kw):
            progress_cb(1, 10240)
            gate.wait(5)
            raise IOError('stopped')

        self.addCleanup(gate.set)
        self._arm_fake_update(download)
        self.assertOk(self.client.post('/api/update/apply'))
        self._await_update_state(('downloading',))
        self.assertErr(self.client.post('/api/update/apply'), 409)
        gate.set()

    def test_the_page_puts_the_download_percentage_on_screen(self):
        """A number moving is the difference between "slow" and "hung", and
        the user's response to the second one is to kill the tool."""
        import io
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'static', 'app.js')
        js = io.open(js_path, encoding='utf-8').read()
        self.assertIn("fetch('/api/update/progress'", js)
        body = js.split('async function _followUpdateProgress(')[1].split('\n}\n')[0]
        self.assertIn('done / p.total', body.replace('p.done', 'done'))
        self.assertIn('%', body)

    def test_check_reports_unreachable_rather_than_up_to_date(self):
        """A blocked network must never be reported as being current."""
        import updater as u
        real = u.fetch_latest_release
        u.fetch_latest_release = lambda *a, **k: None
        try:
            rv = self.client.get('/api/update/check')
            self.assertErr(rv, 502)
            self.assertIn('Could not reach GitHub', json.loads(rv.data)['message'])
        finally:
            u.fetch_latest_release = real

    def test_check_compares_against_the_running_version(self):
        import updater as u
        import app as app_mod
        real = u.fetch_latest_release
        u.fetch_latest_release = lambda *a, **k: {
            'version': '9.9.9', 'tag': 'v9.9.9', 'asset_name': 'CMIS_dist_v9_9_9.zip',
            'asset_url': 'https://x', 'asset_size': 10, 'sha256': None,
            'html_url': 'https://x', 'notes': '', 'published_at': '',
        }
        try:
            body = self.assertOk(self.client.get('/api/update/check'))['data']
            self.assertTrue(body['update_available'])
            self.assertEqual(body['current_version'], app_mod.__version__)
            self.assertFalse(body['can_self_update'], 'source run offered a self-update')
        finally:
            u.fetch_latest_release = real


class TestModuleControlBits(CMISTestCase):
    """Byte 0x1A packs unrelated controls; touching one must not move others."""

    def _set_raw(self, value):
        self.client.post('/api/register/write',
                         data=json.dumps({'page': 0, 'address': 0x1A, 'data': [value]}),
                         content_type='application/json')

    def _raw(self):
        return self.assertOk(self.client.get('/api/module/control'))['data']['raw']

    def test_low_power_toggle_preserves_other_bits(self):
        """Regression: Exit LowPwr used to clear SquelchMethodSelect."""
        self.connect()
        self._set_raw(0xA0)          # BankBroadcast + SquelchMethodSelect(Pav)
        for action in ('low_power', 'high_power'):
            self.client.post('/api/module/control',
                             data=json.dumps({'action': action}),
                             content_type='application/json')
            raw = self._raw()
            self.assertTrue(raw & (1 << 7), f'{action} cleared BankBroadcastEnable')
            self.assertTrue(raw & (1 << 5), f'{action} cleared SquelchMethodSelect')
        self.assertFalse(self._raw() & (1 << 4), 'high_power left LowPwrRequestSW set')

    def test_low_power_actually_toggles_its_own_bit(self):
        self.connect()
        self._set_raw(0x00)
        self.client.post('/api/module/control', data=json.dumps({'action': 'low_power'}),
                         content_type='application/json')
        self.assertTrue(self._raw() & (1 << 4))

    def test_partial_field_set_leaves_unnamed_fields_alone(self):
        self.connect()
        self._set_raw(0xA0)
        self.client.post('/api/module/control',
                         data=json.dumps({'low_pwr': True}),
                         content_type='application/json')
        self.assertEqual(self._raw(), 0xB0, 'a partial set rebuilt the whole byte')

    def test_software_reset_is_self_clearing(self):
        """CMIS marks SoftwareReset WO/SC; reading it back as 1 would leave the
        UI claiming a reset is still in progress forever."""
        self.connect()
        self.client.post('/api/module/control', data=json.dumps({'action': 'reset'}),
                         content_type='application/json')
        self.assertFalse(self._raw() & (1 << 3),
                         'SoftwareReset stayed set after the write')


class TestRawWriteGuards(CMISTestCase):

    def test_write_through_page_select_is_refused(self):
        """A multi-byte write across 0x7F would reprogram the page mid-transfer
        and scatter the rest into whatever page that byte named."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x7C,
                                               'data': [0, 0, 0, 0x12, 0x34]}),
                              content_type='application/json')
        self.assertErr(rv, 400)
        self.assertEqual(_state['backend']._current_page, 0x00,
                         'the refused write still moved the page')

    def test_single_byte_page_select_still_allowed(self):
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x7F, 'data': [0x11]}),
                              content_type='application/json')
        self.assertOk(rv)


class TestPageSelection(CMISTestCase):
    """Page selection is cached; a stale cache silently reads the wrong page."""

    def _page_writes(self, fn):
        """Run fn and return the pages written to the PageMapping register."""
        backend = _state['backend']
        real = backend.write_bytes
        seen = []

        def spy(addr, data):
            if addr == 0x7F:
                seen.append(data[0])
            return real(addr, data)

        backend.write_bytes = spy
        try:
            fn()
        finally:
            backend.write_bytes = real
        return seen

    def test_page_change_waits_the_spec_hold_off(self):
        """CMIS 5.3 gives tBPC, max Bank/Page Change time, as 10 ms.

        Reading sooner can return the previous page's contents on a slow
        module - an intermittent fault that looks like corrupt data.
        """
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        src = io.open(path, encoding='utf-8').read()
        body = src.split('def _set_page(')[1].split('\ndef ')[0]
        self.assertIn('time.sleep(0.010)', body,
                      'page-change hold-off is shorter than tBPC = 10 ms')

    def test_repeated_reads_of_one_page_select_once(self):
        self.connect()
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02],
                         f'thresholds re-selected its page: {pages}')

    def test_alternating_pages_reselect_each_time(self):
        """Caching must not skip a genuine page change."""
        self.connect()
        def alternate():
            for _ in range(2):
                self.client.post('/api/register/read',
                                 data=json.dumps({'page': 0x11, 'address': 0x80, 'length': 1}),
                                 content_type='application/json')
                self.client.post('/api/register/read',
                                 data=json.dumps({'page': 0x10, 'address': 0x80, 'length': 1}),
                                 content_type='application/json')
        self.assertEqual(self._page_writes(alternate), [0x11, 0x10, 0x11, 0x10])

    def test_reconnect_forgets_the_cached_page(self):
        self.connect()
        self.client.get('/api/module/thresholds')       # leaves page 02h selected
        self.connect()                                   # fresh module, page unknown
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02], 'reconnect trusted a stale page cache')

    def test_raw_write_to_page_register_forgets_the_cache(self):
        """Writing 0x7F by hand moves the page out from under the cache."""
        self.connect()
        self.client.get('/api/module/thresholds')       # page 02h cached
        self.client.post('/api/register/write',
                         data=json.dumps({'page': 0, 'address': 0x7F, 'data': [0x11]}),
                         content_type='application/json')
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02], 'cache survived a raw write to 0x7F')

    def test_module_reset_forgets_the_cached_page(self):
        self.connect()
        self.client.get('/api/module/thresholds')
        self.client.post('/api/module/control',
                         data=json.dumps({'action': 'reset'}),
                         content_type='application/json')
        pages = self._page_writes(lambda: self.client.get('/api/module/thresholds'))
        self.assertEqual(pages, [0x02], 'cache survived a module reset')

    def test_values_still_come_from_the_right_page(self):
        """End-to-end guard: caching must not cross-contaminate pages."""
        self.connect()
        self.client.post('/api/module/squelch',
                         data=json.dumps({'tx_squelch_disable': 0x11,
                                          'tx_squelch_force': 0x22,
                                          'rx_output_disable': 0x33,
                                          'rx_squelch_disable': 0x44}),
                         content_type='application/json')
        # Interleave reads of three different pages, then re-check page 10h.
        self.client.get('/api/module/thresholds')   # 02h
        self.client.get('/api/module/monitoring')   # 11h
        self.client.get('/api/module/loopback')     # 13h
        body = self.assertOk(self.client.get('/api/module/squelch'))
        self.assertEqual(body['data']['tx_squelch_disable'], 0x11)
        self.assertEqual(body['data']['rx_squelch_disable'], 0x44)


class TestRegisterTooltips(CMISTestCase):
    """The UI hover tooltips quote CMIS field names and addresses at the user.

    A wrong tooltip is worse than none, so pin the strings that app.js emits to
    the names and byte addresses in OIF CMIS 5.3. Sources: Table 8-69
    (lane-specific controls, Page 10h), Table 8-72 (Staged Control Set 0),
    Tables 8-109/8-111/8-113/8-115 (pattern gen/check, Page 13h), Table 8-121
    (loopback controls), Table 8-99 (tunable laser, Page 12h) and the Module
    Control byte in Lower Memory.
    """

    def _js(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'app.js')
        return io.open(path, encoding='utf-8').read()

    def test_page10h_tooltip_fields(self):
        js = self._js()
        for field, addr in [('InputPolarityFlipTx', '0x81'), ('OutputDisableTx', '0x82'),
                            ('AutoSquelchDisableTx', '0x83'), ('OutputSquelchForceTx', '0x84'),
                            ('OutputPolarityFlipRx', '0x89'), ('OutputDisableRx', '0x8A'),
                            ('AutoSquelchDisableRx', '0x8B')]:
            self.assertIn(field, js, f'{field} missing from tooltips')
            self.assertIn(addr, js, f'{addr} missing from tooltips')
        self.assertIn('DPDeinitLane', js)
        self.assertIn('DPConfigLane', js)

    def test_page13h_tooltip_fields(self):
        """Pattern controls are named <Side>Side<Role><Field>Lane<n>."""
        js = self._js()
        for part in ['Side${role}', 'PatternSelectLane', 'SwapSymbolBits',
                     'DataInvert', 'PreFECEnable', 'PostFECEnable']:
            self.assertIn(part, js, f'{part} missing from PRBS tooltips')
        for field in ['MediaSideOutputLoopbackEnable', 'MediaSideInputLoopbackEnable',
                      'HostSideOutputLoopbackEnable', 'HostSideInputLoopbackEnable']:
            self.assertIn(field, js, f'{field} missing from loopback tooltips')

    def test_page12h_tooltip_fields(self):
        js = self._js()
        for field in ['GridSpacingTx', 'FineTuningEnableTx', 'ChannelNumberTx',
                      'FineTuningOffsetTx', 'CurrentLaserFrequencyTx',
                      'TargetOutputPowerTx', 'TuningInProgressTx', 'WavelengthUnlockedTx']:
            self.assertIn(field, js, f'{field} missing from laser tooltips')

    def test_module_control_tooltip_fields(self):
        js = self._js()
        for field in ['SoftwareReset', 'LowPwrRequestSW',
                      'LowPwrAllowRequestHW', 'SquelchMethodSelect']:
            self.assertIn(field, js, f'{field} missing from Module Control tooltips')

    def _html(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'templates', 'index.html')
        return io.open(path, encoding='utf-8').read()

    def test_static_column_labels_match_register_map(self):
        """Column headers hard-code addresses; keep them tied to cmis_registers.

        These labels drifted once already: after the Page 10h map was corrected
        the DataPath headers still advertised the old bytes, so the header and
        the hover tooltip disagreed about the same control.
        """
        import cmis_registers as c
        html = self._html()
        for label, reg in [('AppSelect', c.REG_APP_SELECT),
                           ('TX Enable', c.REG_TX_OUTPUT_DIS),
                           ('TX Pol Flip', c.REG_TX_POL_FLIP),
                           ('RX Pol Flip', c.REG_RX_POL_FLIP),
                           ('DP Deinit', c.REG_DP_DEINIT)]:
            head = html.split(f'<th>{label}<span class="reg-meta">')[1].split('</span>')[0]
            page, addr = reg[0], reg[1]
            self.assertIn(f'{page:02X}h', head,
                          f'{label} header names the wrong page: {head}')
            self.assertIn(f'0x{addr:02X}', head,
                          f'{label} header names the wrong address: {head}')

    @staticmethod
    def _register_ranges():
        """Every (page, first_byte, last_byte) defined in cmis_registers."""
        import cmis_registers as c
        out = []
        for name in dir(c):
            if not name.startswith('REG_'):
                continue
            val = getattr(c, name)
            if isinstance(val, tuple) and len(val) == 3:
                page, addr, length = val
                out.append((name, page, addr, addr + length - 1))
        return out

    def _resolve(self, page, addr):
        return [n for n, p, lo, hi in self._register_ranges()
                if p == page and lo <= addr <= hi]

    def test_every_displayed_address_exists_in_the_register_map(self):
        """Static Page/address labels are only "executed" when a human reads them.

        Nothing else exercises the strings baked into the column headers and
        the Module Info rows, which is how the FW Revision row came to
        advertise Page 01h 0x84-0x85 for a field the code reads from Lower
        Memory 0x27. Require every address shown to resolve to a real entry in
        cmis_registers, so an invented or stale one fails here.
        """
        import re
        html, js = self._html(), self._js()
        shown = []

        for m in re.finditer(r'class="reg-(?:meta|badge)">([^<]+)<', html):
            label = m.group(1)
            if 'sel=' in label:
                continue  # a diagnostics selector value, not an address
            pm = re.search(r'\b([0-9A-Fa-f]{2})h', label)
            am = re.search(r'0x([0-9A-Fa-f]{2})', label)
            if pm and am:
                shown.append((int(pm.group(1), 16), int(am.group(1), 16),
                              f'index.html header "{label.strip()}"'))

        rows = js.split('const rows = [')[1].split('\n  ];')[0]
        for pg_s, ad_s in re.findall(r"'(Lower|[0-9A-Fa-f]{2}h)',\s*'0x([0-9A-Fa-f]{2})", rows):
            page = None if pg_s == 'Lower' else int(pg_s[:2], 16)
            shown.append((page, int(ad_s, 16),
                          f'Module Info row ({pg_s}, 0x{ad_s})'))

        self.assertGreater(len(shown), 50, 'address extraction stopped working')
        unknown = [(p, a, w) for p, a, w in shown if not self._resolve(p, a)]
        self.assertEqual(unknown, [], 'addresses shown to the user with no '
                                      'matching entry in cmis_registers')

    def test_fw_revision_row_points_at_lower_memory(self):
        """Regression: this row named a Page 01h fibre-length byte."""
        import cmis_registers as c
        rows = self._js().split('const rows = [')[1].split('\n  ];')[0]
        fw = [ln for ln in rows.splitlines() if "'FW Revision'" in ln][0]
        self.assertIn("'Lower'", fw)
        self.assertIn(f'0x{c.REG_FW_ACTIVE_MAJOR[1]:02X}', fw)

    def test_output_status_tx_rx_not_swapped(self):
        """CMIS 5.3 8.10.2: OutputStatusRx is 11h:132, OutputStatusTx is 11h:133."""
        import cmis_registers as c
        self.assertEqual(c.REG_OUTPUT_STATUS_RX[:2], (0x11, 0x84))
        self.assertEqual(c.REG_OUTPUT_STATUS_TX[:2], (0x11, 0x85))

    def test_values_state_their_radix(self):
        """A bare number in a register tool is ambiguous; mark hex and binary."""
        js = self._js()
        self.assertIn('hex = ', js, 'tooltips do not label the hex form')
        self.assertIn('bin = ', js, 'tooltips do not label the binary form')
        self.assertIn('dec', js, 'tooltips do not label the decimal form')
        # The lane-assignment bitmap must not render as bare digits.
        self.assertIn("'0b' + a.host_lane_assign_mask.toString(2)", js)

    def test_apply_handlers_reload_after_write(self):
        """Every Apply must re-read, or the panel and its tooltips go stale.

        The tooltips quote the current register byte, so an Apply that only
        writes leaves the user looking at the value from before their change -
        and hides a module that rejected or clamped the write.
        """
        js = self._js()
        # These three go through the shared write-then-reload helper.
        for fn in ['applySquelch', 'applyLoopback', 'applyPrbs']:
            body = js.split(f'async function {fn}(')[1].split('\nasync function')[0]
            self.assertIn('applyAndReload', body,
                          f'{fn} writes without re-reading the module')
        # applyDatapath waits for ApplyDPInit before re-reading, so it reloads
        # explicitly rather than via the helper.
        dp = js.split('async function applyDatapath(')[1].split('\nasync function')[0]
        self.assertIn('loadDatapath()', dp, 'applyDatapath does not re-read')

    def test_control_endpoint_exposes_raw_byte(self):
        """Tooltips quote the current byte, so the API must return it."""
        self.connect()
        body = self.assertOk(self.client.get('/api/module/control'))
        self.assertIn('raw', body['data'])
        self.assertIsInstance(body['data']['raw'], int)


class TestBackends(CMISTestCase):

    def test_backends_ok(self):
        """Should return list with at least mock backend."""
        rv = self.client.get('/api/backends')
        body = self.assertOk(rv)
        backends = body['data']
        self.assertIsInstance(backends, list)
        self.assertTrue(len(backends) >= 1)
        names = [b['name'] for b in backends]
        for profile in ('mock_coherent', 'mock_dr8', 'mock_sr8', 'mock_fr4x2'):
            self.assertIn(profile, names)

    def test_backends_mock_available(self):
        """All mock profiles must be marked available."""
        rv = self.client.get('/api/backends')
        body = self.assertOk(rv)
        by_name = {b['name']: b for b in body['data']}
        for profile in ('mock_coherent', 'mock_dr8', 'mock_sr8', 'mock_fr4x2'):
            self.assertTrue(by_name[profile]['available'])


# ============================================================
# 2. POST /api/connect
# ============================================================

class TestConnect(CMISTestCase):

    def test_connect_mock_defaults(self):
        """Connect with mock backend, default bus/address."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8'}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['backend'], 'mock_dr8')
        self.assertTrue(_state['connected'])

    def test_connect_explicit_params(self):
        """Connect with explicit bus and integer address."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8', 'bus': 1, 'address': 90}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bus'], 1)
        self.assertEqual(body['data']['address'], 90)

    def test_connect_hex_address(self):
        """Address may be supplied as hex string."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8', 'bus': 0, 'address': '0x50'}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['address'], 0x50)

    def test_connect_unknown_backend(self):
        """Unknown backend name must return error."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'nonexistent'}),
                              content_type='application/json')
        self.assertErr(rv, 400)
        self.assertFalse(_state['connected'])

    def test_connect_invalid_address_string(self):
        """Non-numeric address string must return error (not crash)."""
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8', 'address': 'bad_addr'}),
                              content_type='application/json')
        # Should return 400/500 error, not 500 unhandled exception
        self.assertIn(rv.status_code, (400, 500))
        body = json.loads(rv.data)
        self.assertEqual(body['status'], 'error')

    def test_connect_no_body(self):
        """Empty body → defaults to mock backend, should succeed."""
        rv = self.client.post('/api/connect',
                              data=b'',
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['backend'], 'mock_dr8')

    def test_reconnect_replaces_backend(self):
        """Second connect call should replace first backend."""
        self.connect()
        first_backend = _state['backend']
        self.connect()
        second_backend = _state['backend']
        # Different object instances
        self.assertIsNot(first_backend, second_backend)
        self.assertTrue(_state['connected'])


# ============================================================
# 3. GET /api/disconnect
# ============================================================

class TestDisconnect(CMISTestCase):

    def test_disconnect_when_connected(self):
        """Normal disconnect after connection."""
        self.connect()
        rv = self.client.get('/api/disconnect')
        body = self.assertOk(rv)
        self.assertFalse(_state['connected'])
        self.assertIsNone(_state['backend'])

    def test_disconnect_when_not_connected(self):
        """Disconnect without prior connect should still succeed."""
        rv = self.client.get('/api/disconnect')
        self.assertOk(rv)
        self.assertFalse(_state['connected'])

    def test_disconnect_via_post(self):
        """Disconnect also accepts POST."""
        self.connect()
        rv = self.client.post('/api/disconnect')
        self.assertOk(rv)
        self.assertFalse(_state['connected'])


# ============================================================
# 4. GET /api/module/info
# ============================================================

class TestModuleInfo(CMISTestCase):

    def test_info_not_connected(self):
        """Should return 503 when not connected."""
        rv = self.client.get('/api/module/info')
        self.assertErr(rv, 503)

    def test_info_ok(self):
        """Should return valid module info fields."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        d = body['data']
        self.assertIn('module_id', d)
        self.assertIn('module_type', d)
        self.assertIn('cmis_revision', d)
        self.assertIn('vendor_name', d)
        self.assertIn('vendor_pn', d)
        self.assertIn('vendor_sn', d)
        self.assertIn('date_code', d)
        self.assertIn('host_lanes', d)
        self.assertIn('media_lanes', d)

    def test_info_module_id_qsfpdd(self):
        """Mock registers QSFP-DD (0x1E)."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['module_id'], 0x1E)
        self.assertIn('QSFP-DD', body['data']['module_type'])

    def test_info_cmis_revision(self):
        """CMIS revision should be '5.3' in mock_dr8."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['cmis_revision'], '5.3')

    def test_info_vendor_name(self):
        """Vendor name should be OPENCMIS DEMO (stripped)."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['vendor_name'], 'OPENCMIS DEMO')

    def test_info_num_lanes(self):
        """mock_dr8 is 8 host lanes / 8 media lanes."""
        self.connect()
        rv = self.client.get('/api/module/info')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['host_lanes'], 8)
        self.assertEqual(body['data']['media_lanes'], 8)


# ============================================================
# 5. GET /api/module/status
# ============================================================

class TestModuleStatus(CMISTestCase):

    def test_status_not_connected(self):
        rv = self.client.get('/api/module/status')
        self.assertErr(rv, 503)

    def test_status_ok(self):
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        d = body['data']
        self.assertIn('module_state', d)
        self.assertIn('temperature_c', d)
        self.assertIn('voltage_v', d)
        self.assertIn('interrupt_asserted', d)
        self.assertIn('alarm_active', d)

    def test_status_temperature_range(self):
        """Temperature should be in a sane range for mock (near 45°C)."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        temp = body['data']['temperature_c']
        self.assertIsInstance(temp, float)
        self.assertGreater(temp, 0.0)
        self.assertLess(temp, 100.0)

    def test_status_voltage_range(self):
        """Voltage should be near 3.3V in mock."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        volt = body['data']['voltage_v']
        self.assertAlmostEqual(volt, 3.3, delta=0.1)

    def test_status_module_state_value(self):
        """Mock lower[0x01] = 0x04 → bits[3:1] = 0x04 & 0x0E = 0x04 → ModuleReady."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        # state byte 0x04 → bits[3:1] = 0x04 & 0x0E = 0x04 → 'ModuleReady'
        self.assertEqual(body['data']['module_state'], 'ModuleReady')

    def test_status_alarm_active_type(self):
        """alarm_active should be a boolean."""
        self.connect()
        rv = self.client.get('/api/module/status')
        body = self.assertOk(rv)
        self.assertIsInstance(body['data']['alarm_active'], bool)


# ============================================================
# 6. GET /api/module/monitoring
# ============================================================

class TestModuleMonitoring(CMISTestCase):

    def test_monitoring_not_connected(self):
        rv = self.client.get('/api/module/monitoring')
        self.assertErr(rv, 503)

    def test_monitoring_ok(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        self.assertIn('lanes', body['data'])

    def test_monitoring_8_lanes(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        self.assertEqual(len(body['data']['lanes']), 8)

    def test_monitoring_lane_fields(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        lane = body['data']['lanes'][0]
        for key in ('lane', 'tx_power_uw', 'tx_power_dbm', 'rx_power_uw',
                    'rx_power_dbm', 'tx_bias_ma', 'datapath_state'):
            self.assertIn(key, lane, f"Missing key: {key}")

    def test_monitoring_lane_numbers(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        lane_nums = [l['lane'] for l in body['data']['lanes']]
        self.assertEqual(lane_nums, list(range(1, 9)))

    def test_monitoring_power_positive(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        for lane in body['data']['lanes']:
            self.assertGreater(lane['tx_power_uw'], 0)
            self.assertGreater(lane['rx_power_uw'], 0)

    def test_monitoring_dbm_conversion(self):
        """dBm should correspond to µW conversion."""
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        for lane in body['data']['lanes']:
            expected_dbm = round(10 * math.log10(lane['tx_power_uw'] / 1000.0), 2)
            self.assertAlmostEqual(lane['tx_power_dbm'], expected_dbm, places=1)

    def test_monitoring_datapath_state_valid(self):
        self.connect()
        rv = self.client.get('/api/module/monitoring')
        body = self.assertOk(rv)
        valid_states = {'Deactivated', 'Init', 'TxTurnOn', 'Activated'}
        for lane in body['data']['lanes']:
            state = lane['datapath_state']
            # Either a known state or Unknown(...)
            self.assertTrue(state in valid_states or state.startswith('Unknown'),
                            f"Unexpected state: {state}")


# ============================================================
# 7. GET /api/module/datapath
# ============================================================

class TestDatapathGet(CMISTestCase):

    def test_datapath_not_connected(self):
        rv = self.client.get('/api/module/datapath')
        self.assertErr(rv, 503)

    def test_datapath_get_ok(self):
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        d = body['data']
        self.assertIn('tx_disable_mask', d)
        self.assertIn('dp_deinit_mask', d)
        self.assertIn('app_select', d)
        self.assertIn('lanes', d)

    def test_datapath_get_8_lanes(self):
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        self.assertEqual(len(body['data']['lanes']), 8)

    def test_datapath_default_tx_enabled(self):
        """Mock default TX disable = 0 → all lanes tx_enable=True."""
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        for lane in body['data']['lanes']:
            self.assertTrue(lane['tx_enable'],
                            f"Lane {lane['lane']} should have tx_enable=True")

    def test_datapath_app_select_default(self):
        """Mock default app_select = 1 for all lanes."""
        self.connect()
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        for v in body['data']['app_select']:
            self.assertEqual(v, 1)


# ============================================================
# 8. POST /api/module/datapath
# ============================================================

class TestDatapathSet(CMISTestCase):

    def test_datapath_set_not_connected(self):
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({'tx_disable_mask': 0}),
                              content_type='application/json')
        self.assertErr(rv, 503)

    def test_datapath_set_ok(self):
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({
                                  'tx_disable_mask': 0xAA,
                                  'app_select': [1, 2, 1, 2, 1, 2, 1, 2],
                                  'apply': False
                              }),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertIn('message', body['data'])

    def test_datapath_set_then_get(self):
        """Write tx_disable_mask, then read back and verify."""
        self.connect()
        mask = 0xAA
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': mask, 'apply': False}),
                         content_type='application/json')
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['tx_disable_mask'], mask)

    def test_datapath_set_apply(self):
        """apply=True should not cause an error."""
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({
                                  'tx_disable_mask': 0,
                                  'app_select': [1] * 8,
                                  'apply': True
                              }),
                              content_type='application/json')
        self.assertOk(rv)

    def test_datapath_set_mask_clamp(self):
        """tx_disable_mask > 0xFF should be clamped to 8 bits."""
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=json.dumps({'tx_disable_mask': 0x1FF}),
                              content_type='application/json')
        self.assertOk(rv)
        rv2 = self.client.get('/api/module/datapath')
        body = self.assertOk(rv2)
        # 0x1FF & 0xFF = 0xFF
        self.assertEqual(body['data']['tx_disable_mask'], 0xFF)

    def test_page10h_addresses_match_spec(self):
        """Pin the Page 10h control map to OIF CMIS 5.3 Tables 8-67/8-69/8-70/8-72.

        These addresses were previously off by several bytes, which on real
        hardware silently flipped Tx polarity instead of disabling Tx and
        triggered ApplyDPInit when writing the Rx polarity mask.
        """
        import cmis_registers as c
        self.assertEqual(c.REG_DP_DEINIT[:2],        (0x10, 0x80))  # 128
        self.assertEqual(c.REG_TX_POL_FLIP[:2],      (0x10, 0x81))  # 129
        self.assertEqual(c.REG_TX_OUTPUT_DIS[:2],    (0x10, 0x82))  # 130
        self.assertEqual(c.REG_TX_SQUELCH_DIS[:2],   (0x10, 0x83))  # 131
        self.assertEqual(c.REG_TX_FORCE_SQUELCH[:2], (0x10, 0x84))  # 132
        self.assertEqual(c.REG_RX_POL_FLIP[:2],      (0x10, 0x89))  # 137
        self.assertEqual(c.REG_RX_OUTPUT_DIS[:2],    (0x10, 0x8A))  # 138
        self.assertEqual(c.REG_RX_SQUELCH_DIS[:2],   (0x10, 0x8B))  # 139
        self.assertEqual(c.REG_APPLY_DATAPATH[:2],   (0x10, 0x8F))  # 143
        self.assertEqual(c.REG_APPLY_IMM[:2],        (0x10, 0x90))  # 144
        self.assertEqual(c.REG_APP_SELECT,           (0x10, 0x91, 8))  # 145-152

    def test_datapath_write_lands_on_spec_registers(self):
        """tx_disable must reach OutputDisableTx (130), not a polarity register."""
        self.connect()
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': 0xA5,
                                          'tx_polarity_flip_mask': 0x0F,
                                          'rx_polarity_flip_mask': 0x33,
                                          'apply': False}),
                         content_type='application/json')
        page10 = _state['backend']._registers[0x10]
        self.assertEqual(page10[0x82], 0xA5)  # OutputDisableTx
        self.assertEqual(page10[0x81], 0x0F)  # InputPolarityFlipTx
        self.assertEqual(page10[0x89], 0x33)  # OutputPolarityFlipRx
        self.assertEqual(page10[0x8F], 0x00)  # ApplyDPInit must stay untouched

    def test_squelch_roundtrip_all_four_controls(self):
        self.connect()
        payload = {'tx_squelch_disable': 0x11, 'tx_squelch_force': 0x22,
                   'rx_output_disable': 0x33, 'rx_squelch_disable': 0x44}
        self.assertOk(self.client.post('/api/module/squelch',
                                       data=json.dumps(payload),
                                       content_type='application/json'))
        body = self.assertOk(self.client.get('/api/module/squelch'))
        for k, v in payload.items():
            self.assertEqual(body['data'][k], v, f'{k} did not round-trip')
        page10 = _state['backend']._registers[0x10]
        self.assertEqual(page10[0x83], 0x11)
        self.assertEqual(page10[0x84], 0x22)
        self.assertEqual(page10[0x8A], 0x33)
        self.assertEqual(page10[0x8B], 0x44)

    def test_datapath_set_empty_body(self):
        """Empty body should use defaults without crashing."""
        self.connect()
        rv = self.client.post('/api/module/datapath',
                              data=b'',
                              content_type='application/json')
        self.assertOk(rv)

    def test_datapath_set_app_select_roundtrip(self):
        """pack/unpack roundtrip for app_select values."""
        self.connect()
        app_sel = [1, 2, 3, 4, 1, 2, 3, 4]
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': 0, 'app_select': app_sel}),
                         content_type='application/json')
        rv = self.client.get('/api/module/datapath')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['app_select'], app_sel)


# ============================================================
# 9. POST /api/register/read
# ============================================================

class TestRegisterRead(CMISTestCase):

    def test_register_read_not_connected(self):
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0, 'length': 1}),
                              content_type='application/json')
        self.assertErr(rv, 503)

    def test_register_read_lower_page(self):
        """Read from lower page (address < 0x80)."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['data'], [0x1E])  # Module ID = QSFP-DD

    def test_register_read_upper_page(self):
        """Read from upper page (address >= 0x80)."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x81, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['data'], [0x4F])  # vendor name starts 'O' (OPENCMIS DEMO)

    def test_register_read_multiple_bytes(self):
        """Read multiple bytes at once."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x00, 'address': 0x81, 'length': 16}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['length'], 16)
        self.assertEqual(len(body['data']['data']), 16)
        # Vendor name field 00h:129-144, space padded
        text = bytes(body['data']['data']).decode('ascii').rstrip()
        self.assertEqual(text, 'OPENCMIS DEMO')

    def test_register_read_hex_string_address(self):
        """Address as hex string."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': '0x00', 'length': 1}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_register_read_max_length(self):
        """Length = 128 (boundary, should pass)."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 128}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_register_read_length_zero(self):
        """Length = 0 should return error."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 0}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_read_length_too_large(self):
        """Length > 128 should return error."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 129}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_read_response_fields(self):
        """Response must contain page, address, length, data, hex."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        d = body['data']
        for k in ('page', 'address', 'length', 'data', 'hex'):
            self.assertIn(k, d)

    def test_register_read_hex_format(self):
        """Hex field should be space-separated uppercase hex."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        hex_str = body['data']['hex']
        # Should match pattern like "1E"
        self.assertRegex(hex_str, r'^[0-9A-F]{2}( [0-9A-F]{2})*$')

    def test_register_read_negative_length(self):
        """Negative length should return error."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x00, 'length': -1}),
                              content_type='application/json')
        self.assertErr(rv, 400)


# ============================================================
# 10. POST /api/register/write
# ============================================================

class TestRegisterWrite(CMISTestCase):

    def test_register_write_not_connected(self):
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x81, 'data': [0x00]}),
                              content_type='application/json')
        self.assertErr(rv, 503)

    def test_register_write_ok(self):
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0x10, 'address': 0x81, 'data': [0xFF]}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bytes_written'], 1)

    def test_register_write_read_roundtrip(self):
        """Write a value and read it back."""
        self.connect()
        self.client.post('/api/register/write',
                         data=json.dumps({'page': 0x10, 'address': 0x81, 'data': [0xAB]}),
                         content_type='application/json')
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x10, 'address': 0x81, 'length': 1}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['data'], [0xAB])

    def test_register_write_no_data(self):
        """Missing data field should return error."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x80}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_write_empty_data(self):
        """Empty data list should return error."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x80, 'data': []}),
                              content_type='application/json')
        self.assertErr(rv, 400)

    def test_register_write_multi_byte(self):
        """Write multiple bytes."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0x10, 'address': 0x86, 'data': [0x11, 0x22, 0x33]}),
                              content_type='application/json')
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bytes_written'], 3)

    def test_register_write_byte_clamping(self):
        """Values > 255 should be clamped via & 0xFF."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0x10, 'address': 0x81, 'data': [0x1FF]}),
                              content_type='application/json')
        # Should succeed (0x1FF & 0xFF = 0xFF)
        body = self.assertOk(rv)
        self.assertEqual(body['data']['bytes_written'], 1)

    def test_register_write_lower_page(self):
        """Write to lower page address (< 0x80) should not set page."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps({'page': 0, 'address': 0x02, 'data': [0x01]}),
                              content_type='application/json')
        self.assertOk(rv)
        # Read it back
        rv2 = self.client.post('/api/register/read',
                               data=json.dumps({'page': 0, 'address': 0x02, 'length': 1}),
                               content_type='application/json')
        body = self.assertOk(rv2)
        self.assertEqual(body['data']['data'], [0x01])


# ============================================================
# 11. Boundary / edge cases
# ============================================================

class TestEdgeCases(CMISTestCase):

    def test_all_endpoints_require_connection(self):
        """All module endpoints should return 503 when not connected."""
        endpoints = [
            ('GET', '/api/module/info'),
            ('GET', '/api/module/status'),
            ('GET', '/api/module/monitoring'),
            ('GET', '/api/module/datapath'),
        ]
        for method, path in endpoints:
            rv = self.client.open(path, method=method)
            self.assertEqual(rv.status_code, 503,
                             f"{method} {path} should be 503 when disconnected")

    def test_post_endpoints_require_connection(self):
        for path, payload in [
            ('/api/module/datapath', {'tx_disable_mask': 0}),
            ('/api/register/read', {'page': 0, 'address': 0, 'length': 1}),
            ('/api/register/write', {'page': 0, 'address': 0x80, 'data': [0]}),
        ]:
            rv = self.client.post(path, data=json.dumps(payload),
                                  content_type='application/json')
            self.assertEqual(rv.status_code, 503,
                             f"POST {path} should be 503 when disconnected")

    def test_connect_then_disconnect_then_reconnect(self):
        """Full lifecycle should work cleanly."""
        self.connect()
        self.assertTrue(_state['connected'])
        self.client.get('/api/disconnect')
        self.assertFalse(_state['connected'])
        self.connect()
        self.assertTrue(_state['connected'])

    def test_module_info_after_reconnect(self):
        """After reconnect, module info should still work."""
        self.connect()
        self.client.get('/api/disconnect')
        self.connect()
        rv = self.client.get('/api/module/info')
        self.assertOk(rv)

    def test_register_read_address_boundary_0x7F(self):
        """Address 0x7F is < 0x80, so reads from lower page."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0, 'address': 0x7F, 'length': 1}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_register_read_address_boundary_0x80(self):
        """Address 0x80 is >= 0x80, so reads from upper page."""
        self.connect()
        rv = self.client.post('/api/register/read',
                              data=json.dumps({'page': 0x11, 'address': 0x80, 'length': 1}),
                              content_type='application/json')
        self.assertOk(rv)

    def test_connect_missing_json_content_type(self):
        """A POST that is not declared as JSON must be refused.

        This used to be accepted (get_json(force=True)), which is precisely
        what made the API reachable from any other website: text/plain is a
        CORS-simple content type, so a foreign page could post JSON here with
        no preflight and no consent. Requiring application/json forces the
        preflight that such a page cannot satisfy.
        """
        rv = self.client.post('/api/connect',
                              data=json.dumps({'backend': 'mock_dr8'}),
                              content_type='text/plain')
        self.assertEqual(rv.status_code, 415)

    def test_monitoring_after_datapath_write(self):
        """Monitoring should remain functional after datapath write."""
        self.connect()
        self.client.post('/api/module/datapath',
                         data=json.dumps({'tx_disable_mask': 0xFF}),
                         content_type='application/json')
        rv = self.client.get('/api/module/monitoring')
        self.assertOk(rv)

    def test_status_after_multiple_reads(self):
        """Multiple status reads should all succeed."""
        self.connect()
        for _ in range(5):
            rv = self.client.get('/api/module/status')
            self.assertOk(rv)


# ============================================================
# 12. cmis_registers unit tests
# ============================================================

class TestCmisRegisters(unittest.TestCase):

    def test_parse_temperature_positive(self):
        from cmis_registers import parse_temperature
        raw = struct.pack(">h", int(45.0 * 256))
        self.assertAlmostEqual(parse_temperature(raw), 45.0, places=3)

    def test_parse_temperature_negative(self):
        from cmis_registers import parse_temperature
        raw = struct.pack(">h", int(-10.0 * 256))
        self.assertAlmostEqual(parse_temperature(raw), -10.0, places=3)

    def test_parse_voltage(self):
        from cmis_registers import parse_voltage
        # 3.3V → 33000
        raw = struct.pack(">H", 33000)
        self.assertAlmostEqual(parse_voltage(raw), 3.3, places=4)

    def test_parse_power_uw(self):
        from cmis_registers import parse_power_uw
        # 5000 * 0.1 = 500µW
        raw = struct.pack(">H", 5000)
        self.assertAlmostEqual(parse_power_uw(raw), 500.0, places=3)

    def test_uw_to_dbm_zero(self):
        from cmis_registers import uw_to_dbm
        self.assertEqual(uw_to_dbm(0), -40.0)

    def test_uw_to_dbm_negative(self):
        from cmis_registers import uw_to_dbm
        self.assertEqual(uw_to_dbm(-1), -40.0)

    def test_uw_to_dbm_1000uw_is_0dbm(self):
        from cmis_registers import uw_to_dbm
        self.assertAlmostEqual(uw_to_dbm(1000.0), 0.0, places=5)

    def test_parse_tx_bias_ma(self):
        from cmis_registers import parse_tx_bias_ma
        # 17500 * 0.002 = 35mA
        raw = struct.pack(">H", 17500)
        self.assertAlmostEqual(parse_tx_bias_ma(raw), 35.0, places=3)

    def test_parse_ascii(self):
        from cmis_registers import parse_ascii
        raw = b"INNOLIGHT       "
        self.assertEqual(parse_ascii(raw), "INNOLIGHT")

    def test_parse_ascii_null_terminated(self):
        from cmis_registers import parse_ascii
        raw = b"TEST\x00\x00\x00"
        self.assertEqual(parse_ascii(raw), "TEST")

    def test_parse_dp_states_all_activated(self):
        from cmis_registers import parse_dp_states
        # 4 bits/lane, 2 lanes/byte; 0x4 = DPActivated
        raw = bytes([0x44] * 4)
        states = parse_dp_states(raw)
        self.assertEqual(len(states), 8)
        self.assertTrue(all(s == 'Activated' for s in states))

    def test_parse_dp_states_all_deactivated(self):
        from cmis_registers import parse_dp_states
        # 0x1 = DPDeactivated
        raw = bytes([0x11] * 4)
        states = parse_dp_states(raw)
        self.assertTrue(all(s == 'Deactivated' for s in states))

    def test_unpack_appselect_all_ones(self):
        from cmis_registers import unpack_appselect
        # 1 byte/lane, AppSel in bits[7:4]: 0x11 → AppSel 1
        data = bytes([0x11] * 8)
        lanes = unpack_appselect(data)
        self.assertEqual(lanes, [1, 1, 1, 1, 1, 1, 1, 1])

    def test_pack_unpack_appselect_roundtrip(self):
        from cmis_registers import pack_appselect, unpack_appselect
        original = [1, 2, 3, 4, 5, 6, 7, 8]
        packed = pack_appselect(original)
        # pack only stores 4 bits per lane (nibble), so values must be 0-15
        unpacked = unpack_appselect(packed)
        self.assertEqual(unpacked, [v & 0x0F for v in original])

    def test_module_id_name_known(self):
        from cmis_registers import module_id_name
        self.assertEqual(module_id_name(0x1E), "QSFP-DD CMIS")

    def test_module_id_name_unknown(self):
        from cmis_registers import module_id_name
        result = module_id_name(0xFF)
        self.assertIn('Unknown', result)

    def test_cmis_revision_str(self):
        from cmis_registers import cmis_revision_str
        self.assertEqual(cmis_revision_str(0x50), '5.0')
        self.assertEqual(cmis_revision_str(0x53), '5.3')

    def test_pack_appselect_length(self):
        from cmis_registers import pack_appselect
        # Always 8 bytes (one per lane, zero-padded)
        result = pack_appselect([1] * 8)
        self.assertEqual(len(result), 8)

    def test_pack_appselect_nibble_boundary(self):
        """Values > 15 should be masked to nibble."""
        from cmis_registers import pack_appselect, unpack_appselect
        packed = pack_appselect([0xFF] * 8)
        unpacked = unpack_appselect(packed)
        # 0xFF & 0x0F = 0xF = 15
        self.assertTrue(all(v == 15 for v in unpacked))


# ============================================================
# Request provenance (anti-CSRF / anti-DNS-rebinding)
# ============================================================

class TestForeignRequestsRefused(CMISTestCase):
    """Any other website the user opens must not be able to drive this API.

    Serving no CORS headers does not achieve that: it withholds the response
    from the attacker but the request still executes. Before this guard, a
    text/plain POST from any page wrote to the attached module and returned
    200 - a real risk of damaging expensive hardware, not just a nuisance.
    """

    WRITE = {'page': 0x10, 'address': 130, 'data': [0xFF]}

    def test_the_original_attack_is_refused(self):
        """Verbatim replay of what used to succeed: a CORS-simple POST."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data=json.dumps(self.WRITE),
                              content_type='text/plain',
                              headers={'Origin': 'https://evil.example',
                                       'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(rv.status_code, 403)

    def test_cross_site_fetch_metadata_is_refused(self):
        self.connect()
        rv = self.client.post('/api/register/write', json=self.WRITE,
                              headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(rv.status_code, 403)

    def test_foreign_origin_is_refused(self):
        self.connect()
        rv = self.client.post('/api/register/write', json=self.WRITE,
                              headers={'Origin': 'https://evil.example'})
        self.assertEqual(rv.status_code, 403)

    def test_state_changing_get_is_refused(self):
        """A bare <img src> GET carries no Origin, so Sec-Fetch-Site is the
        only thing standing between a foreign page and this route."""
        self.connect()
        rv = self.client.get('/api/disconnect',
                             headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(rv.status_code, 403)

    def test_dns_rebinding_is_refused(self):
        """With a name the attacker owns pointed at 127.0.0.1 the browser
        calls their page same-origin and would read the replies too. The Host
        header is what gives it away."""
        self.connect()
        rv = self.client.get('/api/module/info',
                             headers={'Host': 'evil.example'})
        self.assertEqual(rv.status_code, 403)

    def test_form_encoded_post_is_refused(self):
        """The layer that holds even if a browser sends no metadata at all."""
        self.connect()
        rv = self.client.post('/api/register/write',
                              data='page=16&address=130&data=255',
                              content_type='application/x-www-form-urlencoded')
        self.assertEqual(rv.status_code, 415)

    def test_the_real_ui_still_works(self):
        """The guard is worthless if it also blocks the page it protects."""
        self.connect()
        rv = self.client.post('/api/register/write', json=self.WRITE,
                              headers={'Origin': 'http://127.0.0.1:5000',
                                       'Sec-Fetch-Site': 'same-origin',
                                       'Host': '127.0.0.1:5000'})
        self.assertOk(rv)

    def test_address_bar_and_localhost_still_work(self):
        """Sec-Fetch-Site: none is what a browser sends for a typed URL."""
        rv = self.client.get('/api/version',
                             headers={'Sec-Fetch-Site': 'none',
                                      'Host': 'localhost:5000'})
        self.assertOk(rv)


# ============================================================
# Update-path hardening
# ============================================================

class TestUpdateTrustBoundary(unittest.TestCase):

    def test_install_path_cannot_inject_powershell(self):
        """A Windows directory may legally be named `$(...)`, and inside a
        double-quoted PowerShell string that runs before the cmdlet does -
        under -ExecutionPolicy Bypass. Paths must be single-quoted literals."""
        import updater as u
        evil = r'D:\tools\$(Start-Process calc.exe)\CMIS'
        ps = u.build_swap_script(evil + r'\_cmis_update', evil)
        self.assertNotIn(f'"{evil}', ps, 'path landed in an expanding string')
        self.assertIn(f"'{evil}", ps)

    def test_embedded_quote_is_escaped(self):
        import updater as u
        self.assertEqual(u.ps_literal(r"D:\it's here"), r"'D:\it''s here'")

    def test_asset_url_must_be_https_on_github(self):
        import updater as u
        for bad in ('http://attacker.example/p.zip',
                    'https://attacker.example/p.zip',
                    'https://github.com.evil.example/p.zip',
                    'http://github.com/o/r/p.zip'):
            self.assertFalse(u.is_trusted_url(bad), bad)
        for good in (_GH_URL, 'https://objects.githubusercontent.com/x'):
            self.assertTrue(u.is_trusted_url(good), good)

    def test_release_pointing_off_github_is_not_installable(self):
        import updater as u
        self.assertIsNone(u.normalize_release({
            'tag_name': 'v9.9.9',
            'assets': [{'name': 'CMIS_dist_v9_9_9.zip', 'size': 1,
                        'browser_download_url': 'https://attacker.example/p.zip'}],
        }))

    def test_download_refuses_an_untrusted_url(self):
        """Belt and braces: the check does not rely on normalize_release
        having been the only way the URL was chosen."""
        import updater as u
        with self.assertRaises(ValueError):
            u.download_asset('http://attacker.example/p.zip', 'unused.zip')

    def test_redirects_off_github_are_refused(self):
        """urllib follows redirects on its own; without this handler a 302
        could walk the download onto plain http or another host entirely."""
        import updater as u
        h = u._TrustedRedirectHandler()
        with self.assertRaises(Exception):
            h.redirect_request(None, None, 302, 'Found', {},
                               'http://attacker.example/p.zip')


PREFS_KEY = 'cmis.ui'


class TestDisplayPreferences(CMISTestCase):
    """The UI is themeable and scalable, which only holds while nothing
    hard-codes a colour or a size behind the variable system's back.

    These read the three frontend files as text. Coarse, but the alternative is
    a browser in the test loop, and what they catch is exactly the silent kind
    of breakage: a stray hex that looks right in the theme it was picked for, a
    breakpoint that quietly stops matching once the user changes scale.
    """

    def _read(self, *parts):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        return io.open(path, encoding='utf-8').read()

    def _css(self):
        return self._read('static', 'style.css')

    def _js(self):
        return self._read('static', 'app.js')

    def _html(self):
        return self._read('templates', 'index.html')

    def _palettes(self):
        """Token names defined by each theme block, keyed by theme name."""
        pattern = r':root(?:,\s*\n:root)?\[data-theme="(\w+)"\]\s*\{(.*?)\n\}'
        return {m.group(1): set(re.findall(r'(--[\w-]+)\s*:', m.group(2)))
                for m in re.finditer(pattern, self._css(), re.S)}

    def _rules(self):
        """The stylesheet with the palette blocks removed."""
        return self._css().split('* {\n  box-sizing', 1)[1]

    def test_every_theme_defines_the_same_tokens(self):
        """A theme that omits a token silently inherits the default palette's.

        That is how a light theme ends up drawing pale-on-white alarm text: it
        looks finished until the one value it forgot lands on the wrong ground.
        """
        palettes = self._palettes()
        self.assertGreaterEqual(len(palettes), 4, f'themes: {sorted(palettes)}')
        self.assertIn('midnight', palettes)
        base = palettes['midnight']
        self.assertGreater(len(base), 25, 'the default palette looks truncated')
        for name, tokens in palettes.items():
            self.assertEqual(base - tokens, set(), f'theme {name} is missing tokens')
            self.assertEqual(tokens - base, set(), f'theme {name} defines extra tokens')

    def test_every_theme_declares_a_colour_scheme(self):
        """Dropdown popups and scrollbars are drawn by the OS, not by CSS, so a
        light theme without color-scheme gets black popups."""
        self.assertEqual(len(re.findall(r'color-scheme:', self._css())),
                         len(self._palettes()))

    def test_no_colour_literals_outside_the_palettes(self):
        """A colour belongs to a theme. A literal belongs to whichever theme
        its author happened to be looking at."""
        offenders = [line.strip() for line in self._rules().splitlines()
                     if re.search(r':[^;]*#[0-9a-fA-F]{3,8}\b', line)
                     or re.search(r':[^;]*\brgba?\(', line)]
        self.assertEqual(offenders, [])

    def test_no_colour_literals_in_the_frontend_scripts(self):
        """The post-update screen used to hard-code pale green on a dark
        ground. On a light theme that is near-white on white, and it is the one
        screen that tells the user how to finish updating."""
        for name, text in (('app.js', self._js()), ('index.html', self._html())):
            self.assertEqual(re.findall(r'(?<!&)#[0-9a-fA-F]{3,8}\b', text), [],
                             f'{name} hard-codes a colour')

    def test_font_sizes_all_use_tokens(self):
        """One step has to move every size together, so no size may opt out."""
        for name, text in (('style.css', self._css()), ('index.html', self._html()),
                           ('app.js', self._js())):
            self.assertEqual(re.findall(r'font-size:\s*\d+px', text), [],
                             f'{name} sets a font-size outside the scale')

    def test_font_step_is_written_with_a_unit(self):
        """calc(10px + 2) is invalid at computed-value time, and an invalid
        font-size falls back to the inherited one - so a missing 'px' would
        collapse every label in the app at once."""
        for text in (self._js(), self._html()):
            for m in re.finditer(r"setProperty\(\s*'--fs-step'\s*,\s*([^)]+)\)", text):
                self.assertIn("'px'", m.group(1), f'unitless --fs-step: {m.group(1)}')

    def test_monospace_survives_the_font_setting(self):
        """Hex dumps and register columns must keep their digits aligned
        whatever family the user picks, so no preset reassigns --font-mono."""
        css = self._css()
        self.assertIn('font-family: var(--font-mono);', css)
        for m in re.finditer(r':root\[data-font-sans="\w+"\]\s*\{([^}]*)\}', css):
            self.assertNotIn('--font-mono:', m.group(1))

    def test_no_magic_viewport_arithmetic(self):
        """The shell used to subtract a hard-coded header height. It was
        already a pixel out, and it multiplies wrongly once the UI can scale."""
        css = self._css()
        self.assertNotIn('calc(100vh', css)
        self.assertIn('inset: 0;', css)

    def test_scale_uses_zoom_not_transform(self):
        """transform: scale() rasterises then resamples - blurry text - and
        does not reflow, so nothing inside would adapt to the new width."""
        css = self._css()
        self.assertIn('zoom: var(--ui-zoom)', css)
        self.assertNotIn('transform: scale(', css)

    def test_breakpoints_use_container_queries(self):
        """@media measures the unzoomed viewport. At 150% on a 1920 screen the
        content is effectively 1280 wide while every media query still reports
        1920, so the breakpoints stop firing exactly when they are needed."""
        css = self._css()
        self.assertGreaterEqual(len(re.findall(r'@container\s', css)), 4)
        self.assertEqual(re.findall(r'@media\s*\(', css), [])
        self.assertIn('container-type: inline-size', css)

    def test_every_table_is_scroll_contained(self):
        """The counters table passes 1300px once total_bits reaches 18 digits,
        which is minutes into any run. With no scroll box it draws outside its
        own card and reads as a rendering fault."""
        html = self._html()
        self.assertEqual(html.count('<table'), html.count('class="table-scroll"'))
        self.assertNotIn('float:right', html)

    def test_theme_is_applied_before_the_first_paint(self):
        """Applying the saved theme from app.js would paint the default palette
        first and then swap, which reads as a flash on every launch."""
        head = self._html().split('</head>', 1)[0]
        self.assertIn(PREFS_KEY, head)
        self.assertLess(head.index(PREFS_KEY), head.index('style.css'))
        self.assertIn('documentElement', head)
        self.assertIn('data-theme', head)

    def test_preferences_survive_the_body_being_replaced(self):
        """_waitForNewVersion() assigns document.body.innerHTML, so anything
        parked on <body> goes with it."""
        block = self._js().split('function applyPrefs', 1)[1].split('\n}', 1)[0]
        self.assertIn('documentElement', block)
        self.assertNotIn('document.body', block)

    def test_preferences_key_agrees_between_bootstrap_and_app(self):
        """The key is necessarily spelled out twice. If the two drift the saved
        settings are silently discarded on every reload."""
        self.assertIn(PREFS_KEY, self._html())
        self.assertIn(PREFS_KEY, self._js())

    def test_auto_scale_never_shrinks(self):
        """innerWidth is CSS pixels, so a 1366x768 laptop at 125% reports about
        1093 - a small workspace, not a dense one. Scaling down there would
        shrink the text that is already hardest to read."""
        block = self._js().split('function autoScale', 1)[1].split('\n}', 1)[0]
        factors = [float(x) for x in re.findall(r'return\s+([\d.]+);', block)]
        self.assertTrue(factors, 'autoScale returns nothing')
        self.assertEqual(min(factors), 1, f'auto scale goes below 100%: {factors}')
        self.assertNotIn('devicePixelRatio', block)

    def test_display_settings_reachable_without_a_connection(self):
        """updateConnectionUI disables every tab button until a module is
        connected, and someone whose UI is unreadable has to fix that first."""
        html = self._html()
        self.assertIn('id="btn-settings"', html)
        tabs = html.split('<nav class="tabs">', 1)[1].split('</nav>', 1)[0]
        self.assertNotIn('btn-settings', tabs)
        button = re.search(r'<button[^>]*id="btn-settings"[^>]*>', html).group(0)
        self.assertNotIn('disabled', button)

    def test_manual_documents_the_display_settings(self):
        """CLAUDE.md requires the manual to track user-visible behaviour."""
        manual = self._read('CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        for phrase in ('显示设置', '主题', '界面缩放'):
            self.assertIn(phrase, manual, f'the manual never mentions {phrase}')


class TestManualMatchesBehaviour(CMISTestCase):
    """The manual ships to customers as the only description of the product.

    These pin the claims that were found wrong once already, so a rewrite or a
    careless version bump cannot quietly put them back.
    """

    def _manual(self):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'CMIS2Customer', 'CMIS模块管理工具操作手册.html')
        return io.open(path, encoding='utf-8').read()

    def test_the_alarm_fallback_is_not_described_as_symmetric(self):
        """ALARM_FALLBACK is -10/+3 dBm; '±10' was never a value in the code."""
        manual = self._manual()
        self.assertNotIn('±10', manual)
        self.assertIn('−10 / +3', manual)

    def test_the_guard_paragraph_keeps_its_version_and_its_scope(self):
        """Bumping the version with a global search-and-replace rewrote history
        here once: the cross-site guard shipped in v2.1.0, not v2.2.0. The
        scope matters just as much - the guard covers /api/, so a reader must
        not conclude the page failing to load is what keeps them safe.
        """
        manual = self._manual()
        self.assertIn('自 <b>v2.1.0</b> 起', manual)
        self.assertIn('/api/', manual)
        for name in ('127.0.0.1', 'localhost', '[::1]'):
            self.assertIn(name, manual, f'{name} also reaches the API')

    def test_the_datapath_apply_carries_a_traffic_warning(self):
        """Apply restarts all eight lanes including the untouched ones, which
        is the easiest way in the whole UI to drop live traffic by accident."""
        manual = self._manual()
        self.assertIn('Apply 会重启全部 8 条 lane', manual)
        section = manual.split('9.3 DataPath 配置表', 1)[1].split('9.4', 1)[0]
        self.assertIn('callout-warn', section)
        self.assertIn('中断', section)

    def test_the_appendix_lists_the_buttons_that_disrupt_traffic(self):
        """It calls itself a button reference; omitting the dangerous ones is
        how a reader concludes none of them are dangerous."""
        appendix = self._manual().split('13. 附录', 1)[1]
        for button in ('Reset Module', 'Enter LowPwr', 'Exit LowPwr', 'Write'):
            self.assertIn(button, appendix, f'{button} is missing')

    def test_a_missing_digest_reads_differently_from_a_bad_one(self):
        """"We checked and it was wrong" and "there was nothing to check
        against" call for different reactions from the user."""
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
        src = io.open(path, encoding='utf-8').read()
        self.assertIn('publishes no SHA-256 digest', src)
        self.assertIn('failed its checksum', src)


class TestRequestGuardScope(CMISTestCase):
    """The provenance guard covers the API, not the page."""

    def test_the_page_and_its_assets_are_not_gated(self):
        """Serving the HTML and the stylesheet grants no capability - every way
        to reach the module is under /api/ - so refusing a cross-site
        navigation only meant that following a link to this tool from a wiki or
        a chat message landed the user on a 403 instead of the UI.
        """
        for path in ('/', '/static/style.css', '/static/app.js'):
            r = self.client.get(path, headers={'Sec-Fetch-Site': 'cross-site'})
            self.assertEqual(r.status_code, 200, path)

    def test_the_api_is_still_gated(self):
        for path in ('/api/version', '/api/backends', '/api/disconnect'):
            r = self.client.get(path, headers={'Sec-Fetch-Site': 'cross-site'})
            self.assertEqual(r.status_code, 403, path)


# ============================================================
# 10b. POST /api/eeprom/read — bulk EEPROM dump, up to 1024 bytes
# ============================================================

class _RecordingBackend:
    """Fake I2C target that records address switches, reads and writes.

    Byte values encode the current slave address, so a test can prove that
    each 256-byte block of a 24C08 was read from its own device and not by
    repeating the first block four times.
    """

    def __init__(self):
        self.address = 0x50
        self.switches = []
        self.reads = []
        self.writes = []
        self.connected = True

    def set_address(self, address):
        self.switches.append(address)
        self.address = address

    def read_bytes(self, register, length):
        self.reads.append((self.address, register, length))
        return bytes(((self.address << 4) ^ register ^ i) & 0xFF
                     for i in range(length))

    def write_bytes(self, register, data):
        # A dump must be strictly read-only: writing 0x7F would corrupt a
        # plain EEPROM byte, and writing any control byte would be worse.
        self.writes.append((register, bytes(data)))

    def disconnect(self):
        self.connected = False

    @property
    def is_connected(self):
        return self.connected


class _NoSwitchBackend(_RecordingBackend):
    """Backend without the optional address-switch capability."""

    def set_address(self, address):
        raise NotImplementedError(
            "_NoSwitchBackend does not support switching the I2C address")


class TestEepromBulkRead(CMISTestCase):

    def tearDown(self):
        super().tearDown()
        _state['page'] = None

    def install(self, backend=None, address=0x50, page=None):
        fake = backend or _RecordingBackend()
        _state['backend'] = fake
        _state['connected'] = True
        _state['bus'] = 0
        _state['address'] = address
        _state['page'] = page
        return fake

    def post(self, body):
        return self.client.post('/api/eeprom/read',
                                data=json.dumps(body),
                                content_type='application/json')

    def test_not_connected(self):
        self.assertErr(self.post({'length': 1024}), 503)

    # ---- Device selection: 24C02 / 24C04 / 24C08 ---------------------------

    def test_chip_24c04_full_dump_uses_two_blocks(self):
        fake = self.install()
        d = self.assertOk(self.post({'chip': '24C04'}))['data']
        self.assertEqual(d['chip'], '24c04')
        self.assertEqual(d['chip_name'], '24C04')
        self.assertEqual(d['capacity'], 512)
        self.assertEqual(d['page_size'], 16)
        self.assertEqual(d['blocks'], 2)
        self.assertEqual(d['address_mode'], 'auto')
        self.assertEqual(d['chunk'], 16)
        self.assertEqual(d['length'], 512)
        self.assertEqual(d['transactions'], 32)
        self.assertEqual([s['address'] for s in d['segments']], [0x50, 0x51])
        self.assertEqual(fake.switches[-1], 0x50)  # port restored

    def test_chip_24c04_block_count_controls_the_address_window(self):
        self.install()
        # 24C04 needs base+0..base+1; 0x7F leaves no room for the second block.
        self.assertErr(self.post({'chip': '24C04', 'base_address': '0x7F',
                                  'length': 16}), 400)
        d = self.assertOk(self.post({'chip': '24C04', 'base_address': '0x7E',
                                     'length': 16}))['data']
        self.assertEqual([s['address'] for s in d['segments']], [0x7E])

    def test_chip_24c08_full_dump(self):
        fake = self.install()
        d = self.assertOk(self.post({'chip': '24C08'}))['data']
        self.assertEqual(d['chip'], '24c08')
        self.assertEqual(d['chip_name'], '24C08')
        self.assertEqual(d['capacity'], 1024)
        self.assertEqual(d['page_size'], 16)
        self.assertEqual(d['blocks'], 4)
        self.assertEqual(d['address_mode'], 'auto')
        self.assertEqual(d['chunk'], 16)          # default = datasheet page
        self.assertEqual(d['length'], 1024)
        self.assertEqual(d['transactions'], 64)
        self.assertEqual([s['address'] for s in d['segments']],
                         [0x50, 0x51, 0x52, 0x53])
        self.assertEqual(fake.switches[-1], 0x50)  # port restored

    def test_chip_24c02_reads_one_256_byte_device(self):
        fake = self.install()
        d = self.assertOk(self.post({'chip': '24C02'}))['data']
        self.assertEqual(d['chip'], '24c02')
        self.assertEqual(d['chip_name'], '24C02')
        self.assertEqual(d['capacity'], 256)
        self.assertEqual(d['page_size'], 8)
        self.assertEqual(d['blocks'], 1)
        self.assertEqual(d['address_mode'], 'single')
        self.assertEqual(d['chunk'], 8)
        self.assertEqual(d['length'], 256)
        self.assertEqual(d['transactions'], 32)
        self.assertEqual([s['address'] for s in d['segments']], [0x50])
        self.assertEqual(fake.switches, [])       # never retargeted
        self.assertEqual(fake.writes, [])

    def test_chip_capacity_is_enforced(self):
        self.install()
        cases = [
            {'chip': '24c02', 'length': 257},
            {'chip': '24c02', 'start': 256},
            {'chip': '24c02', 'start': 200, 'length': 100},
            {'chip': '24c02', 'start': 0x300, 'length': 1},
            {'chip': '24c08', 'length': 1025},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertErr(self.post(body), 400)

    def test_unknown_chip_is_rejected(self):
        self.install()
        for chip in ('24c16', '24C01', '', 'bogus'):
            with self.subTest(chip=chip):
                self.assertErr(self.post({'chip': chip}), 400)

    def test_chip_name_normalisation(self):
        self.install()
        for chip in ('24C08', '24c08', '24-C08', ' 24C08 '):
            with self.subTest(chip=chip):
                d = self.assertOk(self.post({'chip': chip, 'length': 16}))['data']
                self.assertEqual(d['chip'], '24c08')

    def test_chip_overrides_legacy_address_mode(self):
        fake = self.install()
        d = self.assertOk(self.post(
            {'chip': '24C02', 'address_mode': 'auto', 'length': 256}))['data']
        self.assertEqual(d['address_mode'], 'single')
        self.assertEqual([s['address'] for s in d['segments']], [0x50])
        self.assertEqual(fake.switches, [])

    def test_legacy_address_mode_still_works(self):
        self.install()
        d = self.assertOk(self.post(
            {'address_mode': 'single', 'length': 256}))['data']
        self.assertIsNone(d['chip'])
        self.assertIsNone(d['chip_name'])
        self.assertEqual(d['capacity'], 256)
        self.assertEqual(d['chunk'], 32)          # legacy default chunk
        self.assertEqual([s['address'] for s in d['segments']], [0x50])

    def test_profiles_match_the_datasheets(self):
        # Guard the single source of truth against accidental edits: these
        # values are quoted in the UI's chip-information panel.
        profiles = app_module.EEPROM_PROFILES
        self.assertEqual(profiles['24c02']['capacity'], 256)
        self.assertEqual(profiles['24c02']['page_size'], 8)
        self.assertEqual(profiles['24c02']['bit_capacity'], '2 Kbits')
        self.assertEqual(profiles['24c02']['address_mode'], 'single')
        self.assertEqual(profiles['24c04']['capacity'], 512)
        self.assertEqual(profiles['24c04']['page_size'], 16)
        self.assertEqual(profiles['24c04']['blocks'], 2)
        self.assertEqual(profiles['24c04']['bit_capacity'], '4 Kbits')
        self.assertEqual(profiles['24c04']['addresses'], '0xA0–0xA2')
        self.assertEqual(profiles['24c04']['address_mode'], 'auto')
        self.assertEqual(profiles['24c08']['capacity'], 1024)
        self.assertEqual(profiles['24c08']['page_size'], 16)
        self.assertEqual(profiles['24c08']['bit_capacity'], '8 Kbits')
        self.assertEqual(profiles['24c08']['address_mode'], 'auto')

    # ---- Legacy / generic behaviour ---------------------------------------

    def test_default_is_a_full_1024_byte_dump(self):
        self.install(page=0x02)
        d = self.assertOk(self.post({}))['data']
        self.assertEqual(d['start'], 0)
        self.assertEqual(d['end'], 1024)
        self.assertEqual(d['length'], 1024)
        self.assertEqual(d['chunk'], 32)
        self.assertEqual(d['address_mode'], 'auto')
        self.assertEqual(d['base_address'], 0x50)
        self.assertEqual(len(d['data']), 1024)
        self.assertEqual(d['transactions'], 32)
        # Retargeting the bus must invalidate any cached CMIS page selection.
        self.assertIsNone(_state['page'])

    def test_four_segments_one_per_24c08_block(self):
        fake = self.install()
        d = self.assertOk(self.post({}))['data']
        self.assertEqual([s['address'] for s in d['segments']],
                         [0x50, 0x51, 0x52, 0x53])
        self.assertEqual([s['start'] for s in d['segments']],
                         [0, 256, 512, 768])
        self.assertEqual([s['count'] for s in d['segments']], [256] * 4)
        self.assertEqual([s['register_start'] for s in d['segments']], [0] * 4)
        self.assertEqual([d['data'][i] for i in (0, 256, 512, 768)],
                         [0x00, 0x10, 0x20, 0x30])
        # Initial block needs no switch; the last entry is the restore.
        self.assertEqual(fake.switches, [0x51, 0x52, 0x53, 0x50])
        self.assertEqual(fake.writes, [])

    def test_every_chunk_stays_inside_one_256_byte_block(self):
        fake = self.install()
        self.assertOk(self.post({}))
        self.assertEqual(len(fake.reads), 32)
        for addr, register, length in fake.reads:
            self.assertIn(addr, (0x50, 0x51, 0x52, 0x53))
            self.assertLess(register, 256)
            self.assertLessEqual(length, 32)
            self.assertLessEqual(register + length, 256)
        for addr in (0x50, 0x51, 0x52, 0x53):
            self.assertEqual(sum(1 for r in fake.reads if r[0] == addr), 8)

    def test_custom_chunk_size(self):
        fake = self.install()
        d = self.assertOk(self.post({'chunk': 64}))['data']
        self.assertEqual(d['transactions'], 16)
        self.assertEqual(len(d['data']), 1024)
        self.assertEqual([r[1] for r in fake.reads if r[0] == 0x50],
                         [0, 64, 128, 192])

    def test_partial_range_at_top_of_memory(self):
        self.install()
        d = self.assertOk(self.post(
            {'start': 0x300, 'length': 256, 'chunk': 128}))['data']
        self.assertEqual(d['start'], 0x300)
        self.assertEqual(d['length'], 256)
        self.assertEqual([s['address'] for s in d['segments']], [0x53])
        self.assertEqual(d['transactions'], 2)
        self.assertEqual(d['data'][0], 0x30)

    def test_last_byte_is_addressable(self):
        self.install()
        d = self.assertOk(self.post({'start': 0x3FF, 'length': 1}))['data']
        self.assertEqual(d['length'], 1)
        # word address 0xFF on device 0x53 → (0x53<<4) ^ 0xFF
        self.assertEqual(d['data'], [0x30 ^ 0xFF])

    def test_custom_base_address_and_restore(self):
        fake = self.install(address=0x50)
        d = self.assertOk(self.post({'base_address': 0x52, 'length': 512}))['data']
        self.assertEqual([s['address'] for s in d['segments']], [0x52, 0x53])
        self.assertEqual(fake.address, 0x50)

    def test_single_address_mode(self):
        fake = self.install()
        d = self.assertOk(self.post(
            {'address_mode': 'single', 'length': 256}))['data']
        self.assertEqual(len(d['data']), 256)
        self.assertEqual([s['address'] for s in d['segments']], [0x50])
        self.assertEqual(fake.switches, [])

    def test_hex_string_parameters(self):
        self.install()
        d = self.assertOk(self.post(
            {'start': '0x10', 'length': '0x20', 'chunk': '0x10',
             'base_address': '0x50'}))['data']
        self.assertEqual(d['start'], 0x10)
        self.assertEqual(d['length'], 0x20)
        self.assertEqual(d['chunk'], 0x10)
        self.assertEqual(d['transactions'], 2)

    def test_invalid_parameters_are_rejected(self):
        self.install()
        cases = [
            {'length': 0},
            {'length': 1025},
            {'start': -1},
            {'start': 1024},
            {'start': 1000, 'length': 100},
            {'chunk': 0},
            {'chunk': 129},
            {'address_mode': 'bogus'},
            {'base_address': 0x80},
            {'base_address': 0x7E},
            {'address_mode': 'single', 'length': 257},
            {'start': 'not-a-number'},
            {'chunk': '0xZZ'},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertErr(self.post(body), 400)

    def test_short_read_reports_error_and_restores_address(self):
        class Short(_RecordingBackend):
            def read_bytes(self, register, length):
                if self.address == 0x51:
                    return b'\x00'      # deliberately short
                return super().read_bytes(register, length)

        fake = self.install(backend=Short())
        body = self.assertErr(self.post({}), 500)
        self.assertIn('short read', body['message'])
        self.assertEqual(fake.address, 0x50)
        self.assertEqual(fake.switches[-1], 0x50)

    def test_backend_without_address_switch_returns_501(self):
        self.install(backend=_NoSwitchBackend())
        body = self.assertErr(self.post({}), 501)
        self.assertIn('does not support', body['message'])

    def test_bulk_read_works_with_the_real_mock_backend(self):
        self.connect()
        d = self.assertOk(self.post({'length': 1024}))['data']
        self.assertEqual(len(d['data']), 1024)
        self.assertEqual([s['address'] for s in d['segments']],
                         [0x50, 0x51, 0x52, 0x53])

    # ---- Auto update: S/N rule + consumption analysis --------------------

    def _image_backend(self, sn: str, percent: int):
        """A 24C08 model carrying a chosen S/N and consumption percentage."""
        fake = _MemoryBackend()
        data = bytearray([0xAA]) * 1024
        data[0x000:0x000 + len(sn)] = sn.encode('ascii')
        data[0x09C] = (percent >> 8) & 0xFF
        data[0x09D] = percent & 0xFF
        for block, base in enumerate((0x50, 0x51, 0x52, 0x53)):
            for i, b in enumerate(data[block * 256:(block + 1) * 256]):
                fake.memory[(base, i)] = b
        return fake

    def test_read_reports_consumption_and_serial_rule(self):
        fake = self._image_backend('CAD211194102', 0x32)  # 50%, product S/N
        self.install(backend=fake)
        d = self.assertOk(self.post({'chip': '24C08'}))['data']
        self.assertTrue(d['serial_rule_ok'])
        self.assertEqual(d['serial_number'], 'CAD211194102')
        self.assertEqual(d['consumption_offset'], '0x09C')
        self.assertEqual(d['consumption_end'], '0x09D')
        self.assertEqual(d['consumption_percent'], 50)
        self.assertEqual(d['consumption_raw'], [0x00, 0x32])
        self.assertEqual(d['consumption_full_percent'], 100)
        self.assertTrue(d['consumption_needs_fix'])
        self.assertEqual(fake.writes, [])     # Read stays strictly read-only

    def test_read_full_consumption_needs_no_fix(self):
        self.install(backend=self._image_backend('CAA212600066', 100))
        d = self.assertOk(self.post({'chip': '24C08'}))['data']
        self.assertTrue(d['serial_rule_ok'])
        self.assertEqual(d['consumption_percent'], 100)
        self.assertFalse(d['consumption_needs_fix'])

    def test_bad_serial_blocks_auto_update_even_when_consumption_low(self):
        # A random/malformed S/N must not trigger an automatic rewrite.
        self.install(backend=self._image_backend('NOTAPRODUCT00', 20))
        d = self.assertOk(self.post({'chip': '24C08'}))['data']
        self.assertFalse(d['serial_rule_ok'])
        self.assertTrue(d['consumption_needs_fix'])

    def test_consumption_absent_when_dump_does_not_cover_offset(self):
        self.install(backend=self._image_backend('CAD211194102', 50))
        d = self.assertOk(self.post({'chip': '24C02', 'length': 16}))['data']
        self.assertNotIn('consumption_percent', d)
        self.assertNotIn('consumption_needs_fix', d)

    def test_serial_rule_functions(self):
        ok = app_module._serial_matches_product_rule
        for sn in ('CAA212600066', 'CAD211194102', 'XYZ990010000',
                   'CAD211193882'):
            with self.subTest(sn=sn):
                self.assertTrue(ok(sn))
        for sn in ('CAA21260006G', '123212600066', 'CAA2126A0066',
                   'CAA2126000', '', None):
            with self.subTest(sn=sn):
                self.assertFalse(ok(sn))

    def test_consumption_constants_match_report(self):
        self.assertEqual(app_module.CONSUMPTION_OFFSET, 0x09C)
        self.assertEqual(app_module.CONSUMPTION_LENGTH, 2)
        self.assertEqual(app_module.CONSUMPTION_FULL_PERCENT, 0x64)

    # ---- Blank chip detection (auto-program with the selected .bin) ------

    def _blank_backend(self):
        """A virgin 24C08: every byte erased (0xFF), S/N field included."""
        fake = _MemoryBackend()
        for block, base in enumerate((0x50, 0x51, 0x52, 0x53)):
            for i in range(256):
                fake.memory[(base, i)] = 0xFF
        return fake

    def test_blank_chip_flag_on_erased_part(self):
        fake = self._blank_backend()
        self.install(backend=fake)
        d = self.assertOk(self.post({'chip': '24C08'}))['data']
        self.assertTrue(d['blank_chip'])
        # Read stays read-only: a blank chip must not be written by Read itself.
        self.assertEqual(fake.writes, [])
        # 0xFF is not printable ASCII, so no product S/N may be inferred;
        # raw hex still reports the actual erased bytes for diagnostics.
        self.assertIsNone(d['serial_number'])
        self.assertFalse(d['serial_rule_ok'])
        self.assertEqual(d['serial_hex'], 'FF FF FF FF FF FF FF FF FF FF FF FF')

    def test_blank_chip_false_for_programmed_part(self):
        self.install(backend=self._image_backend('CAD211194102', 100))
        d = self.assertOk(self.post({'chip': '24C08'}))['data']
        self.assertFalse(d['blank_chip'])

    def test_blank_chip_absent_on_partial_dump(self):
        self.install(backend=self._blank_backend())
        d = self.assertOk(self.post({'chip': '24C08', 'start': 0x10,
                                     'length': 16}))['data']
        self.assertNotIn('blank_chip', d)

    def test_blank_chip_function_edge_cases(self):
        f = app_module._blank_chip
        fields = app_module._blank_chip_read_fields
        self.assertTrue(f(bytes([0xFF]) * 12, 0))
        self.assertTrue(f(bytes([0xFF]) * 1024, 0))
        self.assertFalse(f(bytes([0xFF]) * 3 + bytes([0x00])
                           + bytes([0xFF]) * 8, 0))          # one programmed byte
        self.assertFalse(f(bytes(1024), 0))                  # all zeros
        # Range gating lives in _blank_chip_read_fields, not in _blank_chip.
        self.assertEqual(fields(bytes([0xFF]) * 11, 0, 11), {})
        self.assertEqual(fields(bytes([0xFF]) * 1024, 4, 1028), {})
        self.assertEqual(fields(bytes([0xFF]) * 1024, 0, 12), {'blank_chip': True})


# ============================================================
# 10c. POST /api/eeprom/write — BIN image + serial number programming
# ============================================================

class _MemoryBackend(_RecordingBackend):
    """A 24Cxx model: one byte per (slave address, word address)."""

    def __init__(self):
        super().__init__()
        self.memory = {}
        self.write_log = []

    def write_bytes(self, register, data):
        for i, b in enumerate(data):
            self.memory[(self.address, register + i)] = b
        self.write_log.append((self.address, register, bytes(data)))
        self.writes.append((self.address, register, bytes(data)))

    def read_bytes(self, register, length):
        self.reads.append((self.address, register, length))
        return bytes(self.memory.get((self.address, register + i), 0xFF)
                     for i in range(length))


class TestEepromWrite(CMISTestCase):

    def tearDown(self):
        super().tearDown()
        _state['page'] = None

    def install(self, backend=None, address=0x50, page=None):
        fake = backend if backend is not None else _MemoryBackend()
        _state['backend'] = fake
        _state['connected'] = True
        _state['bus'] = 0
        _state['address'] = address
        _state['page'] = page
        return fake

    @staticmethod
    def _image(size, fill=0xAA):
        return bytearray([fill]) * size

    def body(self, size=256, chip='24C02', **kw):
        payload = {
            'chip': chip,
            'data_b64': base64.b64encode(bytes(self._image(size))).decode('ascii'),
        }
        payload.update(kw)
        return payload

    def post(self, body):
        # Keep the suite fast: tWR is a hardware concern, not a test concern.
        payload = {'confirm': True, 'write_delay_ms': 0}
        payload.update(body)
        return self.client.post('/api/eeprom/write',
                                data=json.dumps(payload),
                                content_type='application/json')

    def read(self, body):
        return self.client.post('/api/eeprom/read',
                                data=json.dumps(body),
                                content_type='application/json')

    # ---- Destructive-operation guards --------------------------------------

    def test_write_requires_explicit_confirmation(self):
        fake = self.install()
        for confirm in (None, False, 'true', 1):
            with self.subTest(confirm=confirm):
                self.assertErr(self.post(self.body(16, confirm=confirm)), 400)
        self.assertEqual(fake.write_log, [])

    def test_chip_is_required_and_validated(self):
        self.install()
        body = self.body(16)
        body.pop('chip')
        self.assertErr(self.post(body), 400)
        for chip in ('24c16', '24C01', '', None, 'bogus'):
            with self.subTest(chip=chip):
                self.assertErr(self.post(self.body(16, chip=chip)), 400)

    def test_image_data_is_required_and_validated(self):
        self.install()
        self.assertErr(self.post({'chip': '24C02'}), 400)
        self.assertErr(self.post({'chip': '24C02', 'data': []}), 400)
        self.assertErr(self.post({'chip': '24C02', 'data': 'not a list'}), 400)
        body = self.assertErr(self.post(
            {'chip': '24C02', 'data_b64': '!!!not base64!!!'}), 400)
        self.assertIn('base64', body['message'])

    # ---- Serial number at 0x000–0x00B (12 printable ASCII bytes) ------------
    SN = 'CAD211193882'
    SN_BYTES = list(b'CAD211193882')
    SN_HEX = '43 41 44 32 31 31 31 39 33 38 38 32'

    def test_24c02_serial_patch_and_page_writes(self):
        fake = self.install()
        d = self.assertOk(self.post(self.body(256, serial_number=self.SN)))['data']
        self.assertEqual(d['chip'], '24c02')
        self.assertEqual(d['length'], 256)
        self.assertEqual(d['transactions'], 32)
        self.assertEqual(d['page_size'], 8)
        self.assertEqual(d['serial_bytes'], self.SN_BYTES)
        self.assertEqual(d['serial_hex'], self.SN_HEX)
        self.assertEqual(d['serial_number'], self.SN)
        self.assertEqual(d['serial_next'], 'CAD211193883')
        self.assertTrue(d['serial_patched'])
        self.assertEqual(d['serial_range'], '0x000–0x00B')
        self.assertTrue(d['verified'])
        self.assertEqual(d['mismatch_count'], 0)
        # The 12-byte ASCII S/N spans two 8-byte pages on a 24C02.
        self.assertEqual(bytes(fake.memory[(0x50, i)] for i in range(16)),
                         bytes(self.SN_BYTES) + b'\xAA' * 4)
        for addr, reg, part in fake.write_log:
            self.assertEqual(addr, 0x50)
            self.assertLessEqual(len(part), 8)
            self.assertEqual(reg % 8, 0)
            self.assertLessEqual(reg + len(part), 256)
        self.assertEqual(fake.switches, [])      # one device, no retargeting

    def test_serial_accepts_ascii_string_and_code_list(self):
        for sn in (self.SN, list(b'CAD211193882')):
            with self.subTest(sn=sn):
                self.install()
                d = self.assertOk(self.post(self.body(16, serial_number=sn)))['data']
                self.assertEqual(d['serial_bytes'], self.SN_BYTES)
                self.assertEqual(d['serial_number'], self.SN)
        self.install()
        d = self.assertOk(self.post(
            self.body(16, serial_number='cad211193882')))['data']
        self.assertEqual(d['serial_number'], 'cad211193882')  # case preserved

    def test_invalid_serial_numbers_are_rejected(self):
        self.install()
        for sn in ('CAD21119388',          # 11 characters
                   'CAD2111938823',        # 13 characters
                   'CAD 211193882',        # space
                   'CAD21119388中',        # non-ASCII
                   'CAD21119388\n2',       # control character
                   [1, 2, 3],              # too few codes
                   list(b'CAD211193882') + [0x41]):   # too many codes
            with self.subTest(sn=sn):
                self.assertErr(self.post(self.body(16, serial_number=sn)), 400)

    def test_serial_lands_exactly_in_bytes_0_to_11(self):
        fake = self.install()
        self.assertOk(self.post(self.body(16, serial_number=self.SN)))
        # Byte 0x00C must keep the image's own value — the SN is only 12 bytes.
        self.assertEqual(bytes(fake.memory[(0x50, i)] for i in range(13)),
                         bytes(self.SN_BYTES) + b'\xAA')

    def test_next_serial_increments_trailing_decimal_with_wrap(self):
        nxt = app_module._next_eeprom_serial
        self.assertEqual(nxt(b'CAD211193882'), 'CAD211193883')
        self.assertEqual(nxt(b'CAD211193899'), 'CAD211193900')
        self.assertEqual(nxt(b'CAD999999999'), 'CAD000000000')
        self.assertEqual(nxt(b'ABCDEFGHIJKL'), 'ABCDEFGHIJKL')  # no trailing digits
        self.assertEqual(app_module._encode_eeprom_serial(self.SN),
                         bytes(self.SN_BYTES))

    def test_next_serial_keeps_letters_and_increments_digit_run(self):
        nxt = app_module._next_eeprom_serial
        # Letters on either side of the digit run must survive untouched.
        self.assertEqual(nxt(b'CAA2127891EB'), 'CAA2127892EB')
        self.assertEqual(nxt(b'CAD21119388D'), 'CAD21119389D')
        self.assertEqual(nxt(b'CAD211193883'), 'CAD211193884')
        self.assertEqual(nxt(b'CAA2127891EB', '3-9'), 'CAA2127892EB')
        self.assertEqual(nxt(b'CAA2127891EB', '9-9'), 'CAA2127892EB')

    def test_serial_pos_validation_and_read_passthrough(self):
        resolve = app_module._resolve_serial_pos
        self.assertEqual(resolve('3-9', 'CAA2127891EB'), (3, 9))
        self.assertEqual(resolve('', 'CAA2127891EB'), (3, 9))
        self.assertIsNone(resolve('', 'ABCDEFGHIJKL'))
        for bad in ('0-11', 'a-b', '0-2', '10-10'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    resolve(bad, 'CAA2127891EB')
        # A bad range must be rejected BEFORE the destructive write.
        self.install()
        self.assertErr(self.post(self.body(
            16, serial_number='CAA2127891EB', serial_pos='0-11')), 400)
        # Read carries serial_pos through to serial_next.
        self.install()
        self.assertOk(self.post(self.body(16, serial_number='CAA2127891EB')))
        d = self.assertOk(self.read({
            'chip': '24C02', 'length': 16, 'serial_pos': '9-9'}))['data']
        self.assertEqual(d['serial_next'], 'CAA2127892EB')

    def test_serial_patch_requires_the_range_to_cover_0x000(self):
        self.install()
        self.assertErr(self.post(
            self.body(32, start=0x10, serial_number=self.SN)), 400)
        d = self.assertOk(self.post(
            self.body(32, start=0x10, serial_number=self.SN,
                      patch_serial=False)))['data']
        self.assertFalse(d['serial_patched'])
        self.assertIsNone(d['serial_bytes'])

    def test_read_decodes_serial_number_from_chip(self):
        self.install()
        self.assertOk(self.post(self.body(32, serial_number=self.SN)))
        d = self.assertOk(self.read({'chip': '24C02', 'length': 32}))['data']
        self.assertEqual(d['serial_number'], self.SN)
        self.assertEqual(d['serial_bytes'], self.SN_BYTES)
        self.assertEqual(d['serial_hex'], self.SN_HEX)
        self.assertEqual(d['serial_next'], 'CAD211193883')
        self.assertEqual(d['serial_range'], '0x000–0x00B')
        # A dump starting at 0x00C must not invent a serial from its own bytes.
        d2 = self.assertOk(self.read(
            {'chip': '24C02', 'start': 0x0C, 'length': 16}))['data']
        self.assertNotIn('serial_number', d2)

    # ---- Page / block boundaries -------------------------------------------

    def test_24c04_switches_two_addresses_and_restores(self):
        fake = self.install()
        d = self.assertOk(self.post(
            self.body(512, chip='24C04', serial_number=self.SN)))['data']
        self.assertEqual(d['chip'], '24c04')
        self.assertEqual(d['capacity'], 512)
        self.assertEqual(d['page_size'], 16)
        self.assertEqual(d['blocks'], 2)
        self.assertEqual(d['transactions'], 32)
        self.assertEqual([s['address'] for s in d['segments']], [0x50, 0x51])
        self.assertEqual([s['count'] for s in d['segments']], [256, 256])
        self.assertEqual(fake.switches, [0x51, 0x50])
        self.assertEqual(fake.address, 0x50)
        for addr, reg, part in fake.write_log:
            self.assertIn(addr, (0x50, 0x51))
            self.assertLessEqual(len(part), 16)
            self.assertEqual(reg % 16, 0)
            self.assertLessEqual(reg + len(part), 256)
        self.assertTrue(d['verified'])

    def test_24c04_block_boundary_is_never_crossed(self):
        fake = self.install()
        self.assertOk(self.post(self.body(12, start=250, chunk=64, chip='24C04')))
        self.assertEqual([(a, r, len(p)) for a, r, p in fake.write_log],
                         [(0x50, 250, 6), (0x51, 0, 6)])

    def test_24c08_switches_four_addresses_and_restores(self):
        fake = self.install()
        d = self.assertOk(self.post(
            self.body(1024, chip='24C08', serial_number=self.SN)))['data']
        self.assertEqual(d['transactions'], 64)
        self.assertEqual([s['address'] for s in d['segments']],
                         [0x50, 0x51, 0x52, 0x53])
        self.assertEqual([s['count'] for s in d['segments']], [256] * 4)
        self.assertEqual(fake.switches, [0x51, 0x52, 0x53, 0x50])
        self.assertEqual(fake.address, 0x50)
        self.assertEqual(bytes(fake.memory[(0x50, i)] for i in range(12)),
                         bytes(self.SN_BYTES))
        self.assertTrue(d['verified'])
        for addr, reg, part in fake.write_log:
            self.assertIn(addr, (0x50, 0x51, 0x52, 0x53))
            self.assertLessEqual(len(part), 16)
            self.assertEqual(reg % 16, 0)
            self.assertLessEqual(reg + len(part), 256)

    def test_page_boundary_is_never_crossed(self):
        fake = self.install()
        self.assertOk(self.post(self.body(40, start=3, chunk=64, chip='24C08')))
        self.assertEqual([(reg, len(part)) for _, reg, part in fake.write_log],
                         [(3, 13), (16, 16), (32, 11)])

    def test_block_boundary_is_never_crossed(self):
        fake = self.install()
        self.assertOk(self.post(self.body(12, start=250, chunk=64, chip='24C08')))
        self.assertEqual([(a, r, len(p)) for a, r, p in fake.write_log],
                         [(0x50, 250, 6), (0x51, 0, 6)])

    def test_write_then_read_roundtrip(self):
        self.install()
        img = bytearray((i * 7 + 3) & 0xFF for i in range(1024))
        self.assertOk(self.post({
            'chip': '24C08', 'serial_number': self.SN,
            'data_b64': base64.b64encode(bytes(img)).decode('ascii'),
        }))
        img[0:12] = bytes(self.SN_BYTES)
        d = self.assertOk(self.read({'chip': '24C08'}))['data']
        self.assertEqual(d['data'], list(img))
        self.assertEqual(d['serial_number'], self.SN)

    # ---- Verification, errors, compatibility --------------------------------

    def test_verify_can_be_disabled(self):
        fake = self.install()
        d = self.assertOk(self.post(self.body(16, verify=False)))['data']
        self.assertIsNone(d['verified'])
        self.assertEqual(d['verify_reads'], 0)
        self.assertEqual(fake.reads, [])

    def test_verify_reports_mismatches(self):
        class Corrupt(_MemoryBackend):
            def write_bytes(self, register, data):
                if self.address == 0x50 and register == 0x00:
                    data = bytes([data[0] ^ 0xFF]) + bytes(data[1:])
                super().write_bytes(register, data)

        self.install(Corrupt())
        d = self.assertOk(self.post(self.body(16)))['data']
        self.assertFalse(d['verified'])
        self.assertGreaterEqual(d['mismatch_count'], 1)
        self.assertEqual(d['mismatches'][0]['offset'], 0)
        self.assertEqual(d['verify_reads'], 2)

    def test_address_is_restored_after_a_failed_write(self):
        class Fail(_MemoryBackend):
            def write_bytes(self, register, data):
                if self.address == 0x52:
                    raise IOError('NACK on block 2')
                super().write_bytes(register, data)

        fake = self.install(Fail())
        body = self.assertErr(self.post(self.body(1024, chip='24C08')), 500)
        self.assertIn('NACK', body['message'])
        self.assertEqual(fake.address, 0x50)

    def test_backend_without_address_switch_returns_501(self):
        self.install(_NoSwitchBackend())
        self.assertErr(self.post(self.body(1024, chip='24C08')), 501)

    def test_invalid_parameters_are_rejected(self):
        self.install()
        cases = [
            self.body(257),                       # past 24C02 capacity
            self.body(1025, chip='24C08'),
            self.body(100, start=200),
            self.body(16, start=-1),
            self.body(16, start=256),
            self.body(16, chunk=0),
            self.body(16, chunk=129),
            self.body(16, write_delay_ms=101),
            self.body(16, base_address=0x80),
            self.body(1024, chip='24C08', base_address=0x7E),
            self.body(16, start='not-a-number'),
        ]
        for body in cases:
            with self.subTest(body={k: v for k, v in body.items()
                                   if k != 'data_b64'}):
                self.assertErr(self.post(body), 400)

    def test_byte_list_and_hex_string_parameters(self):
        fake = self.install()
        d = self.assertOk(self.post({
            'chip': '24C02', 'data': ['0x41', 0x42, '43'],
            'start': '0x10', 'chunk': '0x8', 'verify': False,
        }))['data']
        self.assertEqual(d['start'], 16)
        self.assertEqual(d['length'], 3)
        self.assertEqual([fake.memory[(0x50, 16 + i)] for i in range(3)],
                         [0x41, 0x42, 43])

    def test_write_works_with_real_mock_backend(self):
        self.connect()
        d = self.assertOk(self.post(
            self.body(256, chip='24C02', verify=False)))['data']
        self.assertEqual(d['length'], 256)
        self.assertEqual(d['transactions'], 32)
        self.assertIsNone(d['verified'])


class _FakeCp2112Dll:
    """A ctypes-compatible fake of SLABHIDtoSMBus.dll — no real hardware needed.

    The backend passes ctypes buffers/byref objects to the DLL, so the fake
    must accept those and write results back through the pointers, exactly as
    the real stdcall functions do.
    """

    def __init__(self, num_devices=1, memory=None):
        self.num_devices = num_devices
        # Continuous EEPROM memory; AddressReadRequest reads from target byte.
        self.memory = bytes(memory) if memory is not None else bytes(512)
        self.calls = []
        self.opened = False
        self.config = None
        self.address_reads = []   # (slave8, count, target_size, target_byte)
        self.writes = []          # (slave8, bytes_payload)

    def HidSmbus_GetNumDevices(self, count_ptr, vid, pid):
        self.calls.append(('GetNumDevices', vid, pid))
        count_ptr._obj.value = self.num_devices
        return 0

    def HidSmbus_Open(self, device_ptr, device_num, vid, pid):
        self.calls.append(('Open', device_num, vid, pid))
        device_ptr._obj.value = 0x1234   # real DLL writes a device handle here
        self.opened = True
        return 0

    def HidSmbus_Close(self, device):
        self.calls.append(('Close',))
        self.opened = False
        return 0

    def HidSmbus_SetSmbusConfig(self, device, bitrate, addr, auto, wto, rto, scl, retries):
        self.config = (bitrate, addr, auto, wto, rto, scl, retries)
        return 0

    def HidSmbus_WriteRequest(self, device, slave, buffer, count):
        # buffer is a ctypes c_ubyte array of length `count`
        self.writes.append((slave, bytes(buffer[:count])))
        return 0

    def HidSmbus_AddressReadRequest(self, device, slave, count, tsize, target):
        self.address_reads.append((slave, count, tsize, target[0]))
        # EEPROM sequential read: from the target byte, `count` consecutive bytes.
        start = target[0]
        data = self.memory[start:start + count]
        data = data + bytes(count - len(data))   # pad short reads with 0x00
        self._pending = data
        self._read_cursor = 0
        return 0

    def HidSmbus_ForceReadResponse(self, device, count):
        self.calls.append(('ForceReadResponse', count))
        self._read_cursor = 0   # response stream restarts from byte 0
        return 0

    def HidSmbus_GetReadResponse(self, device, status_ptr, buffer, buffer_size, got_ptr):
        # Real CP2112 (AN496 §2.14): bufferSize must be >= 61 and each call
        # returns AT MOST buffer_size bytes — mimic that so a too-small buffer
        # or a missing collection loop fails the test instead of passing by luck.
        self.calls.append(('GetReadResponse', buffer_size))
        data = getattr(self, '_pending', b'')
        cur = getattr(self, '_read_cursor', 0)
        chunk = data[cur:cur + buffer_size]
        for i, b in enumerate(chunk):
            buffer[i] = b
        self._read_cursor = cur + len(chunk)
        status_ptr._obj.value = 0x02   # HID_SMBUS_S0_COMPLETE
        got_ptr._obj.value = len(chunk)
        return 0

    def HidSmbus_TransferStatusRequest(self, device):
        return 0

    def HidSmbus_GetTransferStatusResponse(
            self, device, status_ptr, detail_ptr, retries_ptr, bytes_ptr):
        # AN496 §2.17 has FIVE output params; a 3-param signature lets the
        # real DLL scribble into stack garbage (access violation).
        self.calls.append(('GetTransferStatusResponse', 5))
        status_ptr._obj.value = 0x02   # HID_SMBUS_S0_COMPLETE
        detail_ptr._obj.value = 0x00
        retries_ptr._obj.value = 0
        bytes_ptr._obj.value = 0
        return 0

    def HidSmbus_CancelTransfer(self, device):
        return 0


class TestCP2112Backend(CMISTestCase):
    """Unit tests for the CP2112 backend with a fake DLL (no hardware)."""

    def _make(self, dll):
        import i2c_backends.cp2112 as cp
        self._patcher = unittest.mock.patch.object(cp, '_load_cp2112_dll', return_value=dll)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        return cp.CP2112Backend()

    def test_registration_and_probe(self):
        body = self.assertOk(self.client.get('/api/backends'))
        names = [b['name'] for b in body['data']]
        self.assertIn('cp2112', names)

    def test_connect_sets_8bit_address_and_config(self):
        dll = _FakeCp2112Dll(num_devices=2)
        be = self._make(dll)
        be.connect(bus=1, address=0x50)
        self.assertTrue(be.is_connected)
        self.assertTrue(dll.opened)
        self.assertEqual(dll.config[1], 0x02)   # own address
        self.assertEqual(dll.config[2], 0)      # autoReadRespond off
        self.assertIn(('GetNumDevices', 0x10C4, 0xEA90), dll.calls)
        self.assertIn(('Open', 1, 0x10C4, 0xEA90), dll.calls)

    def test_connect_rejects_missing_device_and_bad_bus(self):
        be = self._make(_FakeCp2112Dll(num_devices=0))
        with self.assertRaises(IOError):
            be.connect(0, 0x50)
        be2 = self._make(_FakeCp2112Dll(num_devices=1))
        with self.assertRaises(IOError):
            be2.connect(5, 0x50)   # deviceNum out of range

    def test_read_shifts_address_and_returns_bytes(self):
        dll = _FakeCp2112Dll(memory=b'\x1e\x53\x00\x07' + bytes(508))
        be = self._make(dll)
        be.connect(0, 0x50)
        out = be.read_bytes(0x00, 4)
        self.assertEqual(out, b'\x1e\x53\x00\x07')
        self.assertEqual(dll.address_reads[0][0], 0xA0)   # 0x50 << 1
        self.assertEqual(dll.address_reads[0][1], 4)
        self.assertEqual(dll.address_reads[0][2], 1)      # 1-byte target address
        self.assertEqual(dll.address_reads[0][3], 0x00)

    def test_read_chunks_over_255_bytes(self):
        dll = _FakeCp2112Dll(memory=bytes(i & 0xFF for i in range(300)))
        be = self._make(dll)
        be.connect(0, 0x50)
        out = be.read_bytes(0, 300)
        self.assertEqual(len(out), 300)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[255], 255 % 256)
        self.assertGreaterEqual(len(dll.address_reads), 2)  # split into 2 chunks
        # Second chunk must resume at register 255.
        self.assertEqual(dll.address_reads[1][3], 255)

    def test_getreadresponse_buffer_is_at_least_61(self):
        """Regression: AN496 requires bufferSize >= 61; passing the 8/16-byte
        chunk made the real CP2112 answer 0x04 and every Read fail."""
        dll = _FakeCp2112Dll(memory=b'\x1e\x53\x00\x07' + bytes(508))
        be = self._make(dll)
        be.connect(0, 0x50)
        out = be.read_bytes(0x00, 4)   # small chunk — was the failing case
        self.assertEqual(out, b'\x1e\x53\x00\x07')
        calls = [c for c in dll.calls if c[0] == 'GetReadResponse']
        self.assertTrue(calls, 'GetReadResponse was never called')
        for _, buf_size in calls:
            self.assertGreaterEqual(buf_size, 61,
                                    f'bufferSize={buf_size} violates AN496')

    def test_read_over_61_bytes_loops_getreadresponse(self):
        """GetReadResponse returns at most 61 bytes/call; the backend must
        loop until the full chunk is collected."""
        dll = _FakeCp2112Dll(memory=bytes(i & 0xFF for i in range(300)))
        be = self._make(dll)
        be.connect(0, 0x50)
        out = be.read_bytes(0, 300)
        self.assertEqual(out, bytes(i & 0xFF for i in range(300)))
        # 255-byte chunk → at least 5 GetReadResponse calls (61*4 + 11),
        # plus the 45-byte remainder chunk → at least 6 total.
        calls = [c for c in dll.calls if c[0] == 'GetReadResponse']
        self.assertGreaterEqual(len(calls), 6)

    def test_status_code_names_match_an496(self):
        import i2c_backends.cp2112 as cp
        self.assertIn('INVALID_PARAMETER', cp._status_text(0x04))
        self.assertIn('INVALID_REQUEST_LENGTH', cp._status_text(0x05))
        self.assertIn('INVALID_DEVICE_OBJECT', cp._status_text(0x03))

    def test_dll_signatures_have_correct_arg_counts(self):
        """Regression: a 3-param GetTransferStatusResponse signature makes the
        real DLL write WORD* into stack garbage -> access violation on Connect.
        Also WriteRequest's count must be BYTE (1 byte), not WORD."""
        import ctypes
        import i2c_backends.cp2112 as cp
        dll = cp._load_cp2112_dll()   # real DLL is shipped in the project root
        self.assertEqual(len(dll.HidSmbus_GetTransferStatusResponse.argtypes), 5)
        self.assertEqual(len(dll.HidSmbus_GetReadResponse.argtypes), 5)
        self.assertEqual(len(dll.HidSmbus_WriteRequest.argtypes), 4)
        self.assertIs(dll.HidSmbus_WriteRequest.argtypes[3], ctypes.c_ubyte)
        self.assertEqual(dll.HidSmbus_SetSmbusConfig.argtypes[3], ctypes.c_int)
        self.assertEqual(dll.HidSmbus_SetSmbusConfig.argtypes[7], ctypes.c_ushort)

    def test_write_prepends_register_and_shifts_address(self):
        dll = _FakeCp2112Dll()
        be = self._make(dll)
        be.connect(0, 0x50)
        be.write_bytes(0x10, b'\xAA\xBB\xCC')
        self.assertEqual(len(dll.writes), 1)
        slave, payload = dll.writes[0]
        self.assertEqual(slave, 0xA0)
        self.assertEqual(payload, b'\x10\xAA\xBB\xCC')   # register first

    def test_set_address_retargets(self):
        dll = _FakeCp2112Dll()
        be = self._make(dll)
        be.connect(0, 0x50)
        be.set_address(0x53)
        be.read_bytes(0, 1)
        self.assertEqual(dll.address_reads[0][0], 0xA6)   # 0x53 << 1

    def test_set_address_rejects_invalid(self):
        be = self._make(_FakeCp2112Dll())
        be.connect(0, 0x50)
        with self.assertRaises(ValueError):
            be.set_address(0x80)

    def test_disconnect(self):
        dll = _FakeCp2112Dll()
        be = self._make(dll)
        be.connect(0, 0x50)
        be.disconnect()
        self.assertFalse(be.is_connected)
        self.assertFalse(dll.opened)

    def test_api_error_code_raises(self):
        dll = _FakeCp2112Dll()
        dll.HidSmbus_GetNumDevices = lambda *a: 0x15  # DEVICE_ACCESS_ERROR
        be = self._make(dll)
        with self.assertRaises(IOError) as ctx:
            be.connect(0, 0x50)
        self.assertIn('DEVICE_ACCESS_ERROR', str(ctx.exception))

    def test_probe_reports_unavailable_on_nonwin_bits(self):
        import i2c_backends.cp2112 as cp
        orig = cp._check_python_bits
        try:
            cp._check_python_bits = lambda: 64
            info = cp.CP2112Backend.probe_availability()
            self.assertFalse(info['available'])
            self.assertIn('32', info['description'])
        finally:
            cp._check_python_bits = orig


class TestDefaultBackend(CMISTestCase):
    """A programming bench has a CH341A plugged in, so the page must open on
    `ch341` — not on the first mock profile returned by /api/backends.
    """

    def _read(self, *parts):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        return io.open(path, encoding='utf-8').read()

    def test_ch341_is_the_initial_dropdown_option(self):
        """Also covers the pre-API paint: the HTML placeholder must be ch341."""
        html = self.client.get('/').get_data(as_text=True)
        block = re.search(r'<select id="sel-backend">(.*?)</select>', html, re.S)
        self.assertIsNotNone(block, 'sel-backend dropdown missing')
        self.assertIn('<option value="ch341" selected>ch341</option>', block.group(1))
        self.assertNotIn('<option value="mock"', block.group(1))

    def test_frontend_pins_the_same_default_constant(self):
        js = self._read('static', 'app.js')
        self.assertIn("const DEFAULT_BACKEND = 'ch341';", js)
        self.assertIn('availableName(DEFAULT_BACKEND)', js)
        # The old restore-only logic silently fell through to mock_coherent.
        self.assertNotIn('CSS.escape(previousValue)', js)

    def test_backends_endpoint_reports_ch341(self):
        """The four mock profiles come first — hence the explicit default."""
        body = self.assertOk(self.client.get('/api/backends'))
        names = [b['name'] for b in body['data']]
        self.assertIn('ch341', names)
        self.assertTrue(names[0].startswith('mock_'),
                        'without DEFAULT_BACKEND the browser would open on a mock')


class TestChipProfileSync(CMISTestCase):
    """A chip added to only one of the three profile sources used to leave the
    dropdown, the information card and the API disagreeing with each other.
    """

    def _read(self, *parts):
        import io
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
        return io.open(path, encoding='utf-8').read()

    def test_dropdown_options_match_backend_profiles(self):
        html = self._read('templates', 'index.html')
        block = re.search(r'<select id="eep-chip"[^>]*>(.*?)</select>', html, re.S)
        self.assertIsNotNone(block, 'EEPROM Chip dropdown missing')
        options = re.findall(r'<option value="([^"]+)">([^<]+)</option>',
                             block.group(1))
        values = [v for v, _ in options]
        self.assertEqual(len(values), len(set(values)),
                         f'duplicate option values: {values}')
        profiles = app_module.EEPROM_PROFILES
        self.assertEqual(set(values), set(profiles),
                         'dropdown and EEPROM_PROFILES must list the same chips')
        for value, label in options:
            p = profiles[value]
            with self.subTest(chip=value):
                self.assertIn(f"{p['name']} [{p['voltage']}]", label)
                self.assertIn(f"{p['capacity']} B", label)
                self.assertIn(p['bit_capacity'].replace(' Kbits', ' Kbit'), label)
                self.assertIn(f"{p['page_size']} B page", label)

    def test_frontend_profiles_match_backend(self):
        js = self._read('static', 'app.js')
        block = re.search(r'const EEPROM_CHIPS = \{(.*?)\n\};', js, re.S)
        self.assertIsNotNone(block, 'EEPROM_CHIPS table missing')
        entries = dict(re.findall(r"'(\w+)':\s*\{(.*?)\n  \}", block.group(1), re.S))
        profiles = app_module.EEPROM_PROFILES
        self.assertEqual(set(entries), set(profiles),
                         'JS EEPROM_CHIPS and backend EEPROM_PROFILES diverged')
        fields = (
            ('name', r"name: '([^']+)'", lambda p: p['name']),
            ('bits', r"bits: '([^']+)'", lambda p: p['bit_capacity']),
            ('capacity', r'capacity: (\d+)', lambda p: p['capacity']),
            ('page', r'page: (\d+)', lambda p: p['page_size']),
            ('blocks', r'blocks: (\d+)', lambda p: p['blocks']),
            ('addr', r"addr: '([^']+)'",
             lambda p: f"{p['addresses']} ({p['blocks']} × 256 B)"),
        )
        for key, body in entries.items():
            for field, pattern, want in fields:
                with self.subTest(chip=key, field=field):
                    m = re.search(pattern, body)
                    self.assertIsNotNone(m, f'{key} is missing {field}')
                    got = m.group(1)
                    if field in ('capacity', 'page', 'blocks'):
                        got = int(got)
                    self.assertEqual(got, want(profiles[key]))


class TestSerialAutoIncrementUi(unittest.TestCase):
    """Init is gone; the 自动递增 checkbox gates the post-Write S/N +1."""

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get('/').get_data(as_text=True)
        with open('static/app.js', encoding='utf-8') as fh:
            cls.js = fh.read()

    def test_init_button_removed(self):
        self.assertNotIn('btn-eep-sn-init', self.html)
        self.assertNotIn('btn-eep-sn-init', self.js)

    def test_auto_checkbox_is_checked_and_after_next(self):
        m = re.search(r'<input type="checkbox" id="eep-sn-auto"([^>]*)>', self.html)
        self.assertIsNotNone(m, 'eep-sn-auto checkbox missing')
        self.assertIn('checked', m.group(1))
        row = re.search(r'<div class="eep-sn-row">(.*?)</div>', self.html, re.S).group(1)
        self.assertLess(row.index('btn-eep-sn-next'), row.index('eep-sn-auto'),
                        '自动递增 must sit after the Next button')

    def test_post_write_increment_is_gated_by_checkbox(self):
        self.assertIn('function eepromSnAutoIncrement()', self.js)
        block = re.search(
            r'async function eepromWrite\(\).*?\n  \} finally \{(.*?)\n  \}',
            self.js, re.S)
        self.assertIsNotNone(block, 'eepromWrite finally block not found')
        self.assertIn('if (eepromSnAutoIncrement())', block.group(1))
        self.assertIn('advanceEepromSerial()', block.group(1))

    def test_twelve_ascii_serial_is_consistent_across_sources(self):
        self.assertEqual(app_module.EEPROM_SERIAL_LENGTH, 12)
        self.assertEqual(app_module.EEPROM_SERIAL_RANGE, '0x000–0x00B')
        self.assertIn('const EEPROM_SERIAL_LENGTH = 12;', self.js)
        self.assertIn("const EEPROM_SERIAL_RANGE = '0x000–0x00B';", self.js)
        self.assertIn("const EEPROM_SN_KEY = 'cmis.eeprom.sn.ascii';", self.js)
        self.assertIn('value="CAD211193882"', self.html)
        self.assertIn('maxlength="12"', self.html)
        for legacy in ('0x000–0x003', '1E 53 00 07', '4 B hex', 'big-endian'):
            self.assertNotIn(legacy, self.html)

    def test_advance_keeps_trailing_decimal_width(self):
        advance = app_module._advance_serial_text
        self.assertEqual(advance('CAD211193882'), 'CAD211193883')
        self.assertEqual(advance('CAD211193899'), 'CAD211193900')
        self.assertEqual(advance('CAD999999999'), 'CAD000000000')
        self.assertEqual(advance('ABCDEFGHIJKL'), 'ABCDEFGHIJKL')
        self.assertIn('function advanceSerialText(text, pos', self.js)

    def test_increment_position_control_is_wired(self):
        self.assertIn('id="eep-sn-pos"', self.html)
        self.assertIn('function resolveEepromSnPos(value, text)', self.js)
        self.assertIn('serial_pos: eepromSnPos()', self.js)
        self.assertIn('function eepromSnPos()', self.js)

    def test_auto_update_checkbox_is_wired(self):
        # Opt-in helper, default OFF; a Read with a valid product S/N and a
        # non-100% consumption silently repairs the field without consuming
        # a Serial Number.
        self.assertIn('id="eep-auto-update"', self.html)
        self.assertIn('> 自动更新', self.html)
        m = re.search(r'<input type="checkbox" id="eep-auto-update"([^>]*)>', self.html)
        self.assertIsNotNone(m, 'eep-auto-update checkbox missing')
        self.assertNotIn('checked', m.group(1))   # must default to off
        self.assertIn('function autoUpdateConsumption(d, base)', self.js)
        self.assertIn("autoEl?.checked && d.serial_rule_ok === true", self.js)
        self.assertIn("d.consumption_needs_fix === true", self.js)
        self.assertIn('fixed[rel + 1] = CONSUMPTION_FULL_PERCENT', self.js)
        # The silent write must NOT patch the S/N and must not tick 自动递增.
        block = re.search(r'async function autoUpdateConsumption\(.*?\n\}', self.js, re.S)
        self.assertIsNotNone(block, 'autoUpdateConsumption body not found')
        self.assertIn('patch_serial: false', block.group(0))
        self.assertNotIn('advanceEepromSerial()', block.group(0))
        # Constants mirror the backend.
        self.assertIn('const CONSUMPTION_OFFSET = 0x09C;', self.js)
        self.assertIn('const CONSUMPTION_FULL_PERCENT = 0x64;', self.js)

    def test_chip_sn_decode_remark_was_removed(self):
        # The right-hand Chip S/N decode line is redundant with the editable
        # Serial Number field and was deleted for a cleaner layout. The
        # increment-position control itself stays.
        self.assertNotIn('id="eep-sn-read"', self.html)
        self.assertNotIn("'eep-sn-read'", self.js)
        self.assertNotIn('EEP_SN_IDLE', self.js)
        self.assertIn('id="eep-sn-pos"', self.html)
        # The verbose inline hint became a title tooltip, not visible text.
        self.assertNotIn('0-based 字符区间，如 3-9；留空 = 自动识别数字段', self.html)
        self.assertIn('title="0-based 字符区间（如 3-9）', self.html)


class TestEightBitAddressUi(unittest.TestCase):
    """The UI shows 8-bit I2C addresses (0xA0) and shifts to 7-bit for the API.

    Backend/API semantics stay 7-bit (0x50); only the display and the two
    conversion helpers are 8-bit. EEPROM *byte* addresses (0x000) are unrelated
    and must not be touched.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get('/').get_data(as_text=True)
        with open('static/app.js', encoding='utf-8') as fh:
            cls.js = fh.read()

    def test_inputs_and_labels_show_8_bit(self):
        self.assertIn('id="inp-address" value="0xA0"', self.html)
        self.assertIn('id="eep-base" value="0xA0"', self.html)
        self.assertIn('I2C Address (8-bit, hex or dec)', self.html)
        self.assertIn('Base I2C Address (8-bit, hex/dec)', self.html)
        # EEPROM word address is deliberately unchanged.
        self.assertIn('id="eep-start" value="0x000"', self.html)
        self.assertIn('id="eep-ci-addr">0xA0–0xA6 (4 × 256 B)', self.html)

    def test_conversion_helpers_are_wired(self):
        self.assertIn('function uiAddressTo7Bit(value)', self.js)
        self.assertIn('function format8BitAddress(addr7)', self.js)
        self.assertIn('const address = uiAddressTo7Bit(rawAddr)', self.js)
        self.assertIn('addr:${format8BitAddress(address)}', self.js)
        # All three EEPROM paths (read / write / erase) must convert before
        # the API call.
        self.assertEqual(self.js.count(
            "uiAddressTo7Bit(parseHexOrDec(document.getElementById('eep-base').value))"), 3)

    def test_chip_profiles_use_8_bit_display_addresses(self):
        profiles = app_module.EEPROM_PROFILES
        self.assertEqual(profiles['24c02']['addresses'], '0xA0')
        self.assertEqual(profiles['24c04']['addresses'], '0xA0–0xA2')
        self.assertEqual(profiles['24c08']['addresses'], '0xA0–0xA6')

    def test_legacy_7_bit_entry_is_accepted(self):
        # '0x50' (<= 0x7F) passes through unchanged so old bookmarks still work.
        self.assertIn('return n > 0x7F ? n >> 1 : n;', self.js)


class TestEepromFileRefreshUi(unittest.TestCase):
    """刷新 button re-reads the already-selected .bin into the content area."""

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get('/').get_data(as_text=True)
        with open('static/app.js', encoding='utf-8') as fh:
            cls.js = fh.read()
        with open('static/style.css', encoding='utf-8') as fh:
            cls.css = fh.read()

    def test_refresh_button_exists_and_starts_disabled(self):
        m = re.search(r'(<button[^>]*id="btn-eep-file-refresh"[^>]*>)', self.html)
        self.assertIsNotNone(m, 'btn-eep-file-refresh missing from the file row')
        tag = m.group(1)
        self.assertIn('disabled', tag, 'refresh must start disabled (no file)')
        self.assertIn('btn-secondary', tag)
        self.assertIn('title=', tag, 'hover hint should explain the action')
        tail = self.html[m.end():m.end() + 20]
        self.assertIn('刷新', tail)

    def test_refresh_button_sits_before_the_file_meta(self):
        meta = self.html.index('id="eep-file-info"')
        btn = self.html.index('id="btn-eep-file-refresh"')
        picker = self.html.index('id="eep-file"')
        self.assertLess(picker, btn)
        self.assertLess(btn, meta, '刷新 must precede the file-size meta')
        # The manual edit once produced '<div <button ...>'; guard against it.
        self.assertNotIn('<div <button', self.html)

    def test_file_load_is_shared_and_wired(self):
        self.assertIn('async function loadEepromFile(file)', self.js)
        self.assertIn('await loadEepromFile(event.target.files?.[0])', self.js)
        self.assertIn('async function refreshEepromFile()', self.js)
        self.assertIn("const file = input?.files?.[0];", self.js)
        self.assertIn('const ok = await loadEepromFile(file);', self.js)
        self.assertIn("document.getElementById('btn-eep-file-refresh')"
                      "?.addEventListener('click', refreshEepromFile);", self.js)
        # The picker handler must no longer contain a second copy of the read path.
        picker = self.js[self.js.index('async function handleEepromFile'):
                         self.js.index('/** 刷新:')]
        self.assertNotIn('arrayBuffer()', picker)

    def test_refresh_enables_button_and_restores_same_render_path(self):
        self.assertIn("if (refreshBtn) refreshBtn.disabled = !file;", self.js)
        self.assertIn('setEepromImage(image);', self.js)
        self.assertIn("toast(`已刷新 ${file.name}`, 'success');", self.js)
        self.assertIn('没有选中的 .bin 文件', self.js)

    def test_layout_puts_compact_button_before_the_meta(self):
        row = re.search(r'\.eep-file-row\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(row)
        self.assertIn('minmax(0, 2fr) auto minmax(0, 3fr)', row.group(1))
        btn = re.search(r'\.eep-file-refresh\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(btn)
        self.assertIn('padding: 4px 6px', btn.group(1))


class TestBlankChipAutoProgramUi(unittest.TestCase):
    """A blank-chip Read silently programs the selected .bin with S/N +1."""

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get('/').get_data(as_text=True)
        with open('static/app.js', encoding='utf-8') as fh:
            cls.js = fh.read()
        with open('static/style.css', encoding='utf-8') as fh:
            cls.css = fh.read()

    def test_file_handle_is_tracked_separately_from_the_image(self):
        self.assertIn('let _loadedBinFile = null;', self.js)
        self.assertIn('_loadedBinFile = file || null;', self.js)
        # Read replaces _eepromImage with the chip dump but must not touch the
        # file handle, otherwise the auto-program trigger would never fire.
        self.assertIn('d.blank_chip === true && _loadedBinFile', self.js)

    def test_blank_program_checkbox_gates_the_trigger(self):
        # The auto-program path is opt-in: a Read only writes when the operator
        # explicitly ticked "烧写空白" (which defaults to OFF in the HTML).
        self.assertIn("document.getElementById('eep-blank-program')", self.js)
        self.assertIn('blankEl?.checked && d.blank_chip === true && _loadedBinFile',
                      self.js)
        cb = re.search(r'<input[^>]*id="eep-blank-program"[^>]*>', self.html)
        self.assertIsNotNone(cb, '烧写空白 checkbox missing')
        self.assertNotIn('checked', cb.group(0), '烧写空白 must default to OFF')
        label = re.search(r'<label[^>]*for="eep-blank-program"[^>]*>', self.html)
        self.assertIsNotNone(label)
        self.assertIn('eep-blank-program', self.css)

    def test_auto_program_is_wired_and_mutually_exclusive(self):
        self.assertIn('async function autoProgramBlankChip(d, base)', self.js)
        self.assertIn('await autoProgramBlankChip(d, base);', self.js)
        # The consumption repair must live in the else branch — an erased part
        # must never be auto-repaired as if it were a programmed module.
        block = self.js[self.js.index('blankEl?.checked && d.blank_chip === true'):
                        self.js.index('} finally {', self.js.index('blankEl?.checked && d.blank_chip === true'))]
        self.assertIn('} else {', block)
        self.assertIn('d.serial_rule_ok === true', block)

    def test_advance_before_write_and_no_confirmation_dialog(self):
        fn = self.js[self.js.index('async function autoProgramBlankChip'):
                     self.js.index('/**\n * "Auto update"')]
        self.assertIn('const nextSn = advanceEepromSerial();', fn)
        self.assertIn('serial_number: nextSn,', fn)
        self.assertIn('data_b64: bytesToBase64(binBytes)', fn)
        self.assertIn('patch_serial: true', fn)
        # eepromWrite() would ask for confirmation and increment again.
        self.assertNotIn('eepromWrite()', fn)
        self.assertNotIn('confirmEepromWrite', fn)

    def test_guards_run_before_consuming_a_serial_number(self):
        fn = self.js[self.js.index('async function autoProgramBlankChip'):
                     self.js.index('/**\n * "Auto update"')]
        advance_at = fn.index('advanceEepromSerial()')
        for guard in ('await file.arrayBuffer()', 'parseEepromSerial(currentSn)',
                      'resolveEepromSnPos(eepromSnPos(), currentSn)',
                      'binBytes.length < EEPROM_SERIAL_LENGTH',
                      'binBytes.length > profile.capacity'):
            self.assertLess(fn.index(guard), advance_at, guard)


class TestEepromEraseUi(unittest.TestCase):
    """清空芯片: physical erase of the whole EEPROM to 0xFF."""

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get('/').get_data(as_text=True)
        with open('static/app.js', encoding='utf-8') as fh:
            cls.js = fh.read()

    def test_erase_button_sits_after_clear(self):
        clear = self.html.index('id="btn-eep-clear"')
        erase = self.html.index('id="btn-eep-erase"')
        write = self.html.index('id="btn-eep-write"')
        self.assertLess(clear, erase, '清空 must follow 清除')
        self.assertLess(erase, write, '清空 must precede 写入')
        tag = re.search(r'<button[^>]*id="btn-eep-erase"[^>]*>', self.html)
        self.assertIsNotNone(tag)
        self.assertIn('btn-danger', tag.group(0), 'destructive action must be red')
        self.assertIn('title=', tag.group(0))

    def test_erase_is_wired(self):
        self.assertIn("document.getElementById('btn-eep-erase')?.addEventListener('click', eepromErase)",
                      self.js)
        self.assertIn('async function eepromErase()', self.js)

    def test_erase_writes_all_ff_without_serial_patch(self):
        fn = self.js[self.js.index('async function eepromErase()'):
                     self.js.index('/**\n * Blank-chip auto-program')]
        self.assertIn('new Uint8Array(profile.capacity).fill(0xFF)', fn)
        self.assertIn('patch_serial: false', fn)
        self.assertIn("data_b64: bytesToBase64(bytes)", fn)
        self.assertIn('confirm: true', fn)
        self.assertIn('/api/eeprom/write', fn)
        # loadSn=false keeps the editable S/N untouched; erasing is not a Write.
        self.assertIn('}, false)', fn)
        self.assertNotIn('advanceEepromSerial()', fn)
        self.assertNotIn('eepromWriteNeedsConfirm()', fn)

    def test_erase_always_confirms(self):
        fn = self.js[self.js.index('async function eepromErase()'):
                     self.js.index('/**\n * Blank-chip auto-program')]
        # Mandatory: confirmEepromWrite is called unconditionally, not gated by
        # the 确认 checkbox, so a wiped part can never be silent.
        self.assertIn('if (!(await confirmEepromWrite(message))) return;', fn)


class TestEepromAutoReadUi(unittest.TestCase):
    """烧写空白 ticked + Read click -> periodic read every 2 s; untick stops."""

    @classmethod
    def setUpClass(cls):
        cls.html = app.test_client().get('/').get_data(as_text=True)
        with open('static/app.js', encoding='utf-8') as fh:
            cls.js = fh.read()

    def test_timer_state_and_interval(self):
        self.assertIn('eepromAutoReadInterval: null', self.js)
        self.assertIn('eepromAutoReadIntervalMs: 2000', self.js)
        self.assertIn('async function eepromRead', self.js)

    def test_start_stop_helpers_are_idempotent(self):
        self.assertIn('function startEepromAutoRead()', self.js)
        self.assertIn('function stopEepromAutoRead()', self.js)
        fn = self.js[self.js.index('function startEepromAutoRead()'):
                     self.js.index('function stopEepromAutoRead()',
                                   self.js.index('function startEepromAutoRead()'))]
        # Re-arming is idempotent: never stack multiple intervals.
        self.assertIn('stopEepromAutoRead();', fn)
        self.assertIn('AppState.eepromAutoReadInterval = setInterval(', fn)
        self.assertIn('AppState.eepromAutoReadIntervalMs', fn)
        stop_fn = self.js[self.js.index('function stopEepromAutoRead()'):
                          self.js.index('// Update check',
                                        self.js.index('function stopEepromAutoRead()'))]
        self.assertIn('clearInterval(AppState.eepromAutoReadInterval)', stop_fn)
        self.assertIn('AppState.eepromAutoReadInterval = null', stop_fn)

    def test_tick_guards_untick_disconnect_and_reentry(self):
        fn = self.js[self.js.index('function startEepromAutoRead()'):
                     self.js.index('function stopEepromAutoRead()',
                                   self.js.index('function startEepromAutoRead()'))]
        self.assertIn("document.getElementById('eep-blank-program')", fn)
        self.assertIn("!cb?.checked || !AppState.connected", fn)
        self.assertIn('stopEepromAutoRead(); return;', fn)
        self.assertIn("document.getElementById('btn-eep-read')", fn)
        self.assertIn('if (btn?.disabled) return;', fn)
        self.assertIn('eepromRead();', fn)

    def test_armed_by_read_click_and_stopped_on_untick(self):
        # Only an explicit Read click arms the timer, and only when ticked.
        block = self.js[self.js.index("document.getElementById('btn-eep-read')?.addEventListener('click'"):
                        self.js.index("document.getElementById('eep-file')?.addEventListener('change'")]
        self.assertIn('if (document.getElementById(\'eep-blank-program\')?.checked)', block)
        self.assertIn('startEepromAutoRead();', block)
        cb_block = self.js[self.js.index("document.getElementById('eep-blank-program')?.addEventListener('change'"):
                           self.js.index("document.getElementById('eep-serial')?.addEventListener('input'")]
        self.assertIn('if (!e.target.checked) stopEepromAutoRead();', cb_block)

    def test_disconnect_stops_periodic_read(self):
        fn = self.js[self.js.index('async function disconnectModule()'):
                     self.js.index('function setConnectBusy(')]
        self.assertIn('stopEepromAutoRead()', fn)


# ============================================================
# Run
# ============================================================

if __name__ == '__main__':
    # A failure message quoting the Chinese manual otherwise kills the summary
    # with a UnicodeEncodeError on a GBK console - the failing test's own text
    # is the last thing you want to lose.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromModule(
        sys.modules[__name__]))
    print(f"\n{'='*60}")
    print(f"Tests run: {result.testsRun}")
    print(f"Failures:  {len(result.failures)}")
    print(f"Errors:    {len(result.errors)}")
    if result.failures:
        print("\nFAILURES:")
        for test, tb in result.failures:
            print(f"  {test}: {tb.splitlines()[-1]}")
    if result.errors:
        print("\nERRORS:")
        for test, tb in result.errors:
            print(f"  {test}: {tb.splitlines()[-1]}")
