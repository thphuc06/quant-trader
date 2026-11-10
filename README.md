# quant-trader

Bộ khung nghiên cứu quant + giao dịch có kiểm soát cho **Claude Code**, trên **Binance Spot** (long/flat).
Claude đóng vai trò analyst: đọc dữ liệu qua MCP, lập luận trên bằng chứng, đề xuất *trade intent*.
Mọi con số (indicator, sizing, risk) do code Python đã test tính; mọi lệnh phải qua risk engine và được người duyệt.

> **Trạng thái: checkpoint đang phát triển (2026-11-11).** Mặc định `PAPER`, live trading bị khóa.
> Chưa có chiến lược nào vượt qua ngưỡng kiểm định — kết quả trung thực hiện tại của hệ thống là `NO_TRADE` / `WATCH`.
> Đây không phải lời khuyên đầu tư và không có gì ở đây chứng minh khả năng sinh lời.

## Đã có gì

| Phần | Trạng thái |
|---|---|
| Research & kiến trúc ([research/](research/), [architecture/](architecture/), 5 ADR, threat model) | Xong |
| Core: config an toàn mặc định, domain model (Decimal), SQLite + journal append-only có hash chain, Parquet candles | Xong, có test |
| Binance: REST public + signed (tập endpoint cố định, không retry mù lệnh), filters, kline stream, stop `STOP_LOSS`, thoát lệnh bằng `cancelReplace` | Market data đã chạy thật; **order adapter Demo/Live mới chỉ test với HTTP giả lập** (chưa có demo key) |
| Quant: features nhân quả, regime, 2 baseline strategy, backtester, PSR/DSR/PBO, experiment registry, holdout khóa | Xong, có test |
| ML: triple-barrier, purged walk-forward, calibration | Xong; model **không** dùng cho quyết định |
| Risk engine + execution: sizing phía server, intent → duyệt → re-validate → order → reconcile, kill switch, khóa live | Xong, có test (paper chạy end-to-end với giá thật) |
| Bảo vệ vị thế: mỗi vị thế có trạng thái `PROTECTED/UNPROTECTED/UNKNOWN`; mất stop → tự bật kill switch, chặn lệnh vào mới, `protect_position` để đặt lại | Xong, test với broker giả lập (stop bị reject, unknown, khớp từng phần, race khi thoát lệnh) |
| News/web: provider Tavily/Brave (tùy chọn), fetch chống SSRF, lưu point-in-time | Xong, có test |
| Forward shadow log: `trader shadow-log` ghi mỗi nến 1h đã đóng (features, regime, target của baseline, data quality, commit), bảng append-only, nến thiếu ghi là `missing`; tool chỉ đọc `get_shadow_log` | Xong, có test; **cần operator đăng ký lịch chạy mỗi giờ** ([docs §9](docs/operating-the-trading-desk.md)) |
| MCP server `trading`: 30 tools, 8 resources, 4 prompts; khởi động qua `scripts/run_mcp.py` (tự tìm `.venv`) | Xong, có test (kể cả test spawn server qua stdio thật) |
| Knowledge base ([knowledge/](knowledge/)) | Bộ tài liệu khởi đầu, đều ở trạng thái `candidate` |
| Claude Code harness | `CLAUDE.md`; 4 skills kèm template/reference; 2 subagents chỉ đọc; 3 hook (`SessionStart`, `PreToolUse`, `PostToolUse`); permission least privilege; `state/` để bàn giao giữa các session; [docs/operating-the-trading-desk.md](docs/operating-the-trading-desk.md) |
| Harness eval | 6 kịch bản chạy bằng session `claude -p` mới với Haiku, chấm bằng code. Baseline 5/6 → sau khi sửa skill: 5 kịch bản đạt mọi lần, S2 đạt 3/3 lần cuối. Xem [reports/harness-eval/](reports/harness-eval/) |
| Volatility-scaled exposure | Challenger cho 2 baseline, rolling walk-forward và 2× cost stress; chưa qua toàn bộ gate, không bật paper/live |
| Backtest danh mục (`quant/portfolio.py`), bootstrap lợi nhuận ngày (block 5/10/20), trial accounting cho DSR | Xong, có test (sizing khớp `risk.compute_sizing`) |
| Baseline khung ngày (protocol đăng ký trước, 1 lần chạy) | Turnover giảm ~95%, nhưng không qua gate số lệnh / CI / DSR → inconclusive, không paper |

