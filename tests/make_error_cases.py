#!/usr/bin/env python3
"""Build intentional failures for a PRIVATE leaderboard, without running EDA tools.

Python 3.6+, standard library only. Output must be a new directory. Never submit
these fixtures to a class submission folder. manifest.json lists expected codes;
passwords.csv contains dummy credentials for two encrypted test submissions.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import stat
import struct
import zipfile


def zipped(files, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


def central_field(raw, offset, fmt, value):
    raw = bytearray(raw)
    struct.pack_into(fmt, raw, raw.index(b'PK\x01\x02') + offset, value)
    return bytes(raw)


def make_cases(baseline, encrypted, output):
    with zipfile.ZipFile(str(baseline)) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist() if not item.is_dir()}
    required = ('RTL_code/cnn_top.v', 'syn/syn.tcl', 'tb/rtl_sim.f', 'tb/setting.vh')
    if set(files) != set(required):
        raise ValueError('Use the four-file baseline from the HW4 repo tools/package_submission.py')
    tcl = files['syn/syn.tcl'].decode('utf-8-sig')
    flist = files['tb/rtl_sim.f'].decode('utf-8-sig')
    if '../RTL_code/cnn_top.v' not in tcl or 'set CLK_PERIOD 4.0' not in tcl:
        raise ValueError('Unexpected baseline Tcl')
    output.mkdir(mode=0o700)
    incoming = output / 'incoming'
    incoming.mkdir(mode=0o700)
    manifest, passwords = [], []

    def add(description, code, changes=None, raw=None, filename=None, password=None):
        student = 'M1199{:04d}'.format(len(manifest) + 1)
        filename = filename or student + '.zip'
        if raw is None:
            variant = dict(files)
            for name, content in (changes or {}).items():
                if content is None:
                    variant.pop(name)
                else:
                    variant[name] = content
            raw = zipped(variant)
        path = incoming / filename
        path.write_bytes(raw)
        path.chmod(0o600)
        manifest.append({'filename': filename, 'student_id': student,
                         'case': description, 'expected': code})
        if password is not None:
            passwords.append((student, password))

    add('檔名不符合學號格式', 'INVALID_FILENAME', filename='invalid-name.zip')
    for name in required:
        add('缺少 ' + name, 'MISSING_FILES', {name: None})
    add('多包一層學號目錄', 'INVALID_FORMAT', raw=zipped({'M11990000/' + k: v for k, v in files.items()}))
    add('夾帶 testbench', 'INVALID_FORMAT', {'tb/tb_simple.v': 'module fake; endmodule'})
    add('夾帶 SRAM 模型', 'INVALID_FORMAT', {'RTL_code/sram.v': 'module fake; endmodule'})
    add('ZIP 損毀', 'DECRYPT_FAILED', raw=b'This is not a ZIP archive.\n')
    raw = zipped(files)
    add('只有 AES 方法標記、缺少加密資訊', 'UNSUPPORTED_ENCRYPTION', raw=central_field(raw, 10, '<H', 99))
    add('不支援的壓縮方法', 'UNSUPPORTED_COMPRESSION', raw=central_field(raw, 10, '<H', 98))
    for name in ('../outside.v', '/outside.v', 'RTL_code\\outside.v'):
        add('危險路徑 ' + name, 'UNSAFE_ZIP', {name: 'module fake; endmodule'})
    link = zipfile.ZipInfo('RTL_code/link.v')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    add('ZIP 內符號連結', 'UNSAFE_ZIP', raw=zipped(files, (link, '/etc/passwd')))
    add('大小寫路徑衝突', 'UNSAFE_ZIP', {'RTL_code/CNN_TOP.v': 'module fake; endmodule'})
    add('宣告單檔超過 32 MiB', 'ZIP_LIMIT', raw=central_field(raw, 24, '<I', 32 * 1024 ** 2 + 1))
    add('壓縮倍率超過上限', 'ZIP_LIMIT', {'RTL_code/zeros.v': b'0' * (1024 ** 2)})
    add('空 ZIP', 'ZIP_LIMIT', raw=zipped({}))
    add('ZIP 項目數超過上限', 'ZIP_LIMIT', raw=zipped({'RTL_code/d{}/'.format(i): '' for i in range(2049)}))
    add('INTERVAL 小於 784', 'INVALID_INTERVAL', {'tb/setting.vh': '`define INTERVAL 783\n'})
    add('INTERVAL 造成整數溢位', 'INVALID_INTERVAL', {'tb/setting.vh': '`define INTERVAL 214748365\n'})
    add('setting.vh 夾帶 include', 'INVALID_INTERVAL', {'tb/setting.vh': '`define INTERVAL 784\n`include "extra.v"\n'})
    add('更改合成時脈約束', 'SYN_CHANGED', {'syn/syn.tcl': tcl.replace('set CLK_PERIOD 4.0', 'set CLK_PERIOD 10.0')})
    add('RTL_FILES 夾帶 Tcl 命令', 'SYN_CHANGED', {'syn/syn.tcl': tcl.replace('../RTL_code/cnn_top.v', '[puts INJECTION_TEST]')})
    add('syn.tcl 引用不存在的 RTL', 'MISSING_RTL', {'syn/syn.tcl': tcl.replace('cnn_top.v', 'missing.v')})
    add('RTL_FILES 路徑穿越', 'INVALID_RTL_PATH', {'syn/syn.tcl': tcl.replace('../RTL_code/cnn_top.v', '../RTL_code/../cnn_top.v')})
    add('rtl_sim.f 夾帶編譯選項', 'FLIST_CHANGED', {'tb/rtl_sim.f': flist + '\n+define+INJECTION_TEST\n'})
    add('模擬與合成 RTL 清單不一致', 'FLIST_CHANGED', {'tb/rtl_sim.f': flist + '\n../RTL_code/extra.v\n', 'RTL_code/extra.v': 'module extra; endmodule'})
    add('syn.tcl 非 UTF-8', 'INVALID_ENCODING', {'syn/syn.tcl': b'\xff'})
    encrypted_raw = encrypted.read_bytes()
    add('加密 ZIP 缺少密碼', 'PASSWORD_MISSING', raw=encrypted_raw)
    add('加密 ZIP 密碼錯誤', 'DECRYPT_FAILED', raw=encrypted_raw, password='intentionally-wrong')
    # This real ZipCrypto fixture decrypts correctly, then fails the course Tcl
    # baseline check. It proves decryption is independent of script validation.
    add('密碼正確但 Tcl 不符合課程版本', 'SYN_CHANGED', raw=encrypted_raw, password='test-password')
    add('Verilog 語法錯誤', 'TOOL_FAILED', {'RTL_code/cnn_top.v': 'module cnn_top( THIS IS INVALID VERILOG\n', 'tb/setting.vh': '`define INTERVAL 1000\n'})
    wrong_dut = '''module cnn_top(input clk, rst_n, w_valid, img_valid,
    input [23:0] in_data, output reg out_valid, output [2:0] class_out);
reg [9:0] pixels;
assign class_out = 3'd0;
always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin pixels <= 0; out_valid <= 0; end
    else begin
        out_valid <= 0;
        if (img_valid) begin
            if (pixels == 783) begin pixels <= 0; out_valid <= 1; end
            else pixels <= pixels + 1'b1;
        end
    end
end
endmodule
'''
    add('分類結果錯誤', 'SIM_FAILED', {'RTL_code/cnn_top.v': wrong_dut, 'tb/setting.vh': '`define INTERVAL 1000\n'})
    add('沒有輸出而觸發 testbench timeout', 'SIM_FAILED', {'RTL_code/cnn_top.v': wrong_dut.replace('out_valid <= 1;', 'out_valid <= 0;'), 'tb/setting.vh': '`define INTERVAL 1000\n'})
    with (output / 'passwords.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['學號', '密碼'])
        writer.writerows(passwords)
    (output / 'passwords.csv').chmod(0o600)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('output', type=Path, help='new, private fixture directory')
    parser.add_argument('--encrypted-fixture', type=Path, default=Path(__file__).parent / 'fixtures/encrypted.zip')
    args = parser.parse_args()
    cases = make_cases(args.baseline, args.encrypted_fixture, args.output)
    print('Created {} intentional failures in {}'.format(len(cases), args.output))


if __name__ == '__main__':
    main()
