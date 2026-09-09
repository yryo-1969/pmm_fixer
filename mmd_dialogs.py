"""Win32 automation for driving MikuMikuDance's project-load dialogs."""
import re
import subprocess
import time

import win32con
import win32gui
import win32process

MENU_ID_OPEN_PROJECT = 205
WM_COMMAND = win32con.WM_COMMAND
WM_SETTEXT = win32con.WM_SETTEXT
EDIT_FILENAME_ID = 1148
OPEN_BUTTON_ID = 1
CANCEL_BUTTON_ID = 2


def find_mmd_window(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title.startswith("MikuMikuDance"):
                    found.append(hwnd)
            return True

        win32gui.EnumWindows(cb, None)
        if found:
            return found[0]
        time.sleep(0.5)
    return None


def launch_mmd(exe_path, timeout=20):
    hwnd = find_mmd_window(timeout=1)
    if hwnd:
        return hwnd
    subprocess.Popen([exe_path], cwd=exe_path.rsplit("\\", 1)[0])
    hwnd = find_mmd_window(timeout=timeout)
    if not hwnd:
        raise RuntimeError("MMDの起動を確認できませんでした")
    return hwnd


def _pid_of(hwnd):
    _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def list_owned_dialogs(mmd_hwnd):
    target_pid = _pid_of(mmd_hwnd)
    dialogs = []

    def cb(hwnd, _):
        if hwnd == mmd_hwnd or not win32gui.IsWindowVisible(hwnd):
            return True
        if _pid_of(hwnd) == target_pid:
            dialogs.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return dialogs


def get_dialog_text(hwnd):
    text = win32gui.GetWindowText(hwnd)
    static = ""

    def cb(child, _):
        nonlocal static
        cls = win32gui.GetClassName(child)
        if cls == "Static":
            t = win32gui.GetWindowText(child)
            if t:
                static += t + "\n"
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return text, static.strip()


def has_control(hwnd, control_id):
    try:
        return win32gui.GetDlgItem(hwnd, control_id) != 0
    except Exception:
        return False


def _wait_for_close(hwnd, timeout=10):
    """Poll until hwnd stops being a valid window, so callers don't re-detect
    the same dialog (mid-close, after an async PostMessage click) as new."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not win32gui.IsWindow(hwnd):
            return True
        time.sleep(0.1)
    return False


def click_dialog_button(hwnd, control_id):
    # PostMessage, not SendMessage: clicking a button can synchronously
    # cascade into MMD showing the next dialog before returning from the
    # WM_COMMAND handler. SendMessage would block this thread until that
    # whole chain goes idle, deadlocking against our own polling loop.
    btn = win32gui.GetDlgItem(hwnd, control_id)
    win32gui.PostMessage(hwnd, WM_COMMAND, (0 << 16) | control_id, btn)
    _wait_for_close(hwnd)


def fill_and_open_file(hwnd, path):
    edit = win32gui.GetDlgItem(hwnd, EDIT_FILENAME_ID)
    win32gui.SendMessage(edit, WM_SETTEXT, 0, path)
    time.sleep(0.2)
    open_btn = win32gui.GetDlgItem(hwnd, OPEN_BUTTON_ID)
    win32gui.PostMessage(hwnd, WM_COMMAND, (0 << 16) | OPEN_BUTTON_ID, open_btn)
    _wait_for_close(hwnd)


def trigger_open_project(mmd_hwnd):
    win32gui.PostMessage(mmd_hwnd, WM_COMMAND, (0 << 16) | MENU_ID_OPEN_PROJECT, 0)


MODEL_MISSING_RE = re.compile(r'"(.+?)"のモデルファイルが見つかりません')
SUBSTITUTE_RE = re.compile(r'[（(](.+?)[）)]が見つかりません')

MODEL_SPECIFY_ID = 687
MODEL_SKIP_ID = 688
MODEL_ABORT_ID = 689


def run_autoload(mmd_hwnd, pmm_path, resolved_by_name, log=print, max_steps=200, step_timeout=15):
    """Drive the project-load dialog sequence. resolved_by_name: {basename_without_ext_or_full: path}."""
    trigger_open_project(mmd_hwnd)
    time.sleep(0.5)

    def lookup(name):
        if name in resolved_by_name:
            return resolved_by_name[name]
        for key, path in resolved_by_name.items():
            if key.endswith(name) or name.endswith(key):
                return path
        return None

    first_dialog = _wait_for_dialog(mmd_hwnd, step_timeout)
    if first_dialog is None:
        raise RuntimeError("ファイルを開くダイアログが現れませんでした")
    fill_and_open_file(first_dialog, pmm_path)

    for step in range(max_steps):
        dialog = _wait_for_dialog(mmd_hwnd, step_timeout)
        if dialog is None:
            title = win32gui.GetWindowText(mmd_hwnd)
            if pmm_path.split("\\")[-1] in title:
                log("読み込み完了。")
                return True
            log("ダイアログが見つからずタイムアウトしました。MMDの画面を確認してください。")
            return False

        title, static_text = get_dialog_text(dialog)
        log(f"[{step}] dialog title={title!r} text={static_text!r}")

        if title == "pmm ver.2.0 ロード":
            m = MODEL_MISSING_RE.search(static_text)
            name = m.group(1) if m else ""
            path = lookup(name)
            if path:
                log(f"  -> 場所を指定: {name} = {path}")
                click_dialog_button(dialog, MODEL_SPECIFY_ID)
                time.sleep(0.4)
                browse = _wait_for_dialog(mmd_hwnd, step_timeout)
                if browse is None or not has_control(browse, EDIT_FILENAME_ID):
                    log("  ファイル選択ダイアログが開きませんでした。中断します。")
                    return False
                fill_and_open_file(browse, path)
            else:
                log(f"  -> 候補なし、スキップ: {name}")
                click_dialog_button(dialog, MODEL_SKIP_ID)
            continue

        if title == "ファイルを開く" and not has_control(dialog, EDIT_FILENAME_ID):
            log("  -> 未保存の変更を破棄してロード継続 (OK)")
            click_dialog_button(dialog, OPEN_BUTTON_ID)
            continue

        if title in ("アクセサリ読込", "WAVE読込", "AVIデータ読込", "MikuMikuEffect"):
            m = SUBSTITUTE_RE.search(static_text)
            log(f"  -> 情報ダイアログ、OKで閉じる ({title})")
            click_dialog_button(dialog, CANCEL_BUTTON_ID)
            continue

        if title == "ファイル読込":
            m = SUBSTITUTE_RE.search(static_text)
            name = m.group(1) if m else ""
            path = lookup(name)
            if path:
                log(f"  -> 代替ファイルを指定: {name} = {path}")
                click_dialog_button(dialog, CANCEL_BUTTON_ID)
                time.sleep(0.4)
                browse = _wait_for_dialog(mmd_hwnd, step_timeout)
                if browse is None or not has_control(browse, EDIT_FILENAME_ID):
                    log("  ファイル選択ダイアログが開きませんでした。中断します。")
                    return False
                fill_and_open_file(browse, path)
            else:
                log(f"  !! 候補が見つからないアクセサリです: {name}")
                log("  安全のためここで停止します。MMD側のダイアログで手動対応してください。")
                return False
            continue

        log(f"  未知のダイアログです。手動対応してください: title={title!r} text={static_text!r}")
        return False

    log("ステップ数上限に達しました。")
    return False


def _wait_for_dialog(mmd_hwnd, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        dialogs = list_owned_dialogs(mmd_hwnd)
        if dialogs:
            return dialogs[0]
        time.sleep(0.3)
    return None
