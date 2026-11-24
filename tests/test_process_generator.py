"""Run with python3 -m unittest discover -s tests -v."""

from contextlib import redirect_stderr
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "mihomo-process-generator"
loader = SourceFileLoader("generator", str(SOURCE))
generator = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(generator)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.yaml"
        self.output = self.root / "generated"
        self.observer = self.root / "socketowner-wine-observer.service"
        patcher = patch.object(generator, "OBSERVER", self.observer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def generate(self, config):
        self.config.write_text(config)
        errors = StringIO()
        with redirect_stderr(errors):
            status = generator.generate(self.output, self.config)
        contents = (self.output / "mihomo.service.d/50-process-lookup.conf").read_text()
        return status, contents, errors.getvalue()

    def install_observer(self):
        self.observer.write_text("[Unit]\n")

    def booleans(self, **values):
        directory = Path(tempfile.mkdtemp(dir=self.root))
        for name, value in values.items():
            (directory / name).write_text(value + "\n")
        patcher = patch.object(generator, "BOOLEAN_DIR", directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bpf_requires_loader_and_maps_without_proc_privileges(self):
        status, unit, errors = self.generate(
            "find-process-mode: always\nfind-process-bpf-only: true\n"
            "find-process-bpf-map: /run/mihomo-bpf/conn_owners\n"
        )
        self.assertEqual(status, 0, errors)
        self.assertIn("Requires=socketowner.service", unit)
        self.assertIn("After=socketowner.service", unit)
        self.assertIn("BindReadOnlyPaths=/sys/fs/bpf/mihomo/maps:/run/mihomo-bpf", unit)
        self.assertIn("BindReadOnlyPaths=-/sys/fs/cgroup\n", unit)
        self.assertIn("SystemCallFilter=bpf", unit)
        self.assertNotIn("SYS_PTRACE", unit)
        self.assertNotIn("ProtectProc=default", unit)
        self.assertIn("ExecStart=/usr/local/bin/mihomo", unit)

    def test_proc_grants_process_inspection_without_requiring_loader(self):
        status, unit, errors = self.generate("find-process-bpf-only: false\n")
        self.assertEqual(status, 0, errors)
        self.assertIn("ProtectProc=default", unit)
        for directive in ("CapabilityBoundingSet", "AmbientCapabilities"):
            self.assertIn(f"{directive}=\n{directive}=CAP_NET_ADMIN "
                          "CAP_SYS_PTRACE CAP_DAC_READ_SEARCH", unit)
        self.assertNotIn("CAP_NET_BIND_SERVICE", unit)
        self.assertNotIn("Requires=", unit)
        self.assertIn("Wants=socketowner.service", unit)
        self.assertIn("SystemCallFilter=bpf", unit)
        self.assertIn("BindReadOnlyPaths=-/sys/fs/bpf/mihomo/maps:/run/mihomo-bpf", unit)
        self.assertIn("BindReadOnlyPaths=-/sys/fs/cgroup\n", unit)

    def test_absent_bpf_only_uses_bpf_sandbox(self):
        bpf = "find-process-bpf-map: /run/mihomo-bpf/conn_owners\n"
        for config in ("find-process-mode: always\n",
                       "mixed-port: 7891\n",
                       "find-process-mode: always\nfind-process-bpf-only: null\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config + bpf)
                self.assertEqual(status, 0, errors)
                self.assertIn("Requires=socketowner.service", unit)
                self.assertIn("BindReadOnlyPaths=/sys/fs/bpf/mihomo/maps:/run/mihomo-bpf", unit)
                self.assertNotIn("ProtectProc=default", unit)
                self.assertNotIn("SYS_PTRACE", unit)

    def test_absent_bpf_only_without_service_map_runs_without_lookup(self):
        for config in ("find-process-mode: always\n", "mixed-port: 7891\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 0, errors)
                self.assertEqual(unit, generator.dropin("off"))
                self.assertNotIn("socketowner", unit)
                self.assertNotIn("SYS_PTRACE", unit)

    def test_tproxy_lifts_exec_restriction(self):
        status, unit, errors = self.generate("tproxy-port: 7893\n")
        self.assertEqual(status, 0, errors)
        self.assertIn("NoExecPaths=\n", unit)
        status, unit, errors = self.generate("mixed-port: 7891\n")
        self.assertNotIn("NoExecPaths", unit)

    def test_bind_service_only_for_listener_ports_below_1024(self):
        for config, granted in (
            ("", False),
            ("mixed-port: 443\n", True),
            ("mixed-port: 7891\n", False),
            ("port: 80\n", True),
            ("socks-port: 1080\n", False),
            ("redir-port: 53\n", True),
            ("tproxy-port: 53\n", True),
            ("external-controller: 127.0.0.1:53\n", True),
            ("external-controller: '[::]:53'\n", True),
            ("external-controller: '127.0.0.1:9090'\n", False),
            ("external-controller: '[::1]:53'\n", True),
            ("dns:\n  listen: 127.1.1.1:53\n", True),
            ("dns:\n  listen: '[::]:53'\n", True),
            ("dns:\n  listen: 127.1.1.1:1053\n", False),
            ("dns:\n  enable: true\n", False),
        ):
            with self.subTest(config=config):
                status, unit, errors = self.generate("find-process-mode: off\n" + config)
                self.assertEqual(status, 0, errors)
                self.assertEqual("CAP_NET_BIND_SERVICE" in unit, granted)

    def test_low_port_adds_bind_service_to_proc_capabilities(self):
        status, unit, errors = self.generate(
            "find-process-bpf-only: false\ndns:\n  listen: 127.1.1.1:53\n")
        self.assertEqual(status, 0, errors)
        self.assertIn("CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE "
                      "CAP_SYS_PTRACE CAP_DAC_READ_SEARCH", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE "
                      "CAP_SYS_PTRACE CAP_DAC_READ_SEARCH", unit)

    def test_proc_lookup_warns_when_selinux_boolean_is_off(self):
        self.booleans(mihomo_find_process_proc="0")
        status, unit, errors = self.generate("find-process-bpf-only: false\n")
        self.assertEqual(status, 0, errors)
        self.assertIn("ProtectProc=default", unit)
        self.assertIn("setsebool -P mihomo_find_process_proc on", errors)

    def test_proc_lookup_is_quiet_when_boolean_is_on_or_absent(self):
        self.booleans(mihomo_find_process_proc="1")
        self.assertEqual(self.generate("find-process-bpf-only: false\n")[0], 0)
        self.booleans()
        status, unit, errors = self.generate("find-process-bpf-only: false\n")
        self.assertEqual(status, 0, errors)
        self.assertNotIn("mihomo_find_process_proc", errors)

    def test_tproxy_warns_when_iptables_boolean_is_off(self):
        self.booleans(mihomo_use_iptables="0")
        status, unit, errors = self.generate("find-process-mode: off\ntproxy-port: 7893\n")
        self.assertEqual(status, 0, errors)
        self.assertIn("setsebool -P mihomo_use_iptables on", errors)
        for config in ("find-process-mode: off\ntproxy-port: 0\n",
                       "find-process-mode: off\nmixed-port: 7893\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 0, errors)
                self.assertNotIn("mihomo_use_iptables", errors)

    def test_off_keeps_restricted_base_and_does_not_mount_maps(self):
        for value in ("off", "OFF", "'off'"):
            with self.subTest(value=value):
                status, unit, errors = self.generate(f"find-process-mode: {value}\n")
                self.assertEqual(status, 0, errors)
                self.assertNotIn("SYS_PTRACE", unit)
                self.assertNotIn("BindReadOnlyPaths", unit)
                self.assertNotIn("socketowner", unit)
                self.assertNotIn("SystemCallFilter", unit)

    def test_bpf_syscall_is_only_allowed_with_lookup(self):
        bpf = "find-process-bpf-map: /run/mihomo-bpf/conn_owners\n"
        for config in ("find-process-mode: off\n", "mixed-port: 7891\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 0, errors)
                self.assertNotIn("SystemCallFilter=bpf", unit)
        for config in ("find-process-mode: always\n" + bpf,
                       "find-process-bpf-only: false\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 0, errors)
                self.assertIn("SystemCallFilter=bpf", unit)

    def test_observer_is_wanted_only_when_installed_and_lookup_is_on(self):
        bpf = "find-process-bpf-map: /run/mihomo-bpf/conn_owners\n"
        for config in ("find-process-mode: always\n" + bpf,
                       "find-process-bpf-only: false\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 0, errors)
                self.assertNotIn("socketowner-wine-observer", unit)

        self.install_observer()
        for config in ("find-process-mode: always\n" + bpf,
                       "find-process-bpf-only: false\n"):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 0, errors)
                self.assertIn("Wants=socketowner-wine-observer.service", unit)
                self.assertIn("After=socketowner-wine-observer.service", unit)

        status, unit, errors = self.generate("find-process-mode: off\n")
        self.assertEqual(status, 0, errors)
        self.assertNotIn("socketowner-wine-observer", unit)

    def test_observer_gets_reader_gid_when_installed(self):
        dropin = self.output / "socketowner-wine-observer.service.d/50-reader-group.conf"
        groups = self.root / "group"
        groups.write_text("root:x:0:\nmihomoish:x:7:\nmihomo:x:4242:\n")
        with patch.object(generator, "GROUP_FILE", groups):
            self.generate("find-process-bpf-only: false\n")
            self.assertFalse(dropin.exists())
            self.install_observer()
            self.generate("find-process-bpf-only: false\n")
            self.assertEqual(dropin.read_text(),
                             "[Service]\nEnvironment=READER_GROUP=4242\n")
            dropin.unlink()
            groups.write_text("root:x:0:\n")
            self.generate("find-process-bpf-only: false\n")
            self.assertFalse(dropin.exists())

    def test_blocked_startup_never_restarts_the_placeholder(self):
        self.assertIn("Restart=no", generator.DENY)
        status, unit, errors = self.generate("find-process-mode: invalid\n")
        self.assertEqual(status, 1)
        self.assertEqual(unit, generator.DENY)
        self.assertIn("Restart=no", unit)

    def test_yaml_comments_nested_settings_aliases_and_merges(self):
        status, unit, errors = self.generate(
            "defaults: &defaults\n  find-process-bpf-only: false\n"
            "<<: *defaults\nfind-process-mode: 'always' # comment\n"
            "find-process-bpf-only: true\n"
            "find-process-bpf-map: '/run/mihomo-bpf/conn_owners'\n"
            "proxies:\n  - name: example\n    find-process-mode: off\n"
        )
        self.assertEqual(status, 0, errors)
        self.assertIn("Requires=socketowner.service", unit)

    def test_merges_follow_mihomo_precedence(self):
        bpf = "find-process-bpf-map: /run/mihomo-bpf/conn_owners\n"
        for config, mode in (
            # An anchor that merges another and overrides one of its keys.
            ("base: &base\n  find-process-bpf-only: false\n"
             "settings: &settings\n  <<: *base\n  find-process-bpf-only: true\n"
             "<<: *settings\n" + bpf, "bpf"),
            ("a: &a {find-process-bpf-only: true}\nb: &b {find-process-bpf-only: false}\n"
             "<<: [*b, *a]\n" + bpf, "proc"),
            ("a: &a {find-process-bpf-only: false}\nfind-process-bpf-only: true\n"
             "<<: *a\n" + bpf, "bpf"),
            ("a: &x {find-process-bpf-only: false}\nb: &x {find-process-bpf-only: true}\n"
             "<<: *x\n" + bpf, "bpf"),
            ("find-process-mode: off\n---\nfind-process-mode: always\n", "off"),
            ("extra:\n  a: 1\n  a: 2\nfind-process-mode: off\n", "off"),
        ):
            with self.subTest(config=config):
                self.config.write_text(config)
                self.assertEqual(generator.lookup_mode(self.config), mode)

    def test_scalars_decode_like_mihomo(self):
        bpf = "find-process-bpf-map: /run/mihomo-bpf/conn_owners\n"
        for config, mode in (
            ("find-process-bpf-only: no\n", "proc"),
            ("find-process-bpf-only: Off\n", "proc"),
            ("find-process-bpf-only: FALSE\n", "proc"),
            ("find-process-bpf-only: null\n" + bpf, "bpf"),
            ("find-process-bpf-only: ~\n" + bpf, "bpf"),
            ("find-process-bpf-only:\n" + bpf, "bpf"),
            ("find-process-bpf-only: yes\n" + bpf, "bpf"),
            ("find-process-bpf-only: 'on'\n" + bpf, "bpf"),
            ("find-process-bpf-only: Y\n" + bpf, "bpf"),
            ("find-process-mode: null\n" + bpf, "bpf"),
            ("find-process-mode: !!str Always\n" + bpf, "bpf"),
            ("find-process-mode: off\nfind-process-bpf-map: null\n", "off"),
        ):
            with self.subTest(config=config):
                self.config.write_text(config)
                self.assertEqual(generator.lookup_mode(self.config), mode)

    def test_errors_block_startup_and_do_not_log_credentials(self):
        for config in (
            "password: secret-marker\nfind-process-mode: invalid\n",
            "password: [secret-marker\n",
            "find-process-mode: off\nfind-process-mode: always\n",
            "find-process-mode: [off]\n",
            "find-process-bpf-only: maybe\n",
            "find-process-bpf-only: 'true'\n",
            "find-process-bpf-only: tRue\n",
            "find-process-bpf-only: 1\n",
            "find-process-mode: off\nfind-process-bpf-only: maybe\n",
            "find-process-mode: true\n",
            "a: &a {find-process-mode: off}\nb: &b {find-process-mode: always}\n"
            "<<: *a\n<<: *b\n",
            "a: &a {find-process-mode: off, find-process-mode: always}\n<<: *a\n",
            "a: &a 1\n<<: *a\n",
            "a: &a\n  <<: *a\n<<: *a\n",
            "find-process-bpf-only: true\nfind-process-bpf-map: /wrong/map\n",
            "find-process-bpf-only: true\n",
            "[]\n",
            "",
        ):
            with self.subTest(config=config):
                status, unit, errors = self.generate(config)
                self.assertEqual(status, 1)
                self.assertEqual(unit, generator.DENY)
                self.assertNotIn("secret-marker", unit + errors)
                self.assertIn("startup is blocked", errors)

    def test_missing_file_blocks_startup(self):
        with redirect_stderr(StringIO()):
            self.assertEqual(generator.generate(self.output, self.config), 1)
        self.assertEqual((self.output / "mihomo.service.d/50-process-lookup.conf").read_text(),
                         generator.DENY)

    def test_missing_pyyaml_blocks_startup(self):
        with patch.dict("sys.modules", {"yaml": None}):
            status, unit, errors = self.generate("find-process-mode: off\n")
        self.assertEqual(status, 1)
        self.assertEqual(unit, generator.DENY)
        self.assertIn("PyYAML", errors)

    def test_switching_modes_removes_generated_proc_permissions(self):
        self.generate("find-process-bpf-only: false\n")
        status, unit, errors = self.generate("find-process-mode: off\n")
        self.assertEqual(status, 0, errors)
        self.assertNotIn("SYS_PTRACE", unit)
        self.assertNotIn("ProtectProc=default", unit)


if __name__ == "__main__":
    unittest.main()
