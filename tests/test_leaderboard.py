import http.client
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('leaderboard_app', str(HERE.parent / 'leaderboard.py'))
lb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lb)
TEST_DATA = HERE.parent.parent / 'tb'

TCL = ('# official test fixture, not a replacement for the TA script\n'
       'set TOP cnn_top\n'
       'set RTL_FILES [list \\\n'
       '    ../RTL_code/cnn_top.v];\n'
       'set CLK_PERIOD 4.0;# ns\n'
       'analyze -format verilog $RTL_FILES;\n'
       'report_area -hierarchy > area.rpt\n')
FLIST = ('./tb_simple.v\n./setting.vh\n../RTL_code/cnn_top.v\n'
         '/tools/cad/ADFP/N16ADFP_SRAM_100a.v\n+notimingcheck\n')
SYN_LOG = ('Error: expected fixture diagnostic 1\nError: expected fixture diagnostic 2\n'
           'SRAM macros : 5\nlatches : 0\nWNS : 0.125\ntotal area : 12345.678\n')
AREA = 'Total cell area: 12345.68\n'
TIMING = 'slack (MET) 0.125\n'
LATCH = 'no latch\n'


def valid_files(tcl=TCL, flist=FLIST, interval=2455000):
    return {'RTL_code/cnn_top.v': 'module cnn_top; endmodule\n',
            'syn/syn.tcl': tcl, 'tb/rtl_sim.f': flist,
            'tb/setting.vh': '// comment\n`define INTERVAL {}\n'.format(interval)}


