#!/usr/bin/env python3
"""HW4 leaderboard. Python 3.6+, standard library only; run --help for usage."""

import argparse
import copy
import csv
import datetime
import fcntl
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import pwd
import queue
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlsplit


POLL_SECONDS = 2
SETTLE_SECONDS = 6
SSE_HEARTBEAT_SECONDS = 15
SIM_TIMEOUT = 15 * 60
SYN_TIMEOUT = 30 * 60
MAX_ARCHIVE = 64 * 1024 ** 2
MAX_EXPANDED = 256 * 1024 ** 2
MAX_MEMBER = 32 * 1024 ** 2
MAX_MEMBERS = 2048
MAX_LOG = 16 * 1024 ** 2
MAX_WORK = 2 * 1024 ** 3
FORM_URL = ('https://docs.google.com/forms/d/'
            '1jq-yWiDyvKFq7BkWWeoQiMRwzw4Olf4rNqgk33RFRC8/viewform')
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))
STUDENT_ID_PATTERN = r'[BDM][0-9A-Z]{8}'
STUDENT = re.compile(STUDENT_ID_PATTERN + r'\Z', re.ASCII)
FILENAME = re.compile('(' + STUDENT_ID_PATTERN + r')\.zip\Z', re.ASCII)
JOB_ID = re.compile(r'[0-9]{8}_[0-9]{6}_[0-9]{6}_(?:' + STUDENT_ID_PATTERN + r'|UNKNOWN)_[0-9a-f]{12}\Z')
# Class roster embedded from name2id2user; no external roster is needed at runtime.
STUDENT_NAMES = {
    'B11207001': '蔡安皓',
    'B11207012': '黃品堯',
    'B11207050': '王俞哲',
    'B11207137': '蔡詣辰',
    'B11207139': '陳晏齊',
    'B11232009': '胡守曾',
    'B11232012': '孫靖傑',
    'D11407804': 'PHAN MINH ANH',
    'M11402176': '賴敬予',
    'M11407320': '丁泓文',
    'M11407438': '彭弼暉',
    'M11407439': '林明宏',
    'M11407440': '李勁磊',
    'M11415113': '羅君瑜',
    'M11452035': '劉柏劭',
    'M11454005': '孔恩亞',
    'M11454011': '吳昱嫺',
    'M11454015': '胡培詰',
    'M11454028': '黃聿萌',
    'M11454030': '劉武杰',
    'M11507401': '李品翰',
    'M11507402': '潘郁辰',
    'M11507403': '黃敏聖',
    'M11507406': '羅曜安',
    'M11507407': '丁奕凱',
    'M11507408': '張哲瑋',
    'M11507409': '丁沐恩',
    'M11507411': '洪承佑',
    'M11507413': '施柏丞',
    'M11507416': '施博允',
    'M11507418': '王家磊',
    'M11507419': '林柏佑',
    'M11507420': '陳可維',
    'M11507421': '葉奕宏',
    'M11507424': '黃堃容',
    'M11507426': '紀凱瀚',
    'M11507427': '游騰楓',
    'M11507430': '陳致詠',
    'M11507432': '巫煊崴',
    'M11507433': '邵柏凱',
    'M11507434': '江柏宏',
    'M11507611': '吳家榕',
    'M11507618': '湯光鈞',
    'M11507W01': '余芃節',
    'M11515013': '徐柏揚',
    'M11554001': '林玟瑄',
    'M11554029': '江庭宇',
    'M11554014': '石柏億',
}

REQUIRED = ('RTL_code/cnn_top.v', 'syn/syn.tcl', 'tb/rtl_sim.f', 'tb/setting.vh')
WEIGHTS = {'conv1_w': 600, 'conv2_w': 3200, 'fc1_w': 8192, 'fc2_w': 256,
           'conv1_b': 8, 'conv2_b': 16, 'fc1_b': 32, 'fc2_b': 8}
ACTIVE = {'queued', 'validating', 'simulating', 'synthesizing'}
PASSWORD_ERRORS = {'PASSWORD_MISSING', 'DECRYPT_FAILED', 'CSV_ERROR'}
STATUS_TEXT = {'queued': '等待評分', 'validating': '處理中', 'simulating': '處理中',
               'synthesizing': '處理中', 'success': '通過', 'error': '失敗',
               'interrupted': '已中斷'}


class Issue(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class UploadChanged(Exception):
    pass


def now():
    return datetime.datetime.now(TAIPEI).isoformat(timespec='milliseconds')


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def read_regular(path, limit=MAX_MEMBER):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Issue('INVALID_FILE', '不是一般檔案或檔案過大：' + Path(path).name)
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise Issue('INVALID_FILE', '檔案過大：' + Path(path).name)
        return b''.join(chunks)
    finally:
        os.close(fd)


def atomic_json(path, value, mode=0o644):
    fd, temp = tempfile.mkstemp(prefix='.json-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temp, str(path))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def protected_directory(path, mode):
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    forbidden = 0o077 if mode == 0o700 else 0o022
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & forbidden):
        raise Issue('SETUP', '{} 必須由執行帳號擁有，權限不可寬於 {:o}'.format(path, mode))


def strip_comments(text):
    return re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)


def interval_value(text):
    match = re.fullmatch(r'\s*`define\s+INTERVAL\s+([0-9]+)\s*', strip_comments(text), re.ASCII)
    # WAIT_MAX in the official testbench is a signed 32-bit integer (10 * INTERVAL).
    if not match or len(match[1]) > 10 or not 784 <= int(match[1]) <= 214748364:
        raise Issue('INVALID_INTERVAL', 'setting.vh 必須只有一個十進位 INTERVAL，範圍 784～214748364（避免 testbench 整數溢位）')
    return int(match[1])


def rtl_path(token, available=None):
    if not re.fullmatch(r'[A-Za-z0-9_./-]+\.v', token, re.ASCII):
        raise Issue('INVALID_RTL_PATH', '不合法的 .v 路徑：' + token[:160])
    parts = token.split('/')
    if parts.count('RTL_code') != 1:
        raise Issue('INVALID_RTL_PATH', '.v 必須來自 ZIP 的 RTL_code：' + token[:160])
    tail = parts[parts.index('RTL_code') + 1:]
    if not tail or any(p in ('', '.', '..') for p in tail) or tail[-1].lower() == 'sram.v':
        raise Issue('INVALID_RTL_PATH', '不合法的 RTL_code 路徑：' + token[:160])
    result = 'RTL_code/' + '/'.join(tail)
    if available is not None and result not in available:
        raise Issue('MISSING_RTL', '引用的檔案缺失：' + result)
    return result