Kiểm tra gần nhất (2026-11-11): 322 test pass, `ruff` và `mypy` sạch.

## Kết quả nghiên cứu thật (development period, sau phí)

Dữ liệu 1h Binance Spot, 5 năm, đến trước `2025-11-10` (12 tháng gần nhất là holdout, **chưa mở**).
Chi phí giả định 13 bps mỗi chiều. Chi tiết từng lần chạy: [research/experiments/](research/experiments/).

| Chiến lược | BTC | ETH | SOL | BNB |
|---|---|---|---|---|
| Buy & hold — return / max DD | +60% / −76% | −24% / −81% | −32% / −97% | +58% / −72% |
| `tsmom_trend_v1` — return / Sharpe | −14% / 0.02 | −51% / −0.31 | +84% / 0.55 | −30% / −0.04 |
| `donchian_breakout_v1` — return / Sharpe | +38% / 0.44 | −10% / 0.08 | +54% / 0.47 | +17% / 0.28 |

- **Không cấu hình nào có khoảng tin cậy 90% của Sharpe nằm trên 0** → không cái nào qua gate.
- Trend baseline thua lỗ sau phí ở 3/4 symbol; breakout giảm drawdown so với buy & hold nhưng chưa phân biệt được với nhiễu.
- PBO (BTC): họ tham số trend 0.02, họ breakout 0.45 — họ breakout có nguy cơ overfit cao khi chọn tham số.
- ML (BTC, logistic & gradient boosting): log loss và Brier **kém hơn base rate**, AUC ≈ 0.50 → không có tín hiệu.
- Dữ liệu có 1 nến 1h bị thiếu trên cả 4 symbol (được gắn cờ, không tự vá).

Giới hạn: universe chọn theo hindsight, chi phí là giả định, không mô phỏng partial fill/latency. Xem
[research/ml-and-validation.md](research/ml-and-validation.md).

## Challenger: volatility-scaled exposure

Đã chạy một challenger được xác định trước trên cùng OOS folds với hai baseline. Target exposure long/flat được scale theo realized volatility 30 ngày, chuẩn hóa bằng median volatility của train fold; signal dùng giá đóng cửa và giao dịch tại open kế tiếp. Không leverage, exposure cap 100%, phí 13 bps mỗi chiều tính trên notional đã giao dịch. Walk-forward: train 730 ngày, test 90 ngày, bước 90 ngày; chi phí stress 2×. Dữ liệu có một nến thiếu mỗi symbol, được giữ nguyên và gắn cờ, không nội suy. Holdout bắt đầu `2025-11-10` vẫn khóa.

| Symbol / baseline | Sharpe baseline → overlay | 90% CI ΔSharpe | Net return | Max DD | Turnover/năm | Return ở 2× phí | Gate đạt |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTC / TSMOM | 0.338 → 0.497 | [0.031, 0.284] | +21.5% | −22.0% | 169.9× | −21.9% | 3/6 |
| BTC / Donchian | 0.166 → 0.328 | [0.061, 0.279] | +10.4% | −27.1% | 60.4× | −5.7% | 2/6 |
| ETH / TSMOM | 0.015 → 0.279 | [0.105, 0.410] | +7.8% | −46.7% | 159.3× | −28.8% | 3/6 |
| ETH / Donchian | 0.758 → 0.982 | [0.049, 0.385] | +57.7% | −23.7% | 56.3× | +36.2% | 4/6 |
| SOL / TSMOM | 0.686 → 0.888 | [0.093, 0.308] | +96.5% | −35.2% | 153.1× | +31.9% | 4/6 |
| SOL / Donchian | 0.422 → 0.588 | [0.070, 0.253] | +39.2% | −32.9% | 63.3× | +18.0% | 3/6 |
| BNB / TSMOM | 0.290 → 0.571 | [0.111, 0.456] | +32.8% | −39.5% | 179.7× | −16.8% | 3/6 |
| BNB / Donchian | 0.974 → 1.195 | [0.044, 0.378] | +88.2% | −21.5% | 59.3× | +61.3% | 5/6 |

