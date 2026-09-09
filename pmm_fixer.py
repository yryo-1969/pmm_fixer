"""
PMMリンク切れ修復ツール

使い方:
  python pmm_fixer.py scan  <PMMファイル> [--roots M:\\ E:\\] [--report 出力.xlsx] [--rebuild-index]
  python pmm_fixer.py load  <PMMファイル> [--mmd-exe "M:\\MikuMikuDance_v926x64\\MikuMikuDance.exe"] [--roots M:\\ E:\\] [--report 出力.xlsx] [--rebuild-index]

scan: PMM内の参照ファイルを調べ、壊れているものをドライブ内から探してExcelレポートを作る
load: scanと同じ調査をした上で、MMDを起動（未起動なら）して自動でプロジェクトを読み込み、
      見つかった代替パスを自動投入する。候補が見つからないアクセサリ(.x)があれば、
      安全のためそこで停止し、MMD側のダイアログを手動対応してもらう。
"""
import argparse
import ctypes
import os
import string
import sys

if getattr(sys, "frozen", False):
    # Reports/cache should land next to the .bat launcher the user actually
    # sees, not next to the exe itself — the onedir build tucks the exe away
    # in an app/ subfolder to look less like a dropper to antivirus heuristics.
    # The .bat does `cd /d "%~dp0"` before invoking us, so the process's own
    # cwd is the right place; only fall back to the exe's own folder (e.g.
    # someone double-clicked app\pmm_fixer.exe directly) if that cwd looks
    # like it's actually inside the exe's own install tree.
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    _cwd = os.path.abspath(os.getcwd())
    _APP_DIR = _cwd if os.path.commonpath([_cwd, _exe_dir]) == _cwd and _cwd != _exe_dir else _exe_dir
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, _APP_DIR)

from pmm_scan import (
    build_file_index,
    extract_pmm_refs,
    resolve_refs,
    write_report_xlsx,
)

TOOL_DIR = _APP_DIR


def system_drive():
    return os.environ.get("SystemDrive", "C:").rstrip("\\").upper() + "\\"


def list_all_fixed_drives():
    """List local fixed drives (DRIVE_FIXED=3), skipping removable/network/CD."""
    DRIVE_FIXED = 3
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not (bitmask >> i) & 1:
            continue
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
            drives.append(root)
    return drives or ["C:\\"]


def detect_fixed_drives(exclude_system=True):
    """Fixed drives to search by default.

    Excludes the Windows system drive (usually C:) by default: it rarely
    holds creative asset libraries, is typically the largest/most heavily
    antivirus-scanned drive on the machine, and scanning it as part of the
    default search made a first run take an unreasonably long time. Pass
    --roots explicitly (including the system drive) to override this.
    """
    drives = list_all_fixed_drives()
    if not exclude_system:
        return drives
    filtered = [d for d in drives if d.upper() != system_drive()]
    return filtered or drives


def detect_mmd_exe(index, roots):
    """Look up MikuMikuDance.exe in an already-built file index (covers every
    subfolder under `roots`, however deep — not just a shallow search)."""
    for candidate in index.get("mikumikudance.exe", []):
        return candidate
    # index not built yet / doesn't have it: fall back to an unrestricted walk
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            if "MikuMikuDance.exe" in filenames:
                return os.path.join(dirpath, "MikuMikuDance.exe")
    return None


def _print_index_progress(current_root, dirs_seen, files_seen):
    if current_root is None:
        print(f"\r  完了（フォルダ{dirs_seen:,}件・ファイル{files_seen:,}件を確認）" + " " * 10)
    else:
        print(f"\r  処理中: {current_root} - フォルダ{dirs_seen:,}件・ファイル{files_seen:,}件を確認...",
              end="", flush=True)


def get_index(roots, rebuild_index):
    print(f"ファイルインデックス構築中 (対象: {roots}) ...")
    print("  （初回・ドライブ整理後は数分かかる場合があります。フリーズではありません）")
    index, built_at, used_roots = build_file_index(
        roots, TOOL_DIR, force_rebuild=rebuild_index, on_progress=_print_index_progress
    )
    print(f"インデックス作成日時: {built_at}（{sum(len(v) for v in index.values())} ファイル）")
    return index, built_at, used_roots


