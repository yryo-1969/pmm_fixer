"""Win32 automation for driving MikuMikuDance's project-load dialogs."""
import ctypes
import os
import re
import subprocess
import tempfile
import time

import win32con
import win32gui
import win32process

MB_YESNO = 0x4
MB_ICONWARNING = 0x30
MB_TOPMOST = 0x40000
IDYES = 6

MENU_ID_OPEN_PROJECT = 205
MENU_ID_SAVE_AS = 208
MENU_ID_LOAD_WAV = 206
MENU_ID_LOAD_AVI = 213
WM_COMMAND = win32con.WM_COMMAND
WM_SETTEXT = win32con.WM_SETTEXT
EDIT_FILENAME_ID = 1148
SAVE_EDIT_FILENAME_ID = 1001
OPEN_BUTTON_ID = 1
CANCEL_BUTTON_ID = 2

# MMEffect (the MME plugin) menu commands, reverse-engineered the same way
# as MMD's own MENU_ID_* constants above (live inspection via GetMenu on
# MMD 9.32 x64 with MME installed). MENU_ID_MME_ASSIGN is on MMD's own
# menu bar (opens the assignment manager dialog); the other two are on
# THAT DIALOG'S OWN menu bar, not MMD's, so they must be posted to the
# dialog's hwnd, not mmd_hwnd.
MENU_ID_MME_ASSIGN = 40005
MME_ASSIGN_LIST_ID = 1003  # SysListView32 inside the assignment dialog — used to detect it's actually open
MME_MENU_SAVE_SETTINGS_ID = 40010
MME_MENU_LOAD_SETTINGS_ID = 40009
MME_DIALOG_CANCEL_ID = 2


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
    if not hwnd:
        subprocess.Popen([exe_path], cwd=exe_path.rsplit("\\", 1)[0])
        hwnd = find_mmd_window(timeout=timeout)
        if not hwnd:
            raise RuntimeError("MMDの起動を確認できませんでした")
    _ensure_restored(hwnd)
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


def find_control(hwnd, control_id):
    """Find a descendant control by ID at any nesting depth. GetDlgItem only
    looks at direct children, which misses controls nested inside a shell
    view container (e.g. the Vista-style Save-As dialog's edit box)."""
    found = []

    def cb(child, _):
        try:
            if win32gui.GetDlgCtrlID(child) == control_id:
                found.append(child)
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return found[0] if found else None


def has_control(hwnd, control_id):
    return find_control(hwnd, control_id) is not None


def list_dialog_buttons(hwnd):
    """Enumerate this dialog's own Button-class controls as (id, text) pairs.

    Some dialogs reuse the same numeric control IDs for different buttons
    depending on which variant of the dialog is showing (e.g. control ID
    687 is "指定して読み込む" on the missing-file dialog but "適応させて
    続行" on the structure-mismatch dialog), and the structure-mismatch
    dialog itself can show either 3 or 4 buttons. So a fixed ID is only
    safe when the exact dialog variant is already confirmed by its title
    and message text; anything that needs to work across variants should
    match by button text via this list instead.
    """
    buttons = []

    def cb(child, _):
        try:
            if win32gui.GetClassName(child) == "Button":
                buttons.append((win32gui.GetDlgCtrlID(child), win32gui.GetWindowText(child)))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return buttons


def find_button_by_text(hwnd, *keywords):
    """Return the control ID of the first button whose text contains any keyword."""
    for control_id, text in list_dialog_buttons(hwnd):
        if any(kw in text for kw in keywords):
            return control_id
    return None


def _find_control_retry(hwnd, control_id, timeout=2.0):
    """Like find_control, but polls briefly instead of giving up after one
    try. A dialog chained immediately after the previous one closed (e.g.
    a fallback model assignment with no human pause in between, unlike one
    that went through the picker UI first) can already be visible as a
    top-level window before all of its child controls finish being
    created, so a single EnumChildWindows call right after _wait_for_dialog
    can miss the button/edit control and silently no-op the click."""
    deadline = time.time() + timeout
    while True:
        ctrl = find_control(hwnd, control_id)
        if ctrl is not None or time.time() >= deadline:
            return ctrl
        time.sleep(0.1)


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
    # find_control, not GetDlgItem: some dialog styles (seen so far on the
    # Save-As picker, and occasionally on a plain Open picker too — maybe
    # a timing thing while the dialog is still constructing its children)
    # nest their controls below the dialog's direct children, which
    # GetDlgItem can't see.
    btn = _find_control_retry(hwnd, control_id)
    win32gui.PostMessage(hwnd, WM_COMMAND, (0 << 16) | control_id, btn)
    _wait_for_close(hwnd)


