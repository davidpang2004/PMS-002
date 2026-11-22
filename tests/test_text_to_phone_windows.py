"""Windows "Text it": only hand the sms: link to Microsoft Phone Link. When
Chrome (or anything else) owns sms:, nothing is launched and the caller is
told it couldn't send (method "url_only") instead of a false success."""
import sys
import types
import unittest
from unittest import mock

import dms_server


def fake_winreg(values):
    """values: {(root, path, name): str}"""
    w = types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", HKEY_CLASSES_ROOT="HKCR")

    class Key:
        def __init__(self, root, path): self.root, self.path = root, path
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def OpenKey(root, path):
        if not any(r == root and p == path for (r, p, _n) in values):
            raise OSError("missing")
        return Key(root, path)

    def QueryValueEx(key, name):
        try:
            return values[(key.root, key.path, name)], 1
        except KeyError:
            raise OSError("missing")
    w.OpenKey, w.QueryValueEx = OpenKey, QueryValueEx
    return w


UC = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\sms\UserChoice"


class WindowsTextTests(unittest.TestCase):
    def run_win(self, values):
        with mock.patch.dict(sys.modules, {"winreg": fake_winreg(values)}), \
             mock.patch.object(dms_server.sys, "platform", "win32"), \
             mock.patch("subprocess.Popen") as popen:
            res = dms_server._text_to_phone("+15551234567", "hello")
        return res, popen

    def test_chrome_owns_sms_nothing_launched(self):
        (ok, method, _), popen = self.run_win({("HKCU", UC, "ProgId"): "ChromeHTML"})
        self.assertEqual(method, "url_only")
        popen.assert_not_called()

    def test_no_handler_nothing_launched(self):
        (ok, method, _), popen = self.run_win({})
        self.assertEqual(method, "url_only")
        popen.assert_not_called()

    def test_phone_link_gets_the_sms_link(self):
        pid = "AppXabc123"
        (ok, method, _), popen = self.run_win({
            ("HKCU", UC, "ProgId"): pid,
            ("HKCR", pid + r"\Application", "AppUserModelID"): "Microsoft.YourPhone_8wekyb3d8bbwe!App",
        })
        self.assertEqual(method, "phone_link")
        popen.assert_called_once()
        self.assertIn("sms:+15551234567", " ".join(popen.call_args[0][0]))


if __name__ == "__main__":
    unittest.main()
