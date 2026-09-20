# libmpv

The editor plays video with `libmpv-2.dll`, which is **not stored in this repository**: it is about 115 MB, over
GitHub's 100 MB limit for a single file.

To run the editor, put `libmpv-2.dll` in this folder (`packaging/vendor/win-64/`). The build used during development
is mpv v0.41.0-1012-ge8673660a (the `mpv-dev-x86_64-20260830-git-e8673660ab` archive). The app looks for the file here
before it imports python-mpv (see `_register_libmpv_search_path` in `app/ui/video_widget.py`).

Where that build came from and which licence variant it is are still open items: see section 4 of
[`THIRD-PARTY-NOTICES.md`](../../../THIRD-PARTY-NOTICES.md).
