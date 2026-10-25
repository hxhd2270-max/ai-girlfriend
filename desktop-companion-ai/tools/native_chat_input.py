"""IME-capable composer placed over the wallpaper's existing composer.
Only relative geometry and draft text travel over private stdin/stdout pipes.
"""
import ctypes
from ctypes import wintypes
import json
import sys
import time
import tkinter as tk


def composer_bounds(layout, client):
    x, y, width, height = client
    sx, sy = width / layout['viewportWidth'], height / layout['viewportHeight']
    return (round(x + layout['left'] * sx), round(y + layout['top'] * sy),
            round(layout['width'] * sx), round(layout['height'] * sy), sx)


def wallpaper_host(layout):
    """Find only WE's visible client under the initiating mouse click."""
    user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindow.argtypes = [wintypes.HWND]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    point = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(point))
    matches = []
    trusted = {}

    def client_rect(hwnd):
        rect, origin = wintypes.RECT(), wintypes.POINT()
        if not user.IsWindow(hwnd) or not user.IsWindowVisible(hwnd):
            return None
        if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        return origin.x, origin.y, rect.right, rect.bottom

    def inspect(hwnd, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in trusted:
            handle = kernel.OpenProcess(0x1000, False, pid.value)
            trusted[pid.value] = False
            if handle:
                try:
                    name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    if kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
                        trusted[pid.value] = name.value.lower() in {
                            r'f:\steamlibrary\steamapps\common\wallpaper_engine\wallpaper64.exe',
                            r'f:\steamlibrary\steamapps\common\wallpaper_engine\wallpaper32.exe'}
                finally:
                    kernel.CloseHandle(handle)
        if not trusted[pid.value]:
            return True
        rect = client_rect(hwnd)
        if not rect:
            return True
        x, y, w, h = rect
        if w < 320 or h < 200 or abs(w / h - layout['viewportWidth'] / layout['viewportHeight']) > .06:
            return True
        bx, by, bw, bh, scale = composer_bounds(layout, rect)
        if bx <= point.x <= bx+bw and by <= point.y <= by+bh:
            matches.append((w*h, hwnd, rect))
        return True

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback, wintypes.LPARAM]
    inspect_cb = callback(inspect)
    def top(hwnd, unused):
        inspect(hwnd, unused)
        user.EnumChildWindows(hwnd, inspect_cb, 0)
        return True
    user.EnumWindows(callback(top), 0)
    if not matches:
        raise ValueError('wallpaper_input_target_unavailable')
    _, hwnd, rect = min(matches, key=lambda item: item[0])
    return hwnd, rect, lambda: client_rect(hwnd)


