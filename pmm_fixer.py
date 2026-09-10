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
import time

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
    resolve_emm_refs,
    resolve_refs,
    write_effect_report_xlsx,
    write_plain_listing_for_emm,
    write_plain_listing_for_refs,
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


def search_all_drives_for_file(filename, on_progress=None):
    """Last-resort search for one specific filename across every fixed
    drive, including the system drive (unlike the normal indexed scan,
    which skips it by default and only covers whichever drives were
    checked at startup). No caching, since this targets a single file the
    normal scan already failed to resolve — used from
    pick_accessory_fallback so a human isn't stuck browsing blind when a
    drive they didn't select (or didn't exist yet at scan time) turns out
    to hold it. on_progress(root, matches_so_far) is called periodically."""
    target = filename.lower()
    matches = []
    last_report = time.time()
    for root in list_all_fixed_drives():
        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                if f.lower() == target:
                    matches.append(os.path.join(dirpath, f))
            if on_progress and time.time() - last_report >= 0.5:
                on_progress(root, len(matches))
                last_report = time.time()
    return matches


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


def _open_report(*paths):
    """Try to open each path in turn with whatever's associated with it,
    stopping at the first one that actually launches. Excel (or any other
    .xlsx-capable app) isn't guaranteed to be installed, so callers pass
    the .xlsx first and a plain .txt twin as the fallback — Notepad can
    always open that one."""
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        try:
            os.startfile(p)
            return True
        except OSError:
            continue
    return False