Sharpe point estimate tăng ở cả 8 cặp, nhưng đây là so sánh khám phá trên cùng các cửa sổ và không xóa bỏ selection bias. Chỉ 2/8 dương ở stress phí; 4/8 có dưới 100 giao dịch; DSR hiệu chỉnh theo 49 trials nằm khoảng 0.19–0.66. Turnover 56–180× notional mỗi năm cho thấy việc điều chỉnh exposure mỗi giờ đắt và nhạy với giả định phí. **Không cặp nào qua đủ 6 gate hiện hành**; kết luận là chưa đáng paper-test và không có bằng chứng alpha. Giữ trạng thái `NO_TRADE` / `WATCH`.

Tám records của batch cuối (bao gồm fold metrics và assumptions) nằm trong [research/experiments/](research/experiments/); các lần chạy lỗi và batch trước vẫn được giữ để tính đủ trial count. Lệnh tái chạy trên candles đã ingest: `python -m trader research-volatility-overlay`.

### Follow-up: vùng không giao dịch 5 điểm phần trăm

Trước khi chạy, protocol cố định band 5% equity và đặt tiêu chí giảm turnover tối thiểu 25% so với overlay không band; ngưỡng không được tune theo OOS. Xem [protocol đã đăng ký](research/protocols/volatility-rebalance-band-v1.md). Khi baseline vẫn long, backtester giữ exposure hiện tại nếu chênh target không quá 5 điểm phần trăm; signal flat luôn đóng vị thế. Đây là exploratory vì cùng OOS development đã được xem ở thử nghiệm trước.

| Symbol / baseline | Sharpe có band → không band | 90% CI ΔSharpe | Giảm turnover | Chênh net return | Gate hiện hành |
|---|---:|---:|---:|---:|---:|
| BTC / TSMOM | 0.494 → 0.497 | [−0.020, 0.017] | 0.8% | −0.1 pp | 2/6 |
| BTC / Donchian | 0.313 → 0.328 | [−0.032, 0.003] | 1.5% | −0.7 pp | 2/6 |
| ETH / TSMOM | 0.314 → 0.279 | [0.015, 0.056] | 1.4% | +2.5 pp | 3/6 |
| ETH / Donchian | 0.993 → 0.982 | [−0.009, 0.032] | 2.8% | +1.3 pp | 4/6 |
| SOL / TSMOM | 0.895 → 0.888 | [−0.009, 0.023] | 0.9% | +2.0 pp | 4/6 |
| SOL / Donchian | 0.585 → 0.588 | [−0.021, 0.015] | 1.4% | −0.3 pp | 2/6 |
| BNB / TSMOM | 0.579 → 0.571 | [−0.012, 0.028] | 1.0% | +0.9 pp | 3/6 |
| BNB / Donchian | 1.201 → 1.195 | [−0.014, 0.030] | 1.8% | +1.6 pp | 5/6 |

Kết quả: turnover chỉ giảm **0.8–2.8%**, không cặp nào đạt tiêu chí 25%; cả 8 cặp giữ net return trong phạm vi −5 pp so với overlay không band. Turnover còn khoảng 55–178×/năm, DSR 0.19–0.65; không cặp nào qua đủ gate. Band này **không giải quyết được** turnover hourly, nên dừng hướng 5% và không dò thêm ngưỡng trên cùng OOS. Giữ `NO_TRADE` / `WATCH`; không gọi đây là alpha hoặc ứng viên paper-test.

Tám experiment records của band được lưu trong [research/experiments/](research/experiments/) với IDs bắt đầu `exp_93b1885e`, `exp_051ab9ac`, `exp_8df3235a`, `exp_b43f8a52`, `exp_cf12cce4`, `exp_07fb86e6`, `exp_6c7944b5`, `exp_fd443277`. Chạy lại bằng `python -m trader research-volatility-band` sẽ ghi thêm trials.

### Follow-up: chỉ cập nhật volatility sizing mỗi ngày

Protocol tiếp theo cố định cập nhật multiplier một lần/ngày UTC; baseline vẫn vào/thoát theo tín hiệu giờ. [Protocol](research/protocols/volatility-daily-rebalance-v1.md). Vì development OOS đã được xem, đây tiếp tục là exploratory.