def tcl_assignment(text):
    """Locate a literal RTL_FILES assignment, without invoking a Tcl interpreter."""
    text = text.replace('\r\n', '\n')
    matches = list(re.finditer(r'^[ \t]*set[ \t]+RTL_FILES[ \t]+', text, re.M))
    if len(matches) != 1:
        raise Issue('SYN_CHANGED', 'syn.tcl 必須保留單一 set RTL_FILES 清單')
    match = matches[0]
    bracket, brace, quoted, escaped = 0, 0, False, False
    end = len(text)
    for index in range(match.end(), len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if char == '"' and brace == 0:
            quoted = not quoted
        elif not quoted:
            if char == '{':
                brace += 1
            elif char == '}':
                brace -= 1
            elif not brace and char == '[':
                bracket += 1
            elif not brace and char == ']':
                bracket -= 1
        if char == '\n' and bracket == 0 and brace == 0 and not quoted:
            end = index
            break
        if brace < 0 or bracket < 0:
            raise Issue('SYN_CHANGED', 'syn.tcl 的 RTL_FILES 括號不完整')
    if brace or bracket or quoted or escaped:
        raise Issue('SYN_CHANGED', 'syn.tcl 的 RTL_FILES 清單不完整')
    value = text[match.end():end].strip()
    if value.endswith(';'):
        value = value[:-1].rstrip()
    value = re.sub(r'\\\n[ \t]*', ' ', value)
    if value.startswith('[list') and value.endswith(']') and re.match(r'\[list\s', value):
        value = value[5:-1].strip()
        if '\n' in value:
            raise Issue('SYN_CHANGED', 'RTL_FILES 的 [list] 換行必須使用反斜線延續')
    elif (value.startswith('{') and value.endswith('}')) or (value.startswith('"') and value.endswith('"')):
        value = value[1:-1]
    token_pattern = re.compile(r'\s*(?:"([A-Za-z0-9_./-]+)"|\{([A-Za-z0-9_./-]+)\}|([A-Za-z0-9_./-]+))')
    tokens, offset = [], 0
    while offset < len(value.rstrip()):
        token = token_pattern.match(value, offset)
        if not token:
            raise Issue('SYN_CHANGED', 'RTL_FILES 只允許純文字 .v 路徑，不可含額外 Tcl 命令')
        tokens.append(next(group for group in token.groups() if group is not None))
        offset = token.end()
    if not tokens:
        raise Issue('SYN_CHANGED', 'RTL_FILES 不可為空')
    return text, match.start(), end, tokens


def validated_tcl(submitted, baseline, available):
    actual, start, end, tokens = tcl_assignment(submitted)
    expected, base_start, base_end, unused = tcl_assignment(baseline)
    actual_mask = actual[:start] + '<RTL_FILES>' + actual[end:]
    expected_mask = expected[:base_start] + '<RTL_FILES>' + expected[base_end:]
    if actual_mask != expected_mask:
        a, b = actual_mask.splitlines(), expected_mask.splitlines()
        line = next((i + 1 for i, pair in enumerate(zip(a, b)) if pair[0] != pair[1]), min(len(a), len(b)) + 1)
        raise Issue('SYN_CHANGED', 'syn.tcl 第 {} 行附近與基準不符；只能修改 RTL_FILES 的 .v 清單'.format(line))
    paths = [rtl_path(token, available) for token in tokens]
    if len(paths) != len(set(paths)):
        raise Issue('INVALID_RTL_PATH', 'RTL_FILES 重複引用同一個 .v')
    if 'RTL_code/cnn_top.v' not in paths:
        raise Issue('MISSING_RTL', 'RTL_FILES 缺少 RTL_code/cnn_top.v')
    # Only the allowed path assignment is relocated. The rest is the student's
    # validated script, including its clock/constraints/reporting commands.
    result = actual[:start] + 'set RTL_FILES [list ' + ' '.join('../' + p for p in paths) + '];' + actual[end:]
    final, first, last, final_tokens = tcl_assignment(result)
    if final[:first] + '<RTL_FILES>' + final[last:] != expected_mask:
        raise Issue('SYN_CHANGED', '無法建立已驗證的 syn.tcl 執行副本')
    return result, paths


def flist_tokens(text):
    result = []
    for line in text.replace('\r\n', '\n').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line or line.startswith('//'):
            continue
        result.extend(line.split())
    return result


def validated_flist(submitted, baseline, available, syn_paths):
    actual, expected = flist_tokens(submitted), flist_tokens(baseline)
    def is_rtl(token):
        return 'RTL_code' in token.split('/')
    if [t for t in actual if not is_rtl(t)] != [t for t in expected if not is_rtl(t)]:
        raise Issue('FLIST_CHANGED', 'rtl_sim.f 含有不允許的選項或檔案；僅可增減 RTL_code 內的 .v')
    paths = [rtl_path(t, available) for t in actual if is_rtl(t)]
    if len(paths) != len(set(paths)) or set(paths) != set(syn_paths):
        raise Issue('FLIST_CHANGED', 'rtl_sim.f 的 RTL 清單必須與 syn.tcl 一致，且不可重複')
    generated, inserted = [], False
    for token in expected:
        if is_rtl(token):
            if not inserted:
                generated.extend('../' + p for p in syn_paths)
                inserted = True
        else:
            generated.append(token)
    if not inserted:
        raise Issue('COURSE_ERROR', '官方 rtl_sim.f 缺少 RTL_code 路徑')
    return '\n'.join(generated) + '\n'


def hex_values(raw, count, maximum, name):
    try:
        tokens = strip_comments(raw.decode('ascii')).split()
        if len(tokens) != count or any(not re.fullmatch(r'[0-9a-fA-F]+', t) for t in tokens):
            raise ValueError()
        values = [int(t, 16) for t in tokens]
        if any(v > maximum for v in values):
            raise ValueError()
        return values
    except (ValueError, UnicodeError):
        raise Issue('COURSE_ERROR', '官方資料格式或數量不符：' + name)


class Course:
    def __init__(self, root):
        self.root = Path(root)
        names = ['syn.tcl', '.synopsys_dc.setup', 'rtl_sim.f', 'tb_simple.v',
                 'export/tb/images.hex', 'export/tb/pred.hex']
        names += ['export/weights/' + name + '.hex' for name in sorted(WEIGHTS)]
        self.files = {}
        missing = []
        for name in names:
            try:
                self.files[name] = read_regular(self.root / name)
            except FileNotFoundError:
                missing.append(name)
        if missing:
            raise Issue('COURSE_ERROR', '官方資源缺失：' + '、'.join(missing))
        try:
            self.tcl = self.files['syn.tcl'].decode('utf-8-sig')
            self.flist = self.files['rtl_sim.f'].decode('utf-8-sig')
            tb = self.files['tb_simple.v'].decode('utf-8-sig')
        except UnicodeError:
            raise Issue('COURSE_ERROR', '官方腳本必須使用 UTF-8 編碼')
        tcl_assignment(self.tcl)
        if not re.search(r'\bN_IMG\s*=\s*8\b', tb) or not re.search(r'\bCYCLE\s*=\s*10(?:\.0)?\s*;', tb):
            raise Issue('COURSE_ERROR', '官方 testbench 必須使用 8 張圖片、10 ns 時脈')
        self.images = hex_values(self.files['export/tb/images.hex'], 8 * 2352, 255, 'images.hex')
        self.golden = hex_values(self.files['export/tb/pred.hex'], 8, 7, 'pred.hex')
        for name, count in WEIGHTS.items():
            hex_values(self.files['export/weights/' + name + '.hex'], count,
                       0xffffffff if name.endswith('_b') else 255, name)
        digest = hashlib.sha256()
        for name in sorted(self.files):
            digest.update(name.encode('utf-8') + b'\0' + self.files[name] + b'\0')
        self.digest = digest.hexdigest()

    def prepare(self, work, student_tcl, generated_flist):
        (work / 'syn' / 'syn.tcl').write_text(student_tcl, encoding='utf-8')
        (work / 'syn' / '.synopsys_dc.setup').write_bytes(self.files['.synopsys_dc.setup'])
        (work / 'tb' / 'rtl_sim.f').write_text(generated_flist, encoding='utf-8')
        (work / 'tb' / 'tb_simple.v').write_bytes(self.files['tb_simple.v'])
        for name, raw in self.files.items():
            if name.startswith('export/weights/'):
                target = work / 'tb' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        order = list(range(8))
        while order == list(range(8)):
            random.SystemRandom().shuffle(order)
        data = work / 'tb' / 'export' / 'tb'
        data.mkdir(parents=True, exist_ok=True)
        pixels = [v for i in order for v in self.images[i * 2352:(i + 1) * 2352]]
        expected = [self.golden[i] for i in order]
        (data / 'images.hex').write_text(''.join('{:02x}\n'.format(v) for v in pixels), encoding='ascii')
        (data / 'pred.hex').write_text(''.join('{:02x}\n'.format(v) for v in expected), encoding='ascii')
        return order, expected


def safe_extract(archive_path, destination, password):
    """Validate every member first, then copy bounded streams; never extractall."""
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(str(archive_path)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise Issue('ZIP_LIMIT', 'ZIP 為空或項目數超過 {}'.format(MAX_MEMBERS))
            seen, plan, declared = set(), [], 0
            encrypted = False
            for item in members:
                name = item.filename
                original = getattr(item, 'orig_filename', name)
                if ('\0' in original or '\\' in name
                        or not re.fullmatch(r'[A-Za-z0-9_./-]+', name, re.ASCII)):
                    raise Issue('UNSAFE_ZIP', 'ZIP 路徑僅允許英文字母、數字、底線、連字號與句點：' + name[:160])
                parts = name.rstrip('/').split('/')
                if name.startswith('/') or any(p in ('', '.', '..') for p in parts):
                    raise Issue('UNSAFE_ZIP', 'ZIP 含絕對路徑或路徑穿越：' + name[:160])
                key = '/'.join(parts).casefold()
                if key in seen:
                    raise Issue('UNSAFE_ZIP', 'ZIP 含重複或大小寫衝突的路徑：' + name[:160])
                seen.add(key)
                kind = stat.S_IFMT(item.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not item.is_dir()):
                    raise Issue('UNSAFE_ZIP', 'ZIP 不可包含連結或特殊檔案：' + name[:160])
                if item.is_dir():
                    if parts[0] not in ('RTL_code', 'syn', 'tb') or (parts[0] != 'RTL_code' and len(parts) != 1):
                        raise Issue('INVALID_FORMAT', 'ZIP 根目錄應直接包含 RTL_code、syn、tb，不可再包學號資料夾')
                    continue
                allowed = (parts[0] == 'RTL_code' and len(parts) > 1 and name.endswith('.v')) or name in REQUIRED[1:]
                if not allowed or parts[-1].lower() == 'sram.v' or parts[-1] == 'N16ADFP_SRAM_100a.v':
                    raise Issue('INVALID_FORMAT', '不允許的繳交檔案：' + name[:160] + '；請依雲端格式繳交，勿附 SRAM 模型')
                declared += item.file_size
                if (item.file_size > MAX_MEMBER or declared > MAX_EXPANDED
                        or item.file_size > max(1, item.compress_size) * 1000):
                    raise Issue('ZIP_LIMIT', 'ZIP 展開大小、單一檔案大小或壓縮倍率超過限制')
                if item.compress_type == 99 or b'\x01\x99' in item.extra:
                    raise Issue('UNSUPPORTED_ENCRYPTION', '目前不支援 AES ZIP，請改用傳統 ZIP 加密（例如 zip -e）')
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
                    raise Issue('UNSUPPORTED_COMPRESSION', 'ZIP 使用目前 Python 不支援的壓縮方式')
                encrypted = encrypted or bool(item.flag_bits & 1)
                plan.append(item)
            actual_files = {item.filename for item in plan}
            missing = [name for name in REQUIRED if name not in actual_files]
            if missing:
                raise Issue('MISSING_FILES', '缺少檔案：' + '、'.join(missing))
            if encrypted and not password:
                raise Issue('PASSWORD_MISSING', 'ZIP 已加密，但 CSV 沒有這個學號的密碼；請確認表單及助教的 CSV')
            encoded = password.encode('utf-8') if password else None
            total = 0
            for item in plan:
                target = destination / item.filename
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                size = 0
                with archive.open(item, pwd=encoded) as source, target.open('xb') as out:
                    while True:
                        block = source.read(65536)
                        if not block:
                            break
                        size += len(block)
                        total += len(block)
                        if size > MAX_MEMBER or total > MAX_EXPANDED:
                            raise Issue('ZIP_LIMIT', 'ZIP 實際展開大小超過限制')
                        out.write(block)
                if size != item.file_size:
                    raise Issue('CORRUPT_ZIP', 'ZIP 檔案不完整，請重新壓縮及繳交')
    except Issue:
        raise
    except (zipfile.BadZipFile, RuntimeError, EOFError, ValueError):
        raise Issue('DECRYPT_FAILED', '無法解壓 ZIP：可能是壓縮檔損毀或密碼錯誤；請確認表單密碼並通知助教更新 CSV')
    except NotImplementedError:
        raise Issue('UNSUPPORTED_COMPRESSION', 'ZIP 的加密或壓縮方式不支援')
    return [item.filename for item in plan]


def extract_child():
    """Bound decoder CPU, address space and file size; password is stdin-only."""
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 ** 2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_MEMBER,) * 2)
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.read(65536))
        files = safe_extract(request['archive'], request['destination'], request.get('password'))
        result = {'ok': True, 'files': files}
    except Issue as error:
        result = {'ok': False, 'code': error.code, 'message': error.message}
    except Exception:
        result = {'ok': False, 'code': 'DECRYPT_FAILED', 'message': 'ZIP 無法讀取：可能是檔案損毀、密碼錯誤或編碼不支援'}
    print(json.dumps(result, ensure_ascii=False))