def notify_missing(pmm_path, results, report_path, txt_report_path=None, show_all_clear=True):
    """Pop up a plain message box so a non-technical user notices unresolved
    files without having to read the console or open the Excel report."""
    txt_note = f"\n（Excelが無い場合はこちらでも見られます:\n{txt_report_path}）" if txt_report_path else ""
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
            f"詳細レポート:\n{report_path}{txt_note}",
            "MMDリボーン - 調査完了",
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
        + f"\n\n詳細レポート:\n{report_path}{txt_note}"
    )
    ctypes.windll.user32.MessageBoxW(0, message, "MMDリボーン - 未解決のファイルがあります", MB_OK | MB_ICONWARNING | MB_TOPMOST)


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
        report_path = os.path.join(_report_dir(), _default_report_filename(pmm_path))
    write_report_xlsx(pmm_path, results, built_at, used_roots, report_path)
    print(f"レポート出力: {report_path}")

    # Excel (or another .xlsx-capable app) isn't guaranteed to be installed
    # on every PC this runs on, so always write a zero-dependency plain
    # text twin next to it — same data, openable in Notepad by anyone.
    txt_report_path = os.path.splitext(report_path)[0] + ".txt"
    write_plain_listing_for_refs(pmm_path, results, txt_report_path)

    if popup:
        notify_missing(pmm_path, results, report_path, txt_report_path, show_all_clear=popup_all_clear)

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
                "MMDリボーン - MikuMikuDanceが見つかりません",
                MB_OK | MB_ICONWARNING | MB_TOPMOST,
            )
            return
        print(f"検出: {mmd_exe}")

    results, report_path = do_scan(pmm_path, roots, report_path, rebuild_index, index_bundle=index_bundle,
                                    popup_all_clear=False)

    resolved_by_name = {}
    model_fallback_queue = []
    # MMD's own dialog for a missing model shows its internal object name
    # (often blank/"無名", or some unrelated leftover label — never the
    # real filename, see run_autoload's docstring), so it's useless as a
    # search hint. What scan actually knows and MMD doesn't tell us here
    # is the real original basename from the pmm itself — keep the ones
    # that never got auto-resolved, so the human has something concrete to
    # search for in pick_missing_model_fallback below instead of guessing
    # blind (ocyacya hit this live with 無名/Chinese-named models, 2026-09-10).
    unresolved_model_names = []
    for r in results:
        if r["resolved_path"] and not r["exists"]:
            resolved_by_name[r["name"]] = r["resolved_path"]
            base_noext = os.path.splitext(os.path.basename(r["old_path"]))[0]
            resolved_by_name[base_noext] = r["resolved_path"]
            resolved_by_name[os.path.basename(r["old_path"])] = r["resolved_path"]
            if r["kind"].startswith("モデル"):
                model_fallback_queue.append(r["resolved_path"])
        elif not r["resolved_path"] and not r["exists"] and r["kind"].startswith("モデル"):
            unresolved_model_names.append(os.path.basename(r["old_path"]))

    print(f"MMD起動確認中 ({mmd_exe}) ...")
    mmd_hwnd = mmd_dialogs.launch_mmd(mmd_exe)
    print(f"MMDウィンドウ: {mmd_hwnd}")

    # MMDリボーンはMMD本体のダイアログを自動でクリックしていくが、候補が
    # 複数あるモデルはpick_model_fallback()の別ウィンドウで人間に確認する。
    # その待ち時間にMMD本体のダイアログへ直接クリックしてしまうと、MMDは
    # それを正規の回答として先に進めてしまい、pick_model_fallback側の回答
    # が宙に浮いて処理が止まる（ocyacya、2026-09-10に実機で発生）。事前に
    # 一度だけ注意喚起しておく。
    ctypes.windll.user32.MessageBoxW(
        0,
        "これからMMDリボーンがMMD本体を自動操作します。\n\n"
        "候補が複数あるモデルや、候補なしのアクセサリは、\n"
        "別ウィンドウで選択画面が出ます。\n"
        "その間、MMD本体の画面は直接操作しないでください。\n"
        "（MMD側を先に押してしまうと、選択画面の回答とズレて\n"
        "　処理が止まってしまいます）\n\n"
        "「MMDリボーン - 別名保存の確認」画面が出れば完了です。",
        "MMDリボーン - 自動操作中は操作しないでください",
        MB_OK | MB_ICONWARNING | MB_TOPMOST,
    )

    # do_scan's `results` is a snapshot from before any of this run's manual
    # picks — the all_resolved check below reads it directly, so a model or
    # accessory a human successfully found via one of the pickers wouldn't
    # otherwise count, and the save-prompt would stay hidden even though
    # the load actually succeeded (ocyacya noticed this gap, 2026-09-10;
    # fixed by patching the matching `results` entry's resolved_path here
    # whenever a picker returns something attributable to a specific entry).
    def _mark_resolved(kind_prefix, basename, path):
        for r in results:
            if (r["kind"].startswith(kind_prefix) and not r["exists"] and not r["resolved_path"]
                    and os.path.basename(r["old_path"]) == basename):
                r["resolved_path"] = path
                return

    def pick_missing_model(mmd_internal_name):
        path, matched_name = pick_missing_model_fallback(mmd_internal_name, unresolved_model_names)
        if path and matched_name:
            _mark_resolved("モデル", matched_name, path)
            if matched_name in unresolved_model_names:
                unresolved_model_names.remove(matched_name)
        return path

    def pick_accessory(name):
        path = pick_accessory_fallback(name)
        if path:
            _mark_resolved("アクセサリ", name, path)
        return path

    print("プロジェクトの自動読み込みを開始します。")
    ok = mmd_dialogs.run_autoload(mmd_hwnd, os.path.abspath(pmm_path), resolved_by_name,
                                   model_fallback_queue=model_fallback_queue,
                                   pick_model_callback=pick_model_fallback,
                                   pick_accessory_callback=pick_accessory,
                                   pick_missing_model_callback=pick_missing_model)
    if not ok:
        print("中断: 手動対応が必要なダイアログがあります。MMDの画面を確認してください。")
        return

    print("完了: プロジェクトの読み込みに成功しました。")

    media_ok = _reload_resolved_media(mmd_hwnd, mmd_dialogs, results)

    try:
        pmm_mtime = os.path.getmtime(pmm_path)
    except OSError:
        pmm_mtime = None
    _check_effect_assignments(mmd_hwnd, mmd_dialogs, index_bundle[0], pmm_path, pmm_mtime)

    all_resolved = all(r["exists"] or r["resolved_path"] for r in results) and media_ok
    if not all_resolved:
        return

    answer = ctypes.windll.user32.MessageBoxW(
        0,
        "全てのリンク切れが解決できました。\n\n"
        "修復済みのプロジェクトを別名で保存しますか？\n"
        "（元のファイルはそのまま残ります）",
        "MMDリボーン - 別名保存の確認",
        MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
    )
    if answer != IDYES:
        return

    new_path = _suggest_save_path(pmm_path)
    print(f"別名保存中: {new_path}")
    saved = mmd_dialogs.save_project_as(mmd_hwnd, new_path)
    if saved:
        print(f"保存完了: {new_path}")
        new_report_path = os.path.join(
            os.path.dirname(new_path),
            f"{os.path.splitext(os.path.basename(new_path))[0]}_ファイル場所一覧.xlsx",
        )
        print("保存したプロジェクトのファイル場所一覧を作成中 ...")
        # roots/index_bundle are already built above for this same run, so
        # this re-scan just resolves against the in-memory index — no new
        # drive walk. Every entry should come back "OK（元のパスに存在）"
        # since we just saved the project with these exact paths, so this
        # doubles as a plain "where is everything" listing, not just a
        # broken-link report.
        _, new_report_path = do_scan(new_path, roots, new_report_path, rebuild_index=False,
                                      index_bundle=index_bundle, popup=False)
        new_report_txt_path = os.path.splitext(new_report_path)[0] + ".txt"
        _open_report(new_report_path, new_report_txt_path)

        answer2 = ctypes.windll.user32.MessageBoxW(
            0, f"保存しました:\n{new_path}\n\n"
               f"モデル・アクセサリ・モーションなど各ファイルの場所一覧も作成しました:\n{new_report_path}\n"
               f"（Excelが無い場合はこちらでも見られます:\n{new_report_txt_path}）\n\n"
               "この保存場所（パス）を、コピーしやすいようにテキストファイルにも出力しますか？",
            "MMDリボーン - 保存完了",
            MB_YESNO | MB_ICONINFORMATION | MB_TOPMOST,
        )
        if answer2 == IDYES:
            txt_path = os.path.join(
                os.path.dirname(new_path),
                f"{os.path.splitext(os.path.basename(new_path))[0]}_保存場所.txt",
            )
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(new_path + "\n")
            print(f"保存場所テキスト出力: {txt_path}")
            try:
                os.startfile(txt_path)
            except Exception:
                pass
    else:
        print("保存に失敗しました。MMDの画面を確認してください。")