| Symbol / baseline | Sharpe daily → hourly | Giảm turnover | Chênh net return | Gate hiện hành |
|---|---:|---:|---:|---:|
| BTC / TSMOM | 0.496 → 0.497 | 0.5% | +0.0 pp | 3/6 |
| BTC / Donchian | 0.326 → 0.328 | 0.9% | −0.1 pp | 2/6 |
| ETH / TSMOM | 0.314 → 0.279 | 1.0% | +2.5 pp | 3/6 |
| ETH / Donchian | 1.001 → 0.982 | 1.5% | +2.2 pp | 4/6 |
| SOL / TSMOM | 0.948 → 0.888 | 0.7% | +15.6 pp | 4/6 |
| SOL / Donchian | 0.631 → 0.588 | 1.0% | +5.8 pp | 2/6 |
| BNB / TSMOM | 0.566 → 0.571 | 0.5% | −0.4 pp | 3/6 |
| BNB / Donchian | 1.194 → 1.195 | 1.1% | +0.5 pp | 5/6 |

Turnover chỉ giảm **0.5–1.5%**, đều thấp hơn tiêu chí 25%; 8/8 return khác biệt nằm trong guardrail −5 pp, nhưng vẫn không có cặp nào đạt toàn bộ gate. DSR khoảng 0.19–0.64, turnover còn 55–179×/năm. Kết luận: cadence hằng ngày cũng không xử lý được phần lớn turnover, nên không đáng bắt đầu shadow 90 ngày cho candidate này. Không tune thêm cadence trên OOS cũ; giữ `NO_TRADE` / `WATCH`.

### Phân rã turnover

Backtester gán mỗi lần giao dịch vào một trong năm nguyên nhân và đối soát tổng theo từng bar. Chi phí modeled được phân bổ theo notional thực thi vào fee/spread/slippage, tính theo bps trên initial simulation equity. Quy tắc chi tiết ở `src/trader/quant/backtest.py`; các experiment JSON mới có `transaction_attribution_version: 1`.

Replay diagnostic-only trên cùng development OOS của 8 baseline/overlay pairings cho thấy turnover của hourly overlay phân bổ trung bình theo notional như sau:

| Nguyên nhân | Tỷ trọng turnover | Turnover/năm trung bình |
|---|---:|---:|
| Baseline entry | 49.4% | 55.6× |
| Baseline exit | 37.2% | 41.9× |
| Overlay resize khi còn long | 1.5% | 1.7× |
| Stop chạm | 11.9% | 13.4× |
| Stop gap | 0.0% | 0.0× |

Phân rã xác nhận turnover chủ yếu đến từ entry/exit của baseline (86.6%); resizing volatility chiếm phần nhỏ. Với giả định 13 bps mỗi chiều, modeled cost trung bình mỗi pairing trong toàn OOS tương đương 36.1% initial equity (fee 27.8%, spread 2.8%, slippage 5.6%). Đây là chi phí mô phỏng theo giả định, không phải số fill thực tế. Replay giữ net return và tổng turnover **khớp chính xác** với records gốc; 8 record `turnover_attribution` được đánh dấu diagnostic-only và không tăng DSR trial count. Holdout tiếp tục khóa.

## Giả thuyết đăng ký trước: baseline khung ngày qua backtest danh mục (2026-11-11)

[Protocol](research/protocols/daily-baselines-v1.md) được commit trước khi viết code và chạy đúng **một lần**. Hai
baseline chạy trên nến ngày UTC (gộp từ nến 1h, ngày thiếu giờ bị bỏ, không vá), tham số cố định từ
`features_by_timeframe["1d"]`. Cả hai đi qua backtest danh mục mới (`quant/portfolio.py`): vốn chung cho 4 symbol, sizing
giống risk engine, policy v0.1.0 (0.5% rủi ro/lệnh, tối đa 2 vị thế, 50% exposure). So sánh với cùng luật trên nến 1h.
Cửa sổ 2023-11-12 → 2025-11-10; holdout vẫn khóa.

| | tsmom ngày | donchian ngày | tsmom 1h (tham chiếu) | donchian 1h (tham chiếu) |
|---|---:|---:|---:|---:|
| Net return 2 năm / ở 2× phí | +12.7% / +11.8% | +10.0% / +9.4% | +7.0% / — | +2.3% / — |
| Max drawdown | −8.2% | −5.1% | −10.6% | −10.4% |
| Số lệnh đóng | 37 | 21 | 196 | 190 |
| Turnover/năm | 1.9× | 1.1× | 30.3× | 30.9× |
| Chi phí mô phỏng (bps vốn/năm) | 28 | 15 | 420 | 420 |
| Cận dưới CI 90% Sharpe (thấp nhất trong block 5/10/20 ngày) | −0.37 | −0.38 | — | — |
| DSR (65 trial) | 0.40 | 0.41 | không tính | không tính |