MB_OK = 0x0
MB_YESNO = 0x4
MB_ICONWARNING = 0x30
MB_ICONINFORMATION = 0x40
MB_ICONQUESTION = 0x20
MB_TOPMOST = 0x40000
IDYES = 6


def notify_missing(pmm_path, results, report_path, show_all_clear=True):
    """Pop up a plain message box so a non-technical user notices unresolved
    files without having to read the console or open the Excel report."""
    not_found = [r for r in results if not r["exists"] and not r["resolved_path"] and not r["candidates"]]
    ambiguous = [r for r in results if not r["exists"] and not r["resolved_path"] and r["candidates"]]
    unresolved = not_found + ambiguous
    if not unresolved:
        if not show_all_clear:
            return
        ctypes.windll.user32.MessageBoxW(
            0,
            f"{os.path.basename(pmm_path)}\n\n"
            "リンク切れは見つかりませんでした。全て解決済みです。\n\n"
            f"詳細レポート:\n{report_path}",
            "pmm_fixer - 調査完了",
            MB_OK | MB_ICONINFORMATION | MB_TOPMOST,
        )
        return

    lines = []
    for r in not_found[:10]:
        lines.append(f"・{r['name']}（候補なし）")
    for r in ambiguous[:10]:
        lines.append(f"・{r['name']}（候補{len(r['candidates'])}件、絞り込めず）")
    shown = len(lines)
    remaining = len(unresolved) - shown
    if remaining > 0:
        lines.append(f"...ほか {remaining} 件")

    message = (
        f"{os.path.basename(pmm_path)}\n\n"
        f"見つからなかった／絞り込めなかったファイルが {len(unresolved)} 件あります:\n\n"
        + "\n".join(lines)
        + f"\n\n詳細レポート:\n{report_path}"
    )
    ctypes.windll.user32.MessageBoxW(0, message, "pmm_fixer - 未解決のファイルがあります", MB_OK | MB_ICONWARNING | MB_TOPMOST)


def do_scan(pmm_path, roots, report_path, rebuild_index, index_bundle=None, popup=True, popup_all_clear=True):
    print(f"PMM解析中: {pmm_path}")
    refs = extract_pmm_refs(pmm_path)
    print(f"参照ファイル {len(refs)} 件を検出")

    index, built_at, used_roots = index_bundle or get_index(roots, rebuild_index)

    try:
        pmm_mtime = os.path.getmtime(pmm_path)
    except OSError:
        pmm_mtime = None
    results = resolve_refs(refs, index, pmm_mtime=pmm_mtime)

    ok = sum(1 for r in results if r["exists"])
    auto = sum(1 for r in results if not r["exists"] and r["resolved_path"])
    ambiguous = sum(1 for r in results if not r["exists"] and not r["resolved_path"] and r["candidates"])
    missing = sum(1 for r in results if not r["exists"] and not r["resolved_path"] and not r["candidates"])
    print(f"結果: 既存={ok} / 自動解決={auto} / 要確認(候補複数)={ambiguous} / 候補なし={missing}")

    if report_path is None:
        base = os.path.splitext(os.path.basename(pmm_path))[0]
        report_path = os.path.join(TOOL_DIR, f"{base}_リンク切れ一覧.xlsx")
    write_report_xlsx(pmm_path, results, built_at, used_roots, report_path)
    print(f"レポート出力: {report_path}")

    if popup:
        notify_missing(pmm_path, results, report_path, show_all_clear=popup_all_clear)

    return results, report_path