def extract_bounded(archive, work, password):
    request = json.dumps({'archive': str(archive), 'destination': str(work), 'password': password})
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '_extract'],
                                input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, timeout=60)
        response = json.loads(result.stdout) if result.returncode == 0 else {}
    except subprocess.TimeoutExpired:
        raise Issue('EXTRACT_TIMEOUT', 'ZIP 解壓超過 60 秒，請確認大小與壓縮方式')
    except (ValueError, OSError):
        response = {}
    if not response.get('ok'):
        raise Issue(response.get('code', 'EXTRACT_FAILED'), response.get('message', 'ZIP 解壓失敗或超過資源限制'))
    return response['files']


def parse_simulation(text, expected):
    if re.search(r'\bFAIL\b|\[(?:ERROR|FATAL|TIMEOUT)\]|Error-\[|Fatal:', text, re.I):
        raise Issue('SIM_FAILED', '公開測資未全數通過，或模擬發生錯誤／逾時；請查看 rtl.log')
    matches = re.findall(r'^img\s+(\d+)\s*:\s*golden\s+(\d+),\s*got\s+(\d+)\s+PASS\s*$', text, re.M)
    actual = [(int(i), int(a), int(b)) for i, a, b in matches]
    required = [(i, value, value) for i, value in enumerate(expected)]
    if actual != required or len(re.findall(r'^\s*8\s*/\s*8\s+PASS\s*$', text, re.M)) != 1:
        raise Issue('SIM_FAILED', '必須完整通過洗牌後的 8 張公開測資，且沒有重複或缺少輸出；請查看 rtl.log')
    throughput = re.findall(r'throughput\s*:\s*([0-9.eE+-]+)\s+FPS\s*\(8 images in (\d+) cycles\)', text)
    if len(throughput) != 1:
        raise Issue('SIM_FAILED', '模擬缺少完整的 8 張圖片吞吐量統計')
    fps, cycles = float(throughput[0][0]), int(throughput[0][1])
    if not math.isfinite(fps) or fps <= 0 or cycles <= 0 or not math.isclose(fps, 8e8 / cycles, rel_tol=0.001, abs_tol=0.001):
        raise Issue('SIM_FAILED', '模擬吞吐量統計不合法')
    return fps


NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'


def one_number(pattern, text, description):
    values = re.findall(pattern, text, re.M)
    if len(values) != 1:
        raise Issue('SYN_REPORT', '缺少或重複的合成統計：' + description)
    value = float(values[0])
    if not math.isfinite(value):
        raise Issue('SYN_REPORT', '合成統計不是有效數字：' + description)
    return value


def license_unavailable(text):
    # DC can return zero after compile_ultra fails, then still print reports.
    # Match explicit checkout failures, leaving ordinary Error counts diagnostic.
    return re.search(r'^\s*(?:(?:Error|Fatal):\s*)?(?:.*\(SEC-12\)|'
                     r'(?:Failed|Unable|Cannot) to (?:obtain|get|check\s*out)\b.*\blicen[cs]e\b|'
                     r'Licen[cs]e\s+checkout\s+(?:failed|failure)\b)', text, re.I | re.M)


def parse_synthesis(log, area_report, timing_report, latch_report):
    if license_unavailable(log):
        raise Issue('ENVIRONMENT', '合成工具無法取得授權，未完成合成；請助教查看 syn.log')
    errors = len(re.findall('Error', log))
    if re.search(r'^\s*(?:ERROR:|Fatal:)', log, re.M):
        raise Issue('SYN_FAILED', 'syn.log 含有 ERROR 或 Fatal 訊息；請查看合成 log')
    latches = one_number(r'^\s*latches\s*:\s*(' + NUMBER + r')\s*$', log, 'latches')
    # The course script prints the count before and after compile_ultra.
    # Only the final (post-compile) count describes the ranked netlist.
    macro_counts = re.findall(r'^\s*SRAM macros\s*:\s*(' + NUMBER + r')\s*$', log, re.M)
    if not 1 <= len(macro_counts) <= 2:
        raise Issue('SYN_REPORT', '缺少或重複的合成統計：SRAM macros')
    macros = float(macro_counts[-1])
    if not math.isfinite(macros):
        raise Issue('SYN_REPORT', 'SRAM macros 不是有效數字')
    slack = one_number(r'^\s*WNS\s*:\s*(' + NUMBER + r')\s*$', log, 'WNS')
    area = one_number(r'^\s*total area\s*:\s*(' + NUMBER + r')\s*$', log, 'total area')
    if latches != 0 or latch_report.strip().lower() != 'no latch':
        raise Issue('SYN_LATCH', '合成產生 latch，不計入排名；請查看 latch.rpt')
    if macros < 1 or not macros.is_integer():
        raise Issue('SYN_SRAM', '合成結果沒有有效的 SRAM macro，不計入排名')
    timing = re.findall(r'slack\s*\(MET\)\s+(' + NUMBER + ')', timing_report)
    if slack <= 0 or not timing or any(float(v) <= 0 for v in timing) or 'VIOLATED' in timing_report:
        raise Issue('SYN_TIMING', 'Timing 未通過，slack 必須大於 0；請查看 timing.rpt')
    reported = re.findall(r'Total cell area\s*:\s*(' + NUMBER + ')', area_report)
    if area <= 0 or not reported or not all(math.isfinite(float(v)) and float(v) > 0 for v in reported):
        raise Issue('SYN_REPORT', 'area.rpt 或總面積統計缺失／不合法')
    if not math.isclose(area, float(reported[0]), rel_tol=0.0001, abs_tol=0.02):
        raise Issue('SYN_REPORT', '總面積與 area.rpt 不符，可能是面積加總中途停止；請助教查看合成報告')
    return {'area': area, 'reported_area': float(reported[0]), 'slack': slack,
            'sram_macros': int(macros), 'synthesis_error_count': errors}