def main():
    request = json.loads(sys.stdin.buffer.read(16000))
    draft = request.get('text', '')
    if not isinstance(draft, str) or len(draft) > 2000:
        return 1
    from server.native_input import checked_layout
    layout = checked_layout(request.get('layout'))
    # Match the physical client pixels on mixed-DPI displays. No global hooks.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (OSError, AttributeError):
        pass
    host, client, current_rect = wallpaper_host(layout)
    x, y, width, height, scale = composer_bounds(layout, client)
    root = tk.Tk()
    root.title('慢慢说 · 就地输入')
    root.attributes('-topmost', True)
    root.configure(bg='#282a30')
    root.geometry(f'{width}x{height}+0+0')
    root.update_idletasks()
    # Tk interprets negative offsets as distances from the right/bottom.
    # Native positioning preserves signed virtual-screen coordinates instead.
    user = ctypes.windll.user32
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    hwnd = user.GetAncestor(root.winfo_id(), 2)
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    style = user.GetWindowLongPtrW(hwnd, -16)
    user.SetWindowLongPtrW(hwnd, -16, style & ~0x00C40000)  # caption and resize frame
    extended = user.GetWindowLongPtrW(hwnd, -20)
    user.SetWindowLongPtrW(hwnd, -20, (extended & ~0x80) | 0x40000)
    # Keep the editor independently focusable. Cross-process ownership can
    # leave keyboard focus in WE instead of the native Chinese editor.
    user.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, width, height, 0x0030)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(0, weight=1)
    pixels = max(11, round(layout['fontSize'] * scale))
    font = ('Microsoft YaHei', -pixels)
    editor = tk.Text(root, bg='#282a30', fg='#f1e9dd', insertbackground='#d8bd97',
                     selectbackground='#574c42', font=font, wrap='word', relief='flat',
                     highlightthickness=1, highlightbackground='#686057',
                     highlightcolor='#a1917b', padx=round(12*scale), pady=round(9*scale), undo=True)
    editor.grid(row=0, column=0, sticky='nsew')
    editor.insert('1.0', draft)
    activity_at = [0.0]
    def activity(event=None):
        now = time.monotonic()
        if now - activity_at[0] < .2:
            return
        activity_at[0] = now
        sys.stdout.buffer.write(json.dumps({'action':'activity', 'hasText':bool(editor.get('1.0', 'end-1c'))}).encode('utf-8') + b'\n')
        sys.stdout.buffer.flush()
    editor.bind('<KeyPress>', activity, add='+')
    editor.bind('<<Paste>>', activity, add='+')
    editor.mark_set('insert', 'end-1c')
    footer = tk.Frame(root, bg='#282a30')
    footer.grid(row=1, column=0, sticky='ew', pady=(round(7*scale),0))
    footer.grid_columnconfigure(0, weight=1)
    note = tk.Label(footer, text='Enter 换行 · Ctrl + Enter 发送', bg='#282a30', fg='#aaa49a',
                    font=('Microsoft YaHei', -max(9, round(pixels*.7))), anchor='w')
    note.grid(row=0, column=0, sticky='w')
    result = {'action':'draft', 'text':draft}
    finished = False

    def finish(action):
        nonlocal finished
        if finished:
            return 'break'
        text = editor.get('1.0', 'end-1c')
        if len(text) > 2000:
            note.configure(text='最多 2000 字，请缩短。', fg='#edb8a5')
            if action != 'draft':
                return 'break'
            text = text[:2000]
        if action == 'send' and not text.strip():
            editor.focus_set()
            return 'break'
        finished = True
        result.update(action=action, text=text)
        root.destroy()
        return 'break'

    for col, label, action in [(1,'停止','stop'),(2,'发送','send')]:
        tk.Button(footer, text=label, command=lambda a=action: finish(a),
                  bg='#363539', fg='#d8bd97', activebackground='#494247',
                  activeforeground='#f1e9dd', font=font, relief='flat',
                  padx=round(12*scale), pady=round(5*scale), cursor='hand2',
                  takefocus=True).grid(row=0,column=col,padx=(round(7*scale),0))
    editor.bind('<Control-Return>', lambda e: finish('send'))
    # Leave ordinary Enter/Escape to Chinese IME candidate selection.
    root.protocol('WM_DELETE_WINDOW', lambda: finish('draft'))
    outside_since = None
    def follow_host():
        nonlocal outside_since
        if finished:
            return
        if current_rect() != client:
            finish('draft')
            return
        # Mouse down/up can both fall between polls. Require sustained focus
        # loss with the pointer outside instead; transient IME activation and
        # state inspection while the pointer is inside must not dismiss it.
        point = wintypes.POINT()
        user.GetCursorPos(ctypes.byref(point))
        outside = not (x <= point.x < x+width and y <= point.y < y+height)
        if user.GetForegroundWindow() != hwnd and outside:
            if outside_since is None:
                outside_since = time.monotonic()
            elif time.monotonic() - outside_since >= .6:
                finish('draft')
                return
        else:
            outside_since = None
        root.after(100, follow_host)
    def focus_editor():
        nonlocal hwnd
        root.update_idletasks()
        hwnd = user.GetAncestor(root.winfo_id(), 2)
        style = user.GetWindowLongPtrW(hwnd, -16)
        user.SetWindowLongPtrW(hwnd, -16, style & ~0x00C40000)
        extended = user.GetWindowLongPtrW(hwnd, -20)
        user.SetWindowLongPtrW(hwnd, -20, (extended & ~0x80) | 0x40000)
        user.SetWindowPos(hwnd, wintypes.HWND(-1), x, y, width, height, 0x0030)
        user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.SetWindowTextW(hwnd, "慢慢说 · 就地输入")
        root.lift()
        # The click was received by WE, while this editor is a subprocess.
        # Briefly share its foreground input queue for this explicit user
        # request, then detach immediately. No hooks or background focus loop.
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user.SetActiveWindow.argtypes = [wintypes.HWND]
        user.SetFocus.argtypes = [wintypes.HWND]
        current_thread = kernel.GetCurrentThreadId()
        foreground_thread = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
        attached = foreground_thread != current_thread and user.AttachThreadInput(current_thread, foreground_thread, True)
        try:
            user.SetForegroundWindow(hwnd)
            user.SetActiveWindow(hwnd)
            root.focus_force()
            editor.focus_set()
            user.SetFocus(editor.winfo_id())
        finally:
            if attached:
                user.AttachThreadInput(current_thread, foreground_thread, False)
    root.after(100, focus_editor)
    root.after(1000, follow_host)
    root.after(900000, lambda: finish('draft'))
    root.after(150, lambda: (sys.stdout.buffer.write(b'{"action":"ready"}\n'), sys.stdout.buffer.flush()))
    root.mainloop()
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
    try:
        raise SystemExit(main())
    except (ValueError, OSError):
        raise SystemExit(1) from None
