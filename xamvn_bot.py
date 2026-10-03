#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xamvn Automation Bot
====================
Tự động đăng nhập và đăng bình luận lên diễn đàn XenForo (Xamvn).
Hỗ trợ chạy mượt mà trên môi trường Local (Windows/Linux/macOS) và GitHub Actions.

Tính năng nổi bật:
- Chống chặn Cloudflare TLS & HTTP/2 fingerprinting nhờ curl_cffi (impersonate Chrome 120+).
- Tự động nạp tài khoản theo thứ tự ưu tiên:
  1. CLI arguments: -u / --username, -p / --password
  2. Biến môi trường / .env: XAMVN_USERNAME, XAMVN_PASSWORD
  3. File credentials: html/account hoặc account
- Cấu hình qua config.json: thread mục tiêu, file data link, dòng bắt đầu (start_index), fallback message.
- Trình quản lý link: lấy link theo thứ tự từ start_index, ghép kèm 1 chuỗi 10 ký tự ngẫu nhiên xuống dòng.
- Lịch sử đăng bài (posted_links.json): ghi nhớ các link đã đăng để không bao giờ đăng trùng.
- Tự động fallback: nếu hết link sẽ tự động bình luận "up".
- Quản lý phiên thông minh (Session Cache): lưu cookie xf_user, xf_session để tránh đăng nhập lại liên tục.
- Tự động nhận diện & xử lý Anti-flood: nếu diễn đàn yêu cầu "chờ X giây", bot tự đếm ngược và thử lại.
"""

import os
import sys
import re
import json
import time
import string
import random
import argparse
import subprocess
import urllib.parse
from typing import Optional, Tuple, Dict, Any, Set

# Nạp dotenv nếu khả dụng
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# BeautifulSoup để phân tích DOM XenForo
from bs4 import BeautifulSoup

# Kiểm tra HTTP engine: ưu tiên curl_cffi để vượt Cloudflare
try:
    from curl_cffi import requests as curl_requests
    USE_CURL_CFFI = True
except ImportError:
    import requests as curl_requests
    USE_CURL_CFFI = False

# Đảm bảo UTF-8 trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def extract_video_id(url_or_item: Any) -> Optional[str]:
    """Trích xuất ID video từ URL hoặc item dict"""
    if not url_or_item:
        return None
    url = url_or_item
    if isinstance(url_or_item, dict):
        url = url_or_item.get("video_url") or url_or_item.get("thumb_url") or url_or_item.get("page_link")
    if not isinstance(url, str):
        return None
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        path = url
    base = os.path.basename(path)
    if not base:
        return None
    name, _ = os.path.splitext(base)
    name = re.sub(r'\.(?:fr|th|md)$', '', name, flags=re.IGNORECASE)
    return name or None


class LinkManager:
    """Quản lý danh sách link comment, chống trùng lặp và tự động căn chỉnh vị trí theo ID"""
    def __init__(
        self,
        data_file: str,
        history_file: str = "posted_links.json",
        start_index: int = 100,
        last_posted_id: Optional[str] = None,
        last_posted_url: Optional[str] = None,
        fallback_message: str = "up"
    ):
        self.data_file = data_file
        self.history_file = history_file
        self.start_index = max(0, int(start_index))
        self.last_posted_id = last_posted_id
        self.last_posted_url = last_posted_url
        self.fallback_message = fallback_message
        self.history = self._load_history()

    def _load_history(self) -> Set[str]:
        """Tải danh sách link đã từng đăng"""
        if os.path.isfile(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return set(data)
            except Exception:
                pass
        return set()

    def record_posted(self, link: str):
        """Lưu link đã đăng vào lịch sử"""
        if not link or link == self.fallback_message:
            return
        self.history.add(link)
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(list(self.history), f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def get_next_comment(self) -> Tuple[str, Optional[str], Optional[int], Optional[str]]:
        """
        Lấy nội dung comment tiếp theo.
        Trả về (nội dung comment, link đã chọn, index của link, video_id của link)
        Nếu hết link hoặc file không hợp lệ: trả về (fallback_message, None, None, None)
        """
        if not os.path.isfile(self.data_file):
            return self.fallback_message, None, None, None

        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            return self.fallback_message, None, None, None

        if not isinstance(items, list) or not items:
            return self.fallback_message, None, None, None

        # Kiểm tra và tự động căn chỉnh vị trí (re-align) nếu có last_posted_id hoặc last_posted_url
        target_id = self.last_posted_id
        target_url = self.last_posted_url

        if target_id or target_url:
            matched_idx = None
            # 1. Kiểm tra nhanh tại vị trí ngay trước start_index (start_index - 1)
            prev_idx = self.start_index - 1
            if 0 <= prev_idx < len(items):
                item_prev = items[prev_idx]
                item_link = None
                if isinstance(item_prev, dict):
                    item_link = item_prev.get("video_url") or item_prev.get("page_link") or item_prev.get("thumb_url")
                elif isinstance(item_prev, str):
                    item_link = item_prev.strip()
                item_id = extract_video_id(item_prev)

                if (target_id and item_id == target_id) or (target_url and item_link == target_url):
                    matched_idx = prev_idx

            # 2. Nếu vị trí start_index - 1 không khớp, tìm kiếm toàn bộ danh sách để căn chỉnh lại
            if matched_idx is None:
                for idx, item in enumerate(items):
                    item_link = None
                    if isinstance(item, dict):
                        item_link = item.get("video_url") or item.get("page_link") or item.get("thumb_url")
                    elif isinstance(item, str):
                        item_link = item.strip()
                    item_id = extract_video_id(item)

                    if (target_id and item_id == target_id) or (target_url and item_link == target_url):
                        matched_idx = idx
                        break

                if matched_idx is not None:
                    old_start = self.start_index
                    self.start_index = matched_idx + 1
                    print(f"[{time.strftime('%H:%M:%S')}] 🔄 [AUTO-ALIGN] Tìm thấy video đã đăng gần nhất (ID: '{target_id or 'N/A'}', URL: '{target_url or 'N/A'}') tại index {matched_idx}. Tự động điều chỉnh start_index: {old_start} -> {self.start_index}")

        # Bắt đầu duyệt từ vị trí start_index
        for idx in range(self.start_index, len(items)):
            item = items[idx]
            link = None
            if isinstance(item, dict):
                link = item.get("video_url") or item.get("page_link") or item.get("thumb_url")
            elif isinstance(item, str):
                link = item.strip()

            if link and link not in self.history:
                vid_id = extract_video_id(item)
                comment_text = link
                return comment_text, link, idx, vid_id

        # Đã đăng hết link hoặc không còn link mới
        return self.fallback_message, None, None, None



class XamvnBot:
    def __init__(
        self,
        base_url: str = "https://xamvn.lifestyle",
        session_file: str = ".session.json",
        use_cache: bool = True,
        verbose: bool = False
    ):
        self.base_url = base_url.rstrip("/")
        self.session_file = session_file
        self.use_cache = use_cache
        self.verbose = verbose
        self.session = self._create_session()
        self.logged_in = False
        self.current_user = None

    def _create_session(self):
        """Khởi tạo session với browser fingerprint thực tế"""
        if USE_CURL_CFFI:
            sess = curl_requests.Session(impersonate="chrome120")
        else:
            sess = curl_requests.Session()
            sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            })
        return sess

    def log(self, msg: str, level: str = "INFO"):
        """In thông điệp có format chuẩn"""
        icons = {
            "INFO": "ℹ️ ",
            "SUCCESS": "✅ ",
            "WARN": "⚠️ ",
            "ERROR": "❌ ",
            "DEBUG": "🔍 "
        }
        if level == "DEBUG" and not self.verbose:
            return
        print(f"[{time.strftime('%H:%M:%S')}] {icons.get(level, '')}{msg}")

    # =========================================================================
    # QUẢN LÝ TÀI KHOẢN & CACHE COOKIES
    # =========================================================================

    @staticmethod
    def load_credentials_from_file(file_path: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Tìm và đọc tài khoản từ file (hỗ trợ nhiều định dạng: id/pass, username/password, etc.)
        """
        search_paths = []
        if file_path:
            search_paths.append(file_path)
        search_paths.extend([
            os.path.join("html", "account"),
            "account",
            os.path.join("..", "html", "account"),
            "account.txt"
        ])

        for path in search_paths:
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()

                    # Tìm dạng id: xxx / pass: yyy
                    u_match = re.search(r'(?:id|username|user|taikhoan)\s*:\s*([^\r\n]+)', content, re.IGNORECASE)
                    p_match = re.search(r'(?:pass|password|matkhau)\s*:\s*([^\r\n]+)', content, re.IGNORECASE)

                    if u_match and p_match:
                        return u_match.group(1).strip(), p_match.group(1).strip()

                    # Fallback dạng 2 dòng liên tiếp
                    lines = [line.strip() for line in content.splitlines() if line.strip()]
                    if len(lines) >= 2:
                        return lines[0], lines[1]
                except Exception:
                    continue

        return None, None

    def save_session_cookies(self):
        """Lưu cookies vào file để tái sử dụng"""
        try:
            cookies = {}
            if hasattr(self.session.cookies, "get_dict"):
                cookies = self.session.cookies.get_dict()
            elif hasattr(self.session.cookies, "items"):
                cookies = dict(self.session.cookies.items())

            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2)
            self.log(f"Đã lưu session cache vào {self.session_file}", "DEBUG")
        except Exception as e:
            self.log(f"Không thể lưu session cache: {e}", "DEBUG")

    def load_session_cookies(self) -> bool:
        """Đọc cookies từ file cache nếu còn tồn tại"""
        if not self.use_cache or not os.path.isfile(self.session_file):
            return False
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)

            if not cookies or "xf_user" not in cookies:
                return False

            for name, val in cookies.items():
                self.session.cookies.set(name, val, domain="xamvn.lifestyle")
            self.log("Đã nạp thông tin phiên từ session cache.", "DEBUG")
            return True
        except Exception as e:
            self.log(f"Lỗi khi đọc session cache: {e}", "DEBUG")
            return False

    # =========================================================================
    # PHÂN TÍCH DOM (HTML PARSING) - DÙNG CHUNG CHO CẢ ONLINE VÀ OFFLINE
    # =========================================================================

    @staticmethod
    def extract_csrf_from_html(html_text: str) -> Optional[str]:
        """Trích xuất _xfToken hoặc data-csrf từ trang XenForo"""
        soup = BeautifulSoup(html_text, "html.parser")
        token_input = soup.find("input", {"name": "_xfToken"})
        if token_input and token_input.get("value"):
            return token_input["value"].strip()

        html_tag = soup.find("html")
        if html_tag and html_tag.get("data-csrf"):
            return html_tag["data-csrf"].strip()

        match = re.search(r'data-csrf="([^"]+)"', html_text)
        if match:
            return match.group(1)

        match = re.search(r'name="_xfToken"\s+value="([^"]+)"', html_text)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def extract_reply_form_details(html_text: str) -> Tuple[Optional[str], Dict[str, str]]:
        """Trích xuất form action và các input ẩn của form trả lời XenForo"""
        soup = BeautifulSoup(html_text, "html.parser")
        reply_form = soup.find("form", action=lambda x: x and "add-reply" in x)
        if not reply_form:
            return None, {}

        action_url = reply_form.get("action", "")
        inputs = {}
        for inp in reply_form.find_all("input"):
            name = inp.get("name")
            val = inp.get("value", "")
            if name:
                inputs[name] = val

        return action_url, inputs

    # =========================================================================
    # XÁC THỰC & ĐĂNG NHẬP
    # =========================================================================

    def check_is_logged_in(self) -> bool:
        """Kiểm tra xem session hiện tại đã đăng nhập thành công hay chưa"""
        try:
            resp = self.session.get(f"{self.base_url}/", timeout=15)
            if resp.status_code == 200:
                if 'data-logged-in="true"' in resp.text:
                    self.logged_in = True
                    match_user = re.search(r'data-current-user="([^"]+)"', resp.text)
                    if match_user:
                        self.current_user = match_user.group(1)
                    return True
        except Exception as e:
            self.log(f"Lỗi kiểm tra trạng thái login: {e}", "DEBUG")
        return False

    def login(self, username: str, password: str) -> bool:
        """Thực hiện đăng nhập XenForo bằng username/password"""
        self.log(f"Tiến hành xác thực tài khoản: {username}")

        # Thử khôi phục từ cache trước
        if self.load_session_cookies():
            if self.check_is_logged_in():
                if not self.current_user or self.current_user.lower() == username.lower():
                    self.log(f"Tái sử dụng session còn hiệu lực (Tài khoản: {self.current_user or username})", "SUCCESS")
                    return True
                else:
                    self.log(f"Session cũ thuộc tài khoản [{self.current_user}], không khớp [{username}]. Đăng xuất và đăng nhập mới...", "DEBUG")
                    self.session.cookies.clear()
            else:
                self.log("Session cache đã hết hạn, tiến hành đăng nhập mới...", "DEBUG")

        # 1. Tải trang login để lấy CSRF token
        login_page_url = f"{self.base_url}/login/"
        try:
            r_page = self.session.get(login_page_url, timeout=15)
        except Exception as e:
            self.log(f"Không thể kết nối đến trang đăng nhập {login_page_url}: {e}", "ERROR")
            return False

        if r_page.status_code != 200:
            self.log(f"Tải trang đăng nhập thất bại, mã HTTP: {r_page.status_code}", "ERROR")
            return False

        csrf_token = self.extract_csrf_from_html(r_page.text)
        if not csrf_token:
            self.log("Không tìm thấy CSRF Token (_xfToken) trong trang đăng nhập!", "ERROR")
            return False

        self.log(f"Đã lấy CSRF Token: {csrf_token[:15]}...", "DEBUG")

        # 2. Gửi yêu cầu đăng nhập POST /login/login
        login_post_url = f"{self.base_url}/login/login"
        post_payload = {
            "login": username,
            "password": password,
            "remember": "1",
            "_xfRedirect": f"{self.base_url}/",
            "_xfToken": csrf_token,
            "_xfResponseType": "json"
        }
        headers = {
            "Referer": login_page_url,
            "Origin": self.base_url,
            "X-Requested-With": "XMLHttpRequest"
        }

        try:
            r_post = self.session.post(login_post_url, data=post_payload, headers=headers, timeout=20)
        except Exception as e:
            self.log(f"Lỗi gửi yêu cầu đăng nhập: {e}", "ERROR")
            return False

        # Phân tích phản hồi JSON
        try:
            resp_json = r_post.json()
            if resp_json.get("status") == "ok":
                self.logged_in = True
                self.current_user = username
                self.save_session_cookies()
                self.log(f"Đăng nhập thành công với tài khoản [{username}]!", "SUCCESS")
                return True
            elif resp_json.get("status") == "error":
                errors = resp_json.get("errors", ["Đăng nhập thất bại."])
                self.log(f"Đăng nhập thất bại: {'; '.join(errors)}", "ERROR")
                return False
        except Exception:
            pass

        # Phân tích phản hồi cookie
        if r_post.status_code in (200, 302, 303):
            cookies_dict = self.session.cookies.get_dict() if hasattr(self.session.cookies, "get_dict") else {}
            if "xf_user" in cookies_dict or any(c.name == "xf_user" for c in self.session.cookies):
                self.logged_in = True
                self.current_user = username
                self.save_session_cookies()
                self.log(f"Đăng nhập thành công với tài khoản [{username}] (qua Cookie)!", "SUCCESS")
                return True

        self.log("Đăng nhập không thành công, vui lòng kiểm tra lại tài khoản hoặc mật khẩu.", "ERROR")
        return False

    # =========================================================================
    # ĐĂNG BÌNH LUẬN VÀO BÀI VIẾT (POST REPLY)
    # =========================================================================

    def post_reply(self, thread_identifier: str, comment_text: str, max_flood_retries: int = 2) -> Tuple[bool, Optional[str]]:
        """
        Đăng bình luận vào một chủ đề (thread) với cơ chế tự động chống flood
        :param thread_identifier: Thread ID (vd: '307074') hoặc URL đầy đủ
        :param comment_text: Nội dung bình luận cần đăng
        :param max_flood_retries: Số lần thử lại tối đa nếu bị flood limit
        :return: (thành_công_hay_không, post_id_nếu_có)
        """
        if not self.logged_in:
            self.log("Bạn cần đăng nhập thành công trước khi đăng bình luận!", "ERROR")
            return False, None

        # Chuẩn hoá URL của thread
        if thread_identifier.startswith("http"):
            thread_url = thread_identifier
            match_id = re.search(r'/threads/(?:[^/]+\.)?(\d+)', thread_url)
            thread_id = match_id.group(1) if match_id else None
        else:
            thread_id = thread_identifier.strip("/ ")
            thread_url = f"{self.base_url}/threads/{thread_id}/"

        self.log(f"Đang mở chủ đề thảo luận: {thread_url}")

        for attempt in range(max_flood_retries + 1):
            try:
                r_thread = self.session.get(thread_url, timeout=20)
            except Exception as e:
                self.log(f"Không thể kết nối đến chủ đề: {e}", "ERROR")
                return False, None

            if r_thread.status_code != 200:
                self.log(f"Không thể truy cập chủ đề (HTTP {r_thread.status_code})", "ERROR")
                return False, None

            # Trích xuất form add-reply
            action_url, form_inputs = self.extract_reply_form_details(r_thread.text)
            if not action_url:
                self.log("Không tìm thấy form trả lời! Chủ đề có thể đã bị khóa hoặc tài khoản chưa đủ quyền.", "ERROR")
                return False, None

            if not action_url.startswith("http"):
                action_url = f"{self.base_url}{action_url}"

            self.log(f"Tìm thấy form trả lời. Endpoint: {action_url}")

            # Chuyển đổi xuống dòng thành <br> hoặc <p> cho HTML editor
            html_content = "".join([f"<p>{line}</p>" for line in comment_text.splitlines() if line.strip()])

            # Chuẩn bị payload POST
            post_data = dict(form_inputs)
            post_data["message"] = comment_text
            post_data["message_html"] = html_content
            post_data["_xfResponseType"] = "json"
            post_data["_xfWithData"] = "1"

            headers = {
                "Referer": thread_url,
                "Origin": self.base_url,
                "X-Requested-With": "XMLHttpRequest"
            }

            self.log(f"Đang gửi bình luận:\n{comment_text}\n(Lần gửi {attempt + 1})")

            try:
                r_reply = self.session.post(action_url, data=post_data, headers=headers, timeout=25)
            except Exception as e:
                self.log(f"Lỗi gửi bình luận: {e}", "ERROR")
                return False, None

            # Xử lý kết quả trả về
            try:
                reply_json = r_reply.json()
                if reply_json.get("status") == "ok":
                    html_resp = reply_json.get("html", {})
                    content_str = html_resp.get("content", "") if isinstance(html_resp, dict) else str(html_resp)
                    post_match = re.search(r'data-content="post-(\d+)"', content_str)
                    post_id = post_match.group(1) if post_match else None
                    post_info = f" (Post ID: {post_id})" if post_id else ""

                    self.log(f"ĐĂNG BÌNH LUẬN THÀNH CÔNG!{post_info}", "SUCCESS")
                    return True, post_id

                elif reply_json.get("status") == "error":
                    errors = reply_json.get("errors", [])
                    err_msg = "; ".join(errors) if errors else "Lỗi không xác định."

                    # Phát hiện yêu cầu flood limit: "You must wait at least X seconds..."
                    flood_match = re.search(r'(?:wait at least|chờ ít nhất)\s*(\d+)\s*(?:seconds|giây)', err_msg, re.IGNORECASE)
                    if flood_match and attempt < max_flood_retries:
                        wait_sec = int(flood_match.group(1)) + 1
                        self.log(f"Gặp giới hạn Flood Limit của diễn đàn. Đang tạm dừng {wait_sec} giây trước khi gửi lại...", "WARN")
                        time.sleep(wait_sec)
                        continue

                    self.log(f"Diễn đàn từ chối đăng bài: {err_msg}", "ERROR")
                    return False, None
            except Exception:
                pass

            if r_reply.status_code in (200, 302, 303):
                self.log("Đăng bình luận thành công (Mã phản hồi HTTP hợp lệ)!", "SUCCESS")
                return True, None

            self.log(f"Gửi bình luận không thành công. HTTP {r_reply.status_code}", "ERROR")
            return False, None

        return False, None

    # =========================================================================
    # CHẾ ĐỘ KIỂM THỬ OFFLINE (HTML TEST)
    # =========================================================================

    def run_offline_test(self, html_dir: str = "html") -> bool:
        """
        Kiểm thử phân tích các file HTML có sẵn mà không cần kết nối mạng.
        Đáp ứng yêu cầu 'trong thư mục xamvn đã có đủ html các trang'.
        """
        self.log("=== CHẠY CHẾ ĐỘ KIỂM THỬ OFFLINE TỪ CÁC FILE HTML ===", "INFO")
        all_ok = True

        login_file = os.path.join(html_dir, "login.html")
        reply_file = os.path.join(html_dir, "reply.html")

        # 1. Kiểm tra login.html
        if os.path.isfile(login_file):
            self.log(f"Đang kiểm tra phân tích DOM: {login_file}")
            with open(login_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            csrf = self.extract_csrf_from_html(content)
            if csrf:
                self.log(f"[PASS] Đã trích xuất CSRF Token từ login.html: {csrf}", "SUCCESS")
            else:
                self.log("[FAIL] Không trích xuất được CSRF Token từ login.html", "ERROR")
                all_ok = False
        else:
            self.log(f"Không tìm thấy file {login_file}", "WARN")

        # 2. Kiểm tra reply.html
        if os.path.isfile(reply_file):
            self.log(f"Đang kiểm tra phân tích DOM: {reply_file}")
            with open(reply_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            action, inputs = self.extract_reply_form_details(content)
            if action and "add-reply" in action:
                self.log(f"[PASS] Đã trích xuất Action URL: {action}", "SUCCESS")
                self.log(f"[PASS] Đã trích xuất {len(inputs)} trường input ẩn bao gồm _xfToken", "SUCCESS")
            else:
                self.log("[FAIL] Không trích xuất được form reply từ reply.html", "ERROR")
                all_ok = False
        else:
            self.log(f"Không tìm thấy file {reply_file}", "WARN")

        # 3. Kiểm tra file account
        user, pwd = self.load_credentials_from_file()
        if user and pwd:
            self.log(f"[PASS] Đã đọc thành công tài khoản: ID={user}, Pass={'*' * len(pwd)}", "SUCCESS")
        else:
            self.log("[WARN] Không tìm thấy file account hoặc không đọc được tài khoản", "WARN")

        # 4. Kiểm tra LinkManager và data file nếu có
        cfg = load_config_file("config.json")
        data_file = cfg.get("data_file", "data/videos_likes.json")
        if os.path.isfile(data_file):
            self.log(f"Đang kiểm tra data_file: {data_file}")
            lm = LinkManager(
                data_file=data_file,
                history_file=cfg.get("history_file", "posted_links.json"),
                start_index=cfg.get("start_index", 266),
                last_posted_id=cfg.get("last_posted_id"),
                last_posted_url=cfg.get("last_posted_url")
            )
            msg, link, idx, vid_id = lm.get_next_comment()
            if link:
                self.log(f"[PASS] LinkManager hoạt động tốt: Next Index={idx}, ID={vid_id}, Link={link}", "SUCCESS")
            else:
                self.log(f"[WARN] LinkManager không tìm thấy link kế tiếp (sử dụng fallback: {msg})", "WARN")

        self.log(f"Kết quả kiểm thử offline: {'HOÀN TOÀN ĐẠT' if all_ok else 'CÓ LỖI'}", "SUCCESS" if all_ok else "ERROR")
        return all_ok


# =============================================================================
# CLI ENTRYPOINT
# =============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Xamvn Automation Bot - Login và đăng comment bài tập"
    )
    parser.add_argument("-c", "--config", default="config.json", help="Đường dẫn đến file config.json")
    parser.add_argument("-u", "--username", help="Tài khoản / Email đăng nhập")
    parser.add_argument("-p", "--password", help="Mật khẩu đăng nhập")
    parser.add_argument("-t", "--thread", help="ID hoặc URL của chủ đề cần bình luận")
    parser.add_argument("-m", "--message", help="Nội dung bình luận cần đăng (nếu bỏ qua sẽ tự lấy từ config hoặc data_file)")
    parser.add_argument("-a", "--account-file", help="Đường dẫn đến file account (mặc định tìm html/account)")
    parser.add_argument("--base-url", default="https://xamvn.lifestyle", help="URL diễn đàn (mặc định: https://xamvn.lifestyle)")
    parser.add_argument("--offline", action="store_true", help="Chạy chế độ kiểm thử offline với các file HTML có sẵn")
    parser.add_argument("--no-cache", action="store_true", help="Không sử dụng session cache cũ")
    parser.add_argument("--loop", action="store_true", help="Chạy lặp lại định kỳ (theo interval_seconds)")
    parser.add_argument("--interval", type=int, help="Thời gian chờ giữa các lần đăng (giây, mặc định 300s = 5 phút)")
    parser.add_argument("--count", type=int, default=0, help="Số lần đăng tối đa trong vòng lặp (0 = vô tận, 1 = 1 lần)")
    parser.add_argument("-v", "--verbose", action="store_true", help="In log chi tiết")
    return parser.parse_args()


def load_config_file(config_path: str = "config.json") -> Dict[str, Any]:
    """Tải thiết lập từ config.json"""
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Lỗi đọc file cấu hình {config_path}: {e}")
    return {}


def update_config_progress(
    config_path: str,
    new_index: int,
    video_id: Optional[str] = None,
    video_url: Optional[str] = None
):
    """Cập nhật start_index, last_posted_id, last_posted_url mới vào file config.json để lưu tiến độ tự động"""
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["start_index"] = new_index
            if video_id:
                cfg["last_posted_id"] = video_id
            if video_url:
                cfg["last_posted_url"] = video_url
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Không thể cập nhật tiến độ vào {config_path}: {e}")


def update_config_start_index(config_path: str, new_index: int):
    """Hàm tương thích cũ - gọi update_config_progress"""
    update_config_progress(config_path, new_index)


def check_and_update_redirect(cfg: Dict[str, Any], config_path: str = "config.json") -> Dict[str, Any]:
    """
    Kiểm tra chuyển hướng tên miền từ xamvn.link.
    Nếu phát hiện tên miền mới đổi 3 lần liên tiếp thì ghi vào config.json và cập nhật base_url.
    """
    check_url = cfg.get("redirect_check_url", "https://xamvn.link")
    current_base = cfg.get("base_url", "https://xamvn.lifestyle").rstrip("/")
    pending_url = cfg.get("pending_redirect_url", "").rstrip("/")
    redirect_count = cfg.get("redirect_count", 0)

    print(f"[{time.strftime('%H:%M:%S')}] 🔍 Kiểm tra chuyển hướng tên miền tại: {check_url}")

    detected_base = None
    # Thử kết nối theo dõi redirect (hỗ trợ cả https và http)
    targets = [check_url]
    if check_url.startswith("https://"):
        targets.append(check_url.replace("https://", "http://"))
    elif check_url.startswith("http://"):
        targets.append(check_url.replace("http://", "https://"))

    for target in targets:
        try:
            if USE_CURL_CFFI:
                r = curl_requests.get(target, impersonate="chrome120", allow_redirects=True, timeout=12)
            else:
                r = curl_requests.get(target, allow_redirects=True, timeout=12)

            if r.status_code in (200, 301, 302, 303, 307, 308):
                p = urllib.parse.urlparse(r.url)
                if p.netloc and "xamvn" in p.netloc:
                    detected_base = f"{p.scheme}://{p.netloc}".rstrip("/")
                    break
        except Exception:
            continue

    if not detected_base:
        print(f"[{time.strftime('%H:%M:%S')}] ℹ️ Không phát hiện chuyển hướng mới từ {check_url}. Sử dụng base_url: {current_base}")
        return cfg

    clean_detected = detected_base.rstrip("/")
    clean_current = current_base.rstrip("/")
    config_changed = False

    if clean_detected != clean_current:
        print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Phát hiện tên miền đích mới: {clean_detected} (khác {clean_current})")
        if clean_detected == pending_url:
            redirect_count += 1
            cfg["redirect_count"] = redirect_count
            print(f"[{time.strftime('%H:%M:%S')}] 🔄 Đã phát hiện đổi tên miền {redirect_count}/3 lần liên tiếp.")
        else:
            cfg["pending_redirect_url"] = clean_detected
            cfg["redirect_count"] = 1
            print(f"[{time.strftime('%H:%M:%S')}] 👁️ Bắt đầu theo dõi tên miền mới: {clean_detected} (1/3 lần)")
        config_changed = True

        if cfg["redirect_count"] >= 3:
            print(f"[{time.strftime('%H:%M:%S')}] 🚨 ĐÃ ĐẠT 3 LẦN ĐỔI TÊN MIỀN! Cập nhật BASE_URL chính thức thành: {clean_detected}")
            cfg["base_url"] = clean_detected
            cfg["pending_redirect_url"] = ""
            cfg["redirect_count"] = 0

            # Cập nhật thread_target nếu có thread_id
            thread_id = cfg.get("thread_id") or "307571"
            cfg["thread_target"] = f"{clean_detected}/threads/{thread_id}/"
            config_changed = True
    else:
        # Nếu trùng khớp với base_url hiện tại thì reset bộ đếm theo dõi
        if redirect_count > 0 or pending_url:
            cfg["pending_redirect_url"] = ""
            cfg["redirect_count"] = 0
            config_changed = True

    if config_changed and os.path.isfile(config_path):
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            print(f"[{time.strftime('%H:%M:%S')}] 💾 Đã lưu cấu hình redirect mới vào {config_path}")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️ Lỗi ghi file cấu hình: {e}")

    return cfg


def write_github_summary(title: str, content: str):
    """Ghi báo cáo ra GitHub Actions Step Summary nếu chạy trên CI"""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path and os.path.exists(os.path.dirname(summary_path)):
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"### {title}\n\n{content}\n\n")
        except Exception:
            pass