def do_load(pmm_path, roots, report_path, rebuild_index, mmd_exe):
    import mmd_dialogs

    index_bundle = get_index(roots, rebuild_index)

    if mmd_exe is None:
        print("MikuMikuDance.exe を自動検索中 ...")
        mmd_exe = detect_mmd_exe(index_bundle[0], roots)
        if mmd_exe is None:
            print("MikuMikuDance.exe が見つかりませんでした。--mmd-exe で明示的にパスを指定してください。")
            ctypes.windll.user32.MessageBoxW(
                0,
                "MikuMikuDance.exe が見つかりませんでした。\n\n"
                "MMDがインストールされているドライブが、検索対象として選ばれていない可能性があります。\n"
                "もう一度実行し、ドライブ選択画面でMMDのあるドライブにチェックを入れてください。",
                "pmm_fixer - MikuMikuDanceが見つかりません",
                MB_OK | MB_ICONWARNING | MB_TOPMOST,
            )
            return
        print(f"検出: {mmd_exe}")

    results, report_path = do_scan(pmm_path, roots, report_path, rebuild_index, index_bundle=index_bundle,
                                    popup_all_clear=False)

    resolved_by_name = {}
    model_fallback_queue = []
    for r in results:
        if r["resolved_path"] and not r["exists"]:
            resolved_by_name[r["name"]] = r["resolved_path"]
            base_noext = os.path.splitext(os.path.basename(r["old_path"]))[0]
            resolved_by_name[base_noext] = r["resolved_path"]
            resolved_by_name[os.path.basename(r["old_path"])] = r["resolved_path"]
            if r["kind"].startswith("モデル"):
                model_fallback_queue.append(r["resolved_path"])

    print(f"MMD起動確認中 ({mmd_exe}) ...")
    mmd_hwnd = mmd_dialogs.launch_mmd(mmd_exe)
    print(f"MMDウィンドウ: {mmd_hwnd}")

    print("プロジェクトの自動読み込みを開始します。")
    ok = mmd_dialogs.run_autoload(mmd_hwnd, os.path.abspath(pmm_path), resolved_by_name,
                                   model_fallback_queue=model_fallback_queue,
                                   pick_model_callback=pick_model_fallback)
    if not ok:
        print("中断: 手動対応が必要なダイアログがあります。MMDの画面を確認してください。")
        return

    print("完了: プロジェクトの読み込みに成功しました。")

    media_ok = _reload_resolved_media(mmd_hwnd, mmd_dialogs, results)

    all_resolved = all(r["exists"] or r["resolved_path"] for r in results) and media_ok
    if not all_resolved:
        return

    answer = ctypes.windll.user32.MessageBoxW(
        0,
        "全てのリンク切れが解決できました。\n\n"
        "修復済みのプロジェクトを別名で保存しますか？\n"
        "（元のファイルはそのまま残ります）",
        "pmm_fixer - 別名保存の確認",
        MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
    )
    if answer != IDYES:
        return

    new_path = _suggest_save_path(pmm_path)
    print(f"別名保存中: {new_path}")
    saved = mmd_dialogs.save_project_as(mmd_hwnd, new_path)
    if saved:
        print(f"保存完了: {new_path}")
        ctypes.windll.user32.MessageBoxW(
            0, f"保存しました:\n{new_path}", "pmm_fixer - 保存完了",
            MB_OK | MB_ICONINFORMATION | MB_TOPMOST,
        )
    else:
        print("保存に失敗しました。MMDの画面を確認してください。")


def _reload_resolved_media(mmd_hwnd, mmd_dialogs, results):
    """MMD's own project-load dialogs never offer a substitute-location
    option for a broken WAV/AVI reference (unlike models/accessories) — it
    just silently drops it. For anything scan resolved a new path for,
    load it fresh through the WAV/AVI menu commands instead. Returns False
    if any resolved media failed to load, so the caller doesn't offer to
    save a project that's still missing something.
    """
    menu_by_kind = {"音声(.wav)": mmd_dialogs.MENU_ID_LOAD_WAV, "背景動画(.avi)": mmd_dialogs.MENU_ID_LOAD_AVI}
    ok = True
    for r in results:
        menu_id = menu_by_kind.get(r["kind"])
        if menu_id is None or r["exists"] or not r["resolved_path"]:
            continue
        print(f"{r['kind']} を読み込み直します: {r['resolved_path']}")
        if not mmd_dialogs.load_media_file(mmd_hwnd, menu_id, r["resolved_path"]):
            print(f"  読み込みに失敗しました: {r['resolved_path']}")
            ok = False
    return ok


