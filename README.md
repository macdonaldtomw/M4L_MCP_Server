# m4l MCP server

Edit and debug Max for Live devices (`.amxd`, plain or frozen) and `.maxpat` files on disk, and check them
against the running Live set through the `ableton` MCP's bridge. Python, stdio; register it in your
project's `.mcp.json` next to the `ableton` server.

## Workflow

1. `device_info` opens a device by path or by name (searched in the User Library).
2. Understand it: `patch_tree`, `patch_find`, `patch_box`, `patch_connections`, `patch_trace`,
   `patch_presentation` (`render=true` returns a PNG), `patch_parameters`.
3. Edit **in memory**: `patch_add_box`, `patch_set_attrs`, `patch_remove_box`, `patch_connect`,
   `patch_disconnect`, `patch_edit_batch` (atomic, with `$alias` references), `device_put_file`,
   `device_remove_file`.
4. `patch_validate`, then `device_diff`.
5. `device_save`. It refuses while a Max editor window for that device is open. Ask the user to close
   the window **without saving**, then save again.
6. `live_check_reload` checks whether the Live instance runs the new version. With no editor open,
   Live hot-reloads the device and keeps its state, so nobody needs to reopen the set.

Nothing touches disk until `device_save`. `device_revert` discards edits.

## Addressing

- `file`: an embedded file of a frozen device. Defaults to the top-level patcher. A unique substring is
  enough, e.g. `file="SINGLE"`.
- Box paths: `obj-12`, or `obj-1137::obj-12` to reach into embedded subpatchers (`p`, `patcher`, embedded
  bpatchers). Paths stay inside one file. A bpatcher or abstraction that loads another file is a
  *reference*: address that file directly. `patch_tree` shows where each file is used.
- Instance paths are the keys of Live's parameter registry, e.g. `obj-1137::obj-50::obj-107::obj-107`. They
  chain box ids through references too.

## Safety on save

- Refuses while a Max-owned window whose title contains the device name is open
  ([winutil.py](m4l_mcp/winutil.py)). On 2026-11-11, writing the FCB device with its editor open made
  Max's reload fail, and closing the window then crashed Live. `ignore_open_editor=true` is only for a
  false match.
- Refuses if the file changed on disk since it was read (sha256). `force=true` overrides, after a backup.
- Refuses if the container did not rebuild byte-identically when opened, or if validation finds errors.
- Backs up the old file to `M4L_MCP_BACKUP_DIR` (`dev\m4l-backups\<name>.<timestamp>.bak`). Backups live
  outside the User Library, so Live's browser never lists them.
- Rebuilds, re-parses and compares every entry, then writes a temp file and `os.replace`s it, retrying
  while Windows holds the file.
- Only the embedded files you changed are re-serialised (tab-indented JSON, original newline style and
  trailing `\r\n\0`). Every other entry stays byte-identical.

## What it knows

- **Formats** ([container.py](m4l_mcp/container.py)): the `ampf`/`meta`/`ptch` chunk header, with
  little-endian lengths. The `ptch` chunk holds either JSON or an `mx@c` collective: big-endian header, file
  bodies, then a `dlst` directory of `dire` records (`type fnam sz32 of32 vers flag mdat`, names
  NUL-padded to 4 bytes). The parser checks that rebuilding reproduces the original file before anything is
  saved.
- **Inlet/outlet counts** ([objdb.py](m4l_mcp/objdb.py)): learned from about 6,400 patches that Max saved,
  from Max 9 and Live 12 resources, and cached in `~/.m4l-mcp/objdb.json`. Rebuild with
  `python -m m4l_mcp.objdb --build`. Abstractions and subpatchers are counted from their own
  inlet/outlet objects.
- **Parameters**: UI `live.*` objects use `parameter_enable`. Non-UI objects (`dict`, `coll`, `pattr`) use
  `saved_object_attributes.parameter_enable` / `@parameter_enable 1`. Adding a parameter writes
  `saved_attribute_attributes.valueof`, using a template from the corpus for that class. It also registers
  the parameter in the top-level `parameters` dict once per instance. In an abstraction used several
  times, each instance gets a unique `[n]` long name plus a `parameter_overrides` entry.
- **Live** ([live.py](m4l_mcp/live.py)): JSON-RPC to the Remote Script bridge on 127.0.0.1:8765. Device
  search walks tracks, returns, master and every rack chain. Requests pass `max_items=-1` because the
  bridge truncates results at 200 items. Live lists only automatable parameters
  (`parameter_invisible` 0, not blobs). It shows their **short** names, ordered by `parameter_order` and
  then by long name (case-sensitive). `live_check_reload` compares that list with the file.

## Gotchas

- Live hot-reloads a device when its file changes and keeps its state, but only while no Max editor window
  for it is open. Never write the file while one is open, and never ask the user to save from a window that
  showed reload errors. Dragging the device in again resets values stored in the set.
- Mappings stored in `@parameter_enable` dicts live in the **Live set**. The `.amxd` holds only defaults.
- Max resolves absolute picture paths from another machine by bare file name against the embedded files.
  The validator does the same.
- `mcp` 2.x renamed FastMCP to `MCPServer`. The server imports either.
- ableton-live-mcp window capture on Windows 10 19045: use `draw_border=None`, not `False`.
- This machine intercepts TLS: use `uv … --native-tls` and `git -c http.sslBackend=schannel`.

## Dev

```
cd dev\m4l-mcp
uv venv --native-tls -p 3.13 .venv
uv pip install --native-tls -p .venv\Scripts\python.exe -e ".[test]"
.venv\Scripts\python.exe -m pytest -q tests
```

The tests run on temp copies of the FCB (frozen), More Simpler (plain) and `M4L.SimplerPolyVoiceRev~.maxpat`
files. They never write to the originals.
