# RYC Vietnam V3 — đặc tả hệ thống và câu hỏi nghiên cứu

## 1. Vấn đề hệ thống giải quyết

Hệ thống không cố đoán “đỉnh giá” của trái phiếu. Nó trả lời câu hỏi kinh tế có thể kiểm tra:

> Từ hôm nay, nếu bán trái phiếu ở một ngày khả thi `s` thay vì bán ngay hoặc giữ tiền mặt, giá trị tiền ròng tại cùng ngày cuối kỳ `H` thay đổi thế nào?

Đây là cách diễn đạt đúng hơn của riding the yield curve. Một đường cong dốc lên có thể tạo roll-down, nhưng giá trái phiếu vẫn có thể giảm khi curve dịch chuyển lên, spread nới ra hoặc thanh khoản xấu đi. Vì vậy “giá đang tăng chậm” không phải quy tắc bán. Quy tắc phải so sánh **terminal net wealth** sau coupon, phí, settlement và cơ hội tiền mặt.

Biểu đồ market data có ba biến `z(t, τ)`: ngày thị trường `t`, tenor `τ`, zero yield `z`. Ba snapshot là ba lát cắt theo tenor, không phải một chuỗi thời gian duy nhất. Biểu đồ quyết định chính của V3 là hai chiều:

```text
sale execution date s  →  terminal wealth W(H, s)
```

## 2. Ba bài toán phải tách rời

| Trạng thái | V3 xử lý ra sao | Lý do |
|---|---|---|
| Đang nắm giữ | Đã hỗ trợ trong Model Mode | Coupon trước ngày sale execution vốn thuộc holder; sale proceeds chỉ vào cash account khi settlement. |
| Có tiền mặt, cân nhắc mua | Đã hỗ trợ trong **coupon-safe Model Mode** | Cash bị trừ tại entry settlement, không phải tại ngày tín hiệu. V3 chặn horizon có coupon để không tự đoán record/ex-coupon entitlement. |
| Giữ đến đáo hạn | Không trộn vào exit mode | Redemption là cash flow hợp đồng, không phải lệnh bán ở maturity. Cần ledger payment availability riêng. |

Không được dùng công thức holder cho buyer. Đây là lỗi đã có ở phiên bản legacy: đường đi “mua rồi bán” không trừ ask, phí mua và entry settlement, nên payoff bị thổi phồng.

## 3. Sổ cái toán học

Ký hiệu:

- `t`: ngày quyết định.
- `H`: ngày tiền mặt chung để so sánh mọi phương án.
- `s`: ngày execution của một sale candidate.
- `q_s`: ngày sale proceeds thực sự khả dụng.
- `q_b`: ngày entry purchase thực sự thanh toán.
- `A(a,H)`: hệ số tích lũy của cash account từ `a` đến `H`.
- `N_s`: sale proceeds ròng sau haircut/liquidity stress và phí bán.
- `O_b`: purchase outlay ròng theo ask và phí mua.

### Holder

\[
W_H^{holder}(s)=
\sum_{i:\,t<c_i<s} CF_iA(c_i,H)
+N_sA(q_s,H)
\]

Ngày trùng payment date bị loại. Trong Model Mode, coupon được ghi cho holder khi payment date nằm nghiêm ngặt giữa decision và sale execution. Một empirical study chỉ được mở coupon-window khi có record/ex-coupon/settlement rule theo từng ISIN.

Benchmark holder:

\[
W_H^{sell-now}=N_tA(q_t,H)
\]

### Buyer

Chọn vốn ban đầu `C_t = O_b` trên mỗi VND100 face để no-buy và buy bắt đầu bằng đúng cùng số tiền:

\[
W_H^{no-buy}=C_tA(t,H)
\]

\[
W_H^{buy\to sell}(s)=
C_tA(t,H)-O_bA(q_b,H)+N_sA(q_s,H)
\]

Coupon chưa được thêm vào phương trình buyer V3 vì system không biết nhà đầu tư mới có quyền hưởng coupon hay không. Nếu có bất kỳ payment date nào từ `t` đến `H`, V3 trả `BUYER_COUPON_ENTITLEMENT_REQUIRED` thay vì tạo kết quả đẹp nhưng sai.

