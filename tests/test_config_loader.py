"""설정 검증 회귀 테스트. 실제 계정이나 네트워크를 사용하지 않는다."""

import tempfile
import unittest
from pathlib import Path

from config_loader import ConfigError, load_config


VALID = """[smtp]
user = sender@example.com
password = smtp%#;secret
mail_to = first@example.com, second@example.com
[caster:gnssdata]
host = www.gnssdata.or.kr
port = 2101
user = test-user
password = ntrip%#;secret
"""


class ConfigLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.ini"

    def read(self, content):
        self.path.write_text(content, encoding="utf-8-sig")
        self.path.chmod(0o600)
        return load_config(self.path)

    def test_valid_bom_special_passwords_and_hidden_repr(self):
        result = self.read(VALID)
        self.assertEqual(result.smtp.password, "smtp%#;secret")
        self.assertEqual(result.casters[0].password, "ntrip%#;secret")
        self.assertEqual(len(result.smtp.mail_to), 2)
        self.assertNotIn("smtp%#;secret", repr(result))
        self.assertNotIn("ntrip%#;secret", repr(result))

    def test_empty_recipient_list_rejected(self):
        with self.assertRaises(ConfigError):
            self.read(VALID.replace("first@example.com, second@example.com", ", ,"))

    def test_invalid_hosts_rejected(self):
        for host in [r"www\.gnssdata.or.kr", "https://example.com", "example.com:2101", "bad host", "bad..host"]:
            with self.subTest(host=host), self.assertRaises(ConfigError):
                self.read(VALID.replace("www.gnssdata.or.kr", host))

    def test_ip_addresses_accepted(self):
        for host in ["127.0.0.1", "::1"]:
            with self.subTest(host=host):
                self.assertEqual(self.read(VALID.replace("www.gnssdata.or.kr", host)).casters[0].host, host)

    def test_parse_errors_do_not_expose_source(self):
        for content in ["private-secret-without-section", VALID + "private-secret-without-equals\n"]:
            with self.subTest(content_kind=content.startswith("[")):
                with self.assertRaises(ConfigError) as caught:
                    self.read(content)
                self.assertNotIn("private-secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_default_credentials_rejected(self):
        with self.assertRaises(ConfigError):
            self.read("[DEFAULT]\npassword = inherited-secret\n" + VALID.replace("password = ntrip%#;secret\n", ""))

    def test_missing_file_and_directory_report_config_error(self):
        for path in [self.path, Path(self.temp.name)]:
            with self.subTest(path_type=path.name), self.assertRaises(ConfigError):
                load_config(path)

    def test_invalid_port_duplicate_caster_and_placeholder(self):
        for content in [
            VALID.replace("port = 2101", "port = 70000"),
            VALID.replace("password = ntrip%#;secret", "password = ********"),
            VALID + "[caster:GNSSDATA]\nhost = localhost\nport = 2101\nuser = test\npassword = test\n",
        ]:
            with self.assertRaises(ConfigError):
                self.read(content)


if __name__ == "__main__":
    unittest.main()