def fill_and_open_file(hwnd, path):
    # normpath: tkinter's askopenfilename can hand back a forward-slash
    # path on Windows (e.g. "M:/ray MMD/ray-mmd-1.5.2/ray.x") — MMD's own
    # native "ファイルを開く" rejects that as invalid ("ファイル名は有効
    # ではありません"), leaving its browse dialog sitting there empty
    # instead of actually opening the file (ocyacya hit this live,
    # 2026-09-10). Normalizing here fixes it at the one place every path
    # this function is ever called with passes through.
    edit = _find_control_retry(hwnd, EDIT_FILENAME_ID)
    win32gui.SendMessage(edit, WM_SETTEXT, 0, os.path.normpath(path))
    time.sleep(0.2)
    open_btn = _find_control_retry(hwnd, OPEN_BUTTON_ID)
    win32gui.PostMessage(hwnd, WM_COMMAND, (0 << 16) | OPEN_BUTTON_ID, open_btn)
    return _wait_for_close(hwnd)


# A minimal, degenerate (zero-size, fully transparent) DirectX .x mesh —
# invisible in the scene. Last-resort stand-in when a human has already
# tried and failed to find a real substitute for an accessory (2026-09-10,
# ocyacya's suggestion): rather than leave the whole batch stuck, let them
# explicitly opt into "load anyway, fix it properly later in MMD" — MMD
# itself running with something (even if this one accessory looks wrong or,
# for a lighting/post-effect controller, blows the frame out to white — see
# the earlier reverted attempt to do this unconditionally) beats not being
# able to open the project at all. Deliberately NOT offered for models
# (no equivalent "harmless blank" concept, and a missing model already has
# a safe, real skip via MODEL_SKIP_ID).
_DUMMY_ACCESSORY_X = (
    "xof 0303txt 0032\n"
    "Material pmm_fixer_dummy_mat {\n"
    "0.0000;0.0000;0.0000;0.0000;;\n"
    "0.0000;\n"
    "0.0000;0.0000;0.0000;;\n"
    "0.0000;0.0000;0.0000;;\n"
    "}\n"
    "Mesh pmm_fixer_dummy_mesh{\n"
    "3;\n"
    "0.0000;0.0000;0.0000;,\n"
    "0.0000;0.0000;0.0000;,\n"
    "0.0000;0.0000;0.0000;;\n"
    "1;\n"
    "3;0,1,2;;\n"
    "  MeshMaterialList {\n"
    "1;\n"
    "1;\n"
    "0;\n"
    "{ pmm_fixer_dummy_mat }\n"
    "  }\n"
    "}\n"
)


def _dummy_accessory_path():
    path = os.path.join(tempfile.gettempdir(), "pmm_fixer_dummy_accessory.x")
    if not os.path.exists(path):
        with open(path, "w", encoding="ascii") as f:
            f.write(_DUMMY_ACCESSORY_X)
    return path