### Giá curve và bid không được trộn trực tiếp

PV từ zero curve và bid quan sát có thể khác nhau do spread, thanh khoản hoặc cách xây dựng curve. Vì vậy V3 trước hết giải **Z-spread hằng số** `s_0` từ bid, với cả bid và curve được neo tại ngày thanh toán hiện tại `q_t`:

\[
P_{bid,q_t}=\sum_i CF_i\exp\{-[z_{q_t}(\tau_i)+s_0]\tau_i\}
\]

Sau đó mọi PV bán trong scenario dùng cùng `s_0`, còn haircut là stress riêng. Mỗi giá bán tương lai cũng được định giá tại settlement `q_s`, rồi mới tích lũy tiền bán từ `q_s` đến `H`. Output bắt buộc ghi lại bid, PV trước hiệu chỉnh, Z-spread và PV repriced. Đây là một giả định minh bạch về spread ổn định, không phải dự báo spread.

### Đơn vị

Price và cash flow trong V3 được chuẩn hóa theo **VND100 face**. Với vị thế mệnh giá `F`:

\[
wealth_{VND}=wealth_{per\,100}\times F/100
\]

Output luôn giữ đồng thời `per_100_face` và `for_position_vnd`; điều này ngăn lỗi trộn quote “per 100” với cash flow của toàn bộ mệnh giá.

### Hợp đồng giải thích quyết định

Một kết luận chỉ có ích khi người dùng lần được con số đó trở về các dòng tiền đã tạo ra nó. API công khai vì vậy trả thêm ba trường giải thích; giao diện **không được tự định giá lại** chúng:

| Trường | Ý nghĩa | Quy tắc kiểm tra |
|---|---|---|
| `scenario_weights` | Trọng số của từng kịch bản đã khai báo | Tổng phải bằng 1 trong sai số làm tròn. |
| `cash_ledger` | Sổ cái theo từng kịch bản: loại dòng tiền, ngày nhận/trả, phần tích lũy và giá trị tại H | Tổng `terminal_contribution.per_100_face` trong một kịch bản phải đúng bằng wealth mà engine đã chấm cho kịch bản đó. |
| `cash_breakdown` | Tổng hợp có trọng số của tiền ròng, phần tích lũy và wealth tại H | `net_cash_received + accumulation_to_terminal = terminal_wealth`. |

`cash_breakdown` là bảng trình bày theo **kỳ vọng**, không phải một con đường tiền mặt có thể xảy ra ngoài đời. Muốn xem một con đường thực tế, người dùng phải chọn đúng một kịch bản trong `cash_ledger`. Quy tắc này ngăn việc trộn các kịch bản rồi trình bày nhầm như một giao dịch đã xảy ra.

### Mốc của đường đi kịch bản

Ký hiệu `t` là ngày quyết định, `q₀` là settlement hiện tại, `qₛ` là settlement của ngày bán s. Curve đầu vào và bid dùng hiệu chỉnh đều có value date `q₀`. Từ revision `2026-09-13-curve-anchor-audit`, tỷ lệ dịch chuyển curve là:

`p(qₛ) = (qₛ − q₀)/(H − q₀)`.

Curve bằng curve đầu vào tại `q₀` và nhận đầy đủ cú sốc khai báo tại H. Giá bán và nghiệm biên yield dùng cùng đường đi. Đây là quy ước kịch bản theo value date, chưa phải dự báo ước lượng từ dữ liệu. Revision trước dùng t thay q₀ trong tỷ lệ, không khớp điểm neo khi settlement lag khác 0. Phép thử trái phiếu zero-coupon với curve dịch chuyển kiểm giá tiền và nghiệm hòa vốn bằng công thức đóng cho holder và buyer.

### Phạm vi của “điểm tối ưu”

Ngày được chọn là ngày có terminal wealth kỳ vọng lớn nhất trong tập ngày hợp lệ mà engine đã thử. UI cho phép `monthly` (mỗi tháng và thêm biên cuối kỳ) hoặc `business_day` (mỗi ngày làm việc theo calendar đang khai báo). Vì vậy kết luận phải được đọc là “cực đại trong lưới và scenario đã nhập”; nó không phải nghiệm liên tục, adaptive optimal stopping hay dự báo ngày bán thực tế.

