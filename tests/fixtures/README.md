# ZIP 測試樣本

全部只含假 RTL、設定及測試腳本，密碼均為 `test-password`，不是學生繳交檔案。

- `encrypted.zip`：既有的傳統 ZipCrypto 樣本。
- `aes128.zip`、`aes192.zip`、`aes256.zip`：以 `test_leaderboard.valid_files()` 的四份檔案產生，使用 7-Zip 的 `-tzip -mem=AES128`／`AES192`／`AES256` 與 `-ptest-password`。包含 stored 與 deflate 項目，AES 版本為 AE-2。
- `aes256-ae1.zip`：同一份 AES-256 樣本，依 [WinZip 規格](https://www.winzip.com/en/support/aes-encryption/) 把 local／central headers 的 AES 版本改為 AE-1 並填入原文 CRC，已以 `7z t` 驗證。

7-Zip 只用於建立及獨立驗證樣本，執行測試不需要安裝它。錯誤密碼、驗證碼損壞、CRC 錯誤、危險路徑及大小限制等案例由測試對這些固定樣本建立暫存變體；測試不修改原始樣本。