def write_zip(path, files):
    with zipfile.ZipFile(str(path), 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def make_course(root):
    root.mkdir()
    for source in [TEST_DATA / 'tb_simple.v'] + sorted((TEST_DATA / 'export').rglob('*.hex')):
        target = root / source.relative_to(TEST_DATA)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (root / 'syn.tcl').write_text(TCL, encoding='utf-8')
    (root / 'rtl_sim.f').write_text(FLIST, encoding='utf-8')
    (root / '.synopsys_dc.setup').write_text('set search_path .\n', encoding='ascii')


def sim_log(expected):
    return (''.join('img {:2d} : golden {}, got {} PASS\n'.format(i, v, v) for i, v in enumerate(expected))
            + '8 / 8 PASS\nthroughput : 40.000 FPS  (8 images in 20000000 cycles)\n')


class FakeRunner:
    """Test-only runner. There is no fake grading option in the application CLI."""
    def __init__(self, fail=None, area=12345.678):
        self.fail = fail
        self.area = area
        self.calls = []
        self.observed_images = None

    def run(self, phase, cwd, log_path, stop):
        self.calls.append(phase)
        if self.fail == phase:
            log_path.write_text('test diagnostic\n', encoding='utf-8')
            raise lb.Issue('TEST_FAILURE', 'test failure')
        if phase == 'simulation':
            expected = [int(t, 16) for t in (cwd / 'export/tb/pred.hex').read_text().split()]
            self.observed_images = [int(t, 16) for t in (cwd / 'export/tb/images.hex').read_text().split()]
            log_path.write_text(sim_log(expected), encoding='utf-8')
        else:
            log_path.write_text(SYN_LOG.replace('12345.678', str(self.area)), encoding='utf-8')
            (cwd / 'area.rpt').write_text('Total cell area: {}\n'.format(self.area), encoding='utf-8')
            (cwd / 'timing.rpt').write_text(TIMING, encoding='utf-8')
            (cwd / 'latch.rpt').write_text(LATCH, encoding='utf-8')


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def assertIssue(self, code, function, *args):
        with self.assertRaises(lb.Issue) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)
        return raised.exception.message

    def extract(self, files, password=None):
        path = self.root / 'input.zip'
        write_zip(path, files)
        return lb.safe_extract(path, self.root / ('out-' + str(time.monotonic())), password)

    def test_valid_cloud_zip(self):
        self.assertEqual(set(self.extract(valid_files())), set(lb.REQUIRED))

    def test_reports_each_missing_file(self):
        for missing in lb.REQUIRED:
            with self.subTest(missing=missing):
                files = valid_files()
                del files[missing]
                message = self.assertIssue('MISSING_FILES', self.extract, files)
                self.assertIn(missing, message)

    def test_forbidden_zip_entries(self):
        for name, code in [('../outside.v', 'UNSAFE_ZIP'), ('/outside.v', 'UNSAFE_ZIP'),
                           ('RTL_code/../escape.v', 'UNSAFE_ZIP'), ('RTL_code\\escape.v', 'UNSAFE_ZIP'),
                           ('RTL_code/sram.v', 'INVALID_FORMAT'), ('RTL_code/N16ADFP_SRAM_100a.v', 'INVALID_FORMAT'),
                           ('tb/tb_simple.v', 'INVALID_FORMAT'), ('M11000001/RTL_code/cnn_top.v', 'INVALID_FORMAT')]:
            with self.subTest(name=name):
                files = valid_files()
                files[name] = 'bad'
                self.assertIssue(code, self.extract, files)

    def test_duplicate_and_case_collision(self):
        path = self.root / 'duplicates.zip'
        with zipfile.ZipFile(str(path), 'w') as archive:
            for name, content in valid_files().items():
                archive.writestr(name, content)
            archive.writestr('RTL_code/CNN_TOP.v', 'bad')
        self.assertIssue('UNSAFE_ZIP', lb.safe_extract, path, self.root / 'duplicate-out', None)

    def test_symlink_is_not_extracted(self):
        path = self.root / 'symlink.zip'
        with zipfile.ZipFile(str(path), 'w') as archive:
            for name, content in valid_files().items():
                archive.writestr(name, content)
            info = zipfile.ZipInfo('RTL_code/link.v')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, '/etc/passwd')
        self.assertIssue('UNSAFE_ZIP', lb.safe_extract, path, self.root / 'link-out', None)

    def test_archive_limits(self):
        with mock.patch.object(lb, 'MAX_MEMBERS', 3):
            self.assertIssue('ZIP_LIMIT', self.extract, valid_files())
        with mock.patch.object(lb, 'MAX_EXPANDED', 20):
            self.assertIssue('ZIP_LIMIT', self.extract, valid_files())

    def test_aes_gets_explicit_message(self):
        path = self.root / 'aes.zip'
        write_zip(path, valid_files())
        raw = bytearray(path.read_bytes())
        struct.pack_into('<H', raw, raw.index(b'PK\x01\x02') + 10, 99)
        path.write_bytes(raw)
        self.assertIssue('UNSUPPORTED_ENCRYPTION', lb.safe_extract, path, self.root / 'aes-out', 'anything')

    def test_corrupt_zip(self):
        path = self.root / 'corrupt.zip'
        path.write_bytes(b'not a zip')
        self.assertIssue('DECRYPT_FAILED', lb.safe_extract, path, self.root / 'corrupt-out', None)

    def test_real_zipcrypto_password(self):
        path = HERE / 'fixtures/encrypted.zip'
        self.assertIssue('PASSWORD_MISSING', lb.safe_extract, path, self.root / 'missing', None)
        self.assertIssue('DECRYPT_FAILED', lb.safe_extract, path, self.root / 'wrong', 'wrong')
        files = lb.safe_extract(path, self.root / 'correct', 'test-password')
        self.assertEqual(set(files), set(lb.REQUIRED))

    def test_bounded_decoder_process(self):
        path = self.root / 'bounded.zip'
        write_zip(path, valid_files())
        files = lb.extract_bounded(path, self.root / 'bounded-out', None)
        self.assertEqual(set(files), set(lb.REQUIRED))

    def test_interval(self):
        self.assertEqual(lb.interval_value('// x\n`define INTERVAL 784\n'), 784)
        for text in ('`define INTERVAL 783', '`define INTERVAL 214748365', '`define INTERVAL (784)',
                     '`define INTERVAL 1000\n`include "evil.v"', '`define INTERVAL -1', '`define INTERVAL 1000\ninitial $finish;'):
            with self.subTest(text=text):
                self.assertIssue('INVALID_INTERVAL', lb.interval_value, text)

    def test_tcl_relocates_only_approved_paths(self):
        student = TCL.replace('../RTL_code/cnn_top.v', '/home/student/hw/RTL_code/cnn_top.v \\\n    ../RTL_code/mac.v')
        actual, paths = lb.validated_tcl(student.replace('\n', '\r\n'), TCL, {'RTL_code/cnn_top.v', 'RTL_code/mac.v'})
        self.assertEqual(paths, ['RTL_code/cnn_top.v', 'RTL_code/mac.v'])
        self.assertNotIn('/home/student', actual)
        self.assertIn('set CLK_PERIOD 4.0;# ns', actual)
        self.assertIn('../RTL_code/mac.v', actual)

    def test_literal_tcl_list_variants(self):
        original, start, end, unused = lb.tcl_assignment(TCL)
        for value in ('{../RTL_code/cnn_top.v}', '"../RTL_code/cnn_top.v"', '[list "../RTL_code/cnn_top.v"]'):
            submitted = original[:start] + 'set RTL_FILES ' + value + ';' + original[end:]
            actual, paths = lb.validated_tcl(submitted, TCL, {'RTL_code/cnn_top.v'})
            self.assertEqual(paths, ['RTL_code/cnn_top.v'])

    def test_tcl_rejects_constraints_and_code_in_paths(self):
        for student in (TCL.replace('4.0', '10.0'), TCL + 'exec touch /tmp/evil\n',
                        TCL.replace('../RTL_code/cnn_top.v', '[exec evil]'),
                        TCL.replace('../RTL_code/cnn_top.v', '../RTL_code/cnn_top.v; exec evil'),
                        TCL.replace('../RTL_code/cnn_top.v', '$env(PATH)'),
                        TCL.replace('../RTL_code/cnn_top.v', '../RTL_code/../../evil.v')):
            with self.subTest(student=student):
                with self.assertRaises(lb.Issue):
                    lb.validated_tcl(student, TCL, {'RTL_code/cnn_top.v'})

    def test_tcl_missing_reference(self):
        self.assertIssue('MISSING_RTL', lb.validated_tcl,
                         TCL.replace('cnn_top.v', 'missing.v'), TCL, {'RTL_code/cnn_top.v'})

    def test_flist_rejects_flags_and_mismatch(self):
        result = lb.validated_flist(FLIST, FLIST, {'RTL_code/cnn_top.v'}, ['RTL_code/cnn_top.v'])
        self.assertIn('../RTL_code/cnn_top.v', result)
        for text in (FLIST + '-load evil.so\n', FLIST.replace('+notimingcheck', '+define+EVIL'),
                     FLIST.replace('../RTL_code/cnn_top.v\n', '')):
            self.assertIssue('FLIST_CHANGED', lb.validated_flist, text, FLIST,
                             {'RTL_code/cnn_top.v'}, ['RTL_code/cnn_top.v'])

    def test_simulation_requires_actual_rows(self):
        expected = list(range(8))
        good = sim_log(expected)
        self.assertEqual(lb.parse_simulation(good, expected), 40)
        for bad in ('8 / 8 PASS\n', good.replace('got 3 PASS', 'got 4 FAIL'),
                    good.replace('img  0', 'img  1'), good + '[TIMEOUT]\n',
                    good + '8 / 8 PASS\n', good.replace('40.000', '400.000')):
            with self.subTest(bad=bad):
                self.assertIssue('SIM_FAILED', lb.parse_simulation, bad, expected)

    def test_synthesis_reports(self):
        metrics = lb.parse_synthesis(SYN_LOG, AREA, TIMING, LATCH)
        self.assertEqual(metrics['area'], 12345.678)
        for log, area, timing, latch, code in (
                (SYN_LOG + 'Fatal: real failure\n', AREA, TIMING, LATCH, 'SYN_FAILED'),
                (SYN_LOG.replace('latches : 0', 'latches : 1'), AREA, TIMING, LATCH, 'SYN_LATCH'),
                (SYN_LOG, AREA, TIMING, 'latch u1\n', 'SYN_LATCH'),
                (SYN_LOG.replace('SRAM macros : 5', 'SRAM macros : 0'), AREA, TIMING, LATCH, 'SYN_SRAM'),
                (SYN_LOG.replace('WNS : 0.125', 'WNS : 0'), AREA, TIMING, LATCH, 'SYN_TIMING'),
                (SYN_LOG, AREA, 'slack (VIOLATED) -1.0', LATCH, 'SYN_TIMING'),
                (SYN_LOG, 'not a report', TIMING, LATCH, 'SYN_REPORT')):
            with self.subTest(code=code):
                self.assertIssue(code, lb.parse_synthesis, log, area, timing, latch)

    def test_course_script_prints_macro_count_twice(self):
        log = 'SRAM macros : 6\n' + SYN_LOG
        self.assertEqual(lb.parse_synthesis(log, AREA, TIMING, LATCH)['sram_macros'], 5)
        self.assertIssue('SYN_SRAM', lb.parse_synthesis,
                         log.replace('SRAM macros : 5', 'SRAM macros : 0'), AREA, TIMING, LATCH)

    def test_error_count_is_diagnostic_only(self):
        summary = '\n'.join(SYN_LOG.splitlines()[2:])
        for count in (0, 1, 2, 3):
            log = 'Error: fixture diagnostic\n' * count + summary
            metrics = lb.parse_synthesis(log, AREA, TIMING, LATCH)
            self.assertEqual(metrics['area'], 12345.678)
            self.assertEqual(metrics['synthesis_error_count'], count)
        self.assertIssue('SYN_FAILED', lb.parse_synthesis, summary + '\nERROR: link failed\n', AREA, TIMING, LATCH)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.incoming = self.root / 'incoming'
        self.incoming.mkdir()
        self.csv = self.root / 'passwords.csv'
        self.csv.write_text('學號,密碼\nM11000001,PRIVATE_TEST_SECRET\n', encoding='utf-8')
        self.csv.chmod(0o600)
        self.course = self.root / 'course'
        make_course(self.course)
        self.results = self.root / 'results'
        self.board = lb.Board(self.incoming, self.csv, self.results, self.course)
        self.watcher = lb.Watcher(self.board)

    def tearDown(self):
        self.board.close()
        self.temp.cleanup()

    def collect(self):
        with mock.patch.object(lb, 'SETTLE_SECONDS', 0):
            self.watcher.scan()
            self.watcher.scan()

    def upload(self, files=None, name='M11000001.zip'):
        write_zip(self.incoming / name, valid_files() if files is None else files)
        self.collect()
        return self.board.snapshot()['history'][-1]

    def grade_next(self, runner=None):
        job_id = self.board.tasks.get_nowait()
        self.board.grade(job_id, runner or FakeRunner())
        self.board.tasks.task_done()
        return next(r for r in self.board.snapshot()['history'] if r['id'] == job_id)

    def test_success_shuffle_cleanup_and_permissions(self):
        self.upload()
        runner = FakeRunner()
        record = self.grade_next(runner)
        self.assertEqual(record['status'], 'success', record)
        self.assertEqual(runner.calls, ['simulation', 'synthesis'])
        self.assertNotEqual(record['shuffle_order'], list(range(8)))
        expected_pixels = [v for i in record['shuffle_order'] for v in self.board.course.images[i*2352:(i+1)*2352]]
        self.assertEqual(runner.observed_images, expected_pixels)
        job = self.board.private / record['id']
        self.assertTrue(lb.JOB_ID.fullmatch(job.name))
        self.assertFalse((job / 'work').exists())
        self.assertFalse((job / 'archive.zip').exists())
        self.assertTrue((self.incoming / 'M11000001.zip').exists())
        for name in ('record.json', 'rtl.log', 'syn.log', 'area.rpt', 'timing.rpt', 'latch.rpt'):
            self.assertTrue((job / name).is_file(), name)
        self.assertEqual(stat.S_IMODE(self.board.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.results / 'results.json').stat().st_mode), 0o644)
        raw = (self.results / 'results.json').read_text()
        self.assertNotIn('PRIVATE_TEST_SECRET', raw)
        self.assertEqual(len(self.board.snapshot()['rankings']), 1)

    def test_failure_does_not_erase_previous_success(self):
        self.upload()
        first = self.grade_next()
        self.upload(valid_files(interval=2456000))
        failed = self.grade_next(FakeRunner(fail='simulation'))
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(self.board.snapshot()['rankings'][0]['id'], first['id'])
        self.assertFalse((self.board.private / failed['id'] / 'work').exists())

    def test_latest_success_replaces_better_old_score(self):
        self.upload()
        self.grade_next(FakeRunner(area=1000))
        self.upload(valid_files(interval=2456000))
        latest = self.grade_next(FakeRunner(area=2000))
        self.assertEqual(self.board.snapshot()['rankings'][0]['id'], latest['id'])
        self.assertEqual(self.board.snapshot()['rankings'][0]['area'], 2000)

    def test_changed_tcl_never_runs_tools(self):
        self.upload(valid_files(tcl=TCL.replace('4.0', '10.0')))
        runner = FakeRunner()
        record = self.grade_next(runner)
        self.assertEqual(record['error_code'], 'SYN_CHANGED')
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.board.snapshot()['rankings'], [])

    def test_synthesis_failure_keeps_log_and_cleans_work(self):
        self.upload()
        record = self.grade_next(FakeRunner(fail='synthesis'))
        job = self.board.private / record['id']
        self.assertEqual(record['status'], 'error')
        self.assertTrue((job / 'rtl.log').is_file())
        self.assertTrue((job / 'syn.log').is_file())
        self.assertFalse((job / 'work').exists())

    def test_upload_waits_for_stability(self):
        write_zip(self.incoming / 'M11000001.zip', valid_files())
        self.watcher.scan()
        self.assertEqual(self.board.snapshot()['history'], [])
        self.watcher.scan()
        self.assertEqual(self.board.snapshot()['history'], [])

    def test_symlink_upload_is_not_followed(self):
        (self.incoming / 'M11000001.zip').symlink_to(self.csv)
        self.collect()
        record = self.board.snapshot()['history'][-1]
        self.assertEqual(record['error_code'], 'UNSAFE_FILE')
        self.assertEqual(self.board.snapshot()['rankings'], [])

    def test_snapshot_race_is_retried(self):
        path = self.incoming / 'M11000001.zip'
        write_zip(path, valid_files())
        info = path.stat()
        path.write_bytes(b'changed')
        with self.assertRaises(lb.UploadChanged):
            lb.snapshot_upload(path, self.root / 'snapshot.zip', lb.signature(info))

    def test_unchanged_zip_is_not_regraded_after_restart(self):
        self.upload()
        self.grade_next()
        self.board.close()
        self.board = lb.Board(self.incoming, self.csv, self.results, self.course)
        self.watcher = lb.Watcher(self.board)
        self.collect()
        self.assertEqual(len(self.board.snapshot()['history']), 1)
        self.assertTrue(self.board.tasks.empty())

    def test_queued_snapshot_recovers_after_restart(self):
        original = self.upload()
        self.board.close()
        self.board = lb.Board(self.incoming, self.csv, self.results, self.course)
        self.watcher = lb.Watcher(self.board)
        self.collect()
        self.assertEqual(len(self.board.snapshot()['history']), 1)
        result = self.grade_next()
        self.assertEqual(result['id'], original['id'])
        self.assertEqual(result['status'], 'success')

    def test_password_update_retries_failed_zip(self):
        shutil.copyfile(str(HERE / 'fixtures/encrypted.zip'), str(self.incoming / 'M11000001.zip'))
        self.collect()
        first = self.grade_next()
        self.assertEqual(first['error_code'], 'DECRYPT_FAILED')
        self.csv.write_text('\ufeff學號,密碼\nM11000001,test-password\n', encoding='utf-8')
        self.collect()
        second = self.grade_next()
        self.assertEqual(second['status'], 'success', second)
        self.assertEqual(len(self.board.snapshot()['history']), 2)
        raw = (self.results / 'results.json').read_text()
        self.assertNotIn('test-password', raw)

    def test_unencrypted_zip_without_csv_row(self):
        self.upload(name='M11000002.zip')
        record = self.grade_next()
        self.assertEqual(record['status'], 'success')

    def test_class_roster_ids_survive_csv_collection_and_restart(self):
        students = sorted(lb.STUDENT_NAMES)
        self.csv.write_text('學號,密碼\n' + ''.join(student + ',fixture-password\n' for student in students), encoding='utf-8')
        passwords, unused = lb.read_passwords(self.csv)
        self.assertEqual(set(passwords), set(students))
        for student in students:
            self.assertTrue(lb.FILENAME.fullmatch(student + '.zip'), student)
        selected = ('B11207001', 'D11407804', 'M11507W01', 'M11507401')
        for student in selected:
            self.upload(name=student + '.zip')
            record = self.grade_next()
            self.assertEqual(record['status'], 'success', record)
            self.assertEqual(record['student_id'], student)
            self.assertTrue(lb.JOB_ID.fullmatch(record['id']))
        self.board.close()
        self.board = lb.Board(self.incoming, self.csv, self.results, self.course)
        self.assertEqual({r['student_id'] for r in self.board.snapshot()['rankings']}, set(selected))

    def test_zero_error_synthesis_is_ranked(self):
        class ZeroErrorRunner(FakeRunner):
            def run(self, phase, cwd, log_path, stop):
                super().run(phase, cwd, log_path, stop)
                if phase == 'synthesis':
                    text = log_path.read_text()
                    log_path.write_text('\n'.join(line for line in text.splitlines() if not line.startswith('Error:')))
        self.upload()
        record = self.grade_next(ZeroErrorRunner())
        self.assertEqual(record['status'], 'success')
        self.assertEqual(record['synthesis_error_count'], 0)
        self.assertEqual(len(self.board.snapshot()['rankings']), 1)

    def test_csv_last_row_and_spaces(self):
        self.csv.write_text('\ufeff時間戳記,學號,密碼\nold,M11000001,old\nnew,M11000001,  keep spaces  \n', encoding='utf-8')
        values, version = lb.read_passwords(self.csv)
        self.assertEqual(values['M11000001'], '  keep spaces  ')

    def test_csv_requires_private_permissions(self):
        self.csv.chmod(0o644)
        with self.assertRaises(lb.Issue) as raised:
            lb.read_passwords(self.csv)
        self.assertEqual(raised.exception.code, 'CSV_ERROR')

    def test_second_service_and_changed_baseline_are_rejected(self):
        with self.assertRaises(lb.Issue):
            lb.Board(self.incoming, self.csv, self.results, self.course)
        self.board.close()
        (self.course / 'syn.tcl').write_text(TCL + '# changed\n', encoding='utf-8')
        with self.assertRaises(lb.Issue):
            lb.Board(self.incoming, self.csv, self.results, self.course)
        (self.course / 'syn.tcl').write_text(TCL, encoding='utf-8')
        self.board = lb.Board(self.incoming, self.csv, self.results, self.course)

    def test_competition_ties_and_rank_sum(self):
        base = self.upload()
        records = []
        for i, (area, interval) in enumerate([(100, 2000), (100, 1000), (300, 1000)]):
            record = dict(base, id=str(i), student_id='M1100000' + str(i), status='success', area=area, interval=interval)
            records.append(record)
        ranked = {r['student_id']: r for r in lb.competition_rank(records)}
        self.assertEqual(ranked['M11000000']['area_rank'], 1)
        self.assertEqual(ranked['M11000002']['area_rank'], 3)
        self.assertEqual(ranked['M11000000']['speed_rank'], 3)
        self.assertEqual(ranked['M11000001']['overall_rank'], 1)
        self.assertEqual(ranked['M11000000']['overall_rank'], 2)
        self.assertEqual(ranked['M11000002']['overall_rank'], 2)

    def test_http_privacy_html_escaping_and_live_script(self):
        record = self.upload(name='bad<script>.zip')
        server = lb.make_server(self.board, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            conn.request('GET', '/')
            response = conn.getresponse()
            page = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn('&lt;script&gt;', page)
            self.assertNotIn('<script>', page)
            self.assertNotIn('http-equiv="refresh"', page)
            self.assertIn('<script src="/live.js" defer></script>', page)
            self.assertIn('data-revision="' + self.board.snapshot()['revision'] + '"', page)
            self.assertIn("default-src 'none'", response.getheader('Content-Security-Policy'))
            self.assertIn("script-src 'self'", response.getheader('Content-Security-Policy'))
            self.assertIn("connect-src 'self'", response.getheader('Content-Security-Policy'))
            for path in ('/passwords.csv', '/submissions/' + record['id'] + '/record.json', '/../passwords.csv', '/%2e%2e/passwords.csv'):
                conn.request('GET', path)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 404, path)
            conn.request('GET', '/results.json')
            response = conn.getresponse()
            body = response.read().decode()
            self.assertNotIn('PRIVATE_TEST_SECRET', body)
            self.assertEqual(json.loads(body)['schema_version'], 1)
            conn.request('GET', '/live.js')
            response = conn.getresponse()
            self.assertIn('new EventSource(', response.read().decode())
            self.assertEqual(response.status, 200)
            conn.request('HEAD', '/events')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'')
            conn.request('HEAD', '/')
            response = conn.getresponse()
            self.assertEqual(response.read(), b'')
            conn.request('POST', '/', body=b'student upload')
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 405)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    @contextmanager
    def live_server(self):
        server = lb.make_server(self.board, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            self.board.stop.set()
            with self.board.changed:
                self.board.changed.notify_all()
            server.shutdown()
            server.server_close()
            thread.join()

    def open_events(self, server, revision='', last_id=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        conn.request('GET', '/events?since=' + revision, headers={'Last-Event-ID': last_id} if last_id else {})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('text/event-stream', response.getheader('Content-Type'))
        self.assertIsNone(response.getheader('Content-Length'))
        self.assertEqual(self.read_event(response)['retry'], '2000')
        return conn, response

    def read_event(self, response):
        fields = {}
        while True:
            line = response.readline().decode('utf-8').rstrip('\r\n')
            if not line:
                return fields
            if not line.startswith(':'):
                key, value = line.split(':', 1)
                fields[key] = value.lstrip()

    def test_events_broadcast_to_all_clients_without_blocking_http(self):
        revision = self.board.snapshot()['revision']
        with self.live_server() as server:
            clients = [self.open_events(server, revision) for _ in range(2)]
            try:
                self.board.notice('test', '<script>alert("untrusted")</script>')
                current = self.board.snapshot()['revision']
                for conn, response in clients:
                    event = self.read_event(response)
                    self.assertEqual(event['event'], 'update')
                    self.assertEqual(event['id'], current)
                    message = json.loads(event['data'])
                    self.assertEqual(message['revision'], current)
                    self.assertIn('&lt;script&gt;', message['html'])
                    self.assertNotIn('<script>', message['html'])
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                conn.request('GET', '/results.json')
                response = conn.getresponse()
                self.assertEqual(json.loads(response.read().decode())['revision'], current)
                conn.close()
            finally:
                for conn, response in clients:
                    response.close()
                    conn.close()

    def test_events_catch_up_after_initial_load_and_reconnect(self):
        initial = self.board.snapshot()['revision']
        self.board.notice('test', 'changed before connection')
        with self.live_server() as server:
            conn, response = self.open_events(server, initial)
            first = self.read_event(response)
            response.close()
            conn.close()
            self.assertEqual(first['id'], self.board.snapshot()['revision'])
            self.board.notice('test', 'changed while disconnected')
            latest = self.board.snapshot()['revision']
            conn, response = self.open_events(server, latest, last_id=first['id'])
            try:
                event = self.read_event(response)
                self.assertEqual(event['id'], latest)
                self.assertIn('changed while disconnected', json.loads(event['data'])['html'])
            finally:
                response.close()
                conn.close()

    def test_idle_state_does_not_publish_and_disk_failure_does_not_notify(self):
        initial = self.board.snapshot()['revision']
        self.board.notice('watch', '')
        self.assertIsNone(self.board.wait_for_update(initial, timeout=0.01))
        with mock.patch.object(lb, 'atomic_json', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.board.notice('test', 'must not publish')
        self.assertEqual(self.board.snapshot()['revision'], initial)
        self.assertIsNone(self.board.wait_for_update(initial, timeout=0.01))
        self.board.notice('test', 'persisted update')
        update = self.board.wait_for_update(initial, timeout=0.01)
        self.assertEqual(update['revision'], json.loads((self.results / 'results.json').read_text())['revision'])

    def test_waiting_clients_wake_when_service_closes(self):
        revision = self.board.snapshot()['revision']
        returned = threading.Event()
        def wait():
            self.board.wait_for_update(revision, timeout=30)
            returned.set()
        waiter = threading.Thread(target=wait)
        waiter.start()
        self.board.close()
        self.assertTrue(returned.wait(timeout=2))
        waiter.join(timeout=2)

    def test_http_remains_responsive_during_grading(self):
        self.upload()
        entered, release = threading.Event(), threading.Event()
        class SlowRunner(FakeRunner):
            def run(self, phase, cwd, log, stop):
                if phase == 'simulation':
                    entered.set()
                    release.wait(timeout=5)
                super().run(phase, cwd, log, stop)
        worker = threading.Thread(target=self.grade_next, args=(SlowRunner(),))
        server = lb.make_server(self.board, 0)
        web = threading.Thread(target=server.serve_forever, daemon=True)
        web.start()
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=4))
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            conn.request('GET', '/')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('處理中', response.read().decode())
            conn.close()
        finally:
            release.set()
            worker.join(timeout=6)
            server.shutdown()
            server.server_close()
            web.join()

    def test_tool_timeout_and_nonzero_exit(self):
        runner = lb.ToolRunner()
        with self.assertRaises(lb.Issue) as raised:
            runner.execute([sys.executable, '-c', 'import time; time.sleep(10)'], self.root,
                           self.root / 'timeout.log', 0.1, threading.Event())
        self.assertEqual(raised.exception.code, 'TOOL_TIMEOUT')
        with self.assertRaises(lb.Issue) as raised:
            runner.execute([sys.executable, '-c', 'raise SystemExit(7)'], self.root,
                           self.root / 'exit.log', 3, threading.Event())
        self.assertEqual(raised.exception.code, 'TOOL_FAILED')

    def test_startup_warning_does_not_hide_compiler_failure(self):
        runner = lb.ToolRunner()
        diagnostics = (
            ("/bin/ls: cannot access '/unrelated/INNOVUS': No such file or directory\n"
             "Parsing design file '../RTL_code/cnn_top.v'\nError-[SE] Syntax error\n", 'TOOL_FAILED'),
            ('vcs: Command not found.\n', 'ENVIRONMENT'),
            ('dc_shell: No such file or directory\n', 'ENVIRONMENT'),
            ('License checkout failed.\n', 'ENVIRONMENT'),
            ('Unable to obtain a license.\n', 'ENVIRONMENT'),
        )
        for message, expected in diagnostics:
            with self.subTest(expected=expected, message=message):
                command = [sys.executable, '-c', 'import sys; sys.stdout.write({!r}); sys.exit(1)'.format(message)]
                with self.assertRaises(lb.Issue) as raised:
                    runner.execute(command, self.root, self.root / 'diagnostic.log', 3, threading.Event())
                self.assertEqual(raised.exception.code, expected)


if __name__ == '__main__':
    unittest.main()