def _suggest_save_path(pmm_path):
    folder = os.path.dirname(os.path.abspath(pmm_path))
    base = os.path.splitext(os.path.basename(pmm_path))[0]
    candidate = os.path.join(folder, f"{base}_修復済み.pmm")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}_修復済み{n}.pmm")
        n += 1
    return candidate


_LAST_DIR_FILE = "last_pmm_dir.txt"


def _load_last_dir():
    path = os.path.join(TOOL_DIR, _LAST_DIR_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = f.read().strip()
        return d if d and os.path.isdir(d) else None
    except Exception:
        return None


def _save_last_dir(folder):
    try:
        with open(os.path.join(TOOL_DIR, _LAST_DIR_FILE), "w", encoding="utf-8") as f:
            f.write(folder)
    except Exception:
        pass


def pick_pmm_file():
    """No file was passed on the command line — e.g. someone launched the
    shortcut directly instead of dragging a .pmm onto it. Open a normal
    file-picker instead of just printing usage text and exiting, since
    that's a much easier mistake to recover from.

    Where PMM projects live varies a lot from person to person, so instead
    of guessing a folder, remember whatever folder was picked from last
    time and start there next time.
    """
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="調べる/読み込む .pmm ファイルを選んでください",
        filetypes=[("MikuMikuDance Project", "*.pmm"), ("All files", "*.*")],
        initialdir=_load_last_dir(),
    )
    root.destroy()
    if path:
        _save_last_dir(os.path.dirname(path))
    return path or None


def pick_model_fallback(dialog_name, candidates):
    """MMD is asking about a model it calls dialog_name (its own internal
    object name for that slot, not the filename — e.g. "Null_00" for one
    that was never named, or some unrelated leftover label from years of
    reusing a save slot) and name-based matching couldn't find it, but
    more than one still-unresolved model is available to fill the slot.
    Guessing which one goes where would risk silently mislabeling a
    model, so ask the human instead."""
    try:
        import tkinter
    except Exception:
        return candidates[0]

    root = tkinter.Tk()
    root.title("モデルの割り当てを選んでください")
    root.attributes("-topmost", True)
    tkinter.Label(
        root,
        text=f'MMDが "{dialog_name}" という名前のモデルを探しています。\n'
             "ファイル名からは特定できなかったので、どれを使うか選んでください。\n"
             "(このモデルの本来の名前ではなく、MMD内部の管理名です)",
        justify="left", padx=12, pady=10,
    ).pack(anchor="w")

    listbox = tkinter.Listbox(root, width=100, height=min(10, len(candidates)))
    for c in candidates:
        listbox.insert(tkinter.END, c)
    listbox.select_set(0)
    listbox.pack(padx=12, pady=(0, 10), fill="both", expand=True)

    result = {"path": None}

    def on_ok():
        sel = listbox.curselection()
        if sel:
            result["path"] = candidates[sel[0]]
        root.destroy()

    def on_skip():
        root.destroy()

    btns = tkinter.Frame(root, pady=10)
    btns.pack()
    tkinter.Button(btns, text="これを使う", width=12, command=on_ok).pack(side="left", padx=5)
    tkinter.Button(btns, text="スキップ", command=on_skip).pack(side="left", padx=5)

    root.mainloop()
    return result["path"]


def _drive_label(drive):
    try:
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            drive, ctypes.pointer(free_bytes), ctypes.pointer(total_bytes), None
        )
        total_gb = total_bytes.value / (1024 ** 3)
        free_gb = free_bytes.value / (1024 ** 3)
        size_text = f"（{total_gb:.0f}GB中 空き{free_gb:.0f}GB）"
    except Exception:
        size_text = ""
    tag = "  ※システムドライブ" if drive.upper() == system_drive() else ""
    return f"{drive}{size_text}{tag}"


