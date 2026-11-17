# Báo Cáo Hoàn Thiện Demo: Xác Định Thời Điểm & Tỷ Lệ Bán Tối Ưu (Perfect Foresight)

> [!WARNING]
> **CẢNH BÁO BẮT BUỘC VỀ PHẠM VI:**
> Đây là bản demo thử nghiệm trong điều kiện giả định dữ liệu tương lai đã được biết trước (Perfect Foresight). Kết quả nhằm minh họa cơ chế tìm điểm bán tối ưu $(t^*, q^*)$, không phải dự báo thực tế và **tuyệt đối không đại diện cho kết luận cuối cùng của mô hình nghiên cứu chính (RYC V3)**.

---

## 1. Tóm Tắt Khung Nghiệp Vụ & Bài Toán Tối Ưu Demo

### Biến Quyết Định:
- **$t$ (hoặc $s$):** Thời điểm thực hiện bán vị thế ($s \in [0, H]$ theo tháng).
- **$q$:** Tỷ lệ/khối lượng vị thế bán ra ($q \in [\text{min\_sale\_pct}, \text{max\_sale\_pct}]$).

### Hàm Mục Tiêu $W(H)$:
Hàm mục tiêu tối đa hóa tổng giá trị tích lũy quy về thời điểm đáo hạn cuối kỳ $H$:
$$W(H \mid s, q) = \text{SaleNet}(s, q) \cdot e^{r_{\text{cash}} \cdot (H - s)} + (1 - q) \cdot P(H) + \sum_{m=1}^H \left[ \text{Coupon}(m) - \text{HoldingCost}(m) - \text{RiskPenalty}(m) \right] \cdot e^{r_{\text{cash}} \cdot (H - m)}$$

Trong đó:
- $\text{SaleNet}(s, q) = q \cdot P(s) \cdot \left[1 - \text{Fee} - \text{Tax} - \text{MarketImpact}(q)\right]$
- Tiền bán sớm được tái đầu tư theo lãi suất tiền mặt $r_{\text{cash}}$ đến kỳ $H$.
- Phần vị thế còn lại $(1 - q)$ được bảo lưu và quy đổi theo giá trị $P(H)$ tại cuối kỳ $H$.
- Chi phí nắm giữ và phạt rủi ro chỉ áp dụng trên phần quy mô vị thế còn nắm giữ tại từng tháng.

### Điểm Tối Ưu $(t^*, q^*)$:
$$(t^*, q^*) = \arg\max_{(s, q) \in \mathcal{S} \times \mathcal{Q}} W(H \mid s, q)$$

---

## 2. Các Tập Tin Đã Hoàn Thiện & Cập Nhật

1. **[`ryc_perfect_foresight_demo_model.js`](file:///c:/Users/loitd/OneDrive/Desktop/demo/ryc_perfect_foresight_demo_model.js)**:
   - Module tính toán độc lập, chạy được trên cả Node.js và Trình duyệt.
   - Quản lý tập trung các cấu hình tham số, 4 preset kịch bản (`base`, `low_growth`, `high_growth`, `adverse_shock`).
   - Duyệt lưới toàn diện các quyết định bán thật `q>0`, tính riêng `hold_to_h` cho phương án giữ đến cuối kỳ, tính dải gần tối ưu (`near_optimal`), ma trận độ nhạy (`sensitivity`), so sánh chiến lược và sinh dữ liệu giải thích nguyên nhân.

2. **[`ryc_perfect_foresight_demo.html`](file:///c:/Users/loitd/OneDrive/Desktop/demo/ryc_perfect_foresight_demo.html)**:
   - Giao diện Dashboard Dark-Theme chuyên nghiệp, chuẩn bị sẵn sàng cho buổi thuyết trình/báo cáo ngày mai.
   - Bảng điều khiển tham số chi tiết (Sliders / Inputs / Presets).
   - 4 KPI cards nổi bật: Thời điểm bán $t^*$, Tỷ lệ bán $q^*$, Giá trị mục tiêu $W(H)$, Lợi ích tăng thêm (Lift).
   - Cụm 4 biểu đồ trực quan (SVG thuần responsive): Đường giá, Hàm mục tiêu $W(H)$, Ma trận Heatmap $(s, q)$, Biểu đồ so sánh chiến lược.
   - Biểu đồ phân tích độ nhạy (Sensitivity Analysis) và Hộp giải thích quan hệ nhân quả tự động (Dynamic Causal Reasoning).
   - Bảng so sánh chi tiết các phương án tham chiếu (Bán ngay, Bán sớm, Tối ưu, Bán muộn, Giữ đến $H$).

3. **[`ryc_perfect_foresight_demo_tests.cjs`](file:///c:/Users/loitd/OneDrive/Desktop/demo/ryc_perfect_foresight_demo_tests.cjs)**:
   - Bộ 10 kiểm thử tự động (Unit Tests) xác thực tính đúng đắn của logic kinh tế, tính tái lập (deterministic), tính nhất quán giữa KPI/Chart/Optimum, và kiểm tra tính nghiêm ngặt của văn phong cảnh báo disclaimer.

---

## 3. Kết Quả Kiểm Thử (10/10 Test Passed)

```json
{
  "perfect_foresight_demo": "pass",
  "checks": [
    "rising low-cost path waits",
    "high holding cost sells early",
    "high growth moves optimum later",
    "adverse shock changes timing",
    "boundary grid excludes q=0 from optimisation",
    "q=0 kept only as hold-to-H reference",
    "no feasible/invalid inputs fail closed",
    "near-optimal region",
    "surface/KPI consistency",
    "deterministic repeated run",
    "UI warning and claim hygiene"
  ]
}
```

---

## 4. Hướng Dẫn Truy Cập & Trình Diễn

- Mở trực tiếp file [`ryc_perfect_foresight_demo.html`](file:///c:/Users/loitd/OneDrive/Desktop/demo/ryc_perfect_foresight_demo.html) bằng bất kỳ trình duyệt nào (Chrome, Edge, Firefox).
- Thao tác nhanh các Kịch bản mẫu (Presets) ở góc trái để trình diễn phản ứng của mô hình trước các điều kiện thị trường khác nhau:
  - *Cơ sở (Base Case)*: Cân bằng giữa tăng trưởng và chi phí nắm giữ.
  - *Tăng trưởng cao (High Growth)*: Điểm tối ưu dịch về cuối chu kỳ để tối đa hóa thặng dư giá trị.
  - *Cú sốc bất lợi (Adverse Shock)*: Mô hình chủ động bán trước khi cú sốc xảy ra để bảo toàn vốn.


## 6. Bản sửa thuật toán sau rà soát

Lỗi đã phát hiện: lưới tối ưu cũ cho phép `q=0%`. Khi không bán gì, tháng bán không có ý nghĩa và `W(H)` giống nhau ở mọi tháng. Do bộ sắp xếp ưu tiên tháng nhỏ hơn khi hòa, dashboard thường báo `t*=0, q*=0%`, tạo cảm giác mô hình luôn chọn điểm ban đầu.

Bản sửa: `surface` tối ưu chỉ chứa các phương án bán thật `q>0`; phương án `q=0%` được giữ riêng trong `hold_to_h` và bảng so sánh “Giữ đến H”. Đây là cách phù hợp hơn với câu hỏi demo: “nếu bán thì bán khi nào và bán bao nhiêu”, đồng thời vẫn cho người xem thấy giữ đến cuối kỳ tốt/xấu hơn phương án bán tối ưu ra sao.