def terminate_process(process):
    # Also stop descendants if the launcher already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def directory_size(path):
    total = 0
    for root, directories, files in os.walk(str(path), followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except FileNotFoundError:
                pass
            if total > MAX_WORK:
                return total
    return total


class ToolRunner:
    COMMANDS = {'simulation': ['tcsh', '-c', 'exec vcs -full64 -R -top tb_simple -f rtl_sim.f'],
                'synthesis': ['tcsh', '-c', 'exec dc_shell -f syn.tcl']}

    def run(self, phase, cwd, log_path, stop):
        if not shutil.which('tcsh'):
            raise Issue('ENVIRONMENT', '找不到 tcsh；實際評分需要課程 server 的 VCS/DC 環境')
        self.execute(self.COMMANDS[phase], cwd, log_path,
                     SIM_TIMEOUT if phase == 'simulation' else SYN_TIMEOUT, stop)

    def execute(self, command, cwd, log_path, timeout, stop):
        with log_path.open('wb') as log:
            process = subprocess.Popen(command, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, stdin=subprocess.DEVNULL)
            started, checked = time.monotonic(), 0
            try:
                while process.poll() is None:
                    if stop.wait(0.2):
                        raise Issue('INTERRUPTED', '服務停止，這筆評分已中斷')
                    elapsed = time.monotonic() - started
                    if elapsed > timeout:
                        raise Issue('TOOL_TIMEOUT', '評分工具執行超過 {} 秒，已終止'.format(timeout))
                    if log.tell() > MAX_LOG or log_path.stat().st_size > MAX_LOG:
                        raise Issue('RESOURCE_LIMIT', '工具 log 超過 16 MiB，已終止評分')
                    if elapsed - checked >= 2:
                        checked = elapsed
                        if directory_size(cwd.parent) > MAX_WORK:
                            raise Issue('RESOURCE_LIMIT', '評分工作檔超過 2 GiB，已終止評分')
                if log_path.stat().st_size > MAX_LOG:
                    raise Issue('RESOURCE_LIMIT', '工具 log 超過 16 MiB')
                message = log_path.read_text(encoding='utf-8', errors='replace')[:MAX_LOG]
                if license_unavailable(message):
                    raise Issue('ENVIRONMENT', '評分工具無法取得授權，未完成評分；請助教查看 ' + log_path.name)
                if process.returncode:
                    # Login startup may report unrelated missing EDA directories
                    # before VCS successfully starts. Those lines must not hide
                    # the actual compiler error later in the log.
                    missing_tool = re.search(r'^\s*(?:\S*/)?(?:vcs|dc_shell|tcsh):\s*(?:Command not found|No such file)',
                                             message, re.I | re.M)
                    license_failure = re.search(r'license.*(?:fail|denied)|(?:fail|unable|cannot).*license', message, re.I)
                    if missing_tool or license_failure:
                        raise Issue('ENVIRONMENT', '評分工具、授權或官方路徑無法使用，請助教查看 log')
                    raise Issue('TOOL_FAILED', '評分工具結束碼為 {}，請查看 {}'.format(process.returncode, log_path.name))
            finally:
                terminate_process(process)
                if log_path.stat().st_size > MAX_LOG:
                    log.truncate(MAX_LOG)


def competition_rank(history):
    chosen = {}
    for record in sorted(history, key=lambda r: (r['received_at'], r['id'])):
        if record['status'] == 'success' and record.get('student_id'):
            chosen[record['student_id']] = copy.deepcopy(record)
    records = list(chosen.values())
    for value_key, rank_key in (('area', 'area_rank'), ('interval', 'speed_rank')):
        previous, rank = None, 0
        for index, record in enumerate(sorted(records, key=lambda r: r[value_key]), 1):
            if index == 1 or record[value_key] != previous:
                rank = index
            record[rank_key] = rank
            previous = record[value_key]
    for record in records:
        record['rank_sum'] = record['area_rank'] + record['speed_rank']
    previous, rank = None, 0
    for index, record in enumerate(sorted(records, key=lambda r: r['rank_sum']), 1):
        if index == 1 or record['rank_sum'] != previous:
            rank = index
        record['overall_rank'] = rank
        previous = record['rank_sum']
    return sorted(records, key=lambda r: (r['overall_rank'], r['student_id']))


def read_passwords(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise Issue('CSV_ERROR', '密碼 CSV 必須由執行帳號擁有且設為 600（不可供其他帳號讀寫）')
    try:
        raw = read_regular(path, 2 * 1024 ** 2).decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(raw, newline=''))
        headers = reader.fieldnames or []
        if headers.count('學號') != 1 or headers.count('密碼') != 1:
            raise Issue('CSV_ERROR', '密碼 CSV 必須包含唯一的「學號」與「密碼」欄位')
        result = {}
        for row in reader:
            student = (row.get('學號') or '').strip().upper()
            if not student:
                continue
            if not STUDENT.fullmatch(student) or row.get('密碼') is None or None in row:
                raise Issue('CSV_ERROR', '密碼 CSV 含不合法的學號或欄位數量；請助教檢查匯出檔')
            secret = row['密碼']
            if len(secret) > 1024:
                raise Issue('CSV_ERROR', '密碼 CSV 含過長的密碼欄位')
            result[student] = secret
        if signature(Path(path).lstat()) != signature(info):
            raise Issue('CSV_ERROR', '密碼 CSV 正在更新，稍後會重新讀取')
        return result, signature(info)
    except (UnicodeError, csv.Error):
        raise Issue('CSV_ERROR', '密碼 CSV 無法讀取，請使用 UTF-8 編碼並保留欄位標題')


class Board:
    def __init__(self, watch_dir, password_csv, data_dir, course_dir):
        self.watch_dir = Path(watch_dir).resolve()
        self.password_csv = Path(password_csv).resolve()
        self.data_dir = Path(data_dir).resolve()
        if not self.watch_dir.is_dir():
            raise Issue('SETUP', '監看目錄不存在：' + str(self.watch_dir))
        protected_directory(self.data_dir, 0o755)
        self.private = self.data_dir / 'submissions'
        protected_directory(self.private, 0o700)
        self.lock_handle = open(str(self.data_dir / 'results.lock'), 'a')
        os.chmod(str(self.data_dir / 'results.lock'), 0o600)
        try:
            fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_handle.close()
            raise Issue('SETUP', '同一資料目錄已有排行榜程式執行中')
        try:
            self.course = Course(course_dir)
            self.passwords, self.csv_revision = read_passwords(self.password_csv)
            self.csv_seen = self.csv_revision
            self.csv_issue = None
            self.lock = threading.RLock()
            self.changed = threading.Condition(self.lock)
            self.stop = threading.Event()
            self.tasks = queue.Queue()
            self.notices = {}
            self.history = []
            path = self.data_dir / 'results.json'
            if path.exists():
                try:
                    previous = json.loads(read_regular(path).decode('utf-8'))
                    if previous.get('schema_version') != 1 or not isinstance(previous.get('history'), list):
                        raise ValueError()
                    if previous.get('course_hash') != self.course.digest:
                        raise Issue('SETUP', '官方基準與現有成績版本不同，請改用新的資料目錄，避免混合排名')
                    self.history = previous['history']
                    for record in self.history:
                        if not JOB_ID.fullmatch(record['id']):
                            raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise Issue('SETUP', 'results.json 格式損毀；請先備份修復，程式不會覆蓋它')
            for record in self.history:
                if record['status'] in ACTIVE:
                    job = self.private / record['id']
                    if (job / 'archive.zip').is_file():
                        if (job / 'work').exists():
                            shutil.rmtree(str(job / 'work'))
                        record.update(status='queued', message='服務重啟，等待重新評分')
                        self.tasks.put(record['id'])
                    else:
                        record.update(status='interrupted', error_code='INTERRUPTED', message='上次評分中斷，等待重新收件')
            with self.lock:
                self._save_locked()
        except Exception:
            self.lock_handle.close()
            raise

    def close(self):
        self.stop.set()
        with self.changed:
            self.changed.notify_all()
        self.lock_handle.close()

    def _payload_locked(self):
        ranking = competition_rank(self.history)
        return {'schema_version': 1, 'updated_at': now(), 'course_hash': self.course.digest,
                'policy': {'selection': 'latest_success', 'speed_metric': 'interval', 'ties': 'competition',
                           'syn_error_count': 'diagnostic_only'},
                'summary': {'ranked': len(ranking), 'submissions': len(self.history),
                            'queued': sum(r['status'] == 'queued' for r in self.history),
                            'running': sum(r['status'] in ACTIVE - {'queued'} for r in self.history)},
                'notice': '；'.join(self.notices.values()),
                'rankings': ranking, 'history': copy.deepcopy(self.history)}

    def _save_locked(self):
        payload = self._payload_locked()
        payload['revision'] = uuid.uuid4().hex
        atomic_json(self.data_dir / 'results.json', payload)
        self.payload = payload
        self.changed.notify_all()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.payload)

    def wait_for_update(self, revision, timeout=SSE_HEARTBEAT_SECONDS):
        with self.changed:
            self.changed.wait_for(lambda: self.stop.is_set() or self.payload['revision'] != revision,
                                  timeout=timeout)
            if not self.stop.is_set() and self.payload['revision'] != revision:
                return copy.deepcopy(self.payload)
        return None

    def update(self, job_id, **values):
        with self.lock:
            record = next(r for r in self.history if r['id'] == job_id)
            record.update(values)
            atomic_json(self.private / job_id / 'record.json', record, 0o600)
            self._save_locked()

    def notice(self, key, message):
        with self.lock:
            before = dict(self.notices)
            if message:
                self.notices[key] = message
            else:
                self.notices.pop(key, None)
            if before != self.notices:
                self._save_locked()

    def refresh_passwords(self):
        try:
            current = signature(self.password_csv.lstat())
        except OSError:
            current = None
        if current == self.csv_seen and not self.csv_issue:
            return
        try:
            values, revision = read_passwords(self.password_csv)
        except (OSError, Issue) as error:
            self.csv_seen = current
            self.csv_issue = error.message if isinstance(error, Issue) else '無法讀取密碼 CSV，請助教確認檔案'
            self.notice('csv', self.csv_issue)
            return
        with self.lock:
            self.passwords, self.csv_revision = values, revision
            self.csv_seen, self.csv_issue = revision, None
        self.notice('csv', '')

    def add_record(self, record):
        with self.lock:
            self.history.append(record)
            atomic_json(self.private / record['id'] / 'record.json', record, 0o600)
            self._save_locked()
        if record['status'] == 'queued':
            self.tasks.put(record['id'])

    def grade(self, job_id, runner):
        job = self.private / job_id
        work = job / 'work'
        record = next(r for r in self.snapshot()['history'] if r['id'] == job_id)
        try:
            with self.lock:
                password = self.passwords.get(record['student_id']) if not self.csv_issue else None
                csv_revision, csv_issue = self.csv_revision, self.csv_issue
            self.update(job_id, status='validating', message='檢查格式與腳本', started_at=now(), password_revision=csv_revision)
            archive = read_regular(job / 'archive.zip', MAX_ARCHIVE)
            if hashlib.sha256(archive).hexdigest() != record['sha256']:
                raise Issue('CORRUPT_ZIP', '私有 ZIP 快照雜湊不符，請重新繳交')
            del archive
            try:
                files = extract_bounded(job / 'archive.zip', work, password)
            except Issue as error:
                if error.code == 'PASSWORD_MISSING' and csv_issue:
                    raise Issue('CSV_ERROR', '助教的密碼 CSV 目前無法讀取，更新後會自動重試')
                raise
            del password
            available = set(files)
            try:
                interval = interval_value(read_regular(work / 'tb/setting.vh', 65536).decode('utf-8-sig'))
                tcl, paths = validated_tcl(read_regular(work / 'syn/syn.tcl', 256 * 1024).decode('utf-8-sig'), self.course.tcl, available)
                flist = validated_flist(read_regular(work / 'tb/rtl_sim.f', 65536).decode('utf-8-sig'), self.course.flist, available, paths)
            except UnicodeError:
                raise Issue('INVALID_ENCODING', 'syn.tcl、rtl_sim.f 與 setting.vh 請使用 UTF-8 編碼')
            order, expected = self.course.prepare(work, tcl, flist)
            (work / 'tb/setting.vh').write_text('`define INTERVAL {}\n'.format(interval), encoding='ascii')
            self.update(job_id, interval=interval, shuffle_order=order, rtl_files=paths,
                        status='simulating', message='執行洗牌後的 8 張公開測資')
            runner.run('simulation', work / 'tb', job / 'rtl.log', self.stop)
            fps = parse_simulation(read_regular(job / 'rtl.log', MAX_LOG).decode('utf-8', 'replace'), expected)
            self.update(job_id, fps=fps, status='synthesizing', message='公開測資 8/8 通過，執行合成')
            runner.run('synthesis', work / 'syn', job / 'syn.log', self.stop)
            reports = {}
            for name in ('area.rpt', 'timing.rpt', 'latch.rpt'):
                try:
                    raw = read_regular(work / 'syn' / name, MAX_LOG)
                except FileNotFoundError:
                    raise Issue('SYN_REPORT', '合成報告缺失：' + name)
                (job / name).write_bytes(raw)
                reports[name] = raw.decode('utf-8', 'replace')
            metrics = parse_synthesis(read_regular(job / 'syn.log', MAX_LOG).decode('utf-8', 'replace'),
                                      reports['area.rpt'], reports['timing.rpt'], reports['latch.rpt'])
            self.update(job_id, status='success', error_code='', message='公開測資 8/8、合成與 timing 通過', finished_at=now(), **metrics)
        except Issue as error:
            self.update(job_id, status='interrupted' if error.code == 'INTERRUPTED' else 'error',
                        error_code=error.code, message=error.message, finished_at=now())
        except Exception as error:
            (job / 'error.txt').write_text(type(error).__name__ + '\n', encoding='utf-8')
            self.update(job_id, status='error', error_code='INTERNAL',
                        message='評分程式發生非預期錯誤，請助教檢查這筆紀錄', finished_at=now())
        finally:
            # Retain any available diagnostics even if a later report was missing.
            for name in ('area.rpt', 'timing.rpt', 'latch.rpt'):
                try:
                    raw = read_regular(work / 'syn' / name, MAX_LOG)
                    (job / name).write_bytes(raw)
                except (OSError, Issue):
                    pass
            try:
                if work.exists():
                    shutil.rmtree(str(work))
                (job / 'archive.zip').unlink()
            except FileNotFoundError:
                pass
            except OSError:
                (job / 'cleanup.txt').write_text('工作檔清理失敗，請助教檢查目錄權限；原始繳交 ZIP 未變更。\n', encoding='utf-8')
                self.notice('cleanup', '部分私有工作檔未能清理，請助教查看 submissions 內的 cleanup.txt')