- **Đạt:** turnover giảm còn 3.6–6.4% so với 1h (mục tiêu ≤ 50%); vẫn dương ở 2× phí.
- **Không đạt:** số lệnh (21–37, cần ≥ 100), CI của Sharpe vẫn chứa 0 ở mọi độ dài block, DSR < 0.5.
- **Kết luận theo protocol:** khung ngày giải quyết được cơ chế chi phí nhưng **chưa có bằng chứng edge**. Kết quả
  inconclusive, **không đề xuất paper trading**, giữ `NO_TRADE` / `WATCH`. Không đổi ngưỡng, universe hay cửa sổ
  sau khi đã xem kết quả. Bằng chứng tiếp theo chỉ có thể đến từ dữ liệu forward.
- Lưu ý: sizing 0.5% rủi ro với stop rộng khiến exposure trung bình chỉ 8–11%. Cả hai baseline 1h đều chạm ngưỡng
  drawdown 10% của policy và bị chặn vào lệnh phần lớn thời gian, nên mức giảm turnover đo được là ước lượng thận trọng.
  Cửa sổ OOS này đã bị xem bởi nhiều trial trước, nên mọi kết quả chỉ mang tính khám phá.

Chi tiết và experiment IDs: [research/research-log.md](research/research-log.md).

## Cài đặt

```bash
uv venv --python 3.13 .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
uv pip install --python .venv\Scripts\python.exe -r requirements.lock
uv pip install --python .venv\Scripts\python.exe -e .
python -m trader doctor --network      # kiểm tra config, policy, DB, MCP server, market data
python -m pytest -q
```

Trên macOS/Linux thay `.venv\Scripts\python.exe` bằng `.venv/bin/python`. Không cần `uv`: `python -m venv .venv` rồi
`pip install -r requirements.lock` và `pip install -e . --no-deps` cũng được. MCP server tự tìm `.venv` qua
`scripts/run_mcp.py`, không cần activate trước khi mở Claude Code.

Không cần API key để chạy `PAPER`. Tùy chọn: copy `.env.example` → `.env` (không bao giờ dán key vào chat).

## Bắt đầu một session Claude Code

1. Mở `claude` ở thư mục gốc repo (terminal hoặc VS Code), với role mặc định `trader`: **không** có
   `.claude/settings.local.json` đặt `builder`.
2. Lần đầu: **chấp nhận hộp thoại workspace trust**. Nếu chưa trust, rule `allow` của project bị bỏ qua và mọi tool
   đọc đều hỏi quyền. Duyệt MCP server `trading`, sau đó `/mcp` phải hiện `trading · connected`.
3. Gõ `/trading-desk`. Claude kiểm tra trạng thái hệ thống, risk, vị thế (`sync_positions` sẽ hỏi bạn duyệt) rồi hỏi
   bạn muốn làm gì, ví dụ "phân tích BTCUSDT".

Chi tiết (role, các tool cần duyệt, quy trình approve, xử lý sự cố): [docs/operating-the-trading-desk.md](docs/operating-the-trading-desk.md).

## Lệnh hay dùng

```bash
python -m trader status                      # trạng thái hệ thống (JSON)
python -m trader shadow-log                  # ghi shadow log các nến 1h đã đóng (lên lịch mỗi giờ, docs §9)
python -m trader ingest --intervals 1h       # tải lịch sử nến
python -m trader backtest donchian_breakout_v1 --symbol BTCUSDT   # mỗi lần chạy được ghi lại như 1 trial
python -m trader research-volatility-overlay # challenger OOS trên dữ liệu đã ingest; không tải thay dữ liệu
python -m trader research-volatility-band    # preregistered 5pp band; exploratory trên cùng OOS
python -m trader research-volatility-daily   # preregistered daily sizing; exploratory trên cùng OOS
python -m trader research-turnover-attribution # diagnostic replay; không tính strategy trial
python -m trader research-daily-baselines    # protocol daily-baselines-v1; chạy lại sẽ ghi thêm 2 trial
python -m trader train-model --symbol BTCUSDT --model logreg_v1
python -m trader kill-switch engage --reason "..."
python -m trader mcp                         # chạy MCP server (stdio)
python scripts/harness_eval.py --label run   # eval harness bằng Haiku (tốn quota Claude, ~$0.05/lần)
```

