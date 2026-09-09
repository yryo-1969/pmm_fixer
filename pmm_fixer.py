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
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
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


def detect_fixed_drives():
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


def detect_mmd_exe(roots):
    """Best-effort search for MikuMikuDance.exe under the given roots (shallow-ish)."""
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth > 3:
                dirnames[:] = []
                continue
            if "MikuMikuDance.exe" in filenames:
                return os.path.join(dirpath, "MikuMikuDance.exe")
    return None


def do_scan(pmm_path, roots, report_path, rebuild_index):
    print(f"PMM解析中: {pmm_path}")
    refs = extract_pmm_refs(pmm_path)
    print(f"参照ファイル {len(refs)} 件を検出")

    print(f"ファイルインデックス構築中 (対象: {roots}) ...")
    index, built_at, used_roots = build_file_index(roots, TOOL_DIR, force_rebuild=rebuild_index)
    print(f"インデックス作成日時: {built_at}（{sum(len(v) for v in index.values())} ファイル）")

    results = resolve_refs(refs, index)

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
    return results, report_path


def do_load(pmm_path, roots, report_path, rebuild_index, mmd_exe):
    import mmd_dialogs

    if mmd_exe is None:
        print("MikuMikuDance.exe を自動検索中 ...")
        mmd_exe = detect_mmd_exe(roots)
        if mmd_exe is None:
            print("MikuMikuDance.exe が見つかりませんでした。--mmd-exe で明示的にパスを指定してください。")
            return
        print(f"検出: {mmd_exe}")

    results, report_path = do_scan(pmm_path, roots, report_path, rebuild_index)

    resolved_by_name = {}
    for r in results:
        if r["resolved_path"] and not r["exists"]:
            resolved_by_name[r["name"]] = r["resolved_path"]
            base_noext = os.path.splitext(os.path.basename(r["old_path"]))[0]
            resolved_by_name[base_noext] = r["resolved_path"]
            resolved_by_name[os.path.basename(r["old_path"])] = r["resolved_path"]

    print(f"MMD起動確認中 ({mmd_exe}) ...")
    mmd_hwnd = mmd_dialogs.launch_mmd(mmd_exe)
    print(f"MMDウィンドウ: {mmd_hwnd}")

    print("プロジェクトの自動読み込みを開始します。")
    ok = mmd_dialogs.run_autoload(mmd_hwnd, os.path.abspath(pmm_path), resolved_by_name)
    if ok:
        print("完了: プロジェクトの読み込みに成功しました。")
    else:
        print("中断: 手動対応が必要なダイアログがあります。MMDの画面を確認してください。")


def main():
    parser = argparse.ArgumentParser(description="MMD .pmm のリンク切れ調査・修復ツール")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("pmm_path", help="対象の .pmm ファイル")
        p.add_argument("--roots", nargs="+", default=None,
                        help="検索対象のドライブ/フォルダ（省略時はPC内の固定ドライブを自動検出）")
        p.add_argument("--report", default=None, help="出力するExcelファイルのパス")
        p.add_argument("--rebuild-index", action="store_true", help="ファイルインデックスを作り直す")

    p_scan = sub.add_parser("scan", help="リンク切れを調査してExcelレポートを作る")
    add_common(p_scan)

    p_load = sub.add_parser("load", help="調査した上でMMDに自動投入する")
    add_common(p_load)
    p_load.add_argument("--mmd-exe", default=None,
                         help="MikuMikuDance.exe のパス（省略時は検索対象ドライブ内を自動検索）")

    args = parser.parse_args()
    if args.roots is None:
        args.roots = detect_fixed_drives()

    if args.command == "scan":
        do_scan(args.pmm_path, args.roots, args.report, args.rebuild_index)
    elif args.command == "load":
        do_load(args.pmm_path, args.roots, args.report, args.rebuild_index, args.mmd_exe)


if __name__ == "__main__":
    main()