def pick_roots():
    """Let the user choose which drives to search, instead of silently
    scanning every fixed drive. A drive with lots of files (especially the
    system drive) can turn a first run into a many-minutes wait, worse
    under real-time antivirus scanning — better to make that cost visible
    and optional than to surprise people with it."""
    try:
        import tkinter
    except Exception:
        return None

    all_drives = list_all_fixed_drives()
    defaults = detect_fixed_drives()

    root = tkinter.Tk()
    root.title("検索するドライブを選択")
    root.attributes("-topmost", True)
    tkinter.Label(
        root,
        text="ファイルを探すドライブを選んでください。\n(チェックが多いほど時間がかかります。システムドライブは通常不要です)",
        justify="left", padx=12, pady=10,
    ).pack(anchor="w")

    vars_by_drive = {}
    frame = tkinter.Frame(root, padx=12)
    frame.pack(anchor="w", fill="x")
    for d in all_drives:
        v = tkinter.BooleanVar(value=(d in defaults))
        tkinter.Checkbutton(frame, text=_drive_label(d), variable=v).pack(anchor="w")
        vars_by_drive[d] = v

    result = {"roots": None}

    def on_ok():
        result["roots"] = [d for d, v in vars_by_drive.items() if v.get()]
        root.destroy()

    def on_cancel():
        root.destroy()

    btns = tkinter.Frame(root, pady=10)
    btns.pack()
    tkinter.Button(btns, text="OK", width=10, command=on_ok).pack(side="left", padx=5)
    tkinter.Button(btns, text="キャンセル（デフォルトで続行）", command=on_cancel).pack(side="left", padx=5)

    root.mainloop()
    return result["roots"] or None


def main():
    argv = sys.argv[1:]
    # Someone dragging a .pmm straight onto pmm_fixer.exe (instead of the
    # scan/load .bat launchers) would otherwise hit "invalid choice" from
    # argparse and the console window would vanish before anyone could read
    # it. Treat a bare "<something>.pmm" first argument as `scan` for that.
    if argv and argv[0].lower().endswith(".pmm") and os.path.exists(argv[0]):
        argv = ["scan"] + argv

    parser = argparse.ArgumentParser(description="MMD .pmm のリンク切れ調査・修復ツール")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("pmm_path", nargs="?", default=None,
                        help="対象の .pmm ファイル（省略時はファイル選択ダイアログが開きます）")
        p.add_argument("--roots", nargs="+", default=None,
                        help="検索対象のドライブ/フォルダ（省略時はシステムドライブ以外の固定ドライブを自動検出）")
        p.add_argument("--report", default=None, help="出力するExcelファイルのパス")
        p.add_argument("--rebuild-index", action="store_true", help="ファイルインデックスを作り直す")

    p_scan = sub.add_parser("scan", help="リンク切れを調査してExcelレポートを作る")
    add_common(p_scan)

    p_load = sub.add_parser("load", help="調査した上でMMDに自動投入する")
    add_common(p_load)
    p_load.add_argument("--mmd-exe", default=None,
                         help="MikuMikuDance.exe のパス（省略時は検索対象ドライブ内を自動検索）")

    args = parser.parse_args(argv)

    if args.pmm_path is None:
        args.pmm_path = pick_pmm_file()
        if args.pmm_path is None:
            print("ファイルが選択されなかったため終了します。")
            return

    if args.roots is None:
        args.roots = pick_roots() or detect_fixed_drives()

    if args.command == "scan":
        do_scan(args.pmm_path, args.roots, args.report, args.rebuild_index)
    elif args.command == "load":
        do_load(args.pmm_path, args.roots, args.report, args.rebuild_index, args.mmd_exe)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (0, None):
            print("\n引数の指定に問題があるようです。上の内容を確認してください。")
            print('例: pmm_fixer.exe scan "対象.pmm"')
            input("Enterキーを押すと閉じます...")
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        print("\nエラーが発生しました。上の内容を確認してください。")
        input("Enterキーを押すと閉じます...")
        sys.exit(1)