def _check_effect_assignments(mmd_hwnd, mmd_dialogs, index, pmm_path, pmm_mtime):
    """Report-only check of MMEffect's live effect-file assignments.

    Unlike models/accessories/wav/avi, .fx/.fxsub paths often aren't
    stored as plain strings inside the .pmm at all (confirmed by directly
    byte-scanning real projects — a missing .fx MikuMikuEffect complained
    about had zero occurrences anywhere in the pmm's raw bytes), so the
    only way to see what's actually assigned is to ask MME itself, live,
    via its own assignment-manager dialog. This never rewrites anything:
    picking the right replacement among multiple candidate .fx variants
    (a real case: an effect pack shipping 5 different material presets)
    needs a person's judgment, so this only reports what it finds.
    """
    tmp_emm = os.path.join(TOOL_DIR, f"_effects_tmp_{os.getpid()}.emm")
    if os.path.exists(tmp_emm):
        try:
            os.remove(tmp_emm)
        except OSError:
            pass

    print("MMEffectの割り当て状態を確認中 ...")
    ok = mmd_dialogs.export_effect_assignments(mmd_hwnd, tmp_emm)
    if not ok:
        print("  読み取れませんでした（MMEffectが入っていないか、プロジェクトがエフェクトを使っていない可能性があります）。スキップします。")
        return

    try:
        results = resolve_emm_refs(tmp_emm, index, pmm_mtime=pmm_mtime)
    finally:
        try:
            os.remove(tmp_emm)
        except OSError:
            pass

    if not results:
        print("  エフェクトファイル(.fx/.fxsub)の割り当ては見つかりませんでした。")
        return

    broken = [r for r in results if not r["exists"]]
    print(f"  エフェクトファイル {len(results)} 件中 {len(broken)} 件が現在のパスに存在しません。")
    if not broken:
        return

    effect_report_path = os.path.join(
        TOOL_DIR, f"{os.path.splitext(os.path.basename(pmm_path))[0]}_MMEffect状態.xlsx"
    )
    write_effect_report_xlsx(pmm_path, results, effect_report_path)
    effect_report_txt_path = os.path.splitext(effect_report_path)[0] + ".txt"
    write_plain_listing_for_emm(pmm_path, results, effect_report_txt_path)
    print(f"  MMEffectレポート出力: {effect_report_path}（Excelが無い場合: {effect_report_txt_path}）")
    _open_report(effect_report_path, effect_report_txt_path)


