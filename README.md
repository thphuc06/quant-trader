# quant-trader

Bộ khung nghiên cứu quant + giao dịch có kiểm soát cho **Claude Code**, trên **Binance Spot** (long/flat).
Claude đóng vai trò analyst: đọc dữ liệu qua MCP, lập luận trên bằng chứng, đề xuất *trade intent*.
Mọi con số (indicator, sizing, risk) do code Python đã test tính; mọi lệnh phải qua risk engine và được người duyệt.

> **Trạng thái: checkpoint đang phát triển (2026-11-10).** Mặc định `PAPER`, live trading bị khóa.
> Chưa có chiến lược nào vượt qua ngưỡng kiểm định — kết quả trung thực hiện tại của hệ thống là `NO_TRADE` / `WATCH`.
> Đây không phải lời khuyên đầu tư và không có gì ở đây chứng minh khả năng sinh lời.

## Đã có gì

| Phần | Trạng thái |
|---|---|
| Research & kiến trúc ([research/](research/), [architecture/](architecture/), 5 ADR, threat model) | Xong |
| Core: config an toàn mặc định, domain model (Decimal), SQLite + journal append-only có hash chain, Parquet candles | Xong, có test |
| Binance: REST public + signed (tập endpoint cố định, không retry mù lệnh), filters, kline stream | Market data đã chạy thật; **order adapter Demo/Live mới chỉ test với HTTP giả lập** (chưa có demo key) |
| Quant: features nhân quả, regime, 2 baseline strategy, backtester, PSR/DSR/PBO, experiment registry, holdout khóa | Xong, có test |
| ML: triple-barrier, purged walk-forward, calibration | Xong; model **không** dùng cho quyết định |
| Risk engine + execution: sizing phía server, intent → duyệt → re-validate → order → reconcile, kill switch, khóa live | Xong, có test (paper chạy end-to-end với giá thật) |
| News/web: provider Tavily/Brave (tùy chọn), fetch chống SSRF, lưu point-in-time | Xong, có test |
| MCP server `trading`: 27 tools, 8 resources, 4 prompts | Xong, có test |
| Knowledge base ([knowledge/](knowledge/)) | Bộ tài liệu khởi đầu, đều ở trạng thái `candidate` |
| Claude Code hooks ([.claude/hooks/](.claude/hooks/)) | Script + test xong; **chưa nối vào `.claude/settings.json`** |
| `CLAUDE.md`, Skills (`/trading-desk`…), subagents, `.mcp.json`, tài liệu vận hành | **Chưa làm** — bước tiếp theo |

Test: 1 test đang fail có chủ đích (`test_settings_and_mcp_config_are_valid_and_least_privilege`) vì
`.claude/settings.json` và `.mcp.json` chưa được tạo; các test còn lại pass. `ruff` và `mypy` sạch trên `src/`.

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

## Cài đặt

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.lock
pip install -e . --no-deps
python -m trader doctor --network      # kiểm tra config, policy, DB, MCP server, market data
python -m pytest -q
```

Không cần API key để chạy `PAPER`. Tùy chọn: copy `.env.example` → `.env` (không bao giờ dán key vào chat).

## Lệnh hay dùng

```bash
python -m trader status                      # trạng thái hệ thống (JSON)
python -m trader ingest --intervals 1h       # tải lịch sử nến
python -m trader backtest donchian_breakout_v1 --symbol BTCUSDT   # mỗi lần chạy được ghi lại như 1 trial
python -m trader train-model --symbol BTCUSDT --model logreg_v1
python -m trader kill-switch engage --reason "..."
python -m trader mcp                         # chạy MCP server (stdio)
```

Lệnh **chỉ người vận hành** chạy được (cần terminal thật, bị từ chối nếu gọi từ Claude Code):
`approve <intent_id>`, `kill-switch release`, `live-unlock`, `lesson promote`, `backtest --unlock-holdout`.

## Mô hình an toàn (tóm tắt)

- Claude **không có tool đặt lệnh**: chỉ tạo intent (không có tham số quantity); backend tự tính khối lượng.
- Validate hai lần (lúc tạo và lúc thực thi với dữ liệu mới); thiếu dữ liệu/dữ liệu cũ/policy lỗi → từ chối (fail closed).
- `PAPER`: duyệt qua permission prompt của Claude Code. `BINANCE_DEMO`/`LIVE`: duyệt bằng CLI ngoài LLM, dùng một lần, có hạn.
- Kết quả lệnh không rõ (timeout/5xx) → đối chiếu theo client order ID, **không gửi lại**.
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
.claude/hooks/     guard, session banner, audit (chưa kích hoạt)
tests/             unit, contract (Binance giả lập), integration, failure paths
data/              runtime (git-ignored): candles, SQLite, models
```

## Việc tiếp theo

1. `.claude/settings.json` (permissions + hooks), `.mcp.json`, `CLAUDE.md`, Skills, subagents.
2. Tài liệu vận hành (`docs/`), `state/` cho session handoff.
3. Chạy thử Binance Demo Mode với key demo, đo slippage thật.
4. Paper trading đủ lâu trước khi cân nhắc bất cứ điều gì khác.

Dự án khởi đầu từ một prompt đặc tả (không nằm trong repo). Thiết kế hiện tại nằm ở [architecture/](architecture/);
những điểm khác với đặc tả ban đầu được ghi ở [research/research-log.md](research/research-log.md).
