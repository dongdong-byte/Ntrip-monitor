"""Sourcetable 완전 수신 검증과 보조 목록 저장 테스트. 네트워크를 사용하지 않는다."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mount_cache
from ntrip.sourcetable import SourcetableFetcher, parse_response
from ntrip.chunked import ChunkedDecoder
from ntrip.models import NtripVersion


BODY = (
    "STR;ANHN-RTCM32;ANHN-RTCM32;RTCM 3.2;;2;GPS;KORREF;KOR;36.67;126.13;0;0;X;none;B;N;9600;\r\n"
    "STR;YNJU_RTCM30;YNJU_RTCM30;RTCM 3.0;;2;GPS;KORREF;KOR;36.0;127.0;0;0;X;none;B;N;9600;\r\n"
    "STR;ANHN-RTCM32;dup;RTCM 3.2;;2;GPS;KORREF;KOR;36.67;126.13;0;0;X;none;B;N;9600;\r\n"
    "ENDSOURCETABLE\r\n"
)


def v1_response(body=BODY, length=None):
    length = len(body.encode()) if length is None else length
    return (
        "SOURCETABLE 200 OK\r\n"
        "Server: NTRIP Caster 1.1\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {length}\r\n"
        "\r\n" + body
    ).encode()


def chunked_response(body=BODY, finish=True):
    data = body.encode()
    half = len(data) // 2
    chunks = b""
    for part in (data[:half], data[half:]):
        chunks += f"{len(part):x}\r\n".encode() + part + b"\r\n"
    if finish:
        chunks += b"0\r\n\r\n"
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: gnss/sourcetable\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + chunks
    )


class ParseResponseTests(unittest.TestCase):
    def test_invalid_content_length_rejected(self):
        for length in [-1, "+10", "1_000"]:
            with self.subTest(length=length):
                self.assertFalse(parse_response(v1_response(length=length)).complete)

    def test_end_marker_must_be_last(self):
        self.assertFalse(parse_response(v1_response(BODY + "STR;LATE;data\r\n")).complete)

    def test_chunk_trailer_must_finish(self):
        self.assertFalse(parse_response(chunked_response()[:-2]).complete)

    def test_bad_chunk_separator_rejected(self):
        raw = chunked_response().replace(b"\r\n0\r\n", b"XX0\r\n")
        self.assertFalse(parse_response(raw).complete)

    def test_fragmented_chunked_with_trailer(self):
        raw = chunked_response().partition(b"\r\n\r\n")[2]
        raw = raw[:-2] + b"X-Test: value\r\n\r\n"
        decoder = ChunkedDecoder()
        decoded = b"".join(decoder.feed(bytes([byte])) for byte in raw)
        self.assertTrue(decoder.finished)
        self.assertFalse(decoder.malformed)
        self.assertEqual(decoded, BODY.encode())

    def test_negative_chunk_size_rejected(self):
        decoder = ChunkedDecoder()
        decoder.feed(b"-1\r\nabc\r\n0\r\n\r\n")
        self.assertTrue(decoder.malformed)

    def test_complete_v1_with_content_length(self):
        result = parse_response(v1_response())
        self.assertTrue(result.complete, result.reason)
        # 중복 제거, 정렬, 밑줄 이름도 원본 그대로
        self.assertEqual(result.mounts, ["ANHN-RTCM32", "YNJU_RTCM30"])
        self.assertIn("ENDSOURCETABLE", result.reason)
        self.assertIn("Content-Length", result.reason)

    def test_complete_v2_without_content_length(self):
        raw = b"HTTP/1.1 200 OK\r\nContent-Type: gnss/sourcetable\r\n\r\n" + BODY.encode()
        result = parse_response(raw)
        self.assertTrue(result.complete, result.reason)

    def test_complete_chunked(self):
        result = parse_response(chunked_response())
        self.assertTrue(result.complete, result.reason)
        self.assertEqual(len(result.mounts), 2)

    def test_truncated_body_rejected(self):
        raw = v1_response()
        truncated = raw[: len(raw) - 40]
        result = parse_response(truncated)
        self.assertFalse(result.complete)
        self.assertIn("Content-Length", result.reason)

    def test_missing_end_marker_rejected(self):
        body = BODY.replace("ENDSOURCETABLE\r\n", "")
        result = parse_response(v1_response(body))
        self.assertFalse(result.complete)
        self.assertIn("ENDSOURCETABLE", result.reason)

    def test_unfinished_chunked_rejected(self):
        result = parse_response(chunked_response(finish=False))
        self.assertFalse(result.complete)
        self.assertIn("chunked", result.reason)

    def test_non_200_rejected(self):
        raw = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
        result = parse_response(raw)
        self.assertFalse(result.complete)
        self.assertIn("401", result.reason)

    def test_header_without_blank_line_rejected(self):
        result = parse_response(b"SOURCETABLE 200 OK\r\nServer: x\r\n")
        self.assertFalse(result.complete)

    def test_no_str_lines_rejected(self):
        result = parse_response(v1_response("ENDSOURCETABLE\r\n"))
        self.assertFalse(result.complete)


class FetcherOrderTests(unittest.TestCase):
    """인증 없이 먼저 요청하고, 실패할 때만 인증을 붙이는지 확인한다."""

    def make(self, auth="Authorization: Basic eDp5\r\n"):
        return SourcetableFetcher("caster.example", 2101, auth, 1.0)

    def test_total_read_deadline_stops_trickling_data(self):
        fetcher = self.make()
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.return_value = b"incomplete"
        with mock.patch("ntrip.sourcetable.socket.create_connection", return_value=sock), mock.patch(
            "ntrip.sourcetable.time.monotonic", side_effect=[0, 0.2, 1.1]
        ):
            raw = fetcher._raw_request("/", NtripVersion.V2, False)
        self.assertEqual(sock.recv.call_count, 1)
        self.assertFalse(parse_response(raw).complete)

    def test_no_auth_first_and_stop_on_success(self):
        fetcher = self.make()
        with mock.patch.object(
            fetcher, "_raw_request", return_value=v1_response()
        ) as raw:
            self.assertEqual(fetcher.fetch(), ["ANHN-RTCM32", "YNJU_RTCM30"])
        self.assertEqual(raw.call_count, 1)
        self.assertFalse(raw.call_args.args[2])

    def test_auth_used_after_no_auth_404(self):
        not_found = b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n"
        fetcher = self.make()
        responses = [not_found, not_found, v1_response()]
        with mock.patch.object(
            fetcher, "_raw_request", side_effect=responses
        ) as raw:
            self.assertEqual(len(fetcher.fetch()), 2)
        self.assertEqual([c.args[2] for c in raw.call_args_list], [False, False, True])

    def test_without_account_auth_variants_skipped(self):
        not_found = b"HTTP/1.1 404 Not Found\r\n\r\n"
        fetcher = self.make(auth="")
        with mock.patch.object(
            fetcher, "_raw_request", return_value=not_found
        ) as raw:
            self.assertEqual(fetcher.fetch(), [])
        self.assertEqual(raw.call_count, 2)


class MountCacheTests(unittest.TestCase):
    def test_list_command_fails_when_no_mounts_available(self):
        import liveness_main

        config = mock.Mock(casters=[mock.Mock(name="caster")])
        with mock.patch.object(liveness_main, "setup_logging"), mock.patch.object(
            liveness_main, "load_config", return_value=config
        ), mock.patch.object(liveness_main, "build_checker"), mock.patch.object(
            liveness_main, "resolve_mounts", return_value=[]
        ), mock.patch("sys.argv", ["liveness_main.py", "--list-mounts", "--source", "caster"]):
            with self.assertRaises(SystemExit) as caught:
                liveness_main.main()
        self.assertEqual(caught.exception.code, 1)

    def test_config_error_exits_nonzero(self):
        import liveness_main
        from config_loader import ConfigError

        with mock.patch.object(liveness_main, "setup_logging"), mock.patch.object(
            liveness_main, "load_config", side_effect=ConfigError("missing")
        ), mock.patch("sys.argv", ["liveness_main.py", "--list-mounts"]):
            with self.assertRaises(SystemExit) as caught:
                liveness_main.main()
        self.assertEqual(caught.exception.code, 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mounts" / "gnssdata.txt"

    def test_write_then_read_roundtrip(self):
        ok = mount_cache.write_mounts(
            self.path, ["B-RTCM32", "/A-RTCM30", "B-RTCM32", " "], "host:2101"
        )
        self.assertTrue(ok)
        self.assertEqual(mount_cache.read_mounts(self.path), ["A-RTCM30", "B-RTCM32"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_empty_list_keeps_existing_file(self):
        mount_cache.write_mounts(self.path, ["A-RTCM30"], "host:2101")
        self.assertFalse(mount_cache.write_mounts(self.path, [], "host:2101"))
        self.assertEqual(mount_cache.read_mounts(self.path), ["A-RTCM30"])

    def test_failed_replace_keeps_existing_file(self):
        mount_cache.write_mounts(self.path, ["A-RTCM30"], "host:2101")
        with mock.patch("mount_cache.os.replace", side_effect=OSError("disk")):
            ok = mount_cache.write_mounts(self.path, ["X-RTCM32"], "host:2101")
        self.assertFalse(ok)
        self.assertEqual(mount_cache.read_mounts(self.path), ["A-RTCM30"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_missing_file_returns_empty(self):
        self.assertEqual(mount_cache.read_mounts(self.path), [])

    def test_invalid_encoding_returns_empty(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"\xff\xfe\x80")
        self.assertEqual(mount_cache.read_mounts(self.path), [])

    def test_incomplete_response_falls_back_without_overwriting_cache(self):
        from liveness_main import LIVENESS_CONFIG, resolve_mounts

        mount_cache.write_mounts(self.path, ["Original-RTCM32"], "host:2101")
        before = self.path.read_bytes()
        fetcher = SourcetableFetcher("caster.example", 2101, "", 1)
        checker = mock.Mock()
        checker.fetch_mountpoints.side_effect = fetcher.fetch
        with mock.patch.object(fetcher, "_raw_request", return_value=v1_response()[:-10]), mock.patch.dict(
            LIVENESS_CONFIG, {"MOUNTS_DIR": str(self.path.parent)}
        ):
            self.assertEqual(resolve_mounts(checker, "gnssdata", "auto"), ["Original-RTCM32"])
            self.assertEqual(resolve_mounts(checker, "gnssdata", "caster"), [])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
