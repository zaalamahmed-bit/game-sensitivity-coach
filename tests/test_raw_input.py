"""Synthetic Raw Input tests: no device registration or live input capture."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ctypes
import struct
import threading
import unittest
from unittest import mock

from aim_observer import raw_input


def packet(pointer_size=8, dx=-123, dy=456, flags=8, buttons=0x401,
           button_data=-120, device=None):
    if device is None:
        device = 0x123456789ABCDEF0 if pointer_size == 8 else 0xABCDEF00
    payload = struct.pack("<H2xHHIiiI", flags, buttons, button_data & 0xFFFF,
                          0, dx, dy, 0)
    header_format = "<IIQQ" if pointer_size == 8 else "<IIII"
    size = struct.calcsize(header_format) + len(payload)
    return struct.pack(header_format, 0, size, device, 1) + payload


class RawMouseParserTests(unittest.TestCase):
    def test_native_structure_sizes_and_offsets(self):
        self.assertEqual(ctypes.sizeof(raw_input.RAWMOUSE), 24)
        self.assertEqual(raw_input.RAWMOUSE.buttons.offset, 4)
        self.assertEqual(raw_input.RAWMOUSE.lLastX.offset, 12)
        self.assertEqual(raw_input.RAWMOUSE.lLastY.offset, 16)
        self.assertEqual(ctypes.sizeof(raw_input.RAWINPUTHEADER),
                         8 + 2 * ctypes.sizeof(ctypes.c_void_p))

    def test_both_abis_preserve_signed_motion_and_simultaneous_buttons(self):
        for pointer_size in (4, 8):
            with self.subTest(pointer_size=pointer_size):
                event = raw_input.parse_raw_mouse(packet(pointer_size), 987654321,
                                                   pointer_size)
                self.assertEqual(event["t_ns"], 987654321)
                self.assertEqual((event["dx"], event["dy"]), (-123, 456))
                self.assertEqual(event["flags"], 8)
                self.assertEqual(event["button_flags"], 0x401)
                self.assertEqual(event["button_data"], -120)
                self.assertEqual(event["device"], "0x123456789abcdef0"
                                 if pointer_size == 8 else "0xabcdef00")

    def test_extreme_deltas_and_positive_horizontal_wheel(self):
        data = packet(dx=-(2 ** 31), dy=2 ** 31 - 1, buttons=0x800,
                      button_data=120)
        event = raw_input.parse_raw_mouse(data, 1, 8)
        self.assertEqual(event["dx"], -(2 ** 31))
        self.assertEqual(event["dy"], 2 ** 31 - 1)
        self.assertEqual(event["button_flags"], 0x800)
        self.assertEqual(event["button_data"], 120)

    def test_absolute_flag_and_zero_device_are_preserved(self):
        event = raw_input.parse_raw_mouse(packet(dx=65535, dy=0, flags=3,
                                                 device=0), 1, 8)
        self.assertEqual(event["flags"], 3)
        self.assertEqual(event["dx"], 65535)
        self.assertEqual(event["device"], "0x0000000000000000")

    def test_rejects_truncated_or_mismatched_packets(self):
        data = packet()
        malformed = [b"", data[:23], data[:-1], data + b"extra",
                     struct.pack("<IIQQ", 0, 24, 0, 0)]
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    raw_input.parse_raw_mouse(value, 1, 8)

    def test_rejects_non_mouse_packets_and_invalid_abi(self):
        data = bytearray(packet())
        struct.pack_into("<I", data, 0, 1)
        with self.assertRaisesRegex(ValueError, "mouse"):
            raw_input.parse_raw_mouse(data, 1, 8)
        with self.assertRaisesRegex(ValueError, "pointer size"):
            raw_input.parse_raw_mouse(packet(), 1, 16)


class RawMouseObserverTests(unittest.TestCase):
    def test_requires_callable(self):
        with self.assertRaises(TypeError):
            raw_input.RawMouseObserver(None)

    def test_stop_without_start_is_safe_and_does_not_load_dlls(self):
        with mock.patch.object(raw_input, "_Win32") as windows:
            observer = raw_input.RawMouseObserver(lambda event: None)
            self.assertFalse(observer.running)
            observer.stop()
            observer.stop()
            windows.assert_not_called()

    def test_non_windows_start_fails_without_loading_dlls(self):
        observer = raw_input.RawMouseObserver(lambda event: None)
        with mock.patch.object(raw_input.sys, "platform", "linux"), \
                mock.patch.object(raw_input, "_Win32") as windows:
            with self.assertRaisesRegex(OSError, "Windows"):
                observer.start()
            windows.assert_not_called()

    def test_startup_error_is_reported_and_registration_lock_is_released(self):
        failure = OSError("synthetic DLL failure")
        observer = raw_input.RawMouseObserver(lambda event: None)
        with mock.patch.object(raw_input.sys, "platform", "win32"), \
                mock.patch.object(raw_input, "_Win32", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Could not start") as raised:
                observer.start()
            self.assertIs(raised.exception.__cause__, failure)
            self.assertIs(observer.error, failure)
            self.assertFalse(observer.running)
            with self.assertRaisesRegex(RuntimeError, "recording failed"):
                observer.stop()
            # Another attempt must report its own startup error, not an active
            # registration left behind by the previous initialization failure.
            with self.assertRaisesRegex(RuntimeError, "Could not start"):
                observer.start()

    def test_start_stop_uses_mouse_only_registration_and_releases_resources(self):
        api = mock.Mock()
        api.kernel32.GetCurrentThreadId.return_value = 321
        api.kernel32.GetModuleHandleW.return_value = 11
        api.user32.CreateWindowExW.return_value = 22
        api.user32.RegisterClassW.return_value = 1
        registrations = []
        quit_posted = threading.Event()

        def register(device_pointer, count, structure_size):
            self.assertEqual(count, 1)
            self.assertEqual(structure_size, ctypes.sizeof(raw_input.RAWINPUTDEVICE))
            device = ctypes.cast(device_pointer,
                                 ctypes.POINTER(raw_input.RAWINPUTDEVICE)).contents
            registrations.append((device.usUsagePage, device.usUsage,
                                  device.dwFlags, device.hwndTarget))
            return 1

        def get_message(*args):
            if not quit_posted.wait(1):
                raise RuntimeError("Synthetic message loop was not stopped")
            return 0

        def post_quit(thread_id, message, wparam, lparam):
            self.assertEqual((thread_id, message, wparam, lparam),
                             (321, raw_input.WM_QUIT, 0, 0))
            quit_posted.set()
            return 1

        api.user32.RegisterRawInputDevices.side_effect = register
        api.user32.GetMessageW.side_effect = get_message
        api.user32.PostThreadMessageW.side_effect = post_quit
        received = mock.Mock()
        observer = raw_input.RawMouseObserver(received)
        with mock.patch.object(raw_input.sys, "platform", "win32"), \
                mock.patch.object(raw_input, "_Win32", return_value=api):
            observer.start()
            try:
                self.assertTrue(observer.running)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    observer.start()
                other_observer = raw_input.RawMouseObserver(received)
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    other_observer.start()
            finally:
                observer.stop()
        self.assertFalse(observer.running)
        self.assertIsNone(observer.error)
        self.assertEqual(registrations, [
            (1, 2, raw_input.RIDEV_INPUTSINK, 22),
            (1, 2, raw_input.RIDEV_REMOVE, None),
        ])
        api.user32.DestroyWindow.assert_called_once_with(22)
        api.user32.UnregisterClassW.assert_called_once()
        received.assert_not_called()

    def test_window_proc_keeps_message_timestamp_and_always_cleans_up(self):
        observer = raw_input.RawMouseObserver(lambda event: None)
        observer._api = mock.Mock()
        observer._read_input = mock.Mock()
        with mock.patch.object(raw_input.time, "perf_counter_ns", return_value=777):
            result = observer._window_proc(11, raw_input.WM_INPUT, 0, 22)
        self.assertEqual(result, 0)
        observer._read_input.assert_called_once_with(22, 777)
        observer._api.user32.DefWindowProcW.assert_called_once_with(
            11, raw_input.WM_INPUT, 0, 22)

    def test_window_proc_surfaces_parse_failure_and_performs_cleanup(self):
        observer = raw_input.RawMouseObserver(lambda event: None)
        observer._api = mock.Mock()
        failure = ValueError("synthetic malformed input")
        observer._read_input = mock.Mock(side_effect=failure)
        self.assertEqual(observer._window_proc(11, raw_input.WM_INPUT, 0, 22), 0)
        self.assertIs(observer.error, failure)
        self.assertTrue(observer._stop_requested.is_set())
        observer._api.user32.PostQuitMessage.assert_called_once_with(1)
        observer._api.user32.DefWindowProcW.assert_called_once_with(
            11, raw_input.WM_INPUT, 0, 22)

    def test_read_input_decodes_synthetic_win32_buffer(self):
        received = []
        observer = raw_input.RawMouseObserver(received.append)
        observer._api = mock.Mock()
        data = packet(ctypes.sizeof(ctypes.c_void_p))

        def read(handle, command, buffer, size_pointer, header_size):
            self.assertEqual(handle, 22)
            self.assertEqual(command, raw_input.RID_INPUT)
            self.assertEqual(header_size, ctypes.sizeof(raw_input.RAWINPUTHEADER))
            size = ctypes.cast(size_pointer, ctypes.POINTER(raw_input.UINT))
            size.contents.value = len(data)
            if buffer is None:
                return 0
            self.assertEqual(ctypes.addressof(buffer) % 4, 0)
            ctypes.memmove(buffer, data, len(data))
            return len(data)

        observer._api.user32.GetRawInputData.side_effect = read
        observer._read_input(22, 333)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["t_ns"], 333)
        self.assertEqual(received[0]["dx"], -123)
        self.assertEqual(received[0]["button_data"], -120)


if __name__ == "__main__":
    unittest.main()
