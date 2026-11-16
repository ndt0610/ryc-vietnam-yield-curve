# Protocol nghiên cứu RYC Vietnam V3

Routing 28/09/2026 — DEC-047: tài liệu này tiếp tục mô tả fixed-date V3
baseline. [Kế hoạch phát triển mới](RYC_DEVELOPMENT_AND_RESEARCH_PLAN_2026-09-28.md)
đặt forecast/policy cập nhật/multi-lot thành PLANNED; chúng cần protocol v2
khóa trước OOS, không tự được chứng nhận bởi protocol hoặc tests V3 cũ.

## 1. Tuyên bố có thể bảo vệ

Đề tài **không** tuyên bố phát minh riding the yield curve, dự báo được giá trái phiếu, hay xây một chiến lược đã chứng minh sinh lời tại Việt Nam.

Tuyên bố cần kiểm tra là hẹp hơn và có thể phản biện:

> Với holder trái phiếu Chính phủ VND coupon cố định, quy tắc chọn ngày bán theo tiền ròng tại cùng `H` tạo chênh lệch tiền cuối kỳ bao nhiêu so với bán ngay và các ngày bán cố định, trong tầng bằng chứng nào; lựa chọn đó còn phân biệt được khi thay đổi giả định về curve, settlement, coupon và chi phí hay không?

Đây là câu hỏi cần đo, không là giả thuyết mặc định quy tắc sẽ tốt hơn. Cực đại kỳ vọng có điều kiện trong engine không bảo đảm cực đại tiền thực nhận, ít rủi ro hay ít đổi hành động. Fixed-date không đổi ngày bán theo giả định theo định nghĩa; vì vậy không dùng action-flip để tuyên bố policy ổn định hơn fixed-date. Sensitivity là chẩn đoán phụ, không thay chỉ tiêu tiền ròng.

Đóng góp kỳ vọng là **protocol và evidence discipline**: đưa một ý tưởng riding quen thuộc vào sổ cái point-in-time, đặt chuẩn tiền mặt cùng `H`, và báo rõ khi dữ liệu không đủ để phát biểu về khả năng thực thi.

Rà soát đối kháng 13/09/2026: những thực hành này là điều kiện chất lượng,
chưa tự là novelty. [Hướng ngách ứng viên](RYC_V3_ADVERSARIAL_REVIEW.md) đặt
câu hỏi khi nào evidence đủ phân biệt các ngày bán và thiếu hụt nào có thể
đảo thứ hạng. DEC-024 có [research CLI tổng hợp](RYC_INFORMATION_EXPERIMENT_REPORT.md)
đo information partitions trên ledger; chưa tích hợp sản phẩm hoặc có bằng
chứng thị trường. Primary ΔW và gate HNX giữ nguyên.

## 2. Câu hỏi, đối tượng và kết quả chính

| Thành phần | Chốt trước khi chạy |
|---|---|
| Đối tượng chính | Holder của TPCP VND fixed-rate bullet; chưa mở buyer thực nghiệm. |
| Ngày quyết định `t` | Cuối tháng, chỉ khi curve và quote/curve-model được ghi nhận là đã có trước cutoff. |
| Horizon `H` | 6 tháng kể từ `t`; primary sample không chồng lấn. |
| Hành động | Bán ngay, bán tại mọi tháng hợp lệ đến `H`, và redemption nếu `H` đã qua đáo hạn. |
| Hàm mục tiêu ex-ante | `argmax_a E_t[W_H(a)]`, với `a` là sale date/redemption; buyer sau này thêm no-buy cash. |
| Kiểm tra nhận diện | `margin = E_t[W_H(a*)] − max_{a≠a*}E_t[W_H(a)]`; chỉ diễn giải lựa chọn là phân biệt được khi `margin > ε`. |
| Kiểm tra fragility | Với mỗi future sale `s` và scenario `ω`, giải `δ_{s,ω}` để `W_H(s;ω,δ)=W_H(sell-now;ω)`, trong đó `δ` là parallel yield shift tăng thêm tại sale settlement. |
| Chỉ tiêu chính | `ΔW_H = W_H(policy) − W_H(sell-now)` sau phí và settlement. |
| Chỉ tiêu phụ | worst-case edge, realised regret (chỉ chẩn đoán hindsight), support, số lần đổi hành động, và tỷ lệ loại episode. |

`ΔW_H` phải là kết quả evaluator trên dữ liệu quan sát sau quyết định; expected edge do engine dự tính chỉ là kết quả ex-ante riêng. Trong curve-level, cả hai giá đều là giá mô hình của reference bond; trong executable evidence mới dùng giá thực thi đủ điều kiện. Báo chênh lệch per 100 và quy mô VND, không so annualized holding returns khác horizon.

