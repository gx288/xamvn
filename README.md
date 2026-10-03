# Xamvn Forum Automation Bot 🤖

Công cụ tự động hoá đăng nhập và gửi bình luận lên diễn đàn XenForo (Xamvn) nhằm phục vụ bài tập thực hành kiểm thử web automation.

Script được tối ưu hóa toàn diện để chạy ổn định cả trên **máy Local** (Windows / macOS / Linux) và **GitHub Actions**.

---

## 🌟 Điểm nổi bật & Kỹ thuật giải quyết

1. **Vượt cơ chế chống Bot & Cloudflare TLS Fingerprint**:
   - Sử dụng `curl_cffi` mô phỏng đầy đủ TLS JA3/JA4 và HTTP/2 fingerprint của trình duyệt Chrome 120+, tránh bị Cloudflare chặn HTTP 403.
   - Tự động fallback sang thư viện `requests` tiêu chuẩn với headers trình duyệt thực tế nếu không có `curl_cffi`.

2. **Quản lý phiên (Session Caching)**:
   - Tự động lưu cache cookie (`.session.json`) sau khi đăng nhập thành công.
   - Các lần chạy tiếp theo sẽ kiểm tra phiên còn sống hay không. Nếu còn, tái sử dụng ngay lập tức mà không cần gửi lại request đăng nhập, giúp tốc độ thực thi dưới 2 giây và giảm thiểu rủi ro bị khóa tài khoản.

3. **Cơ chế chống Flood (Anti-Flood Auto-Retry)**:
   - XenForo áp dụng giới hạn tần suất gửi bài (Flood Control). Bot tự động bắt lỗi *"You must wait at least X seconds"*, đếm ngược thông minh và tự động gửi lại.

4. **Hỗ trợ 2 chế độ Online & Offline**:
   - **Online**: Kết nối trực tiếp, đăng nhập và đăng bài vào topic thực tế.
   - **Offline** (`--offline`): Phân tích DOM từ các file HTML đã lưu sẵn trong thư mục `html/` (`login.html`, `reply.html`) để trích xuất CSRF Token (`_xfToken`) và payload form mà không cần mạng.

5. **Bảo mật thông tin đăng nhập**:
   - File `.gitignore` được thiết lập kỹ càng: loại trừ vĩnh viễn file `account`, `html/account`, `.env`, `.session.json` khỏi git để không bao giờ bị lộ ra ngoài GitHub repository.

---

## 📁 Cấu trúc thư mục dự án

```text
xamvn/
├── .github/
│   └── workflows/
│       └── xamvn_bot.yml       # GitHub Actions workflow (hỗ trợ workflow_dispatch)
├── html/                       # Dữ liệu HTML mẫu & tài khoản
│   ├── account                 # File chứa ID/Pass test (đã được .gitignore bảo vệ)
│   ├── homepage.html           # Trang chủ mẫu
│   ├── login.html              # Trang đăng nhập mẫu (chứa _xfToken, form login)
│   └── reply.html              # Trang chủ đề mẫu (chứa form add-reply)
├── tests/
│   └── test_parser.py          # Unit test kiểm tra phân tích DOM trên các file HTML
├── .env.example                # File mẫu cấu hình biến môi trường
├── .gitignore                  # Cấu hình loại trừ tài khoản, cache, logs khỏi Git
├── requirements.txt            # Danh sách thư viện phụ thuộc
├── xamvn_bot.py                # File script chính thực thi toàn bộ logic
└── README.md                   # Hướng dẫn sử dụng chi tiết
```

---

## 🚀 Hướng dẫn cài đặt

### 1. Yêu cầu hệ thống
- Python 3.9 trở lên (đã kiểm thử tốt trên Python 3.11, 3.12).

### 2. Cài đặt thư viện phụ thuộc
```bash
pip install -r requirements.txt
```

---

## 💻 Cách chạy trên máy Local

Bot tự động tìm thông tin tài khoản theo thứ tự:
1. Tham số dòng lệnh (`-u`, `-p`)
2. Biến môi trường (`XAMVN_USERNAME`, `XAMVN_PASSWORD`)
3. File `html/account` (định dạng `id: ...` và `pass: ...`)

### Cách 1: Chạy tự động (Sử dụng file `html/account` có sẵn)
Chỉ cần chạy lệnh sau, bot sẽ tự nạp tài khoản từ `html/account`, đăng nhập và gửi bình luận vào thread mặc định `307074`:
```bash
python xamvn_bot.py
```

### Cách 2: Truyền tài khoản và chủ đề tùy ý qua CLI
```bash
python xamvn_bot.py -u "ten_tai_khoan" -p "mat_khau" -t "307074" -m "Nội dung bình luận của tôi"
```

### Cách 3: Sử dụng file cấu hình `.env`
Tạo file `.env` từ file mẫu:
```bash
cp .env.example .env
```
Điền thông tin vào `.env`, sau đó chạy:
```bash
python xamvn_bot.py
```

### Cách 4: Chế độ kiểm thử Offline (Không dùng mạng)
Dùng để kiểm tra khả năng bóc tách DOM từ `html/login.html` và `html/reply.html`:
```bash
python xamvn_bot.py --offline
```
Hoặc chạy Unit Test:
```bash
python -m unittest tests/test_parser.py
```

---

## ☁️ Cách thiết lập và chạy trên GitHub Actions

Script đã được cấu hình sẵn GitHub Actions tại `.github/workflows/xamvn_bot.yml`.

### Bước 1: Cấu hình GitHub Secrets
1. Vào repository trên GitHub -> **Settings** -> **Secrets and variables** -> **Actions**.
2. Nhấn **New repository secret** và thêm 2 biến:
   - `XAMVN_USERNAME`: Tên tài khoản / Email đăng nhập.
   - `XAMVN_PASSWORD`: Mật khẩu tài khoản.

### Bước 2: Lịch chạy tự động & Kích hoạt thủ công
- **Lịch chạy tự động (Cron Schedule)**: Đã cấu hình chạy tự động theo **giờ Việt Nam (UTC+7)** vào các mốc:
  - **18:15, 19:15, 20:15, 21:15, 22:15, 23:15, 00:15** (mỗi giờ 1 lần, tổng cộng **7 lần/ngày**).
  - Mỗi lần chạy tự động sẽ lấy link video tiếp theo trong dataset, tự động cộng `start_index` trong `config.json` và commit lưu tiến độ lên repository bằng `[skip ci]`.
- **Kích hoạt thủ công**:
  1. Vào tab **Actions** trên GitHub repository.
  2. Chọn workflow **Xamvn Forum Automation Bot**.
  3. Nhấn **Run workflow** (có thể để trống để bot tự lấy theo `config.json`).

---

## 🛡️ Lưu ý về An toàn & Git

- File `html/account`, `.session.json` và `posted_links.json` đã được đưa vào `.gitignore`. 
- **Tuyệt đối không dùng `git add -f html/account`** để tránh đẩy thông tin nhạy cảm lên GitHub.
- Trước khi push, bạn có thể kiểm tra danh sách file sẽ commit bằng lệnh:
  ```bash
  git status
  ```
