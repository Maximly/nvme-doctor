# Security policy

NVMe Doctor reads hardware/system state and may need root privileges for complete health, topology, log, or direct USB access.

## Safety properties

Normal diagnostic collection is read-only with respect to drive data/configuration. NVMe Doctor does not format or sanitize media, flash firmware, modify namespaces, reset controllers, start destructive tests, or change ASPM/APST/power settings.

On macOS, optional direct USB backends can temporarily unmount/eject/capture an external enclosure so read-only NVMe or ATA health commands can be issued. This is operationally disruptive even though no media/configuration write command is sent. Close applications using the disk and keep backups appropriate to the value of the data.

## Sensitive output

Text/JSON reports may contain:

- drive serial numbers and firmware versions;
- hostnames;
- PCI/USB/SCSI addresses and topology;
- mount/path information;
- selected kernel/system log fragments.

Review reports before posting them publicly.

Please report security-sensitive issues privately to the KernelSoft maintainers rather than opening a public issue containing sensitive system logs or identifiers.