### Ngày bán cố định khác quy tắc ra quyết định liên tục

V3 giải `max_s Σ w_ω W_H(s,ω)` trên lưới ngày khả thi đã công bố. Người dùng đang chọn một kế hoạch bán dựa trên các giả định hôm nay. Kết quả không phải nghiệm optimal stopping thích ứng với dữ liệu mới mỗi ngày. Việc sau này chạy lại mô hình không tự chứng minh chính sách cập nhật ấy tối ưu.

Nếu mở hướng optimal stopping, phải xác định trạng thái quan sát được, mô hình chuyển trạng thái, thời điểm nhận coupon, settlement, quyền chờ và hàm mục tiêu/rủi ro rồi mới thiết lập quy hoạch động. Chưa đủ dữ liệu để ước lượng và kiểm định những thành phần đó; không gắn nhãn optimal stopping cho engine hiện tại. Cực đại kỳ vọng cũng chưa xét mức chịu lỗ của một nhà đầu tư cụ thể; worst-case và ε chỉ là chẩn đoán đã khai báo.

### Giữ đến đáo hạn

Khi `H ≥ maturity`, holder có thêm phương án redemption:

\[
W_H^{maturity}=\sum_{i:t<c_i\leq maturity}CF_iA(c_i,H)
\]

Nó được chấm cùng expected wealth, worst wealth, regret và scenario support như các ngày bán. Trong Model Mode, `c_i` đồng thời là ngày tiền payment có thể tái đầu tư; empirical mode phải thay bằng bằng chứng payment-availability.

### Cushion yield của holder

Một ngày bán trong tương lai không chỉ cần có `W_H` cao trong scenario đã chọn; holder còn cần biết nó chịu được bao nhiêu bất lợi về lãi suất trước khi thua bán ngay. Với mỗi scenario `ω`, V3 giữ nguyên cash rate, haircut, Z-spread và level/slope shift của scenario, rồi tìm cú sốc **song song tăng thêm** `δ` tại settlement `q_s` sao cho:

\[
W_H^{holder}(s;\omega,\delta)=W_H^{sell-now}(\omega).
\]

`δ > 0` nghĩa là phương án chờ vẫn chịu được một mức yield tăng thêm; `δ < 0` nghĩa là nó đã thua sell-now và cần yield giảm bấy nhiêu để hòa. Mỗi scenario có một nghiệm riêng; bảng audit chỉ báo mức thấp nhất khi mọi scenario đều có nghiệm trong miền tìm kiếm ±5.000 bp. Không có nghiệm được báo rõ, không biến thành một con số rất lớn. Cushion là sensitivity toán học, **không phải forecast, VaR hay xác suất thắng**. Nó không tham gia hàm mục tiêu.

## 4. Hàm mục tiêu và chẩn đoán nhạy cảm

Mỗi scenario đưa ra \(\widehat W_H(s;\omega)\). V3 tính:

- expected terminal wealth;
- decision margin: expected wealth của phương án thắng trừ phương án đứng thứ hai;
- worst scenario wealth;
- maximum regret so với ngày tốt nhất trong từng scenario;
- robust support: tỷ trọng scenario mà ngày đó nằm trong dung sai kinh tế `ε`.

Mục tiêu chính của mô hình là:

\[
\arg\max_{a \in \mathcal A}\;\mathbb E\left[W_H(a;\omega)\right]
\]

Trong đó \(\mathcal A\) gồm mọi ngày bán đủ điều kiện, cộng giữ đến đáo hạn nếu \(H\) đã qua đáo hạn; với buyer, \(\mathcal A\) còn gồm **không mua và giữ tiền mặt**. Nếu bằng nhau đúng mức sai số số học, mô hình chọn ngày tạo tiền sớm hơn. Đây là quy tắc chọn duy nhất.

Vùng bền chỉ là chẩn đoán:

\[
\mathcal Z_\epsilon=
\left\{s:\operatorname{support}(s)\geq p_{min}\right\}
\]