def main():
    args = parse_args()

    print("=" * 65)
    print("🤖 XAMVN FORUM AUTOMATION BOT")
    print(f"HTTP Engine: {'curl_cffi (Chrome TLS Impersonate)' if USE_CURL_CFFI else 'requests (Standard)'}")
    print("=" * 65)

    # Đọc config.json
    cfg = load_config_file(args.config)

    # Kiểm tra chuyển hướng tên miền từ xamvn.link
    cfg = check_and_update_redirect(cfg, args.config)

    # Xác định base_url linh hoạt từ config
    base_url = args.base_url if args.base_url != "https://xamvn.lifestyle" else (cfg.get("base_url") or os.getenv("XAMVN_BASE_URL", "https://xamvn.lifestyle"))

    bot = XamvnBot(
        base_url=base_url,
        use_cache=not args.no_cache,
        verbose=args.verbose
    )

    # Chế độ kiểm thử offline từ các file HTML
    if args.offline:
        success = bot.run_offline_test()
        write_github_summary("Kiểm thử Offline", f"Trạng thái: {'THÀNH CÔNG' if success else 'THẤT BẠI'}")
        sys.exit(0 if success else 1)

    # 1. Xác định thông tin đăng nhập
    username = args.username or os.getenv("XAMVN_USERNAME")
    password = args.password or os.getenv("XAMVN_PASSWORD")

    if not username or not password:
        file_u, file_p = bot.load_credentials_from_file(args.account_file)
        username = username or file_u
        password = password or file_p

    if not username or not password:
        bot.log("LỖI: Không tìm thấy thông tin tài khoản đăng nhập!", "ERROR")
        bot.log("Các phương thức cung cấp tài khoản:", "INFO")
        bot.log("  1. Tham số: python xamvn_bot.py -u <username> -p <password>", "INFO")
        bot.log("  2. Biến môi trường: XAMVN_USERNAME, XAMVN_PASSWORD", "INFO")
        bot.log("  3. File: html/account (chứa dòng 'id: ...' và 'pass: ...')", "INFO")
        sys.exit(1)

    # 2. Xác định thread mục tiêu
    thread_target = (
        args.thread
        or os.getenv("XAMVN_THREAD_ID")
        or cfg.get("thread_target")
        or "307571"
    )

    # 3. Quản lý link và cấu hình lặp
    data_file = cfg.get("data_file", "data/videos_likes.json")
    start_index = cfg.get("start_index", 100)
    last_posted_id = cfg.get("last_posted_id")
    last_posted_url = cfg.get("last_posted_url")
    history_file = cfg.get("history_file", "posted_links.json")
    fallback_message = cfg.get("fallback_message", "up")
    configured_msg = args.message or os.getenv("XAMVN_MESSAGE") or cfg.get("default_message")

    interval_sec = args.interval or cfg.get("interval_seconds", 300)
    is_loop = args.loop or cfg.get("loop", False)
    max_count = args.count or cfg.get("count", 0)

    link_mgr = LinkManager(
        data_file=data_file,
        history_file=history_file,
        start_index=start_index,
        last_posted_id=last_posted_id,
        last_posted_url=last_posted_url,
        fallback_message=fallback_message
    )

    # 4. Đăng nhập một lần trước khi bắt đầu vòng lặp
    if not bot.login(username, password):
        bot.log("Quá trình đăng nhập thất bại. Dừng thực thi.", "ERROR")
        write_github_summary("Đăng nhập thất bại", f"Không thể đăng nhập với user: `{username}`")
        sys.exit(1)

    post_count = 0
    while True:
        post_count += 1
        bot.log(f"--- BẮT ĐẦU LẦN ĐĂNG #{post_count} ---", "INFO")

        chosen_link = None
        chosen_idx = None
        chosen_id = None

        if configured_msg:
            current_message = configured_msg
        else:
            current_message, chosen_link, chosen_idx, chosen_id = link_mgr.get_next_comment()
            if chosen_link:
                bot.log(f"Đã chọn link từ data_file (Dòng/Index #{chosen_idx}, ID: {chosen_id or 'N/A'}): {chosen_link}")
            else:
                bot.log(f"Dữ liệu link đã dùng hết hoặc không khả dụng. Sử dụng tin nhắn fallback: \"{current_message}\"", "WARN")

        # Đăng bình luận
        time.sleep(1.0)
        reply_ok, post_id = bot.post_reply(thread_target, current_message)

        if reply_ok:
            if chosen_link:
                link_mgr.record_posted(chosen_link)
                bot.log(f"Đã lưu link vào lịch sử {history_file} (Tổng số link đã đăng: {len(link_mgr.history)})", "SUCCESS")
                if chosen_idx is not None and args.config:
                    update_config_progress(args.config, chosen_idx + 1, chosen_id, chosen_link)
                    bot.log(f"Đã cập nhật tiến độ vào {args.config} (start_index={chosen_idx + 1}, id={chosen_id or 'N/A'})", "SUCCESS")

            bot.log("=" * 65)
            bot.log(f"LẦN ĐĂNG #{post_count} THÀNH CÔNG!", "SUCCESS")
            bot.log("=" * 65)

            write_github_summary(
                f"Lần #{post_count} - Kết quả chạy Bot",
                f"✅ **Đăng bình luận thành công!**\n- User: `{username}`\n- Thread: `{thread_target}`\n- Post ID: `{post_id or 'N/A'}`\n- Message:\n```\n{current_message}\n```"
            )
        else:
            bot.log(f"Lần đăng #{post_count} không thành công.", "ERROR")
            write_github_summary(f"Lần #{post_count} - Lỗi", f"❌ Không thể đăng bình luận vào thread `{thread_target}`")

        # Kiểm tra điều kiện lặp
        if not is_loop:
            break

        if max_count > 0 and post_count >= max_count:
            bot.log(f"Đã hoàn thành đủ {max_count} lần đăng theo yêu cầu.", "SUCCESS")
            break

        bot.log(f"⏳ Tạm dừng {interval_sec} giây ({(interval_sec / 60):.1f} phút) trước lần đăng tiếp theo... (Nhấn Ctrl+C để dừng)", "INFO")
        try:
            for _ in range(interval_sec):
                time.sleep(1)
        except KeyboardInterrupt:
            bot.log("Người dùng đã dừng bot bằng Ctrl+C.", "WARN")
            break


if __name__ == "__main__":
    main()