def _reload_resolved_media(mmd_hwnd, mmd_dialogs, results):
    """MMD's own project-load dialogs never offer a substitute-location
    option for a broken WAV/AVI reference (unlike models/accessories) — it
    just silently drops it. For anything scan resolved a new path for,
    load it fresh through the WAV/AVI menu commands instead. For anything
    scan found NO candidate for, ask via the same search/browse/skip
    picker used for accessories (ocyacya, 2026-09-10 — previously this
    just silently dropped the reference with no chance to find it).
    Returns False if any resolved media failed to load, so the caller
    doesn't offer to save a project that's still missing something
    (declining/skipping in the picker doesn't count as a failure, same as
    skipping a model/accessory doesn't).
    """
    menu_by_kind = {"音声(.wav)": mmd_dialogs.MENU_ID_LOAD_WAV, "背景動画(.avi)": mmd_dialogs.MENU_ID_LOAD_AVI}
    picker_by_kind = {"音声(.wav)": pick_missing_wav_fallback, "背景動画(.avi)": pick_missing_avi_fallback}
    ok = True
    for r in results:
        menu_id = menu_by_kind.get(r["kind"])
        if menu_id is None or r["exists"]:
            continue
        path = r["resolved_path"]
        if not path:
            name = os.path.basename(r["old_path"])
            picker = picker_by_kind.get(r["kind"])
            path = picker(name) if picker else None
            if not path:
                print(f"{r['kind']} の候補が見つからず、スキップしました: {name}")
                continue
            r["resolved_path"] = path  # so the final all_resolved check sees it too
        print(f"{r['kind']} を読み込み直します: {path}")
        if not mmd_dialogs.load_media_file(mmd_hwnd, menu_id, path):
            print(f"  読み込みに失敗しました: {path}")
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
_REPORT_DIR_FILE = "last_report_dir.txt"