Lệnh **chỉ người vận hành** chạy được (cần terminal thật, bị từ chối nếu gọi từ Claude Code):
`approve <intent_id>`, `kill-switch release`, `live-unlock`, `lesson promote`, `backtest --unlock-holdout`.

## Mô hình an toàn (tóm tắt)

- Claude **không có tool đặt lệnh**: chỉ tạo intent (không có tham số quantity); backend tự tính khối lượng.
- Validate hai lần (lúc tạo và lúc thực thi với dữ liệu mới); thiếu dữ liệu/dữ liệu cũ/policy lỗi → từ chối (fail closed).
- `PAPER`: duyệt qua permission prompt của Claude Code. `BINANCE_DEMO`/`LIVE`: duyệt bằng CLI ngoài LLM, dùng một lần, có hạn.
- Kết quả lệnh không rõ (timeout/5xx) → đối chiếu theo client order ID, **không gửi lại**.
- Mỗi vị thế trên sàn có stop `STOP_LOSS` (stop-market). Stop lỗi hoặc không rõ trạng thái → kill switch tự bật, chặn
  lệnh vào mới cho tới khi vị thế được bảo vệ lại. Thoát lệnh dùng `cancelReplace` nên không còn khoảng hở
  "đã hủy stop nhưng bán thất bại".
- `LIVE` cần đồng thời: `TRADING_MODE=LIVE`, `LIVE_TRADING_ENABLED=true`, key live, risk policy `status: approved`, và `live-unlock` còn hạn.
- Không có code rút/chuyển tiền, margin hay leverage.
- Giới hạn đã biết: ranh giới LLM ↔ execution là logic, cùng OS user — xem [architecture/threat-model.md](architecture/threat-model.md) (T3) trước khi dùng tiền thật.

Risk policy ([configs/risk_policy.yaml](configs/risk_policy.yaml)) đang là **provisional** — toàn bộ ngưỡng là ước lượng thận trọng, cần bạn review.

## Cấu trúc

```
src/trader/        config, domain, storage, exchanges/binance, marketdata, quant/, news, knowledge,
                   risk, execution, learning, desk (facade), mcp_server, cli
configs/           app.yaml, risk_policy.yaml
research/          research Phase 0, source register, experiments/ (mọi lần chạy, kể cả thất bại)
architecture/      system design, threat model, risk policy draft, ADR
knowledge/         knowledge base có provenance và status
.claude/           settings, hooks, skills (+ references), read-only agents
CLAUDE.md          luật cho mọi session Claude Code
docs/              hướng dẫn vận hành cho operator
state/             ghi chú bàn giao giữa các session, plan hiện tại
scripts/           run_mcp.py (khởi động MCP), harness_eval.py
reports/           kết quả eval harness (transcript thô git-ignored)
tests/             unit, contract (Binance giả lập), integration, failure paths
data/              runtime (git-ignored): candles, SQLite, models
```

## Việc tiếp theo

1. Không tune thêm volatility band trên development OOS đã xem; tiếp tục giữ holdout khóa.
2. Chạy thử một chu trình Binance Demo có giám sát (vào lệnh → stop `PROTECTED` → `sync_positions` → thoát bằng SELL
   intent) để kiểm chứng execution ngoài môi trường giả lập; đo fill/slippage thật.
3. Operator đăng ký lịch chạy `trader shadow-log` mỗi giờ (docs §9) để bắt đầu tích lũy dữ liệu forward sạch.
4. Quyết định của operator: có thêm hai luật khung ngày vào shadow log để quan sát forward hay không (backtest
   danh mục, bootstrap ngày và giả thuyết khung 1d đã xong; kết quả inconclusive, xem ở trên).
5. Chỉ đề cử paper-test khi một ứng viên mới vượt toàn bộ gate trên dữ liệu hợp lệ và vẫn bền ở chi phí stress.

Dự án khởi đầu từ một prompt đặc tả (không nằm trong repo). Thiết kế hiện tại nằm ở [architecture/](architecture/);
những điểm khác với đặc tả ban đầu được ghi ở [research/research-log.md](research/research-log.md).