def _open_with_retry(mmd_hwnd, browse, path, name, pick_callback, log, max_attempts=3,
                      allow_dummy_fallback=False):
    """fill_and_open_file, but if MMD rejects the path (its browse dialog
    doesn't close — e.g. an invalid-path error MMD puts up on top of it)
    and a picker callback is available, ask the human to try a different
    file instead of leaving the run stuck on an empty browse dialog
    (ocyacya hit this live, 2026-09-10, after a bad manual pick). Returns
    True once some path is accepted, False if attempts run out or the
    human declines and (when allow_dummy_fallback) also declines the
    dummy-file offer.

    path may be None to start (nothing was auto-resolved at all — scan
    found no candidate) — the picker is consulted immediately in that
    case, same as after a rejected attempt, so both "auto-resolved but
    wrong" and "never resolved" funnel through the same retry+dummy path."""
    for _attempt in range(max_attempts):
        if path:
            if fill_and_open_file(browse, path):
                return True
            log(f"  !! MMDがこのファイルを受け付けませんでした: {path}")
            # A rejected path typically leaves a small MMD error dialog on
            # top of the still-open browse dialog rather than closing
            # anything — dismiss one such dialog (single button) if present.
            stray = _wait_for_dialog(mmd_hwnd, 2)
            if stray is not None and not has_control(stray, EDIT_FILENAME_ID):
                buttons = list_dialog_buttons(stray)
                if len(buttons) == 1:
                    click_dialog_button(stray, buttons[0][0])
        if not pick_callback:
            break
        log(f"  -> ファイルを選んでください: {name}")
        path = pick_callback(name)
        if not path:
            break

    if allow_dummy_fallback:
        answer = ctypes.windll.user32.MessageBoxW(
            0,
            f'"{name}" の代わりのファイルが見つかりませんでした。\n\n'
            "見えない・当たり判定のないダミーファイルで代替して、\n"
            "とりあえず読み込みを続けますか？\n"
            "（後でMMD本体のアクセサリ操作から、ご自身で正しい\n"
            "　ファイルに直せます。ただしこのアクセサリが照明・\n"
            "　エフェクト系だった場合、画面が白飛びするなど見た目が\n"
            "　崩れる可能性があります）",
            "MMDリボーン - ダミーファイルで代替しますか？",
            MB_YESNO | MB_ICONWARNING | MB_TOPMOST,
        )
        if answer == IDYES:
            log(f"  -> ダミーファイルで代替して続行します（後で本物を確認してください）: {name}")
            return fill_and_open_file(browse, _dummy_accessory_path())
    return False


def _ensure_restored(mmd_hwnd):
    # A dialog opened while the owner is minimized can end up created but not
    # shown, so menu-triggered dialogs are unreliable unless we restore first.
    if win32gui.IsIconic(mmd_hwnd):
        win32gui.ShowWindow(mmd_hwnd, win32con.SW_RESTORE)
        time.sleep(0.3)


def trigger_open_project(mmd_hwnd):
    _ensure_restored(mmd_hwnd)
    win32gui.PostMessage(mmd_hwnd, WM_COMMAND, (0 << 16) | MENU_ID_OPEN_PROJECT, 0)


# ".*?", not ".+?": a model that was never named inside MMD (公"無名") shows
# up as a literally empty pair of quotes (""のモデルファイルが見つかりません),
# and "+" can't match zero characters — that silently missed the dialog
# entirely and fell through to the generic "unknown dialog, stop" branch
# below instead of the (already-written, already-safe) blank-name handling
# in lookup()/next_model_fallback (ocyacya hit this live, 2026-09-10).
MODEL_MISSING_RE = re.compile(r'"(.*?)"のモデルファイルが見つかりません')
MODEL_STRUCTURE_MISMATCH_RE = re.compile(r'"(.+?)"のモデル構造がpmm保存時のものと異なります')
SUBSTITUTE_RE = re.compile(r'[（(](.+?)[）)]が見つかりません')

MODEL_SPECIFY_ID = 687
MODEL_SKIP_ID = 688
MODEL_ABORT_ID = 689

MISMATCH_ACCEPT_KEYWORDS = ("適応",)  # "このモデルにpmmファイルのモデル情報を適応させて続行"


