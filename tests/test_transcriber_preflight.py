"""Ön tanı işlevini donanım yüklemeden ve iş parçacığı açmadan doğrular."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import List
import unittest
from unittest.mock import Mock, call


class TranscriberPreflightTests(unittest.TestCase):
    """Yalnızca ayrıştırılan ön tanı işlevini sahte bağımlılıklarla çalıştırır."""

    def setUp(self):
        source_path = Path(__file__).parents[1] / "vesper" / "transcriber.py"
        source = ast.parse(source_path.read_text(encoding="utf-8"))
        function = next(
            node for node in source.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_preflight_diagnostics"
        )
        self.find_spec = Mock(return_value=None)
        self.dll_copies = Mock(return_value=[])
        self.log = Mock()
        self.namespace = {
            "List": List,
            "sys": SimpleNamespace(platform="win32"),
            "DEVICE": "cuda",
            "os": SimpleNamespace(environ={"CUDA_PATH": r"C:\CUDA"}),
            "importlib": SimpleNamespace(util=SimpleNamespace(find_spec=self.find_spec)),
            "log": self.log,
            "CUDA_DLLS": ("cublas64_12.dll", "cudnn64_9.dll"),
            "_dll_copies_in_path": self.dll_copies,
        }
        module = ast.Module(body=[function], type_ignores=[])
        exec(compile(module, str(source_path), "exec"), self.namespace)
        self.diagnostics = self.namespace["_preflight_diagnostics"]

    def test_missing_nvidia_parent_does_not_skip_dll_diagnostics(self):
        self.find_spec.side_effect = ModuleNotFoundError(
            "No module named 'nvidia'", name="nvidia"
        )
        self.diagnostics([r"C:\CUDA\bin"])
        self.assertEqual(self.find_spec.call_args_list, [call("nvidia.cublas"), call("nvidia.cudnn")])
        self.assertEqual(self.dll_copies.call_args_list, [call("cublas64_12.dll"), call("cudnn64_9.dll")])
        self.log.warning.assert_not_called()
        self.assertEqual(self.log.debug.call_count, 2)

    def test_present_packages_keep_existing_warnings(self):
        self.find_spec.return_value = object()
        self.diagnostics([])
        self.assertEqual(self.log.warning.call_args_list, [
            call("pip paketi '%s' hâlâ kurulu. Çakışma yaratabilir.", "nvidia.cublas"),
            call("pip paketi '%s' hâlâ kurulu. Çakışma yaratabilir.", "nvidia.cudnn"),
        ])

    def test_absent_subpackages_do_not_warn(self):
        self.diagnostics([])
        self.assertEqual(self.find_spec.call_count, 2)
        self.log.warning.assert_not_called()

    def test_missing_child_package_error_is_not_hidden(self):
        error = ModuleNotFoundError("broken child", name="nvidia.cublas")
        self.find_spec.side_effect = error
        with self.assertRaises(ModuleNotFoundError) as caught:
            self.diagnostics([])
        self.assertIs(caught.exception, error)

    def test_broken_installed_package_dependency_is_not_hidden(self):
        error = ModuleNotFoundError("missing dependency", name="unexpected_dependency")
        self.find_spec.side_effect = error
        with self.assertRaises(ModuleNotFoundError) as caught:
            self.diagnostics([])
        self.assertIs(caught.exception, error)

    def test_unexpected_discovery_error_is_not_hidden(self):
        error = RuntimeError("discovery failure")
        self.find_spec.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.diagnostics([])
        self.assertIs(caught.exception, error)

    def test_non_cuda_device_skips_discovery(self):
        self.namespace["DEVICE"] = "cpu"
        self.diagnostics([])
        self.find_spec.assert_not_called()
        self.dll_copies.assert_not_called()

    def test_non_windows_platform_skips_discovery(self):
        self.namespace["sys"].platform = "linux"
        self.diagnostics([])
        self.find_spec.assert_not_called()
        self.dll_copies.assert_not_called()


if __name__ == "__main__":
    unittest.main()