def snapshot_upload(path, target, expected):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or signature(info) != expected:
            raise UploadChanged()
        if info.st_size > MAX_ARCHIVE:
            raise Issue('ZIP_LIMIT', 'ZIP 超過 64 MiB 大小限制')
        digest, size = hashlib.sha256(), 0
        with target.open('xb') as out:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise Issue('ZIP_LIMIT', 'ZIP 超過 64 MiB 大小限制')
                digest.update(chunk)
                out.write(chunk)
        if signature(os.fstat(fd)) != expected or signature(path.lstat()) != expected:
            raise UploadChanged()
        return digest.hexdigest()
    finally:
        os.close(fd)


class Watcher:
    def __init__(self, board):
        self.board = board
        self.pending = {}

    def scan(self):
        board = self.board
        board.refresh_passwords()
        previous = {}
        for record in board.snapshot()['history']:
            previous[record['filename']] = record
        present = set()
        for path in sorted(board.watch_dir.iterdir()):
            if path.suffix.lower() != '.zip':
                continue
            present.add(path.name)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            version = signature(info)
            old = previous.get(path.name)
            if old and old['source_version'] == version and old['status'] != 'interrupted':
                retry = (old.get('error_code') in PASSWORD_ERRORS and not board.csv_issue
                         and old.get('password_revision') != board.csv_revision)
                if not retry:
                    continue
            pending = self.pending.get(path.name)
            if not pending or pending[0] != version:
                self.pending[path.name] = (version, time.monotonic(), now())
                continue
            if time.monotonic() - pending[1] < SETTLE_SECONDS:
                continue
            match = FILENAME.fullmatch(path.name)
            student = match[1] if match else None
            stamp = datetime.datetime.now(TAIPEI).strftime('%Y%m%d_%H%M%S_%f')
            job_id = '{}_{}_{}'.format(stamp, student or 'UNKNOWN', uuid.uuid4().hex[:12])
            job = board.private / job_id
            job.mkdir(mode=0o700)
            try:
                owner = pwd.getpwuid(info.st_uid).pw_name
            except KeyError:
                owner = str(info.st_uid)
            record = {'id': job_id, 'student_id': student, 'filename': path.name,
                      'received_at': pending[2], 'source_version': version,
                      'uploader': owner, 'source_uid': info.st_uid, 'sha256': None,
                      'status': 'queued', 'error_code': '', 'message': '等待評分',
                      'password_revision': board.csv_revision,
                      'area': None, 'interval': None, 'fps': None}
            try:
                if not student:
                    raise Issue('INVALID_FILENAME', '檔名必須是學號.zip，例如 M11000001.zip')
                if not stat.S_ISREG(info.st_mode):
                    raise Issue('UNSAFE_FILE', '繳交 ZIP 必須是一般檔案，不可為連結、目錄或特殊檔案')
                record['sha256'] = snapshot_upload(path, job / 'archive.zip', version)
            except (FileNotFoundError, UploadChanged):
                shutil.rmtree(str(job))
                self.pending.pop(path.name, None)
                continue
            except (OSError, Issue) as error:
                record.update(status='error', error_code=error.code if isinstance(error, Issue) else 'UNREADABLE_ZIP',
                              message=error.message if isinstance(error, Issue) else '無法讀取 ZIP，請檢查檔案權限',
                              finished_at=now())
                if (job / 'archive.zip').exists():
                    (job / 'archive.zip').unlink()
            board.add_record(record)
            self.pending.pop(path.name, None)
        self.pending = {k: v for k, v in self.pending.items() if k in present}

    def run(self):
        while not self.board.stop.is_set():
            try:
                self.scan()
                self.board.notice('watch', '')
            except OSError:
                self.board.notice('watch', '監看目錄目前無法讀取，程式會繼續重試')
            self.board.stop.wait(POLL_SECONDS)


