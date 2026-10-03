#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit tests for Xamvn parser using local HTML fixtures
"""

import os
import unittest
from xamvn_bot import XamvnBot

class TestXamvnParser(unittest.TestCase):
    def setUp(self):
        self.bot = XamvnBot(use_cache=False)
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.html_dir = os.path.join(self.base_dir, "html")

    def test_login_csrf_extraction(self):
        login_file = os.path.join(self.html_dir, "login.html")
        self.assertTrue(os.path.exists(login_file), f"File {login_file} does not exist")
        
        with open(login_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        
        csrf = self.bot.extract_csrf_from_html(content)
        self.assertIsNotNone(csrf, "CSRF token should not be None")
        self.assertTrue("," in csrf, "CSRF token should contain timestamp and hash")

    def test_reply_form_extraction(self):
        reply_file = os.path.join(self.html_dir, "reply.html")
        self.assertTrue(os.path.exists(reply_file), f"File {reply_file} does not exist")

        with open(reply_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        action, inputs = self.bot.extract_reply_form_details(content)
        self.assertIsNotNone(action, "Reply action URL should not be None")
        self.assertIn("add-reply", action, "Action URL should contain add-reply")
        self.assertIn("_xfToken", inputs, "Inputs should contain _xfToken")

if __name__ == "__main__":
    unittest.main()
