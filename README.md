# Home Duo Recovery

A desktop app for Windows and macOS that reads a **WD My Cloud Home / Home Duo** disk
directly, shows its contents with the **original folder structure**, and copies the
folders you select to another disk. Copies can be retried and restarted without
re-copying files that are already done.

It does **not** need WSL, Linux, mdadm or any ext4 driver, and it **never writes to the
source disk**. The source is opened read-only, and all parsing happens in user space.

## Quick start (no technical knowledge needed)

**It's a desktop app: you double-click it.** There's nothing to install, and you don't
need a website or an internet connection.

**Download:** open the
[Releases page](https://github.com/cuizhanming/wd-mycloudhome-recovery/releases/latest)
and pick the zip file for your computer (Windows, Mac with Apple Silicon, or Mac with
Intel).

### Windows

1. Unzip **`HomeDuoRecovery-windows.zip`** anywhere, for example on your Desktop.
2. Open the unzipped folder and **double-click `HomeDuoRecovery.exe`**.
3. Windows asks *"Do you want to allow this app to make changes?"*. Click **Yes**. The
   app needs this permission to read the disk; it doesn't change your PC.
   - If a blue *"Windows protected your PC"* box appears, click **More info**, then
     **Run anyway**. This appears because the app isn't from the Microsoft Store.
4. Plug in the Home Duo disk. If Windows asks to **format** or **initialise** it, click
   **Cancel**.
5. In the app: choose the disk at the top, click **Open disk**, tick the folders you
   want, choose where to **Copy to**, and click **Copy selected**.
6. If the copy stops (unplugged cable, the PC went to sleep, and so on), open the app
   again, choose the same **Copy to** folder, and click **Retry / Resume remaining**.

### Mac

1. Double-click **`HomeDuoRecovery-mac.zip`** to unzip it, then double-click
   **`Home Duo Recovery.app`** (or drag it to *Applications* first).
   - The first time, macOS may say it *"cannot be opened"* or *"cannot be verified"*.
     Click **Done**, open **System Settings ▸ Privacy & Security**, scroll down, and click
     **Open Anyway**. On older macOS you can instead **right-click the app ▸ Open ▸ Open**.
     This is only needed once.
2. Plug in the Home Duo disk. If macOS says the disk is *not readable*, click **Ignore**.
   **Never click Initialize.**
3. Follow steps 5–6 above. macOS asks for your password when you open the disk.

> Building the Mac app requires a Mac: run `./build_macos.sh` once (see *Run from source / build*).

## How a Home Duo stores files

Each Home Duo disk is one half of a Linux `md` RAID1 mirror, formatted ext4:

```
GPT
├─ partition 1  (2 GB)   md RAID1 member - system
└─ partition 2  (rest)   md RAID1 member (superblock v1.2) - ext4 data volume
     ├─ restsdk/data/db/index.db        SQLite catalog (table Files)
     ├─ restsdk/data/files/<c>/<contentID>   file contents, opaque names
     └─ userStorage/auth0|<user>/...     newer firmware: some files keep real names
```

`Files` rows hold `id`, `parentID`, `name`, `mimeType` (`application/x.wd.dir` marks a
folder), `contentID`, `size`, `mTime` and `storageID`. The app rebuilds each path by
following `parentID` up to the root (`auth0|<user>` or `Trash`). It then copies the
content from `restsdk/data/files/<contentID[0]>/<contentID>`, or from `userStorage/...`
when `storageID` points to a `Filesystems` row. This is the same idea as
[restsdk_public.py](https://github.com/springfielddatarecovery/mycloud-restsdk-recovery-script/blob/master/restsdk_public.py),
but it builds the tree once instead of doing an O(n²) search, and it adds resume and
verification.

Only one mirror half is needed. RAID levels other than 1 (mirror) are not supported.

## Using the app

1. Connect the Home Duo disk (for example with a USB SATA dock).
   - **Never initialise, format or erase it.** The disk only looks unreadable because
     Windows and macOS don't understand its Linux format.
   - **Windows:** Windows shows no drive letter for it, which is expected. The `.exe`
     asks for administrator rights because reading a physical disk requires them.
     Opening the disk also turns on Windows' write protection for it, so any attempt to
     initialise or format it fails with "media is write protected". The protection
     lasts until the disk is unplugged or the PC restarts, and is applied again on
     reconnect. Nothing is stored on the disk, and it is never applied to the system
     disk.
   - **macOS:** if macOS says the disk is not readable, click **Ignore** (or **Eject**),
     never **Initialize...**. macOS offers no equivalent lock, so the app warns about
     this when the disk is opened. When you open the disk, macOS asks for an
     administrator password (`authopen`).
2. Pick the disk under **Source disk** and click **Open disk**. Alternatives:
   - **Open image file...** opens a `ddrescue` or `dd` image of the disk or partition
     (recommended if the disk is failing).
   - **Open mounted folder...** opens a folder that contains `restsdk`, such as
     `\\wsl$\Ubuntu\mnt\ssd`. Any folder inside it works too. For `\\wsl$` and
     `\\wsl.localhost` folders, the app reads through a small read-only helper that runs
     inside WSL as root. This is needed because Home Duo's `restsdk` folder is readable
     only by root, and Windows changes `|` in names to another character.
3. Tick the folders or files to copy. Ticking a folder includes everything inside it,
   and you can untick individual items within it.
4. Choose **Copy to** (a folder on the target disk), then click **Copy selected**.
5. If the disk disconnects, the computer sleeps, the target fills up, or you click
   **Stop**: fix the problem, reopen the app if needed, select the same **Copy to**
   folder, and click **Retry / Resume remaining**.

### How resume works

- The job state lives in `<target>/.homeduo-recovery/manifest.sqlite`, along with the
  source disk it belongs to. Resuming with a different source requires confirmation
  (CLI: `--force`).
- Each file is written to a temporary file of its own (`name.<id>.partial`), flushed to
  disk and size-checked. It is then moved into place without replacing anything. A file
  is marked *done* only after that move, and its size and SHA-256 are recorded.
- **Retry / Resume** copies every file that is not yet done. A file that was
  interrupted is copied again from the beginning. Files marked done whose output was
  deleted or changed size are queued again.
- Clicking **Copy selected** again with the same target adds new files only. Files
  already done stay done.
- Nothing in the target is ever overwritten. If a file already exists at the
  destination, it is accepted only when its bytes are identical to the source.
  Otherwise the recovered copy is saved as `name (recovered).ext`.
- Content listed in `index.db` but absent from the disk is marked *missing*. Missing
  and failed files are listed in `.homeduo-recovery/problems.csv`.
- The computer is kept awake while a copy runs.

### Names on Windows and macOS

Characters that Windows and exFAT don't allow (`< > : " / \ | ? *`) become `-`.
Reserved names such as `CON` get a `_` prefix, and trailing dots and spaces are
removed. Names that differ only in letter case get a ` (2)` suffix. Long paths are
supported on Windows. File modification times are restored from `index.db`.

## Command line

The command line uses the same engine:

```
python -m homeduo_recovery disks
python -m homeduo_recovery tree   \\.\PhysicalDrive1 --depth 3
python -m homeduo_recovery copy   \\.\PhysicalDrive1 E:\Recovered --path "auth0|5e62.../Photos"
python -m homeduo_recovery resume \\.\PhysicalDrive1 E:\Recovered
python -m homeduo_recovery status E:\Recovered
```

On Linux, `SOURCE` can be `/dev/sde` or a mounted folder such as `/mnt/ssd`.

## Run from source / build

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # macOS: .venv/bin/pip ...
.venv\Scripts\python -m homeduo_recovery           # start the app
```

- Windows `.exe`: `.\build_windows.ps1` creates `dist\HomeDuoRecovery-windows.zip`.
- macOS `.app`: run `./build_macos.sh` **on a Mac** (PyInstaller cannot cross-compile).
- **Releases:** on GitHub, open **Actions ▸ Release ▸ Run workflow**, type a version
  (for example `v0.1.1`) and click **Run workflow**. Alternatively push a tag:
  `git tag v0.1.1 && git push origin v0.1.1`. Either way, GitHub Actions runs the tests,
  builds the Windows, Apple Silicon and Intel Mac versions with that version number, and
  publishes them on the Releases page.

## Tests

```
sudo bash tests/make_test_images.sh tests/data     # in WSL/Linux: builds synthetic disk images
python -m pytest -q
```

The images contain a GPT disk with a degraded md RAID1 (v1.2) running ext4, and a bare
ext3 filesystem with 1K blocks and block maps. Their contents exercise awkward names,
case collisions, sparse files, a large file, the `userStorage` layout, Trash, and a
missing content file. The tests cover full copies verified by SHA-256,
disconnect-then-retry, and stop-then-restart.

## Limitations

- Only the macOS raw-disk path (`authopen` on `/dev/rdiskN`) is untested; the rest of
  the code is cross-platform.
- Supported layouts: ext2, ext3 and ext4 (extents, 64-bit, `flex_bg`, `meta_bg`, inline
  data), with md superblocks v0.90, v1.0, v1.1 and v1.2. Encrypted (fscrypt) files
  cannot be read.
- `index.db` comes from the disk as-is. If it is damaged, use the `dbBackup` copy or a
  commercial tool.