def worker_loop(board, runner):
    while not board.stop.is_set():
        try:
            job_id = board.tasks.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            board.grade(job_id, runner)
        except Exception as error:
            # A diagnostics/disk error must not silently kill the only worker.
            print('評分紀錄 {} 無法完成：{}'.format(job_id, type(error).__name__), file=sys.stderr)
        finally:
            board.tasks.task_done()


CSS = """
:root{color-scheme:dark;--bg:#13161b;--panel:#1a1e25;--border:#2c323d;--text:#edf0f5;--muted:#a0a9b8;--accent:#a5e8cf}
*{box-sizing:border-box}html{scroll-behavior:auto}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif}
a{color:var(--accent);text-underline-offset:4px}a:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.wrap{max-width:1520px;margin:auto;padding:38px 40px 30px}.eyebrow{color:var(--muted);font-size:11px;letter-spacing:.19em;text-transform:uppercase}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;margin-bottom:28px}h1{font-size:32px;letter-spacing:-.04em;margin:9px 0 5px;font-weight:600}h1 span{color:var(--muted);font-weight:400}
.intro,.muted{color:var(--muted)}.intro{margin:0;font-size:13px}
.boards{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr) minmax(0,1.25fr);gap:16px;align-items:start}.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden}.featured{border-color:#486357;background:#1b2324}.card-head{padding:22px 22px 17px;border-bottom:1px solid var(--border)}.card-head h2{font-size:17px;margin:0;font-weight:600;letter-spacing:.06em}.featured .card-head h2{color:var(--accent)}
.table-scroll{max-height:540px;overflow:auto}table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}th{font-size:11px;color:var(--muted);font-weight:500;white-space:nowrap}th,td{padding:14px 18px;border-bottom:1px solid var(--border)}tbody tr:last-child td{border-bottom:0}.rank-table th:last-child,.rank-table td:last-child{text-align:right}.rank-table td:first-child{width:43px;color:var(--muted)}.rank-table tbody tr:first-child td:first-child{color:var(--accent)}.student,.number{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-variant-numeric:tabular-nums}.student{font-size:12px;white-space:nowrap}.name,.number{white-space:nowrap}.sub{display:block;color:var(--muted);font-size:10px;margin-top:3px;font-family:inherit}.score{font-size:20px;color:var(--accent)}.empty{padding:42px 22px;color:var(--muted);text-align:center;font-size:13px}
.explain{color:var(--muted);font-size:12px;margin:18px 0 36px;padding-left:2px}.history-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin-bottom:14px}.history-head h2{font-size:18px;font-weight:500;margin:0}.history-head p{font-size:12px;color:var(--muted);margin:0}.history{overflow:auto}.history table{min-width:900px}.history td{vertical-align:top}.history td:last-child{max-width:530px;min-width:250px;overflow-wrap:anywhere}.when{white-space:nowrap;font-size:12px;color:var(--muted)}.badge{display:inline-block;font-size:11px;padding:3px 8px;border:1px solid #48505e;border-radius:5px;white-space:nowrap;color:#c4ccd8}.badge.success{color:var(--accent);border-color:#436655;background:#243b31}.badge.error,.badge.interrupted{color:#f0aaa8;border-color:#694442;background:#3b2728}.badge.simulating,.badge.synthesizing,.badge.validating{color:#e6cc94;border-color:#665a40}.message{font-size:12px}
.notice{padding:14px 18px;border:1px solid #756346;background:#302a21;border-radius:8px;color:#e9d4aa;margin:0 0 24px;font-size:13px}.footer{display:flex;justify-content:space-between;gap:18px;margin-top:24px;color:var(--muted);font-size:11px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
@media(min-width:1100px){.rank-table th,.rank-table td{padding-left:10px;padding-right:10px}}
@media(max-width:1100px){.wrap{padding:26px 22px}.boards{grid-template-columns:1fr;gap:16px}.card-head{padding:17px 13px}.rank-table th,.rank-table td{padding:12px 9px}}
@media(max-width:760px){.wrap{padding:24px 16px}header{display:block}h1{font-size:28px}.boards{grid-template-columns:1fr;gap:16px}.card-head{padding:19px}.rank-table th,.rank-table td{padding:14px 8px}.student,.name{font-size:12px}.table-scroll{max-height:380px}.history-head,.footer{display:block}.history-head p{margin-top:4px}.footer span{display:block;margin:5px 0}}
"""


def escaped(value):
    return html.escape(str(value), quote=True)


def formatted(value, decimals=0):
    return '—' if value is None else ('{:,.%df}' % decimals).format(value)