Benchmark primary là sell-now. Fixed 3/6 tháng là đối chứng phụ được khóa trước; chỉ so trên episode có cùng ngày quyết định, `H`, thông tin, quy mô và action khả thi. Nếu ngày bán cố định không hợp lệ do settlement/coupon, ghi benchmark unavailable và báo số episode so cặp; không tự đổi sang ngày thuận lợi hơn. Hold khi `H<M` cần thanh lý đủ settlement trước H, không được thay bằng giá đánh dấu tại H rồi gọi là tiền mặt.

Tại từng `t`, engine chỉ dùng forecast/assumption đã khóa tại `t`. Sau đó evaluator tách riêng curve/quote và cash data quan sát để chấm `W_H` thực tế. Không được dùng curve tại sale date để quay ngược tái đầu tư coupon trước đó.

## 3. Công thức được chấm

Với holder, sale execution là `s`, settlement là `q_s`, và `A_ω(a,H)` là cash factor trong scenario hoặc dữ liệu realised:

\[
W_H(s;\omega) =
\sum_{i:\,t<c_i<s} CF_i A_\omega(c_i,H)
+ N_s(\omega) A_\omega(q_s,H).
\]

`N_s` là dirty bid/curve-model sale price sau haircut và sell fee. Sell-now dùng chính bid hiện có và settlement hiện có; không được thay âm thầm bằng candidate tháng sau. Khi `H` qua maturity, redemption là phương án tiền mặt riêng:

\[
W_H^{maturity}(\omega)=\sum_{i:\,t<c_i\leq M} CF_i A_\omega(c_i,H).
\]

Chỉ khi so buyer mới dùng vốn ban đầu bằng entry outlay và thêm no-buy cash. Không lấy công thức holder để đánh giá người chưa sở hữu trái phiếu.

## 4. Ba tầng bằng chứng — không được lẫn

| Tầng | Có thể khẳng định | Không được khẳng định | Dữ liệu tối thiểu |
|---|---|---|---|
| `MODEL_ONLY` | Tính đúng theo các giả định đã khai báo. | Lợi nhuận thị trường hay lệnh có thể khớp. | Curve, cashflow, phí, scenario do người dùng nhập. |
| `CURVE_LEVEL_SIMULATION` | Hành vi của quy tắc trên curve-model có provenance, trong phạm vi vintage/thời gian thực sự chứng minh được. | Fill/chi phí giao dịch thật theo ISIN. | Spot curve và bằng chứng availability trước cutoff, synthetic/reference bond, convention và cash-rate methodology. |
| `EXECUTABLE_EVIDENCE` | Kết quả đã qua kiểm định theo quote/fill và lịch thanh toán thực. | Khả năng tổng quát ngoài sample. | Security master, bid/ask/fill, entitlement, calendar, cash path, protocol OOS. |