def _load_last_dir(filename=_LAST_DIR_FILE):
    path = os.path.join(TOOL_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = f.read().strip()
        return d if d and os.path.isdir(d) else None
    except Exception:
        return None


def _save_last_dir(folder, filename=_LAST_DIR_FILE):
    try:
        with open(os.path.join(TOOL_DIR, filename), "w", encoding="utf-8") as f:
            f.write(folder)
    except Exception:
        pass


def _default_report_filename(pmm_path):
    base = os.path.splitext(os.path.basename(pmm_path))[0]
    return f"{base}_リンク切れ一覧.xlsx"


def _report_dir():
    """Where scan/load reports land when the caller doesn't pick a folder
    (e.g. reports triggered internally, not through main()'s picker).
    Falls back to the tool's own folder, same as always."""
    return _load_last_dir(_REPORT_DIR_FILE) or TOOL_DIR


def pick_report_dir():
    """Ask where to save this run's Excel/txt reports. Remembers the choice
    for next time; canceling keeps whatever was chosen before (or the
    tool's own folder, the original default, if nothing was ever chosen)."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(
        title="レポート（Excel/txt）の保存先フォルダを選んでください",
        initialdir=_report_dir(),
    )
    root.destroy()
    if folder:
        _save_last_dir(folder, _REPORT_DIR_FILE)
    return folder or None


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
        text="⚠ このウィンドウで選ぶまで、MMD本体の画面は操作しないでください",
        justify="left", padx=12, pady=6, fg="#c00000", font=("", 10, "bold"),
    ).pack(anchor="w")
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

    root.lift()
    root.focus_force()
    root.mainloop()
    return result["path"]


def pick_missing_file_fallback(window_title, kind_label, name, filetypes, stop_button_text):
    """Generic 'no candidate found for this named file' picker: search all
    drives for `name` (its real filename — unlike a missing model, the
    caller here always has a genuine filename to search by), browse for a
    substitute manually (filtered to `filetypes`), or give up. Shared by
    accessories (.x, during project load) and WAV/AVI (during the post-
    load media reload step, since MMD's own dialogs never offer a
    substitute-location option for those at all and just silently drop
    the reference — see mmd_dialogs.load_media_file). 2026-09-10: for
    accessories specifically, auto-continuing without asking was tried and
    rejected twice (cancelling MMD's browse dialog aborts the whole
    remaining project load; a blank placeholder file can silently break
    rendering for lighting/post-effect controllers) — a human has to
    actually supply or decline a real file."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None

    root = tkinter.Tk()
    root.title(window_title)
    root.attributes("-topmost", True)
    tkinter.Label(
        root,
        text="⚠ このウィンドウで選ぶまで、MMD本体の画面は操作しないでください",
        justify="left", padx=12, pady=6, fg="#c00000", font=("", 10, "bold"),
    ).pack(anchor="w")
    tkinter.Label(
        root,
        text=f'MMDが "{name}" という{kind_label}を探していますが、\n'
             "候補が見つかりませんでした（選択したドライブの中には無かったようです）。",
        justify="left", padx=12, pady=6,
    ).pack(anchor="w")

    status = tkinter.StringVar(value="")
    tkinter.Label(root, textvariable=status, justify="left", padx=12, wraplength=480).pack(anchor="w")

    result = {"path": None}

    def finish(path):
        result["path"] = path
        root.destroy()

    def on_browse():
        path = filedialog.askopenfilename(
            title=f'"{name}" の代わりに使うファイルを選んでください',
            filetypes=filetypes,
        )
        if path:
            finish(path)

    def on_manual():
        finish(None)

    results_frame = tkinter.Frame(root)
    listbox = tkinter.Listbox(results_frame, width=100, height=6)

    def on_use_selected():
        sel = listbox.curselection()
        if sel:
            finish(listbox.get(sel[0]))

    def on_search_again():
        search_btn.config(state="disabled")

        def progress(drive, count):
            status.set(f"検索中: {drive} を確認中...（{count}件見つかりました）")
            root.update_idletasks()

        status.set("全ドライブ（システムドライブ含む）を検索しています。数分かかる場合があります...\n"
                    "（この間ウィンドウの上部に「応答なし」と出ることがありますが、\n"
                    "　下の検索状況が更新されていれば正常に動作中です）")
        root.update_idletasks()
        matches = search_all_drives_for_file(name, on_progress=progress)
        search_btn.config(state="normal")
        if not matches:
            status.set(f"見つかりませんでした。「ファイルを選ぶ」で手動指定するか、"
                        f"「{stop_button_text}」を押してください。")
            return
        status.set(f"{len(matches)}件見つかりました。使うものを選んで「これを使う」を押してください:")
        listbox.delete(0, tkinter.END)
        for m in matches:
            listbox.insert(tkinter.END, m)
        listbox.select_set(0)
        listbox.pack(padx=0, pady=(4, 6), fill="both", expand=True)
        use_btn.pack(side="left", padx=5)

    btns = tkinter.Frame(root, pady=6)
    btns.pack()
    tkinter.Button(btns, text="ファイルを選ぶ", width=14, command=on_browse).pack(side="left", padx=5)
    search_btn = tkinter.Button(btns, text="全ドライブでもう一度探す", command=on_search_again)
    search_btn.pack(side="left", padx=5)
    use_btn = tkinter.Button(btns, text="これを使う", command=on_use_selected)
    tkinter.Button(btns, text=stop_button_text, command=on_manual).pack(side="left", padx=5)

    results_frame.pack(padx=12, pady=(0, 10), fill="both", expand=True)

    root.lift()
    root.focus_force()
    root.mainloop()
    return result["path"]


def pick_accessory_fallback(name):
    return pick_missing_file_fallback(
        "アクセサリファイルを選んでください", "アクセサリファイル(.x)", name,
        [("X ファイル", "*.x"), ("すべてのファイル", "*.*")],
        "MMD側で手動対応する",
    )


def pick_missing_wav_fallback(name):
    return pick_missing_file_fallback(
        "音声ファイルを選んでください", "音声ファイル(.wav)", name,
        [("WAV ファイル", "*.wav"), ("すべてのファイル", "*.*")],
        "この音声はスキップする",
    )


def pick_missing_avi_fallback(name):
    return pick_missing_file_fallback(
        "背景動画ファイルを選んでください", "背景動画ファイル(.avi)", name,
        [("AVI ファイル", "*.avi"), ("すべてのファイル", "*.*")],
        "この動画はスキップする",
    )


def pick_missing_model_fallback(mmd_internal_name, unresolved_names):
    """MMD can't find a model and neither name-lookup nor the leftover
    fallback queue found a candidate. MMD's own name for this dialog is
    usually useless for a human too — blank ("無名"), or some unrelated
    leftover label — so it's not shown as something to search by. What IS
    useful: `unresolved_names`, the real original basenames scan already
    extracted from the pmm itself for every model that never got
    auto-resolved project-wide (e.g. a Chinese-named file scan's string
    matching couldn't connect to anything) — pick one of those as the
    search target instead of guessing from MMD's label.

    Returns (path, matched_name) — matched_name identifies which entry in
    `unresolved_names` this resolves (so the caller can patch its scan
    results and stop offering it as a hint again): the searched hint when
    resolved via search, the sole entry when there's exactly one and
    "ファイルを選ぶ" was used (unambiguous), otherwise None if it can't be
    attributed with confidence. (path, None) still resolves the model in
    MMD; it just means the final save-prompt can't be sure everything's
    fixed, same as before this attribution existed."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None, None

    root = tkinter.Tk()
    root.title("モデルファイルを選んでください")
    root.attributes("-topmost", True)
    tkinter.Label(
        root,
        text="⚠ このウィンドウで選ぶまで、MMD本体の画面は操作しないでください",
        justify="left", padx=12, pady=6, fg="#c00000", font=("", 10, "bold"),
    ).pack(anchor="w")
    shown_name = mmd_internal_name if mmd_internal_name else "（名前なし・無名モデル）"
    tkinter.Label(
        root,
        text=f'MMDが "{shown_name}" というモデルを探していますが、候補が見つかりませんでした。\n'
             "(これはMMD内部の管理名で、実際のファイル名ではありません)",
        justify="left", padx=12, pady=6,
    ).pack(anchor="w")

    status = tkinter.StringVar(value="")
    tkinter.Label(root, textvariable=status, justify="left", padx=12, wraplength=520).pack(anchor="w")

    result = {"path": None, "name": None}
    last_searched = {"name": None}

    def finish(path, matched_name):
        result["path"] = path
        result["name"] = matched_name
        root.destroy()

    def on_browse():
        path = filedialog.askopenfilename(
            title="代わりに使うモデルファイルを選んでください",
            filetypes=[("PMX/PMD ファイル", "*.pmx;*.pmd"), ("すべてのファイル", "*.*")],
        )
        if path:
            # Only attributable without ambiguity if there was just one
            # possible unresolved model to begin with.
            solo = unresolved_names[0] if len(unresolved_names) == 1 else None
            finish(path, solo)

    def on_skip():
        finish(None, None)

    results_frame = tkinter.Frame(root)
    results_listbox = tkinter.Listbox(results_frame, width=100, height=6)

    def on_use_selected():
        sel = results_listbox.curselection()
        if sel:
            finish(results_listbox.get(sel[0]), last_searched["name"])

    def on_search_hint():
        sel = hint_listbox.curselection()
        if not sel:
            status.set("先に、検索したい元のファイル名を上のリストから選んでください。")
            return
        target = unresolved_names[sel[0]]
        last_searched["name"] = target
        search_btn.config(state="disabled")

        def progress(drive, count):
            status.set(f"検索中: {drive} を確認中...（{count}件見つかりました）")
            root.update_idletasks()

        status.set(f'"{target}" を全ドライブ（システムドライブ含む）で検索しています。数分かかる場合があります...\n'
                    "（この間ウィンドウの上部に「応答なし」と出ることがありますが、\n"
                    "　下の検索状況が更新されていれば正常に動作中です）")
        root.update_idletasks()
        matches = search_all_drives_for_file(target, on_progress=progress)
        search_btn.config(state="normal")
        if not matches:
            status.set(f'"{target}" は見つかりませんでした。「ファイルを選ぶ」で手動指定するか、'
                        "「スキップする」を押してください。")
            return
        status.set(f"{len(matches)}件見つかりました。使うものを選んで「これを使う」を押してください:")
        results_listbox.delete(0, tkinter.END)
        for m in matches:
            results_listbox.insert(tkinter.END, m)
        results_listbox.select_set(0)
        results_listbox.pack(padx=0, pady=(4, 6), fill="both", expand=True)
        use_btn.pack(side="left", padx=5)

    if unresolved_names:
        tkinter.Label(
            root,
            text="スキャンで見つからなかった元のモデル名（ヒント。1つ選んで検索できます）:",
            justify="left", padx=12, pady=6,
        ).pack(anchor="w")
        hint_listbox = tkinter.Listbox(root, width=100, height=min(6, len(unresolved_names)))
        for n in unresolved_names:
            hint_listbox.insert(tkinter.END, n)
        hint_listbox.select_set(0)
        hint_listbox.pack(padx=12, pady=(0, 6), fill="x")
    else:
        hint_listbox = None

    btns = tkinter.Frame(root, pady=6)
    btns.pack()
    tkinter.Button(btns, text="ファイルを選ぶ", width=14, command=on_browse).pack(side="left", padx=5)
    if hint_listbox is not None:
        search_btn = tkinter.Button(btns, text="選んだ名前で全ドライブ検索", command=on_search_hint)
        search_btn.pack(side="left", padx=5)
    use_btn = tkinter.Button(btns, text="これを使う", command=on_use_selected)
    tkinter.Button(btns, text="スキップする", command=on_skip).pack(side="left", padx=5)

    results_frame.pack(padx=12, pady=(0, 10), fill="both", expand=True)

    root.lift()
    root.focus_force()
    root.mainloop()
    return result["path"], result["name"]


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

    if args.report is None:
        # Ask only the very first time (no folder remembered yet) — after
        # that, reuse the remembered folder silently instead of prompting
        # on every single run (ocyacya found the every-run prompt annoying
        # once the folder rarely changes; 2026-09-10).
        if _load_last_dir(_REPORT_DIR_FILE) is None:
            pick_report_dir()
        args.report = os.path.join(_report_dir(), _default_report_filename(args.pmm_path))

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