def rank_card(kind, records):
    labels = {'overall': ('OVERALL', ('面積名次', '速度名次', '名次總和')),
              'area': ('AREA', ('面積 · μm²',)),
              'speed': ('INTERVAL', ('INTERVAL · cycles', 'FPS'))}
    title, headings = labels[kind]
    key = {'area': 'area_rank', 'overall': 'overall_rank', 'speed': 'speed_rank'}[kind]
    rows = []
    for record in sorted(records, key=lambda r: (r[key], r['student_id'])):
        student = escaped(record['student_id'])
        name = escaped(STUDENT_NAMES.get(record['student_id'], '—'))
        if kind == 'area':
            values = [formatted(record['area'], 3)]
        elif kind == 'speed':
            values = [formatted(record['interval']), formatted(record['fps'], 3)]
        else:
            values = [formatted(record['area_rank']), formatted(record['speed_rank']),
                      '<span class="score">{}</span>'.format(record['rank_sum'])]
        rows.append('<tr><td class="number">{}</td><td class="student">{}</td><td class="name">{}</td>'.format(record[key], student, name)
                    + ''.join('<td class="number">' + value + '</td>' for value in values) + '</tr>')
    body = ('<div class="table-scroll" data-scroll="' + kind + '"><table class="rank-table"><caption class="sr-only">' + title
            + '</caption><thead><tr><th>排名</th><th>學號</th><th>名稱</th>'
            + ''.join('<th>' + heading + '</th>' for heading in headings)
            + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>') if rows else '<div class="empty">等待第一筆通過的成績</div>'
    return ('<section class="card ' + ('featured' if kind == 'overall' else '') + '"><div class="card-head"><h2>'
            + title + '</h2></div>' + body + '</section>')


def render_content(data):
    history_rows = []
    for record in reversed(data['history'][-100:]):
        received = record['received_at']
        when = received[5:10] + ' ' + received[11:19]
        state = record['status']
        message = escaped(record.get('message', ''))
        if record.get('error_code') in PASSWORD_ERRORS:
            message += ' <a href="' + FORM_URL + '" target="_blank" rel="noopener noreferrer">確認密碼表單 ↗</a>'
        history_rows.append('<tr><td class="when" title="{}">{}</td><td class="student">{}<span class="sub">{}</span></td>'
                            '<td><span class="badge {}">{}</span></td><td class="number">{}</td><td class="number">{}</td>'
                            '<td class="message" title="{}">{}</td></tr>'.format(
                                escaped(received), escaped(when), escaped(record.get('student_id') or record['filename']),
                                escaped(record.get('uploader', '')), escaped(state), escaped(STATUS_TEXT.get(state, state)),
                                formatted(record.get('area'), 3), formatted(record.get('interval')), escaped(record['id']), message))
    history = ('<table><caption class="sr-only">最新 100 筆繳交紀錄</caption><thead><tr><th>收件時間</th><th>學號 / 檔案擁有者</th>'
               '<th>狀態</th><th>面積 · μm²</th><th>INTERVAL · cycles</th><th>說明</th></tr></thead><tbody>' + ''.join(history_rows) + '</tbody></table>') if history_rows else '<div class="empty">尚無繳交紀錄。檔案穩定後將自動開始檢查。</div>'
    notice = '<div class="notice" role="status">' + escaped(data['notice']) + '</div>' if data.get('notice') else ''
    return ('<header><div><div class="eyebrow">NTUST / Advanced Digital IC Design</div><h1>HW4 <span>／</span> 即時排行榜</h1>'
            '<p class="intro">BloodMNIST CNN 硬體實作 · 公開測資排行榜</p></div></header>' + notice
            + '<div class="boards">'
            + ''.join(rank_card(k, data['rankings']) for k in ('overall', 'area', 'speed')) + '</div>'
            '<p class="explain">總排名依面積名次與速度名次之和計算，同分共用名次（1、1、3）。每位同學採最近一次成功成績，需公開測資 8/8 與合成皆通過才入榜。</p>'
            '<div class="history-head"><h2>繳交紀錄</h2><p>最新 100 筆 · 失敗繳交不覆蓋上一筆成功成績</p></div>'
            '<section class="card history" id="history" data-scroll="history">' + history + '</section><footer class="footer">'
            '<span>此排行不包含隱藏測資，非官方最終成績。</span></footer>')


def render_page(data):
    return ('<!doctype html><html lang="zh-Hant" data-revision="' + escaped(data.get('revision', ''))
            + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>HW4 即時排行榜 · NTUST</title><style>' + CSS + '</style><script src="/live.js" defer></script></head>'
            '<body><noscript><p class="notice">啟用 JavaScript 即可即時更新；目前可手動重新整理查看最新資料。</p></noscript>'
            '<main class="wrap">' + render_content(data) + '</main></body></html>')


LIVE_JS = """'use strict';
(() => {
  const source = new EventSource('/events?since=' + encodeURIComponent(document.documentElement.dataset.revision));
  source.addEventListener('update', event => {
    const data = JSON.parse(event.data);
    const main = document.querySelector('main');
    const scroll = Array.from(main.querySelectorAll('[data-scroll]'), node => ({
      key: node.dataset.scroll, left: node.scrollLeft, top: node.scrollTop
    }));
    const x = window.scrollX, y = window.scrollY;
    main.innerHTML = data.html;
    document.documentElement.dataset.revision = data.revision;
    scroll.forEach(position => {
      const node = main.querySelector('[data-scroll="' + position.key + '"]');
      if (node) { node.scrollLeft = position.left; node.scrollTop = position.top; }
    });
    window.scrollTo(x, y);
  });
})();
"""


class LeaderboardServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(board, port=8000):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, head=False):
            try:
                url = urlsplit(self.path)
                path = url.path
            except ValueError:
                path = ''
            if path == '/events':
                if head:
                    self.send_bytes(200, b'', 'text/event-stream; charset=utf-8', head=True)
                else:
                    since = parse_qs(url.query).get('since', [''])[0]
                    self.send_events(self.headers.get('Last-Event-ID') or since)
                return
            if path == '/live.js':
                self.send_bytes(200, LIVE_JS.encode('utf-8'), 'text/javascript; charset=utf-8', head)
                return
            if path not in ('/', '/results.json'):
                self.send_bytes(404, b'Not found\n', 'text/plain; charset=utf-8', head)
                return
            data = board.snapshot()
            if path == '/':
                body = render_page(data).encode('utf-8')
                kind = 'text/html; charset=utf-8'
            else:
                body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
                kind = 'application/json; charset=utf-8'
            self.send_bytes(200, body, kind, head)

        def send_bytes(self, status, body, kind, head=False):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.security_headers()
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def security_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

        def send_events(self, revision):
            self.connection.settimeout(10)
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Accel-Buffering', 'no')
                self.security_headers()
                self.end_headers()
                self.wfile.write(b'retry: 2000\n: connected\n\n')
                self.wfile.flush()
                while not board.stop.is_set():
                    data = board.wait_for_update(revision)
                    if board.stop.is_set():
                        break
                    if data is None:
                        self.wfile.write(b': keepalive\n\n')
                    else:
                        revision = data['revision']
                        message = json.dumps({'revision': revision, 'html': render_content(data)}, ensure_ascii=False)
                        self.wfile.write(('id: ' + revision + '\nevent: update\ndata: ' + message + '\n\n').encode('utf-8'))
                    self.wfile.flush()
            except OSError:
                pass  # Disconnects and slow readers release their handler thread.

        def do_GET(self):
            self.respond()

        def do_HEAD(self):
            self.respond(head=True)

        def do_POST(self):
            self.send_bytes(405, b'Uploads are not supported.\n', 'text/plain; charset=utf-8')

        def log_message(self, format_string, *args):
            pass
    return LeaderboardServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='HW4 暗色排行榜：監看 ZIP、評分、HTTP 網頁，零額外 Python 套件')
    parser.add_argument('--watch-dir', required=True, type=Path, help='助教指定的 ZIP 繳交目錄（僅監看此層）')
    parser.add_argument('--password-csv', required=True, type=Path, help='含「學號」「密碼」欄位、權限 600 的 CSV')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent, help='成績 JSON、鎖定檔及私有評分資料目錄，預設與本程式同目錄')
    parser.add_argument('--port', type=int, default=8000, help='HTTP port，預設 8000，綁定 127.0.0.1')
    parser.add_argument('--course-dir', type=Path, default=Path('/home/Project4'), help='助教核准的官方資源目錄，預設 /home/Project4')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('port 必須介於 1～65535')
    board = None
    server = None
    threads = []

    def terminate(signum, frame):
        raise KeyboardInterrupt()

    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        os.umask(0o077)
        board = Board(args.watch_dir, args.password_csv, args.data_dir, args.course_dir)
        server = make_server(board, args.port)
        threads = [threading.Thread(target=Watcher(board).run, name='watcher', daemon=True),
                   threading.Thread(target=worker_loop, args=(board, ToolRunner()), name='grader', daemon=True)]
        for thread in threads:
            thread.start()
        print('Leaderboard: http://127.0.0.1:{}/'.format(server.server_port), flush=True)
        print('Course SHA256: ' + board.course.digest, flush=True)
        print('評分完成會清理工作目錄；原始繳交 ZIP 保留。Ctrl+C 停止。', flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    except (Issue, OSError) as error:
        print('啟動失敗：' + (error.message if isinstance(error, Issue) else str(error)), file=sys.stderr)
        return 2
    finally:
        if board is not None:
            board.stop.set()
        if server is not None:
            server.server_close()
        for thread in threads:
            thread.join(timeout=65)
        if board is not None:
            board.close()
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == '_extract':
        extract_child()
    else:
        sys.exit(main())