Nếu vùng rỗng, mô hình vẫn trả phương án cực đại kỳ vọng và báo rằng lựa chọn nhạy với scenario đã khai báo. Nếu vùng tồn tại, nó cho biết các ngày gần tối ưu trong tỷ trọng scenario đã chọn. Không được dùng vùng này để thay mục tiêu cực đại kỳ vọng, và support không phải xác suất thành công.

`Decision margin` là kiểm tra khác với robust support. Nếu margin không lớn hơn \(\epsilon\) đã công bố, mô hình vẫn nêu maximiser theo quy tắc toán học nhưng phải diễn giải rằng phương án thắng và phương án về nhì **không phân biệt được về mặt kinh tế** ở độ phân giải đã chọn. Nó không phải khoảng tin cậy thống kê.

Margin phụ thuộc vào lưới action đã đánh giá. Lưới business-day thường làm runner-up là ngày liền kề và margin rất nhỏ; lưới monthly trả lời câu hỏi quản trị khác: có đáng đổi kế hoạch trong tháng hay không. Output luôn ghi `candidate_grid`; protocol phải khóa lưới trước khi xem kết quả, không đổi lưới chỉ để tạo margin đẹp.

## 5. Cách dùng Decision Workspace

1. Mở [Decision Workspace V3](http://127.0.0.1:8772/ryc_v3_decision_workspace.html) sau khi chạy `CHAY_RYC_V3.ps1` nếu local server chưa chạy.
2. Chọn trạng thái holder hoặc buyer. Với holder, nhập ngày mua/giá mua để thấy P/L bối cảnh, nhưng không để sunk cost chi phối quyết định tương lai.
3. Dán cash-flow schedule chính thức theo đúng ISIN. Dòng cuối phải gồm VND100 principal.
4. Nhập bid hoặc ask dirty price, chi phí, H, curve hiện tại và scenario. Curve input chỉ nhận continuous zero spot; par yield phải được quy đổi theo methodology trước.
5. Đọc kết quả theo thứ tự: data tier → phương án cực đại kỳ vọng → expected/worst edge → settlement timeline → robust diagnostic → audit table.
6. Với holder, đọc thêm `Cushion yield`: đây là phần trả lời trực tiếp “chờ thêm chịu được bao nhiêu yield tăng trước khi kém bán ngay?”. Dấu âm là cảnh báo rằng phương án đó cần yield giảm mới bắt kịp benchmark.
7. Dùng preset `Cautious`, `Roll-down` và `Buyer coupon-safe` để kiểm tra trực giác. Đây là bài học phương pháp, không phải dữ liệu thị trường Việt Nam.

Browser không tính tài chính. Nó gửi payload cục bộ đến `ryc_v3_engine.py`; JSON export chứa cả input và versioned result contract để tái lập kết quả.

## 6. Từ Model Mode sang bài nghiên cứu Việt Nam thật

V3 hiện là lớp quyết định chuẩn. Để làm empirical study, không thêm một dashboard mới; thay manual assumptions bằng data contract có các gate sau:

| Gate | Bằng chứng bắt buộc |
|---|---|
| Security master | ISIN, fixed-rate bullet, actual schedule, issue/maturity, day count, coupon entitlement. |
| Curve | curve value date, source publication time ICT, hash/revision, methodology, compounding. |
| Quote/fill | ISIN, dirty/clean basis, bid/ask/fill, size, availability time, raw hash. |
| Calendar | source/version/hash của trading và settlement calendar; `q_s` phải được tính lại từ execution date + rule. |
| Cash account | rate interval bao phủ coupon/proceeds availability đến H, không gap/overlap. |
| Point in time | mọi input forecast `available_at ≤ information_cutoff ≤ decision`. |
| Protocol | costs, epsilon, candidate grid, tie-break, benchmark, OOS partition và overlap treatment khóa trước test. |

Data Console V2.1 là công cụ nghiên cứu nền cho các gate này. Nó không còn là giao diện chính và buyer empirical runner đã được khóa, bởi fill/calendar/coupon-entitlement chưa đủ hoàn chỉnh.

### Phạm vi empirical MVP nên chốt

1. TPCP VND fixed-rate bullet.
2. **Holder-only** ở giai đoạn đầu.
3. Monthly decision grid, non-overlapping episodes, common terminal cash date.
4. Benchmark: sell-now; fixed-horizon ride; redemption chỉ khi ledger payment availability hoàn chỉnh.
5. Forecast baseline: unchanged/random-walk curve trước, sau đó mới đến Diebold–Li hoặc mô hình khác.
6. Báo cáo theo tầng bằng chứng: `MODEL_ONLY` → `MARKET_SIMULATION` → `EXECUTABLE_EVIDENCE`.

Điểm đóng góp có thể bảo vệ của bài nghiên cứu không phải “phát minh riding the curve”, mà là protocol point-in-time cho TPCP Việt Nam gắn selection rule với terminal wealth, settlement, coupon, bid–ask/liquidity và data-quality gates.

## 7. Nguồn miễn phí để xây foundation

- [HNX Yield Curve](https://hnx.vn/vi-vn/trai-phieu/duong-cong-loi-suat.html?site=in) cho 11 node lịch sử và xuất Excel: dùng cho nghiên cứu curve-level, gắn nhãn `EX_POST_CURVE_ONLY` nếu thiếu giờ công bố.
- [Bản tin HNX ngày 13/02/2026](https://hnx.vn/vi-vn/m-tin-tuc-hnx/Cap%20nhat%20tinh%20hinh%20bien%20dong%20lai%20suat%20tren%20thi%20truong%20TPCP%20ngay%2013022026-599009-1.html) có giờ công bố và tệp nguồn: chỉ khi `published_at ≤ decision_at` mới được gắn `PIT_SIGNAL_ELIGIBLE`.
- `ryc_data_registry.html` là đường thu thập công khai cho nghiên cứu: researcher chọn tệp nguồn đã lưu, browser tự tính SHA-256 và ghi tên/kích thước tệp, URL, publication/capture time, methodology và node. Hash không lưu bản thân tệp; bản gốc vẫn phải được lưu cùng nhật ký nghiên cứu. `ryc_snapshot_collector.html` chỉ là scratchpad minh hoạ và bị hạ nhãn, không được dùng cho evidence.
- Trang giao dịch gần nhất của HNX chỉ là `REFERENCE_NON_EXECUTABLE`: không có ISIN, bid/ask, thời điểm khớp hoặc lịch sử đủ để làm fill.
- Dữ liệu per-bond/fill cần cho backtest thật là gói HNX có phí, theo [danh mục HNX 2026](https://owa.hnx.vn/ftp/PORTALNEW/FileContent/HNX_Danh%20muc%20goi%20tin%20va%20bang%20gia%20dich%20vu%20CCTT%2820260105_145538_848%29.pdf). Vì thế bài nghiên cứu miễn phí phải trung thực ở cấp curve-level/synthetic bond.
- Bieri & Chincarini, *Riding the Yield Curve*, Journal of Fixed Income (2005): [PDF](https://www.david-bieri.com/docs/RidingYieldCurve_BieriChincarini_JFI05.pdf).
- Diebold & Li, yield-curve forecasting baseline: [NBER working paper](https://www.nber.org/papers/w10048).
- HNX về nguồn giá và tính thanh khoản của chỉ số TPCP: [tài liệu HNX](https://www.hnx.vn/vi-vn/ModuleReportBonds/Bond_IDX_OverView/Bond_IDX_BaiVietTieuChiChonRoChiSo).

## 8. Những điều V3 cố ý chưa hứa

- Không nói yield curve dốc lên đồng nghĩa giá trái phiếu tăng.
- Không gắn nhãn BUY/SELL có thể thực hiện từ manual scenario.
- Không tự suy luận holiday, ex-coupon date, tax, fixed fee hoặc liquidity từ dữ liệu thiếu.
- Không gọi một JSON qua validator là “backtest thành công”.

Đó là giới hạn thiết kế, không phải thiếu sót trình bày: system thà trả “không có vùng bền” hoặc “thiếu entitlement data” còn hơn biến giả định thành kết luận đầu tư.
