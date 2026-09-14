# System Data Inspector for macOS

A local-only storage scanner focused on the folders macOS commonly groups into **System Data**.

## Get started
Complete .app file is in releases tab.

## What it itemizes

- `~/Library/Caches`
- `~/Library/Application Support`
- app Containers and Group Containers
- local iPhone/iPad backups
- Messages attachments
- Mail data
- Xcode DerivedData, archives, DeviceSupport, and simulators
- Docker data
- `/Library` caches, logs, and Application Support
- `/private/var`, including virtual-memory-related storage
- `/private/tmp`
- `/Users/Shared`
- files >= 1 GB in scanned locations
- local APFS / Time Machine snapshots

It also shows the largest immediate subfolders under the main Library/private-var roots.

## Safety

The application is intentionally **read-only**. It does not delete files.

That matters because macOS "System Data" is not one folder. It is a Finder storage category that can include app support files, caches, snapshots, device backups, logs, VM/swap data, developer assets, and files Finder cannot classify cleanly.


## Full Disk Access

Without Full Disk Access, macOS will deny reads to some locations, so the app may report entries as "skipped".

For a compiled app:

**System Settings → Privacy & Security → Full Disk Access → add “System Data Inspector.app”**

When running from Terminal instead, grant Full Disk Access to the terminal app you use.

## How size is measured

The scanner uses each file's allocated blocks (`st_blocks × 512`) when available instead of only logical file length. This is closer to actual disk consumption. Symlinks are not followed and hard-linked files are counted once per individual scan.

APFS cloning, purgeable space, snapshots, sparse files, and Finder's own storage categorization can still make the numbers differ from Finder's "System Data" total.

## Good targets to investigate first

If your Mac shows ~132 GB of System Data, common large contributors include:

- old iPhone/iPad backups
- Xcode simulators / DerivedData / DeviceSupport
- Docker VM images and volumes
- Messages attachments
- large `Application Support` folders
- `/private/var`
- local Time Machine/APFS snapshots

Do **not** blindly delete `/private/var`, `/Library`, swap files, or unknown Application Support folders.