HNX hiện hiển thị Spot Rate curve và tin cập nhật thị trường công khai. Tuy nhiên danh mục dịch vụ HNX liệt kê dữ liệu yield curve lịch sử và kết quả theo mã trái phiếu trong các gói thông tin; do đó dữ liệu miễn phí không đủ để tự gọi một kết quả là per-bond executable backtest. [Trang TPCP HNX](https://sims.hnx.vn/vi-vn/trai-phieu.html), [danh mục dữ liệu HNX 2026](https://owa.hnx.vn/ftp/PORTALNEW/FileContent/HNX_Danh%20muc%20goi%20tin%20va%20bang%20gia%20dich%20vu%20CCTT%2820260105_145538_848%29.pdf).

Đường cong cũng không đồng nghĩa bid có thể khớp: HNX mô tả chỉ số trái phiếu dùng kết hợp giá giao dịch, giá chào có cam kết, giá từ hệ thống curve và giá mô hình. Điều này buộc `CURVE_LEVEL_SIMULATION` phải ghi rõ basis của giá. [Phương pháp chỉ số trái phiếu HNX](https://www.hnx.vn/vi-vn/ModuleReportBonds/Bond_IDX_OverView/Bond_IDX_BaiVietTieuChiChonRoChiSo).

## 5. Protocol chạy được cho dữ liệu miễn phí

**DEC-025 — availability và độ đại diện là hai phép kiểm khác nhau.**
Một nguồn phản biện lịch sử của World Bank2023 §41 đặt vấn đề về tuổi/loại
giá đầu vào HNX; xem [review và bytes](../data/research/source-review-2026-09-13/REVIEW.md).
Không suy tình trạng2026 hoặc gán độ trễ cố định từ nguồn này. Capture trước
t xác minh đã thấy bytes, không chứng minh quotes/trades đầu vào đại diện
thị trường tại t. Curve value date là mốc định giá, không mặc nhiên là giờ
giao dịch/thỏa thuận của mọi đầu vào. Nếu nguồn không nêu input-age hoặc
phương pháp tổng hợp, ghi UNKNOWN và giới hạn kết luận vào published-curve
simulation; không dùng kết quả ấy để nhận forecast skill/executable P/L.
DEC-026 bổ sung một kiểm tra research-only cho trường hợp nguồn chỉ trả về
khoảng cash/fee: phân hoạch và chi phí phải khóa trước, không được lộ future
shock, và kết quả chỉ được đọc trong synthetic finite scope. Không dùng thí
nghiệm này để suy ra availability, chất lượng HNX, expected wealth hay
novelty.
Nghiên cứu tuổi giá cần đặc tả và nguồn riêng, không tự dịch ngày dữ liệu.

**Đính chính về độ phủ và nguồn (13/09/2026):** không có ngưỡng phổ quát “24 tháng là đủ” được chứng minh cho bài này. Validator kiểm cấu trúc có thể chạy với một curve hợp lệ liên quan đến episode; điều đó không chứng minh cỡ mẫu đủ để suy luận. Cần công bố số episode hoàn tất không chồng lấn, thời gian train/test, dữ liệu thiếu và phụ thuộc theo thời gian. Nhiều mã trên cùng ngày có thể cùng chịu một cú sốc lãi suất, nên số dòng không bằng số quan sát độc lập. Với horizon sáu tháng, 24 tháng liên tục chỉ chứa tối đa bốn khoảng sáu tháng không chồng lấn trước các bước loại/training; không có lý do tự nâng nó thành bằng chứng hiệu quả.

Hash nhận diện nội dung tệp, không chứng minh tính xác thực của nguồn hoặc giờ công bố. Registry lưu riêng hash tính từ bytes và hash khai báo thủ công, nhưng cả hai vẫn cần đối chiếu bản gốc và revision. Metadata từ thao tác nhập tay không tự được chứng nhận point-in-time. Bản thu thập hôm nay không chứng minh người nghiên cứu đã biết bản đó ở một ngày trong quá khứ.

**Ranh giới tính mới:** Bieri–Chincarini (2005) đã khảo sát nhiều chiến lược riding và thảo luận bộ lọc biến động yield hòa vốn trong văn liệu trước đó. Vì vậy rate cushion và tối ưu theo kịch bản chưa phải tính mới của đề tài. Điểm đóng góp ứng viên là đánh giá thực nghiệm xem quyết định còn phân biệt được sau quy đổi dòng tiền, settlement, chi phí và sai số dự báo trong bối cảnh TPCP Việt Nam. Đây là giả thuyết đóng góp cần soát văn liệu tiếp, chưa được xác nhận độc đáo. [Bản tác giả](https://www.david-bieri.com/docs/RidingYieldCurve_BieriChincarini_JFI05.pdf).

Đây là thiết kế bài nghiên cứu sinh viên có thể hoàn thành một cách trung thực:

1. Thu snapshot curve công khai **từ ngày hiện tại trở đi**, lưu file gốc, URL, hash, `captured_at`, `published_at` nếu có, curve value date, methodology/compounding nếu nguồn nêu. Thiếu publication không tự cấm nghiên cứu prospective: capture có provenance được xác minh trước cutoff có thể chứng minh đã thấy đúng bytes đó lúc thu, nhưng không suy ngược publication hoặc khả dụng lịch sử. Capture sau cutoff không được dùng cho tín hiệu cũ. Verifier nguồn/availability hiện chưa hoàn thành, nên chưa bundle nào đủ pilot; metadata nhập tay không giải điều kiện này.
2. Dùng một hoặc nhiều **synthetic reference fixed-rate bullet** với cash-flow được công bố ngay trong bài. Định giá qua spot curve, constant Z-spread đã công bố và haircut/fee sensitivity. Đây là mô phỏng curve-level, không phải trái phiếu đã khớp lệnh.
3. Trước khi xem period test, khóa: horizon, candidate grid, cash alternative, Z-spread rule, fee/haircut grid, `ε`, scenario weights, benchmark, và cách xử lý ngày thiếu.
4. Mỗi `t`: baseline forecast là unchanged curve; chọn `argmax E_t[W_H]`; giữ sổ cái ex-ante. Khi tới sale/H, dùng snapshot realised đúng ngày đã lưu để chấm riêng. Nếu missing/stale snapshot, loại episode với mã lỗi; không thay ngày khác.
5. Báo cáo toàn bộ số episode dự kiến, hợp lệ và bị loại; bảng `ΔW_H` theo từng episode; median/mean; phân phối; sensitivity matrix. Không báo p-value nếu số episode hoặc thời gian theo dõi chưa đủ.

Khi chuỗi đủ dài, baseline forecast phải luôn là unchanged/random walk. Dynamic Nelson–Siegel hoặc AR(1) chỉ là mô hình cạnh tranh; nó phải được fit rolling trên train/validation trước, rồi đánh giá final test riêng. Forecast RMSE thấp hơn không tự chứng minh `ΔW_H` cao hơn.

## 6. Điều kiện loại episode (fail closed)

Một episode bị loại, không nội suy/đoán, khi thiếu bất kỳ điều kiện nào:

- reference bond chưa được định nghĩa ex-ante, đã đáo hạn tại quyết định hoặc cash-flow schedule không hoàn chỉnh; executable evidence còn phải xác minh phát hành/hợp đồng của ISIN;
- curve/quote không có evidence `available_at ≤ information_cutoff ≤ decision_at`;
- curve value date và quote value date không cùng basis;
- sale settlement sau `H`, execution/payment window có coupon mà chưa có rule entitlement;
- cash alternative không bao phủ từng khoảng từ coupon/proceeds đến `H`;
- giá không có dirty/clean basis đã khai báo; executable evidence còn phải có size/time/bid-side, curve-level phải khai báo model-price basis và không gán cho nó fill/quote thật;
- primary episodes chồng lấn trái với protocol.

`ryc_v21_contract_validator.py` kiểm contract khai báo, không chứng thực toàn bộ các điều kiện nguồn nêu trên. Verifier qualification và evaluator thị trường chưa triển khai đầy đủ. V3 Decision Engine duy trì sổ cái cash-flow cho lớp model.

Thay đổi engine phải có phản ví dụ và kiểm thử phù hợp qua [KIEM_TRA_LOI_RYC_V3.ps1](./KIEM_TRA_LOI_RYC_V3.ps1). Lỗi protocol phải được sửa khi phát hiện; test phần mềm đạt không xác nhận phương pháp nghiên cứu hoặc nguồn dữ liệu.

## 7. Kiểm định và phản biện cần có trong bài

1. **Accounting identity:** tổng terminal contribution trong ledger đúng bằng `W_H` cho mọi scenario/candidate.
2. **No-arbitrage synthetic fixture:** flat curve, cùng cash rate, không phí phải không tạo riding alpha qua coupon/settlement.
3. **Decision completeness:** lưới phải có sell-now, biên cuối đủ settlement trước `H`, và redemption khi phù hợp.
4. **Decision identifiability:** công bố runner-up và decision margin; margin không vượt `ε` phải được ghi là không phân biệt được, không được thổi thành timing signal.
5. **Rate-fragility:** công bố yield cushion theo scenario. Cushion âm nghĩa candidate cần yield giảm mới hòa sell-now; không đổi cushion thành dự báo hay xác suất.
6. **Sensitivity:** khóa tập perturbations trước test; ±25/50 bp level/slope là ví dụ thiết kế, không là biên forecast đã được hiệu chuẩn. Khóa tương tự spread/haircut, cash rate, fee, `ε` và settlement lag. Báo action flip của policy, khoảng expected edge/margin và wealth của từng benchmark dưới cùng perturbation; không chọn một tổ hợp đẹp nhất. Không dùng action flip bằng 0 của fixed-date làm đối chứng khả năng dự báo. `margin > ε` chỉ là phân biệt theo tolerance đã chọn, không là significance thống kê hoặc bảo đảm trước mọi sai số dữ liệu.
7. **Benchmark:** sell-now, fixed 3/6-month exits, hold/redemption khi cùng `H`; oracle chỉ là diagnostic regret.
8. **Statistics:** primary non-overlap; nếu làm rolling robustness, báo overlap và dùng block bootstrap/HAC phù hợp. Không coi mỗi tháng chồng lấn là quan sát độc lập.

## 8. Tiêu chuẩn để bài được nộp với lời kết đúng

Một báo cáo tốt có thể kết luận: “quy tắc được mô tả, tái lập và có/không bền trước sensitivity trong tầng bằng chứng đã nêu.” Nó không được kết luận “chiến lược kiếm lời ở Việt Nam” nếu chỉ có `MODEL_ONLY` hay `CURVE_LEVEL_SIMULATION`.

Để nâng lên `EXECUTABLE_EVIDENCE`, cần tối thiểu một dataset per-bond hợp pháp có quote/fill, calendar, coupon entitlement, cash rate và timestamp đủ đầy, rồi chạy OOS đã khóa protocol. Không có shortcut giao diện hay AI nào thay được bằng chứng đó.
