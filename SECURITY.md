# Security policy

NVMe Doctor reads hardware/system state and may be run as root to obtain complete device/log information.

The diagnostic path is read-only on both Linux and macOS: it does not format namespaces, sanitize drives, reset controllers, flash firmware, start destructive tests, or change ASPM/APST/other power settings.

JSON/text reports can contain serial numbers, hostnames, PCI addresses and log fragments. Review them before posting publicly.

Please report security issues privately to the KernelSoft maintainers rather than opening a public issue containing sensitive system logs.