def run_autoload(mmd_hwnd, pmm_path, resolved_by_name, model_fallback_queue=None,
                  pick_model_callback=None, pick_accessory_callback=None,
                  pick_missing_model_callback=None, log=print,
                  max_steps=200, step_timeout=15):
    """Drive the project-load dialog sequence.

    pick_missing_model_callback(mmd_internal_name) is called when a model
    can't be found by name/fallback matching at all (the usual case when
    it's genuinely unresolvable: a blank/"無名" MMD internal name, or a
    real file whose actual filename doesn't textually match anything, e.g.
    Chinese-named). Should return a substitute path, or None/falsy to skip
    this model as before.

    pick_accessory_callback(name) is called when an accessory (.x) can't be
    found and scan resolved no candidate for it either. Should return a
    substitute path, or None/falsy to stop and leave MMD's dialog for a
    human — never guessed automatically, since a wrong guess here can
    silently break rendering (see the no-auto-continue comment below).

    resolved_by_name: {basename_without_ext_or_full: path} for name-based lookup.
    model_fallback_queue: an ordered list of resolved model (.pmx/.pmd) paths,
    in the same order they were found while scanning the PMM. The "model
    file not found" dialog shows MMD's own internal object name for that
    model slot (editable in MMD, e.g. "Null_00" for one that was never
    named, or some unrelated leftover label from years of reusing a save
    slot) — not the filename — so name-based lookup can legitimately find
    nothing even though scan resolved a path for that exact model.
    When name lookup fails: if exactly one unresolved model is left, use
    it (no real choice to make). If more than one remains, this could
    guess by scan order, but that's still a guess — instead call
    pick_model_callback(dialog_name, remaining_paths) and let the human
    decide, since picking the wrong one would silently mislabel a model.
    pick_model_callback should return a chosen path (removed from the
    queue by the caller) or None to skip.
    """
    trigger_open_project(mmd_hwnd)
    time.sleep(0.5)
    fallback_queue = list(model_fallback_queue or [])
    consumed_paths = set()

    def lookup(name):
        # A blank name (regex failed to pull a name out of the dialog text)
        # must never reach the substring fallback below: key.endswith("")
        # is trivially true for every key, so it would "match" the first
        # entry in resolved_by_name regardless of which model is actually
        # being asked about.
        if not name:
            return None
        path = None
        if name in resolved_by_name:
            path = resolved_by_name[name]
        else:
            for key, candidate in resolved_by_name.items():
                if key.endswith(name) or name.endswith(key):
                    path = candidate
                    break
        if path:
            consumed_paths.add(path)
        return path

    def next_model_fallback(dialog_name):
        remaining = [p for p in fallback_queue if p not in consumed_paths]
        if not remaining:
            return None
        if len(remaining) == 1:
            path = remaining[0]
        elif pick_model_callback:
            path = pick_model_callback(dialog_name, remaining)
        else:
            path = remaining[0]
        if path:
            consumed_paths.add(path)
            fallback_queue.remove(path)
        return path

    pmm_path_submitted = False

    for step in range(max_steps):
        dialog = _wait_for_dialog(mmd_hwnd, step_timeout)
        if dialog is None:
            if not pmm_path_submitted:
                raise RuntimeError("ファイルを開くダイアログが現れませんでした")
            title = win32gui.GetWindowText(mmd_hwnd)
            if pmm_path.split("\\")[-1] in title:
                log("読み込み完了。")
                return True
            if not win32gui.IsWindow(mmd_hwnd):
                # GetWindowText on a dead hwnd just returns "" rather than
                # raising, so without this check a genuine MMD crash looks
                # identical to "still loading, just slow" — confusing when
                # a heavy effect (e.g. one of several shader-heavy
                # accessories in a row) actually took MMD down entirely
                # (ocyacya hit this live, 2026-09-10).
                log("MMD本体が終了（クラッシュまたは強制終了）しています。"
                    "直前に読み込んだファイルが原因の可能性があります。ログの直前の項目を確認してください。")
                return False
            log("ダイアログが見つからずタイムアウトしました。MMDの画面を確認してください。")
            return False

        title, static_text = get_dialog_text(dialog)
        log(f"[{step}] dialog title={title!r} text={static_text!r}")

        if title == "ファイルを開く" and not pmm_path_submitted and has_control(dialog, EDIT_FILENAME_ID):
            fill_and_open_file(dialog, pmm_path)
            pmm_path_submitted = True
            continue

        if title == "pmm ver.2.0 ロード":
            mismatch = MODEL_STRUCTURE_MISMATCH_RE.search(static_text)
            if mismatch:
                # A substitute was already assigned for this model slot (by
                # name lookup or fallback) but MMD says its bone/material
                # structure doesn't match what the PMM's animation data
                # expects — i.e. probably the wrong file. Forcing "適応させ
                # て続行" here keeps a batch run from stalling, at the cost
                # of that one model's appearance possibly being wrong (its
                # shape/bones get bent to fit the pmm's stored animation
                # data even though they don't really match). ocyacya chose
                # this over stopping-for-a-human (2026-09-10): finish the
                # load and spot-check flagged models afterward instead.
                model_name = mismatch.group(1)
                accept_id = find_button_by_text(dialog, *MISMATCH_ACCEPT_KEYWORDS)
                if accept_id is not None:
                    log(f"  !! {model_name!r} は構造不一致ですが、pmmファイルのモデル情報を適応させて"
                        "強制続行します（見た目がおかしい可能性あり・後で確認してください）")
                    click_dialog_button(dialog, accept_id)
                    continue
                buttons = list_dialog_buttons(dialog)
                log(f"  !! 割り当てた代替モデルの構造が元と一致しません: {model_name!r}")
                log("  「適応して続行」ボタンが見つからないため、安全のためここで停止します。"
                    "MMDの画面で選択してください。")
                if buttons:
                    log("  このダイアログの選択肢: " + " / ".join(text for _id, text in buttons))
                return False
            m = MODEL_MISSING_RE.search(static_text)
            if m is None:
                log(f"  未知のモデル関連ダイアログです。手動対応してください: text={static_text!r}")
                return False
            name = m.group(1)
            path = lookup(name)
            fell_back = False
            if not path:
                path = next_model_fallback(name)
                fell_back = path is not None
            if path:
                how = "（名前が一致しないため代替割り当て）" if fell_back else ""
                log(f"  -> 場所を指定: {name} = {path} {how}")
                click_dialog_button(dialog, MODEL_SPECIFY_ID)
                time.sleep(0.4)
                browse = _wait_for_dialog(mmd_hwnd, step_timeout)
                if browse is None or not has_control(browse, EDIT_FILENAME_ID):
                    log("  ファイル選択ダイアログが開きませんでした。中断します。")
                    return False
                if not _open_with_retry(mmd_hwnd, browse, path, name, pick_missing_model_callback, log):
                    log(f"  !! 代替ファイルの指定に失敗しました: {name!r}")
                    return False
            else:
                # Before giving up automatically: MMD's own name for this
                # slot is often useless for a human too (blank/"無名", or
                # some unrelated leftover label — see the docstring above),
                # so let the caller offer a proper file browser (and,
                # unlike this internal label, the caller knows the *real*
                # basenames scan couldn't resolve project-wide, which are
                # actually searchable — e.g. the Chinese-named .pmx case
                # ocyacya hit, 2026-09-10).
                picked = pick_missing_model_callback(name) if pick_missing_model_callback else None
                if picked:
                    log(f"  -> 手動で代替ファイルを指定: {name!r} = {picked}")
                    click_dialog_button(dialog, MODEL_SPECIFY_ID)
                    time.sleep(0.4)
                    browse = _wait_for_dialog(mmd_hwnd, step_timeout)
                    if browse is None or not has_control(browse, EDIT_FILENAME_ID):
                        log("  ファイル選択ダイアログが開きませんでした。中断します。")
                        return False
                    if not _open_with_retry(mmd_hwnd, browse, picked, name, pick_missing_model_callback, log):
                        log(f"  !! 代替ファイルの指定に失敗しました: {name!r}")
                        return False
                else:
                    log(f"  -> 候補なし、スキップ: {name!r}")
                    click_dialog_button(dialog, MODEL_SKIP_ID)
            continue

        if title == "ファイルを開く" and not has_control(dialog, EDIT_FILENAME_ID):
            log("  -> 未保存の変更を破棄してロード継続 (OK)")
            click_dialog_button(dialog, OPEN_BUTTON_ID)
            continue

        if title in ("アクセサリ読込", "WAVE読込", "AVIデータ読込", "BMPファイル読込", "MikuMikuEffect"):
            m = SUBSTITUTE_RE.search(static_text)
            log(f"  -> 情報ダイアログ、OKで閉じる ({title})")
            click_dialog_button(dialog, CANCEL_BUTTON_ID)
            continue

        if title == "ファイル読込":
            m = SUBSTITUTE_RE.search(static_text)
            name = m.group(1) if m else ""
            path = lookup(name)
            log(f"  -> 代替ファイルを指定: {name} = {path}" if path
                else f"  -> 候補が見つからないアクセサリです: {name}")
            # Cancelling the browse dialog instead of specifying something
            # was tried live and rejected (2026-09-10): it aborts the WHOLE
            # remaining project load in this MMD version — not just this
            # one accessory, discarding everything already loaded so far
            # too. So always open the browse dialog and let
            # _open_with_retry take it from here (auto path if we have
            # one, else straight to the picker) — never cancel out of it.
            click_dialog_button(dialog, CANCEL_BUTTON_ID)
            time.sleep(0.4)
            browse = _wait_for_dialog(mmd_hwnd, step_timeout)
            if browse is None or not has_control(browse, EDIT_FILENAME_ID):
                log("  ファイル選択ダイアログが開きませんでした。中断します。")
                return False
            if not _open_with_retry(mmd_hwnd, browse, path, name, pick_accessory_callback, log,
                                     allow_dummy_fallback=True):
                log(f"  !! 代替ファイルの指定に失敗しました: {name}")
                log("  安全のためここで停止します。MMD側のダイアログで手動対応してください。")
                return False
            continue

        log(f"  未知のダイアログです。手動対応してください: title={title!r} text={static_text!r}")
        return False

    log("ステップ数上限に達しました。")
    return False


