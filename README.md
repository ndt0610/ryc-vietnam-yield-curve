# Vietnam Government Bond Yield-Curve Research

**Mô phỏng quyết định giữ hay bán trái phiếu tại cùng một ngày mục tiêu**, với giả định đường cong lợi suất và chi phí được khai báo rõ. Dự án dùng Python cho định giá và so sánh dòng tiền; giao diện giúp kiểm tra kết quả, phương án kế tiếp và giới hạn của mô hình.

## Demo nổi bật: Perfect Foresight V3

Trong fixture tổng hợp hiện có, giữ đến ngày mục tiêu cho giá trị mô hình **96.704.399 VND**. Phương án bán đứng thứ hai kém **92.229 VND**, thấp hơn ngưỡng kinh tế minh họa **96.704 VND**. Vì vậy giao diện nói rõ: giữ đứng đầu về số học trong lưới đang xét, nhưng chênh lệch chưa vượt ngưỡng đã khai báo.

![Thẻ kết luận của workspace Python V3: giữ đến ngày mục tiêu đứng đầu trong lưới, nhưng chênh lệch 92.229 VND chưa vượt ngưỡng 96.704 VND.](demos/perfect-foresight/screenshots/decision-summary.png)

*Đây là fixture zero-coupon tổng hợp trên vị thế 100 triệu VND, không phải giá trái phiếu thị trường. Mở [README và cách chạy demo](demos/perfect-foresight/README.md) để xem so sánh phương án, đường cong và cách tính.*

### Dự án đang giải câu hỏi gì?

Khi đã khai báo ngày quyết định, ngày mục tiêu, dòng tiền trái phiếu, đường cong lợi suất, lãi tiền mặt và chi phí giao dịch, phương án giữ hoặc bán một/hai đợt nào có giá trị mô hình cao nhất tại cùng ngày mục tiêu? Kết quả chỉ áp dụng cho **lưới ngày, tỷ lệ và giả định đã đưa vào**.

### Người xem có thể kiểm tra gì?

- Kết luận chính cùng chênh lệch so với phương án kế tiếp trong toàn bộ lưới và ngưỡng kinh tế khai báo.
- Bảng giữ, bán một đợt và bán hai đợt tại cùng ngày mục tiêu.
- Timeline đường cong theo ngày, đường cong lợi suất và giá trái phiếu mô hình suy từ đường cong.
- Tab Regret với kết quả mô phỏng tổng hợp; kế hoạch ban đầu được giữ nguyên khi so với hindsight oracle.
- Chi tiết tiền đã nhận và giá trị mô hình của phần trái phiếu còn giữ.

![Biểu đồ và bảng so sánh giá trị tại ngày mục tiêu của phương án giữ, bán một đợt và bán hai đợt.](demos/perfect-foresight/screenshots/hold-vs-sell-comparison.png)

## Nghiên cứu RYC và các giả định

Engine chính tính giá trị cuối kỳ từ dòng tiền trái phiếu, coupon, tái đầu tư, settlement, phí và các kịch bản đường cong. Perfect Foresight V3 dùng cùng lõi Python; JavaScript dựng giao diện và gửi payload, không tự định giá thay cho engine.

Các ví dụ được công bố đều là `MODEL_ONLY / DECLARED_SYNTHETIC`. Chưa có báo giá/khớp lệnh thực tế, chưa xác minh đủ value date và day-count của đường cong HNX, và chưa có kiểm định ngoài mẫu. Khi ngày mục tiêu trước ngày đáo hạn, giá trị phần trái phiếu còn giữ là **model mark**, chưa phải tiền có thể thu. Đây không phải dự báo hoặc khuyến nghị giao dịch.

## Chạy demo Perfect Foresight V3

Cần Python 3.11 trở lên; demo không cần cài thư viện Python bên ngoài. Từ thư mục gốc repository:

```bash
python demos/perfect-foresight/app/ryc_v3_local_server.py --host 127.0.0.1 --port 8772
```

Mở <http://127.0.0.1:8772/ryc_perfect_foresight_workspace.html>. ECharts và font được lưu cùng demo để chạy trên localhost; phép tính được xử lý bởi Python V3.

Kiểm tra phần engine và giao diện:

```bash
python demos/perfect-foresight/tests/ryc_perfect_foresight_tests.py
node demos/perfect-foresight/tests/ryc_perfect_foresight_ui_tests.cjs
```

## Cấu trúc repository

- `ryc_v3_engine.py`, `ryc_v3_local_server.py` — engine và local API của dự án RYC.
- `demos/perfect-foresight/app/` — snapshot workspace Python V3 mới nhất, JSON ví dụ và dependency giao diện.
- `demos/perfect-foresight/tests/` — test engine và hợp đồng UI của demo.
- `demos/perfect-foresight/screenshots/` — ảnh kết luận, so sánh phương án và đường cong/giá mô hình.
- `demos/legacy-js-prototype/` — prototype JS cũ, được lưu riêng và không đại diện cho kết quả RYC hiện tại.

**Phạm vi:** nghiên cứu prototype cục bộ; không kết nối nguồn thị trường, không gửi lệnh giao dịch và không chứng minh hiệu quả đầu tư.
