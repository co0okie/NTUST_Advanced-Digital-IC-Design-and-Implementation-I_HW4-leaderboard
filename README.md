# HW4 即時排行榜

單一 Python 程式，包含目錄監看、ZIP 驗證、VCS/DC 評分、JSON 紀錄及暗色網頁。
Python **3.6.8 以上**，僅標準函式庫，不用安裝套件。HTML 與少量 JavaScript 都由同一個 `.py` 提供。

## 啟動

```sh
chmod 600 /path/to/passwords.csv
python3 leaderboard.py --watch-dir /path/to/submissions --password-csv /path/to/passwords.csv
```

在 **server 的瀏覽器**開啟 <http://127.0.0.1:8000/>。`localhost` 是瀏覽器所在的機器；
本機瀏覽器若要連 server，需要適當的轉送。繳交目錄每 2 秒掃描；網頁採 SSE 推送，不定時重新整理。

程式成功寫入 `results.json` 後通知已連線的瀏覽器；瀏覽器只更新畫面內容並保留捲動位置。
資料沒有改變就不更新。連線每 15 秒送一次很小的保活註解，不讀取 JSON、不重繪頁面；
連線中斷時瀏覽器會自動重連，並取得最新版本。關閉 JavaScript 時仍可查看初始畫面及手動重新整理。
使用瀏覽器內建的 [Server-Sent Events / EventSource](https://html.spec.whatwg.org/multipage/server-sent-events.html)，不需要 WebSocket 套件。

| 參數 | 用途／預設值 |
| --- | --- |
| `--watch-dir` | 必填；助教指定的繳交目錄，只掃描這一層的 ZIP |
| `--password-csv` | 必填；助教的密碼 CSV |
| `--json-dir` | 成績與診斷資料；預設與 `.py` 同目錄 |
| `--port` | 預設 `8000`，僅監聽 `127.0.0.1` |
| `--course-dir` | 官方資源目錄；預設 `/home/Project4` |

完整範例：

```sh
python3 leaderboard.py \
  --watch-dir /path/to/submissions \
  --password-csv /path/to/passwords.csv \
  --json-dir /path/to/leaderboard-data \
  --course-dir /path/to/approved-course-files \
  --port 8000
```

JSON 目錄必須由執行帳號擁有，且不可讓群組／其他帳號寫入。程式會建立它。
按 Ctrl+C 或傳送 SIGTERM 停止；下次啟動會恢復尚有私有 ZIP 快照的未完成工作。

## 正式執行的資料與 log

`browser.log` 是開發時啟動瀏覽器的重導向輸出，正式使用不需要。
程式本身輸出到終端機，不會自動產生 `service.log`。需要背景執行並保存輸出時，可使用：

```sh
mkdir -p runtime/logs
nohup python3 leaderboard.py \
  --watch-dir /path/to/submissions \
  --password-csv /path/to/passwords.csv \
  --json-dir runtime/data \
  > runtime/logs/service.log 2>&1 &
```

```text
runtime/
  logs/service.log
  data/
    results.json
    results.lock
    submissions/
```

`results.lock` 透過作業系統的 `flock` 防止兩個程序同時修改同一個 JSON 目錄。
關閉程式或程序結束時鎖定自動釋放；空的 lock 檔會保留，檔案存在不代表服務仍在執行。
若省略 `--json-dir`，為維持原有介面，相同資料仍預設放在 `.py` 旁。

## 官方資源與評分環境

`--course-dir` 必須包含：

```text
syn.tcl
.synopsys_dc.setup
rtl_sim.f
tb_simple.v
export/
  tb/images.hex
  tb/pred.hex
  weights/conv1_w.hex、conv1_b.hex、conv2_w.hex、conv2_b.hex
  weights/fc1_w.hex、fc1_b.hex、fc2_w.hex、fc2_b.hex
```

這些是**助教核准的基準**，應放在學生不能修改的位置。不能拿學生 ZIP 當基準。
repo 已將原始資料包解開到 `tb/`，並保存課程 server 的 Tcl、DC setup 和 `.f` 於 `syn/`、`tb/`。
排行榜正式使用的基準仍須由助教確認；開發帳號的私人測試副本不代表已核准正式部署。

程式啟動時把資源讀進記憶體並計算 SHA256；每次工作使用相同內容。
若新的基準與既有成績版本不同，拒絕混用，請選新的 `--json-dir`。
官方 `.f` 中引用的 TSMC SRAM 模型，以及 DC 的標準元件／SRAM library，仍須在 server 可用。

評分以 `tcsh -c` 載入課程工具環境，分別呼叫：

```text
vcs -full64 -R -top tb_simple -f rtl_sim.f
dc_shell -f syn.tcl
```

本機沒有 VCS/DC 時不能產生真實成績。單元測試有**僅供測試的替代工具**，正式 CLI 沒有假成績模式。
官方基準的時序約束原樣保留，包括目前 server 的 4 ns 設定；不自動改為 testbench 的 10 ns。

開發時 server 上這份腳本實際產生 **0 次 Error**，與 PDF 的 2 次規定不同。
依本專案確認的規則，**不以 Error 次數判斷失敗**，次數只記在 `synthesis_error_count` 供診斷。
仍要求工具正常結束、有效報告、SRAM、零 latch 與正 slack；明確的 `ERROR:`／`Fatal:` 訊息也會阻止入榜。

## CSV 與 ZIP

CSV 使用 UTF-8，可有 BOM；以欄位名稱讀取，其他 Google 表單欄位可保留：

```csv
時間戳記,電子郵件地址,學號,密碼
2026/10/16 00:10:00,student@example.com,M11000001,example-password
```

- 密碼 CSV 必須由執行帳號擁有且權限為 `600`。不放進公開 JSON 或 log。
- 學號去除前後空白並轉大寫；密碼的空白保留。同學號以 CSV 最後一筆為準。
- 更新 CSV 後，自動重試最新一筆因密碼缺失／解密失敗而失敗的 ZIP。
- 未加密 ZIP 不要求出現在密碼 CSV。傳統 ZipCrypto 可用；**AES ZIP 不支援**，也不自動安裝套件。
- 解密錯誤附表單填寫連結；表單更新後仍須助教把新的 CSV 放到指定位置。

學號接受 B、D 或 M 開頭，後接 8 個大寫英文字母或數字（包括名單中的 `M11507W01`）；
密碼 CSV 與內部紀錄使用同一格式。

ZIP 檔名如 `M11000001.zip`，根目錄必須是作業的**雲端繳交格式**：

```text
RTL_code/
  cnn_top.v
  其他自寫 .v（可放子目錄）
syn/
  syn.tcl
tb/
  rtl_sim.f
  setting.vh
```

不能再多包一層學號資料夾；不能使用 Moodle 的簡化格式，或附上助教 testbench、SRAM 模型。
路徑只接受英文字母、數字、底線、連字號、句點及目錄分隔符，避免編譯器／Tcl 路徑歧義。

`syn.tcl` 除 `set RTL_FILES ...` 的純文字 `.v` 清單外，須與基準完全相同（CRLF 換行可接受）。
允許 `[list ...]`、大括號或引號形式；相對或絕對 RTL 路徑都會對應回 ZIP 中的 `RTL_code/...`。
**驗證學生腳本後，只調整允許的路徑清單，再驗證並執行同一份私有副本**；不是任意執行上傳的 Tcl。
`.f` 的固定項目／選項必須保留，RTL 清單須與 Tcl 一致，實際模擬清單由程式重建。

`setting.vh` 只允許註解與一個十進位 `INTERVAL` 定義，範圍為 784～214748364；上限避免
官方 testbench 的 `10 * INTERVAL` 超出 32-bit signed integer。

## 排名與紀錄

1. 等 ZIP 的大小／時間戳穩定 6 秒，建立私有快照再驗證。這是穩定性判斷；上傳若再次變動會重新處理。
2. 官方 8 張圖片與答案成對洗牌；要求逐張正確、一次完整 `8 / 8 PASS`，並有合理的 FPS 統計。
3. 模擬通過才合成。要求正常結束、完整報告、零 latch、正 slack，以及有效 SRAM macro；`Error` 次數僅供診斷。
4. 面積使用官方 Tcl 印出的 leaf cell 總面積（標準元件＋SRAM）；另保存 `area.rpt` 的數字供核對。
5. 每人採最近一次**成功**成績；面積與 INTERVAL 各自採競賽排名（1、1、3），名次相加後再排名。

JSON 記錄 server 觀察到的收件時間（UTC+8）、檔案擁有者、ZIP 雜湊、洗牌順序、狀態、錯誤與成績。
學號來自檔名，**不等同驗證過的身分**；檔案擁有者另列出供助教追查。
公開測資通過不代表已通過隱藏測資，也不能證明沒有硬編碼；網站不是官方最終成績。

頁面只顯示 AREA、OVERALL、INTERVAL 三榜與繳交紀錄；收件時間採台北時間。
三榜均列出學號與名稱。48 人姓名對照直接內建在 `leaderboard.py` 的 `STUDENT_NAMES`，
來源為 `name2id2user`；執行時不需另外帶這個檔案，查不到名稱顯示「—」。
等待與處理狀態列在各筆紀錄中，錯誤只顯示說明；診斷代碼仍保留在 JSON。
已用 `M11507401` 重新完成 server 的 VCS 洗牌 8/8 與 DC 合成，確認三榜顯示名稱，
服務重啟後仍保留成績，工作檔清除且診斷報告保留。

```text
JSON 目錄/
  results.json                       # 原子更新，644；全部紀錄與排名
  results.lock                       # 防止同目錄重複啟動
  submissions/                       # 700，HTTP 不提供這個目錄
    20261016_013200_123456_M11000001_a1b2c3d4e5f6/
      record.json                    # 600，該筆狀態、雜湊與洗牌順序
      rtl.log、syn.log
      area.rpt、timing.rpt、latch.rpt  # 有產生的報告都保留
```

每筆評分結束（成功／失敗都一樣）清除私有 ZIP 快照、解壓內容及 VCS/DC 中間檔，
**原始繳交 ZIP 不會刪除**。若中途失敗，保留已產生的診斷檔，不會假裝有尚未產生的報告。
歷史報告長期保留；若原始 ZIP 後來被覆蓋，只有雜湊不能還原舊版程式，需原檔才能重現。

預設限制：ZIP 64 MiB、單檔 32 MiB、展開 256 MiB、2048 項、壓縮倍率 1000、解壓 60 秒；
模擬 15 分鐘、合成 30 分鐘、工具 log 16 MiB、工作檔約 2 GiB。
工具執行中的磁碟／log 限額是週期性檢查，並非硬式檔案系統 quota。
細部常數集中在程式開頭。缺工具／授權、檔案損毀、逾時、測資或合成失敗都不入榜。

依確認的部署方式，學生 HDL **直接以執行帳號身分執行，沒有 sandbox**。
安全解壓、固定指令與 Tcl 比對不會隔離 HDL 的系統呼叫；程式不宣稱能防止蓄意惡意 HDL。

## 本機測試

從 repo 根目錄執行：

```sh
python3 -m unittest discover -s leaderboard/tests -v
```

測試使用 `tb/` 內的原始資料，官方 Tcl／工具輸出以測試 fixture 代替，不會呼叫課程 server 或改動原測資。
`tests/fixtures/encrypted.zip` 為合成測試資料，密碼是 `test-password`，用來實測傳統 ZIP 加密。
已在課程 server 使用初版 CNN 驗證真實 VCS、TSMC SRAM model、DC、報告解析、排名與缺檔錯誤流程。
排行榜測試也已在 Python 3.6.8 通過，包含 SSE 多人連線、斷線補資料及私有檔案存取檢查；完整 RTL 驗證結果見 [專案說明](../README.md)。

另可產生 36 筆刻意錯誤的繳交，供自己的測試目錄驗證完整收件流程：

```sh
python3 leaderboard/tests/make_error_cases.py dist/M11000001.zip build/leaderboard-error-cases
```

輸出目錄必須尚未存在；`incoming/` 是測試 ZIP，`manifest.json` 列出預期錯誤，
`passwords.csv` 是兩筆假密碼（權限 600）。涵蓋缺檔、格式、損毀、加密、路徑、解壓限制、
腳本修改、RTL 清單、編碼、編譯失敗、分類錯誤及 testbench 逾時。
**僅用於私人測試目錄，勿交到全班的正式收件目錄。** 這個工具不會自動提交或執行 VCS/DC。

2026-10-16 已在課程 server 的私人排行榜實際提交這 36 筆 ZIP，另測試兩筆以符號連結／目錄
冒充 ZIP 的情境；修正分類後，38/38 的錯誤碼符合預期。語法錯誤、分類錯誤與無輸出案例
使用真正 VCS 執行；等待／處理狀態透過 SSE 正常送出。失敗不影響原成功排名，工作檔及 ZIP
快照皆清除，原始繳交檔與可用診斷紀錄保留。

這批測試找出工具分類問題：登入環境的無關 `INNOVUS` 目錄缺失警告會遮蔽後續語法錯誤。
現在只針對評分工具不存在或授權失敗判定環境問題；一般編譯失敗保持 `TOOL_FAILED`。
回歸測試已涵蓋這種 log，以及真正的工具缺失和授權失敗。
46 項本機測試在 Python 3.6.8 與 3.14 通過，包含名單學號的 CSV、收件與重啟載入；
合成失敗判斷與工具逾時另由本機測試驗證，
未在課程 server 刻意製造授權故障或長時間逾時。