def _wait_for_dialog_with_control(mmd_hwnd, control_id, timeout):
    """Like _wait_for_dialog, but keeps polling past a transient/unrelated
    window until one actually carrying control_id shows up (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd in list_owned_dialogs(mmd_hwnd):
            if has_control(hwnd, control_id):
                return hwnd
        time.sleep(0.3)
    return None


def load_media_file(mmd_hwnd, menu_id, path, log=print, step_timeout=15):
    """Load a WAV/AVI via its File-menu command (206/213). Unlike models and
    accessories, MMD's own project-load dialog never offers a "specify a
    substitute location" option for a missing WAV/AVI — it only ever shows
    a generic, name-less "not found" info box (OK only) and silently drops
    the reference. The only way to actually fix a broken audio/video link
    is to load it fresh through this menu after the project has opened,
    the same way a human would, then save the project."""
    _ensure_restored(mmd_hwnd)
    win32gui.PostMessage(mmd_hwnd, WM_COMMAND, (0 << 16) | menu_id, 0)
    time.sleep(0.5)

    dialog = _wait_for_dialog_with_control(mmd_hwnd, EDIT_FILENAME_ID, step_timeout)
    if dialog is None:
        log(f"  ファイルを開くダイアログが出ませんでした: {path}")
        return False

    fill_and_open_file(dialog, path)

    extra = _wait_for_dialog(mmd_hwnd, 3)
    if extra is not None:
        title, static_text = get_dialog_text(extra)
        log(f"  読み込み後に想定外のダイアログが出ました: title={title!r} text={static_text!r}")
        return False
    return True


def export_effect_assignments(mmd_hwnd, out_path, log=print, step_timeout=15):
    """Export MMEffect's current effect-file assignment state to out_path,
    via the same UI a person would use: MMD's own MMEffect menu (エフェクト
    割当, id 40005) opens the assignment manager dialog, and THAT DIALOG'S
    OWN menu bar (not MMD's — WM_COMMAND for these two IDs must go to the
    dialog's hwnd) has File > 設定を保存 (id 40010) to write it out.

    This exists because .fx/.fxsub paths are frequently NOT stored as
    plain strings inside the .pmm the way model/accessory/motion paths
    are (confirmed by directly byte-scanning real projects: a model whose
    MikuMikuEffect dialog complained about a missing .fx had zero
    occurrences of that filename anywhere in the pmm's raw bytes) — so
    unlike everything else this tool checks, effect assignments can only
    be inspected by asking MME itself, live, for its current state.

    out_path must not already exist — an overwrite prompts for
    confirmation with a dialog layout this doesn't handle, so callers
    should pick a fresh temp path rather than reuse one.

    Closes the assignment dialog afterward (Cancel — nothing was changed,
    only exported) and returns True if out_path was actually written.
    """
    _ensure_restored(mmd_hwnd)
    win32gui.PostMessage(mmd_hwnd, WM_COMMAND, (0 << 16) | MENU_ID_MME_ASSIGN, 0)
    time.sleep(0.5)

    assign_dialog = _wait_for_dialog_with_control(mmd_hwnd, MME_ASSIGN_LIST_ID, step_timeout)
    if assign_dialog is None:
        log("  エフェクト割り当てダイアログが開きませんでした（MMEffectが入っていない可能性があります）。")
        return False

    win32gui.PostMessage(assign_dialog, WM_COMMAND, (0 << 16) | MME_MENU_SAVE_SETTINGS_ID, 0)
    time.sleep(0.5)

    save_dialog = _wait_for_dialog_with_control(mmd_hwnd, SAVE_EDIT_FILENAME_ID, step_timeout)
    if save_dialog is None:
        log("  設定の保存ダイアログが開きませんでした。")
        if win32gui.IsWindow(assign_dialog):
            click_dialog_button(assign_dialog, MME_DIALOG_CANCEL_ID)
        return False

    edit = _find_control_retry(save_dialog, SAVE_EDIT_FILENAME_ID)
    win32gui.SendMessage(edit, WM_SETTEXT, 0, out_path)
    time.sleep(0.2)
    save_btn = _find_control_retry(save_dialog, OPEN_BUTTON_ID)
    win32gui.PostMessage(save_dialog, WM_COMMAND, (0 << 16) | OPEN_BUTTON_ID, save_btn)
    _wait_for_close(save_dialog)

    if win32gui.IsWindow(assign_dialog):
        click_dialog_button(assign_dialog, MME_DIALOG_CANCEL_ID)

    return os.path.exists(out_path)


def save_project_as(mmd_hwnd, new_path, log=print, step_timeout=15):
    """Trigger File > Save As and save to new_path. Returns True on success.
    Bails out (returns False) on any dialog it doesn't recognize, rather
    than guessing — e.g. an unexpected overwrite-confirmation prompt."""
    _ensure_restored(mmd_hwnd)
    win32gui.PostMessage(mmd_hwnd, WM_COMMAND, (0 << 16) | MENU_ID_SAVE_AS, 0)
    time.sleep(0.5)

    dialog = _wait_for_dialog_with_control(mmd_hwnd, SAVE_EDIT_FILENAME_ID, step_timeout)
    if dialog is None:
        log("名前を付けて保存ダイアログが開きませんでした。")
        return False

    edit = find_control(dialog, SAVE_EDIT_FILENAME_ID)
    win32gui.SendMessage(edit, WM_SETTEXT, 0, new_path)
    time.sleep(0.2)
    save_btn = find_control(dialog, OPEN_BUTTON_ID)
    win32gui.PostMessage(dialog, WM_COMMAND, (0 << 16) | OPEN_BUTTON_ID, save_btn)
    _wait_for_close(dialog)

    extra = _wait_for_dialog(mmd_hwnd, 3)
    if extra is not None:
        title, static_text = get_dialog_text(extra)
        log(f"保存後に想定外のダイアログが出ました: title={title!r} text={static_text!r}")
        log("MMDの画面を確認して手動対応してください。")
        return False

    target_name = new_path.split("\\")[-1]
    deadline = time.time() + 5
    while time.time() < deadline:
        if target_name in win32gui.GetWindowText(mmd_hwnd):
            return True
        time.sleep(0.3)
    log("保存できたか確認できませんでした。MMDの画面を確認してください。")
    return False


def _wait_for_dialog(mmd_hwnd, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        dialogs = list_owned_dialogs(mmd_hwnd)
        if dialogs:
            return dialogs[0]
        time.sleep(0.3)
    return None
